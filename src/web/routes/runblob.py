from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional, Tuple

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage

from bot.routers.generation import send_generation_result
from bot.states import CreateStates, GenStates
from core.config import settings
from db.engine import SessionLocal
from db.models import Task, User
from services.telegram_safe import safe_send_text

router = APIRouter()
log = logging.getLogger("runblob")

# -------------------- helpers: status normalization --------------------

_TERMINAL_COMPLETED = {"completed", "done", "success"}
_TERMINAL_MODERATED = {
    "moderation_blocked", "moderated", "blocked", "filtered",
    "rejected", "safety_blocked"
}
_TERMINAL_FAILED = {"failed", "error", "internal_error", "timeout"}

def _normalize_status(s: str) -> str:
    s = (s or "").lower().strip()
    if s in _TERMINAL_COMPLETED:
        return "completed"
    if s in _TERMINAL_MODERATED:
        return "moderation_blocked"
    if s in _TERMINAL_FAILED:
        return "failed"
    return "failed"  # неизвестное -> ошибка

# -------------------- redis lock per task_uuid --------------------

async def _acquire_webhook_lock(task_uuid: str, ttl: int = 180) -> Optional[Tuple[aioredis.Redis, str]]:
    r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
    key = f"wb:lock:{task_uuid}"
    try:
        ok = await r.set(key, "1", nx=True, ex=ttl)
        if ok:
            return r, key
        return None
    except Exception:
        try:
            await r.close()
        except Exception:
            pass
        return None

async def _release_webhook_lock(lock: Optional[Tuple[aioredis.Redis, str]]) -> None:
    if not lock:
        return
    r, key = lock
    try:
        await r.delete(key)
    except Exception:
        pass
    finally:
        try:
            await r.close()
        except Exception:
            pass
        
async def _clear_pending_marker(task_uuid: str) -> None:
    try:
        r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
        await r.delete(f"task:pending:{task_uuid}")
    except Exception:
        pass

# -------------------- FSM cleanup: remove "Генерирую…" --------------------

async def _clear_wait_and_reset(bot, chat_id: int, *, back_to: str = "auto") -> None:
    """
    Снимает 'Генерирую…' и возвращает пользователя:
      • если режим был create -> ждём новый текстовый промт
      • иначе -> ждём промт для правок
    """
    r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_FSM)
    try:
        storage = RedisStorage(redis=r, key_builder=DefaultKeyBuilder(with_bot_id=True))
        me = await bot.get_me()
        fsm = FSMContext(storage=storage, key=StorageKey(me.id, chat_id, chat_id))

        data = await fsm.get_data()
        wait_id = data.get("wait_msg_id")
        if wait_id:
            try:
                await bot.delete_message(chat_id, wait_id)
            except Exception:
                pass
            await fsm.update_data(wait_msg_id=None)

        mode = (data.get("mode") or "").lower()
        target = back_to
        if target == "auto":
            target = "create" if mode == "create" else "edit"

        if target == "create":
            await fsm.update_data(mode="create", edits=[], photos=[])
            await fsm.set_state(CreateStates.waiting_prompt)
        else:
            await fsm.set_state(GenStates.waiting_prompt)
    finally:
        await r.aclose()

# -------------------- webhook --------------------

@router.post("/webhook/runblob")
async def runblob_callback(req: Request):
    # 1) JSON
    try:
        payload = await req.json()
    except Exception:
        log.warning(json.dumps({"event": "webhook.hit", "error": "invalid_json"}, ensure_ascii=False))
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    # 2) лог заголовка
    # log.info(json.dumps({
    #     "event": "webhook.hit",
    #     "keys": list(payload.keys()),
    #     "raw_len": len(json.dumps(payload, ensure_ascii=False)),
    # }, ensure_ascii=False))

    # 3) нормализация
    task_uuid = payload.get("task_uuid") or payload.get("task_id") or payload.get("id")
    raw_status = str(payload.get("status", "")).lower()
    status = _normalize_status(raw_status)
    result_urls = payload.get("result_image_urls") or payload.get("result") or []
    credits_used = int(payload.get("credits_used") or 1)

    if not task_uuid:
        return JSONResponse({"ok": False, "error": "no_task_uuid"}, status_code=400)
    await _clear_pending_marker(task_uuid)
    # 4) эксклюзивная обработка (лок)
    lock = await _acquire_webhook_lock(task_uuid, ttl=180)
    if lock is None:
        log.info(json.dumps({"event": "webhook.skip_locked", "task_uuid": task_uuid}, ensure_ascii=False))
        return JSONResponse({"ok": True})

    try:
        # 5) транзакция/идемпотентность
        async with SessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.task_uuid == task_uuid))).scalar_one_or_none()
            if not task:
                log.info(json.dumps({"event": "webhook.no_task", "task_uuid": task_uuid}, ensure_ascii=False))
                return JSONResponse({"ok": True})

            if getattr(task, "delivered", False):
                log.info(json.dumps({"event": "webhook.already_delivered", "task_uuid": task_uuid}, ensure_ascii=False))
                return JSONResponse({"ok": True})

            # обновим статус и возможные кредиты (без delivered)
            await s.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(status=status, credits_used=credits_used)
            )
            await s.commit()

            user = await s.get(User, task.user_id)
            bot = req.app.state.bot

            # ---- COMPLETED ----
            if status == "completed":
                if not result_urls:
                    await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                    await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                    await s.execute(update(Task).where(Task.id == task.id).values(delivered=True))
                    await s.commit()
                    log.info(json.dumps({"event": "webhook.completed.no_urls", "task_uuid": task_uuid}, ensure_ascii=False))
                    return JSONResponse({"ok": True})

                # списание кредитов — считаем ДО коммита, не читаем поле после коммита
                before = int(user.balance_credits or 0)
                new_balance = max(0, before - credits_used)
                await s.execute(
                    update(User).where(User.id == user.id).values(balance_credits=new_balance)
                )
                await s.commit()

                # маркер «списано» (для возможного возврата в очереди)
                try:
                    r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
                    await r.setex(f"credits:debited:{task_uuid}", 86400, "1")
                except Exception:
                    pass

                # скачать результат
                image_url = result_urls[0]
                out_dir = "/tmp/nanobanana"
                os.makedirs(out_dir, exist_ok=True)
                local_path = os.path.join(out_dir, f"{task_uuid}.png")

                async with httpx.AsyncClient() as client:
                    last_exc = None
                    for _ in range(3):
                        try:
                            r = await client.get(image_url, timeout=120)
                            r.raise_for_status()
                            with open(local_path, "wb") as f:
                                f.write(r.content)
                            last_exc = None
                            break
                        except Exception as e:
                            last_exc = e
                            await asyncio.sleep(2)

                    if last_exc:
                        await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                        await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                        await s.execute(update(Task).where(Task.id == task.id).values(delivered=True))
                        await s.commit()
                        log.warning(json.dumps({"event": "webhook.download_failed", "task_uuid": task_uuid}, ensure_ascii=False))
                        return JSONResponse({"ok": True})

                # success: отправляем и помечаем доставленным
                await send_generation_result(user.chat_id, task_uuid, task.prompt, image_url, local_path, bot)
                await s.execute(update(Task).where(Task.id == task.id).values(delivered=True))
                await s.commit()
                # log.info(json.dumps({"event": "webhook.completed.sent", "task_uuid": task_uuid}, ensure_ascii=False))
                return JSONResponse({"ok": True})

            # ---- MODERATION ----
            if status == "moderation_blocked":
                # показываем ОДИН раз на задачу
                try:
                    rr = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
                    shown = await rr.setnx(f"msg:mod:{task_uuid}", "1")
                    if shown:
                        await rr.expire(f"msg:mod:{task_uuid}", 86400)
                        await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                        await safe_send_text(
                            bot,
                            user.chat_id,
                            "🛡️ Запрос не прошёл модерацию — измените промт или фото и попробуйте снова: /gen",
                        )
                except Exception:
                    # на всякий случай всё равно не спамим — очищаем ожидание
                    await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")

                await s.execute(update(Task).where(Task.id == task.id).values(delivered=True))
                await s.commit()
                log.info(json.dumps({"event": "webhook.moderation", "task_uuid": task_uuid}, ensure_ascii=False))
                return JSONResponse({"ok": True})

            # ---- FAILED / UNKNOWN ----
            await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
            await safe_send_text(
                bot,
                user.chat_id,
                    "⚠️ Не удалось сгенерировать изображение. Попробуйте снова чуть позже: /gen",
            )
            await s.execute(update(Task).where(Task.id == task.id).values(delivered=True))
            await s.commit()
            log.info(json.dumps({"event": "webhook.failed", "task_uuid": task_uuid, "raw_status": raw_status}, ensure_ascii=False))
            return JSONResponse({"ok": True})

    finally:
        await _release_webhook_lock(lock)
