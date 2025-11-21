from __future__ import annotations

import os
import uuid
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select, update

from arq import create_pool
from arq.connections import RedisSettings
import tempfile  

from core.config import settings
from db.engine import SessionLocal
from db.models import BroadcastJob, User

router = Router()

# Папка для медиа
MEDIA_DIR = Path(tempfile.gettempdir()) / "broadcast_media"
MEDIA_DIR.mkdir(exist_ok=True)


def _is_admin(uid: int) -> bool:
    """Проверка, является ли пользователь админом"""
    return settings.ADMIN_ID and int(settings.ADMIN_ID) == int(uid)


@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message):
    """
    Рассылка:
    1. /broadcast Текст — текстовая
    2. Фото + /broadcast Текст — с фото
    3. Видео + /broadcast Текст — с видео
    """
    if not _is_admin(msg.from_user.id):
        return
    
    raw_text = (msg.caption or msg.text or "").strip()
    if not raw_text.startswith("/broadcast"):
        return
    
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.answer(
            "📣 <b>Использование:</b>\n\n"
            "1️⃣ Текст: <code>/broadcast Ваш текст</code>\n"
            "2️⃣ Фото: прикрепите фото + <code>/broadcast Текст</code>\n"
            "3️⃣ Видео: прикрепите видео + <code>/broadcast Текст</code>",
            parse_mode="HTML"
        )
        return
    
    payload = parts[1].strip()
    
    media_type = None
    media_file_id = None
    
    # Фото
    if msg.photo:
        media_type = "photo"
        media_file_id = msg.photo[-1].file_id
    
    # 🔧 ВИДЕО - только file_id
    elif msg.video:
        media_type = "video"
        media_file_id = msg.video.file_id
    
    # Создать Job
    job_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        total_users = (await session.execute(select(User.chat_id))).scalars().unique().all()
        
        bj = BroadcastJob(
            id=job_id,
            created_by=msg.from_user.id,
            text=payload,
            media_type=media_type,
            media_file_id=media_file_id,
            media_file_path=None,  # ✅ Всегда None
            status="queued",
            total=len(total_users)
        )
        session.add(bj)
        await session.commit()

    # Запустить в ARQ
    redis_pool = await create_pool(
        RedisSettings(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            database=settings.REDIS_DB_CACHE,  # ✅ CACHE
        )
    )
    await redis_pool.enqueue_job("broadcast_send", job_id)
    
    media_info = ""
    if media_type == "photo":
        media_info = "\n📸 С фото"
    elif media_type == "video":
        media_info = "\n🎬 С видео"
    
    await msg.answer(
        f"🚀 Запустил рассылку <code>#{job_id}</code>{media_info}\n"
        f"Всего: <b>{bj.total}</b>\n\n"
        f"Отмена: <code>/broadcast_cancel {job_id}</code>\n"
        f"Статус: <code>/broadcast_status {job_id}</code>",
        parse_mode="HTML"
    )

@router.message(Command("broadcast_cancel"))
async def cmd_broadcast_cancel(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    
    parts = (msg.text or "").split(" ", 1)
    if len(parts) < 2:
        await msg.answer("Использование: <code>/broadcast_cancel JOB_ID</code>", parse_mode="HTML")
        return
    
    job_id = parts[1].strip()
    async with SessionLocal() as session:
        await session.execute(
            update(BroadcastJob)
            .where(BroadcastJob.id == job_id)
            .values(status="cancelled")
        )
        await session.commit()
    
    await msg.answer(f"⏹ Отменил рассылку <code>#{job_id}</code>", parse_mode="HTML")


@router.message(Command("broadcast_status"))
async def cmd_broadcast_status(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    
    parts = (msg.text or "").split(" ", 1)
    if len(parts) < 2:
        await msg.answer("Использование: <code>/broadcast_status JOB_ID</code>", parse_mode="HTML")
        return
    
    job_id = parts[1].strip()
    async with SessionLocal() as session:
        row = await session.execute(select(BroadcastJob).where(BroadcastJob.id == job_id))
        bj = row.scalars().first()
    
    if not bj:
        await msg.answer("❌ Не найдено")
        return
    
    media_info = ""
    if bj.media_type == "photo":
        media_info = "\n📸 Тип: фото"
    elif bj.media_type == "video":
        media_info = "\n🎬 Тип: видео"
    
    await msg.answer(
        f"📊 Рассылка <code>#{bj.id}</code>\n"
        f"Статус: <b>{bj.status}</b>{media_info}\n"
        f"Всего: <b>{bj.total}</b>\n"
        f"Отправлено: <b>{bj.sent}</b>\n"
        f"Ошибок: <b>{bj.failed}</b>\n"
        f"{('💬 ' + bj.note) if bj.note else ''}",
        parse_mode="HTML"
    )


@router.message(Command("broadcast_test"))
async def cmd_broadcast_test(msg: Message):
    """Тестовая рассылка только админу"""
    if not _is_admin(msg.from_user.id):
        return
    
    raw_text = (msg.caption or msg.text or "").strip()
    if not raw_text.startswith("/broadcast_test"):
        return
    
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Использование: <code>/broadcast_test Текст</code>", parse_mode="HTML")
        return
    
    payload = parts[1].strip()
    
    media_type = None
    media_file_id = None
    
    if msg.photo:
        media_type = "photo"
        media_file_id = msg.photo[-1].file_id
    elif msg.video:
        media_type = "video"
        media_file_id = msg.video.file_id
    
    try:
        if media_type == "photo" and media_file_id:
            await msg.bot.send_photo(
                msg.from_user.id, 
                photo=media_file_id, 
                caption=f"🧪 ТЕСТ:\n\n{payload}",
                parse_mode="HTML"
            )
        elif media_type == "video" and media_file_id:
            await msg.bot.send_video(
                msg.from_user.id, 
                video=media_file_id, 
                caption=f"🧪 ТЕСТ:\n\n{payload}",
                parse_mode="HTML"
            )
        else:
            await msg.answer(f"🧪 ТЕСТ:\n\n{payload}",parse_mode="HTML")
        
        await msg.answer("✅ Тест отправлен!")
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}")