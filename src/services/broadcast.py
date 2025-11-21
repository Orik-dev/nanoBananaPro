"""
✅ ИСПРАВЛЕНО: Production-ready broadcast с защитой от race condition и deadlock
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any
import logging

from sqlalchemy import select, update, delete
from aiogram import Bot
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

from core.config import settings
from db.engine import SessionLocal
from db.models import BroadcastJob, User

log = logging.getLogger("broadcast")


async def broadcast_send(ctx: dict[str, Any], job_id: str):
    """
    ✅ ИСПРАВЛЕНО: убрана вложенная транзакция
    """
    bot: Bot = ctx["bot"]
    
    base_rps = settings.BROADCAST_RPS
    concurrency = settings.BROADCAST_CONCURRENCY
    batch_size = settings.BROADCAST_BATCH
    check_cancel_every = 10

    sem = asyncio.Semaphore(concurrency)
    
    current_rps = base_rps
    tokens: asyncio.Queue[None] = asyncio.Queue(maxsize=max(1, int(current_rps * 2)))
    pump_task = None
    
    async def _rate_pump():
        nonlocal current_rps
        try:
            while True:
                interval = 1.0 / max(1, current_rps)
                try:
                    tokens.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    pump_task = asyncio.create_task(_rate_pump())

    async with SessionLocal() as session:
        row = await session.execute(select(BroadcastJob).where(BroadcastJob.id == job_id))
        bj = row.scalars().first()
        
        if not bj or bj.status in ("done", "cancelled"):
            pump_task.cancel()
            log.info(f"Broadcast {job_id} already finished")
            return

        # ✅ ИСПРАВЛЕНО: простой UPDATE без вложенной транзакции
        await session.execute(
            update(BroadcastJob)
            .where(BroadcastJob.id == job_id)
            .values(status="running")
        )
        await session.commit()

        sent = 0
        failed = 0
        fallback = 0
        rate_limited_count = 0
        cancelled = False

        async def _send(chat_id: int, text: str, media_type: str | None, 
               media_file_id: str | None, media_file_path: str | None) -> str:
   
            nonlocal current_rps, rate_limited_count
            
            try:
                await asyncio.wait_for(tokens.get(), timeout=30)
            except asyncio.TimeoutError:
                log.warning(f"Token queue timeout for chat {chat_id} - continuing anyway")
            
            async with sem:
                for attempt in range(3):
                    try:
                        if media_type == "photo" and media_file_id:
                            await bot.send_photo(
                                chat_id, 
                                photo=media_file_id, 
                                caption=text,
                                parse_mode="HTML",
                                request_timeout=45
                            )
                        elif media_type == "video" and media_file_id:
                            await bot.send_video(
                                chat_id, 
                                video=media_file_id,
                                caption=text,
                                parse_mode="HTML",
                                request_timeout=180
                            )
                        else:
                            await bot.send_message(
                                chat_id, 
                                text,
                                parse_mode="HTML", 
                                request_timeout=15
                            )
                        
                        if rate_limited_count > 0:
                            rate_limited_count = max(0, rate_limited_count - 1)
                        
                        return "success"
                    
                    except TelegramBadRequest as e:
                        error_msg = str(e).lower()
                        
                        if "too many requests" in error_msg or "retry after" in error_msg:
                            import re
                            match = re.search(r'retry after (\d+)', error_msg)
                            wait_time = int(match.group(1)) if match else 10
                            
                            rate_limited_count += 1
                            if rate_limited_count > 3:
                                current_rps = max(5, current_rps * 0.7)
                                log.warning(f"🐌 Slowing down: RPS={current_rps:.1f}")
                            
                            if attempt < 2:
                                log.debug(f"⏳ Rate limit for {chat_id}, waiting {wait_time}s")
                                await asyncio.sleep(wait_time)
                                continue
                        
                        if attempt == 2:
                            log.warning(f"⚠️ BadRequest for {chat_id}: {str(e)[:100]}")
                            try:
                                await bot.send_message(
                                    chat_id, 
                                    text,
                                    parse_mode="HTML", 
                                    request_timeout=15
                                )
                                return "fallback"
                            except TelegramForbiddenError:
                                try:
                                    async with SessionLocal() as s2:
                                        await s2.execute(delete(User).where(User.chat_id == chat_id))
                                        await s2.commit()
                                except Exception:
                                    pass
                                return "failed"
                            except Exception:
                                return "failed"
                        
                        await asyncio.sleep(2)
                        continue
                    
                    except TelegramForbiddenError:
                        try:
                            async with SessionLocal() as s2:
                                await s2.execute(delete(User).where(User.chat_id == chat_id))
                                await s2.commit()
                        except Exception:
                            pass
                        return "failed"
                    
                    except TelegramRetryAfter as e:
                        if attempt < 2:
                            await asyncio.sleep(e.retry_after)
                            continue
                        return "failed"
                    
                    except Exception as e:
                        if "timeout" in str(e).lower() and attempt < 2:
                            log.warning(f"⏳ Timeout for {chat_id}, retry {attempt + 1}/3")
                            await asyncio.sleep(5)
                            continue
                        
                        if attempt == 2:
                            log.error(f"❌ Unexpected error for {chat_id}: {str(e)[:100]}")
                        return "failed"
                
                return "failed"

        last_chat_id = 0
        total_processed = 0
        
        while not cancelled:
            # ✅ ИСПРАВЛЕНО: SELECT без вложенной транзакции
            st_row = await session.execute(
                select(BroadcastJob.status)
                .where(BroadcastJob.id == job_id)
            )
            status = st_row.scalar_one_or_none()
            
            if status == "cancelled":
                cancelled = True
                log.warning(f"🛑 Broadcast {job_id} cancelled")
                break

            try:
                res = await session.execute(
                    select(User.chat_id)
                    .where(User.chat_id > last_chat_id)
                    .order_by(User.chat_id)
                    .limit(batch_size)
                )
                chat_ids = res.scalars().all()
            except Exception as e:
                log.error(f"❌ Failed to fetch users: {e}")
                await session.execute(
                    update(BroadcastJob)
                    .where(BroadcastJob.id == job_id)
                    .values(
                        status="error",
                        sent=sent,
                        failed=failed,
                        fallback=fallback,
                        note=f"DB error: {e}"
                    )
                )
                await session.commit()
                break
            
            if not chat_ids:
                log.info(f"✅ Broadcast {job_id} complete: sent={sent}, fallback={fallback}, failed={failed}")
                break

            for i in range(0, len(chat_ids), check_cancel_every):
                if cancelled:
                    break
                
                if i > 0:
                    # ✅ ИСПРАВЛЕНО: без вложенной транзакции
                    st_row = await session.execute(
                        select(BroadcastJob.status)
                        .where(BroadcastJob.id == job_id)
                    )
                    if st_row.scalar_one_or_none() == "cancelled":
                        cancelled = True
                        log.warning(f"🛑 Broadcast {job_id} cancelled during batch")
                        break
                
                chunk = chat_ids[i:i + check_cancel_every]
                tasks = [
                    asyncio.create_task(_send(cid, bj.text, bj.media_type, bj.media_file_id, bj.media_file_path))
                    for cid in chunk
                ]
                results = await asyncio.gather(*tasks)
                
                sent += sum(1 for r in results if r == "success")
                failed += sum(1 for r in results if r == "failed")
                fallback += sum(1 for r in results if r == "fallback")
                total_processed += len(results)
            
            await session.execute(
                update(BroadcastJob)
                .where(BroadcastJob.id == job_id)
                .values(
                    sent=sent,
                    failed=failed,
                    fallback=fallback,
                    note=f"Progress: {total_processed}/{bj.total}. Fallback: {fallback}"
                )
            )
            await session.commit()
            
            last_chat_id = chat_ids[-1]

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        
        final_status = "cancelled" if cancelled else "done"
        final_note = f"{'Cancelled' if cancelled else 'Completed'}. Fallback: {fallback}"
        
        await session.execute(
            update(BroadcastJob)
            .where(BroadcastJob.id == job_id)
            .values(
                status=final_status,
                sent=sent,
                failed=failed,
                fallback=fallback,
                note=final_note
            )
        )
        await session.commit()
        
        if settings.ADMIN_ID and not cancelled:
            try:
                total = sent + failed + fallback
                success_rate = (sent / total * 100) if total > 0 else 0
                fallback_rate = (fallback / total * 100) if total > 0 else 0
                failed_rate = (failed / total * 100) if total > 0 else 0
                
                media_info = ""
                if bj.media_type == "photo":
                    media_info = " (📸 фото)"
                elif bj.media_type == "video":
                    media_info = " (🎬 видео)"
                
                await bot.send_message(
                    settings.ADMIN_ID,
                    f"📣 Рассылка <code>#{job_id}</code>{media_info} завершена\n\n"
                    f"📊 Статистика:\n"
                    f"├ Всего: <b>{total}</b>\n"
                    f"├ ✅ Медиа: <b>{sent}</b> ({success_rate:.1f}%)\n"
                    f"├ ⚠️ Текст: <b>{fallback}</b> ({fallback_rate:.1f}%)\n"
                    f"└ ❌ Ошибки: <b>{failed}</b> ({failed_rate:.1f}%)",
                    parse_mode="HTML"
                )
            except Exception as e:
                log.exception(f"Failed to send admin notification: {e}")