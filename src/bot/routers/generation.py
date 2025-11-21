from __future__ import annotations

import os
import sys
import asyncio
import logging
import time
from typing import List, Dict, Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from aiogram.exceptions import TelegramBadRequest

from bot.states import CreateStates
from db.engine import SessionLocal
from db.models import User
from services.pricing import CREDITS_PER_GENERATION
from bot.states import GenStates
from bot.keyboards import kb_gen_step_back, kb_final_result
from services.queue import enqueue_generation
from services.telegram_safe import (
    safe_answer,
    safe_send_text,
    safe_send_photo,
    safe_send_document,
    safe_edit_text,
    safe_delete_message,
)
from core.config import settings

log = logging.getLogger("generation")
router = Router()

_DEBOUNCE_TASKS: Dict[int, asyncio.Task] = {}


def resource_path(relative_path: str) -> str:
    try:
        base_path = sys._MEIPASS  # type: ignore
    except Exception:
        base_path = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base_path, relative_path)


PLACEHOLDER_PATH = resource_path(os.path.join('..', '..', 'assets', 'placeholder_light_gray_block.png'))


@router.message(F.photo | F.document)
async def auto_start_on_photo(m: Message, state: FSMContext):
    caption = (m.caption or "").strip().lower()
    if caption.startswith("/broadcast"):
        return
    
    cur = await state.get_state()
    
    if cur == GenStates.final_menu.state:
        await state.clear()
        await cmd_gen(m, state, show_intro=False)
        if (m.caption or "").strip():
            await state.update_data(auto_prompt=(m.caption or "").strip())
        if m.photo:
            await handle_images(m, state)
        elif _is_image_document(m):
            await handle_document_images(m, state)
        return
    
    if cur == GenStates.uploading_images.state:
        if (m.caption or "").strip():
            await state.update_data(auto_prompt=(m.caption or "").strip())
        if m.photo:
            await handle_images(m, state)
        elif _is_image_document(m):
            await handle_document_images(m, state)
        else:
            await safe_send_text(m.bot, m.chat.id, "Можно загрузить только изображения (PNG, JPG, WEBP).")
        return
    
    if cur == GenStates.waiting_prompt.state:
        await safe_send_text(
            m.bot, m.chat.id,
            "Сейчас нужно ввести промт текстом.\n\n"
            "Если хотите заменить фото — нажмите кнопку «↩️ Назад» выше."
        )
        return
    
    if cur == GenStates.generating.state:
        # ✅ Убрано дублирующееся сообщение - не отвечаем вообще
        return
    
    if cur in {CreateStates.waiting_prompt.state, CreateStates.selecting_aspect_ratio.state, CreateStates.generating.state}:
        await safe_send_text(
            m.bot, m.chat.id,
            "Режим /create работает без загрузки фото.\n"
            "Просто опишите, что нужно создать.\n\n"
            "Если хотите редактировать фото — используйте /gen"
        )
        return
    
    if cur and "Topup" in str(cur):
        await safe_send_text(
            m.bot, m.chat.id,
            "Завершите оплату или нажмите /start чтобы выйти."
        )
        return
    
    await cmd_gen(m, state, show_intro=False)
    if (m.caption or "").strip():
        await state.update_data(auto_prompt=(m.caption or "").strip())
    if m.photo:
        await handle_images(m, state)
    elif _is_image_document(m):
        await handle_document_images(m, state)


async def _kick_generation_now(m: Message, state: FSMContext, prompt: str) -> None:
    prompt = (prompt or "").strip()
    if len(prompt) < 3:
        await state.set_state(GenStates.waiting_prompt)
        await safe_send_text(m.bot, m.chat.id, "Введите промт (что изменить):", reply_markup=kb_gen_step_back())
        return

    data = await state.get_data()
    photos = data.get("photos", [])
    
    if not photos:
        await state.set_state(GenStates.waiting_prompt)
        await safe_send_text(m.bot, m.chat.id, "Введите промт (что изменить):", reply_markup=kb_gen_step_back())
        return

    file_ids = [p["file_id"] for p in photos]
    await state.set_state(GenStates.generating)
    wait_msg = await safe_send_text(m.bot, m.chat.id, "Генерирую…")
    await state.update_data(
        prompt=prompt,
        mode="edit",
        wait_msg_id=getattr(wait_msg, "message_id", None),
        gen_started_at=int(time.time()),
    )
    await enqueue_generation(m.from_user.id, prompt, file_ids)


@router.message(Command("gen"))
async def cmd_gen(m: Message, state: FSMContext, user_id: Optional[int] = None, show_intro: bool = True):
    await state.clear()
    uid = user_id or m.from_user.id

    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.chat_id == uid))).scalar_one_or_none()
        if u is None:
            await safe_send_text(m.bot, m.chat.id, "Нажмите /start, чтобы инициализироваться.")
            return

        if u.balance_credits < CREDITS_PER_GENERATION:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Карта РФ(₽)", callback_data="m_rub")],
                [InlineKeyboardButton(text="⭐️ Звёзды", callback_data="m_stars")],
            ])
            await safe_send_text(
                m.bot, m.chat.id,
                "Баланс генераций равен 0. Пополните баланс, чтобы продолжить.",
                reply_markup=keyboard,
            )
            return

    await start_generation(m, state, show_intro=show_intro)


async def start_generation(m: Message, state: FSMContext, show_intro: bool = True) -> None:
    _cancel_debounce(m.chat.id)
    await state.clear()
    await state.set_state(GenStates.uploading_images)
    await state.update_data(photos=[], album_id=None, finalized=False)

    if show_intro:
        text = "Пришлите 1-4 фотографии которые нужно изменить или объединить"
        if os.path.exists(PLACEHOLDER_PATH):
            await safe_send_photo(m.bot, m.chat.id, FSInputFile(PLACEHOLDER_PATH), caption=text)
        else:
            await safe_send_text(m.bot, m.chat.id, text)


def _is_image_document(msg: Message) -> bool:
    if not msg.document:
        return False
    mt = (msg.document.mime_type or "").lower()
    if mt.startswith("image/"):
        return True
    name = (msg.document.file_name or "").lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        if name.endswith(ext):
            return True
    return False


# def _cancel_debounce(chat_id: int) -> None:
#     task = _DEBOUNCE_TASKS.pop(chat_id, None)
#     if task and not task.done():
#         task.cancel()

def _cancel_debounce(chat_id: int) -> None:
    task = _DEBOUNCE_TASKS.pop(chat_id, None)  # pop - хорошо
    if task and not task.done():
        task.cancel()

async def _finalize_to_prompt(m: Message, state: FSMContext) -> None:
    _cancel_debounce(m.chat.id)

    data = await state.get_data()
    if data.get("finalized"):
        return

    photos: List[Dict[str, str]] = data.get("photos", [])
    if not photos:
        return

    await state.update_data(finalized=True)

    auto_prompt = (data.get("auto_prompt") or "").strip()
    if auto_prompt:
        await state.update_data(auto_prompt=None)
        return await _kick_generation_now(m, state, auto_prompt)

    await state.set_state(GenStates.waiting_prompt)
    await safe_send_text(m.bot, m.chat.id, "Введите промт (что изменить):", reply_markup=kb_gen_step_back())

    
def _schedule_album_finalize(m: Message, state: FSMContext, delay: float = 2.0):
    async def _debounce():
        try:
            await asyncio.sleep(delay)
            await _finalize_to_prompt(m, state)
        except asyncio.CancelledError:
            return
        finally:
            _DEBOUNCE_TASKS.pop(m.chat.id, None)

    _cancel_debounce(m.chat.id)
    _DEBOUNCE_TASKS[m.chat.id] = asyncio.create_task(_debounce())


async def _accept_photo(m: Message, state: FSMContext, item: Dict[str, str]) -> None:
    data = await state.get_data()
    photos: List[Dict[str, str]] = data.get("photos", [])
    album_id: Optional[str] = data.get("album_id")
    finalized: bool = data.get("finalized", False)

    if finalized:
        await safe_send_text(m.bot, m.chat.id, "Изображения уже приняты.")
        return

    # ✅ ИСПРАВЛЕНО: проверка ДО append
    if len(photos) >= 4:
        return  # Молча игнорируем 5+ фото

    mgid = getattr(m, "media_group_id", None)

    if not photos:
        if mgid:
            await state.update_data(album_id=str(mgid))
            photos.append(item)
            await state.update_data(photos=photos)
            _schedule_album_finalize(m, state, delay=2.0)
            return
        else:
            photos.append(item)
            await state.update_data(photos=photos)
            await _finalize_to_prompt(m, state)
            return

    if album_id is not None:
        if mgid and str(mgid) == album_id:
            photos.append(item)
            await state.update_data(photos=photos)
            _schedule_album_finalize(m, state, delay=2.0)
            return
        else:
            await safe_send_text(m.bot, m.chat.id, "Изображения уже приняты.")
            return

    await safe_send_text(m.bot, m.chat.id, "Изображения уже приняты.")
    return


@router.message(GenStates.uploading_images, F.photo)
async def handle_images(m: Message, state: FSMContext) -> None:
    try:
        if (m.caption or "").strip():
            await state.update_data(auto_prompt=(m.caption or "").strip())
        await _accept_photo(m, state, {"type": "photo", "file_id": m.photo[-1].file_id})
    except Exception:
        await safe_send_text(m.bot, m.chat.id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")


@router.message(GenStates.uploading_images, F.document)
async def handle_document_images(m: Message, state: FSMContext) -> None:
    try:
        if not _is_image_document(m):
            await safe_send_text(m.bot, m.chat.id, "Можно прикрепить только изображения. Поддержка: PNG, JPG, WEBP.")
            return
        if (m.caption or "").strip():
            await state.update_data(auto_prompt=(m.caption or "").strip())
        await _accept_photo(m, state, {"type": "document", "file_id": m.document.file_id})
    except Exception:
        await safe_send_text(m.bot, m.chat.id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")


@router.message(GenStates.uploading_images)
async def handle_text_while_upload(m: Message, state: FSMContext) -> None:
    await safe_send_text(m.bot, m.chat.id, "Пришлите 1-4 фотографии которые нужно изменить или объединить")


@router.callback_query(GenStates.waiting_prompt, F.data == "back_to_images")
async def back_to_images(c: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(c)
    _cancel_debounce(c.message.chat.id)
    await state.set_state(GenStates.uploading_images)
    await state.update_data(photos=[], album_id=None, finalized=False)
    await safe_edit_text(c.message, "Пришлите 1-4 фотографии которые нужно изменить или объединить")

@router.message(GenStates.waiting_prompt, F.text.startswith("/"))
async def gen_state_commands(m: Message, state: FSMContext):
    cmd = (m.text or "").split(maxsplit=1)[0].lower()

    if cmd == "/start":
        await state.clear()
        from bot.routers.commands import cmd_start
        await cmd_start(m)
        return
    if cmd == "/help":
        await state.clear()
        from bot.routers.commands import cmd_help
        await cmd_help(m)
        return
    if cmd == "/buy":
        await state.clear()
        from bot.routers.commands import cmd_buy
        await cmd_buy(m, state)
        return
    if cmd == "/example":
        await state.clear()
        from bot.routers.commands import cmd_example
        await cmd_example(m)
        return
    if cmd == "/bots":
        await state.clear()
        from bot.routers.commands import show_other_bots
        await show_other_bots(m, state)
        return
    if cmd in ("/gen", "/create"):
        await state.clear()
        if cmd == "/gen":
            await cmd_gen(m, state, show_intro=True)
        else:
            from bot.routers.commands import cmd_create
            await cmd_create(m, state)
        return

@router.message(GenStates.waiting_prompt, F.text)
async def got_user_prompt(m: Message, state: FSMContext) -> None:
    prompt = m.text.strip()
    if not prompt:
        await safe_send_text(m.bot, m.chat.id, "Введите промт (что изменить):")
        return
    if len(prompt) < 3:
        await safe_send_text(m.bot, m.chat.id, "Промт слишком короткий. Опишите задачу минимум в 3 символах 🙂")
        return
    if len(prompt) > 2000:
        prompt = prompt[:2000]

    data = await state.get_data()
    photos: List[Dict[str, str]] = data.get("photos", [])
    file_ids = [p["file_id"] for p in photos]

    await state.set_state(GenStates.generating)
    try:
        wait_msg = await safe_send_text(m.bot, m.chat.id, "Генерирую…")
        await state.update_data(
            prompt=prompt,
            mode="edit",
            wait_msg_id=getattr(wait_msg, "message_id", None),
            gen_started_at=int(time.time()),
        )
        await enqueue_generation(m.from_user.id, prompt, file_ids)
    except Exception:
        await safe_send_text(m.bot, m.chat.id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")


@router.message(GenStates.final_menu)
async def handle_final_menu_message(m: Message, state: FSMContext) -> None:
    if not m.text:
        await safe_send_text(m.bot, m.chat.id, "Напишите текстом, что изменить в результате, и я сгенерирую новую версию.")
        return

    prompt = (m.text or "").strip()
    
    if len(prompt) < 3:
        await safe_send_text(m.bot, m.chat.id, "Опишите правку чуть подробнее (минимум 3 символа).")
        return
    
    if len(prompt) > 2000:
        prompt = prompt[:2000]

    data = await state.get_data()
    photos: List[Dict[str, str]] = data.get("photos") or []
    if not photos:
        await safe_send_text(m.bot, m.chat.id, "Не удалось найти исходные изображения. Нажмите «Начать заново».")
        return

    base_prompt = (data.get("base_prompt") or data.get("prompt") or "").strip()
    edits = list(data.get("edits") or [])
    edits.append(prompt)
    cumulative_prompt = " ".join([base_prompt] + edits).strip()
    if len(cumulative_prompt) > 4000:
        cumulative_prompt = cumulative_prompt[:4000]

    file_ids = [p["file_id"] for p in photos]
    await state.set_state(GenStates.generating)
    try:
        wait_msg = await safe_send_text(m.bot, m.chat.id, "Генерирую…")
        await state.update_data(
            prompt=cumulative_prompt,
            base_prompt=base_prompt,
            edits=edits,
            mode="edit",
            wait_msg_id=getattr(wait_msg, "message_id", None),
            gen_started_at=int(time.time()),
        )
        await enqueue_generation(m.from_user.id, cumulative_prompt, file_ids)
    except Exception:
        await safe_send_text(m.bot, m.chat.id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")

@router.callback_query(F.data == "new_image")
async def new_image_any_state(c: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(c)
    _cancel_debounce(c.message.chat.id)
    await state.clear()
    await start_generation(c.message, state, show_intro=True)


@router.callback_query(GenStates.final_menu, F.data == "regenerate")
async def regenerate(c: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(c)
    data = await state.get_data()
    prompt = data.get("prompt")
    photos: List[Dict[str, str]] = data.get("photos")
    if not (prompt and photos):
        await safe_send_text(c.bot, c.message.chat.id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
        return
    try:
        wait_msg = await safe_send_text(c.bot, c.message.chat.id, "Генерирую…")
        await state.update_data(
            wait_msg_id=getattr(wait_msg, "message_id", None),
            gen_started_at=int(time.time()),
        )
        file_ids = [p["file_id"] for p in photos]
        await enqueue_generation(c.from_user.id, prompt, file_ids)
    except Exception:
        await safe_send_text(c.bot, c.message.chat.id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")


@router.callback_query(GenStates.final_menu, F.data == "send_file")
async def send_file_cb(c: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(c)
    data = await state.get_data()
    file_path = data.get("file_path")
    if file_path and os.path.exists(file_path):
        ok = await safe_send_document(c.bot, c.message.chat.id, file_path, caption="Скачать файлом — качество будет лучше, чем при просмотре здесь")
        if ok is None:
            return
    else:
        await safe_send_text(c.bot, c.message.chat.id, "Файл недоступен. Попробуйте сгенерировать снова.")


@router.callback_query(GenStates.final_menu, F.data == "cancel")
async def cancel_session(c: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(c)
    _cancel_debounce(c.message.chat.id)
    await state.clear()
    await safe_send_text(c.bot, c.message.chat.id, "Сессия завершена. Наберите /gen для нового изображения.")
    try:
        await safe_delete_message(c.bot, c.message.chat.id, c.message.message_id)
    except Exception:
        pass


# async def send_generation_result(
#     chat_id: int,
#     task_uuid: str,
#     prompt: str,
#     image_url: str,
#     file_path: str,
#     bot: Bot,
# ) -> None:
#     from aiogram.fsm.context import FSMContext
#     from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
#     from aiogram.fsm.storage.base import StorageKey
#     import redis.asyncio as redis

#     redis_cli = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_FSM)
    
#     try:
#         storage = RedisStorage(redis=redis_cli, key_builder=DefaultKeyBuilder(with_bot_id=True))
#         bot_info = await bot.get_me()
#         state = FSMContext(storage=storage, key=StorageKey(bot_info.id, chat_id, chat_id))

#         data = await state.get_data()
#         wait_msg_id = data.get("wait_msg_id")
#         if wait_msg_id:
#             try:
#                 await bot.delete_message(chat_id, wait_msg_id)
#             except Exception:
#                 pass

#         mode = (data.get("mode") or "edit").lower().strip()

#         if file_path and os.path.exists(file_path):
#             await safe_send_document(
#                 bot,
#                 chat_id,
#                 file_path,
#                 caption="Скачать файлом — качество будет лучше, чем при просмотре здесь"
#             )

#         if mode == "create":
#             result_msg = await safe_send_photo(
#                 bot,
#                 chat_id,
#                 # image_url, 
#                 FSInputFile(file_path),
#                 caption="Готово ✅ Напишите новый промт, чтобы сгенерировать ещё.",
#                 reply_markup=None,
#             )
            
#             result_file_id = None
#             if result_msg and result_msg.photo:
#                 result_file_id = result_msg.photo[-1].file_id
            
#             await state.clear()
#             await state.set_state(CreateStates.waiting_prompt)
#             await state.update_data(
#                 mode="create",
#                 prompt=prompt,
#                 last_result_file_id=result_file_id,
#                 file_path=file_path,
#             )
#             return

#         result_msg = await safe_send_photo(
#             bot,
#             chat_id,
#             # image_url,
#             FSInputFile(file_path),
#             caption="<b>Если хотите что-то изменить или добавить напишите в чат ⬇️</b>",
#             reply_markup=kb_final_result(),
#         )

#         result_file_id = None
#         if result_msg and result_msg.photo:
#             result_file_id = result_msg.photo[-1].file_id

#         photos = data.get("photos", [])
#         base_prompt = data.get("base_prompt") or prompt
#         edits = data.get("edits") or []
        
#         await state.clear()
#         await state.set_state(GenStates.final_menu)
#         await state.update_data(
#             mode="edit",
#             prompt=prompt,
#             base_prompt=base_prompt,
#             edits=edits,
#             photos=photos,
#             last_result_file_id=result_file_id,
#             file_path=file_path,
#         )
    
#     finally:
#         await redis_cli.aclose()


async def send_generation_result(
    chat_id: int,
    task_uuid: str,
    prompt: str,
    image_url: str,
    file_path: str,
    bot: Bot,
    preview_path: Optional[str] = None,  # ✅ ДОБАВЛЕНО
) -> None:
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
    from aiogram.fsm.storage.base import StorageKey
    import redis.asyncio as redis

    redis_cli = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_FSM)
    
    try:
        storage = RedisStorage(redis=redis_cli, key_builder=DefaultKeyBuilder(with_bot_id=True))
        bot_info = await bot.get_me()
        state = FSMContext(storage=storage, key=StorageKey(bot_info.id, chat_id, chat_id))

        data = await state.get_data()
        wait_msg_id = data.get("wait_msg_id")
        if wait_msg_id:
            try:
                await bot.delete_message(chat_id, wait_msg_id)
            except Exception:
                pass

        mode = (data.get("mode") or "edit").lower().strip()
        
        # ✅ Если preview_path не передан - используем оригинал
        if not preview_path:
            preview_path = file_path

        # ✅ Отправляем document для скачивания (оригинал)
        if file_path and os.path.exists(file_path):
            await safe_send_document(
                bot,
                chat_id,
                file_path,
                caption="Скачать файлом — качество будет лучше, чем при просмотре здесь"
            )

        # ✅ Режим create
        if mode == "create":
            result_msg = await safe_send_photo(
                bot,
                chat_id,
                FSInputFile(preview_path),  # ✅ Используем превью
                caption="Готово ✅ Напишите новый промт, чтобы сгенерировать ещё.",
                reply_markup=None,
            )
            
            result_file_id = None
            if result_msg and result_msg.photo:
                result_file_id = result_msg.photo[-1].file_id
            
            await state.clear()
            await state.set_state(CreateStates.waiting_prompt)
            await state.update_data(
                mode="create",
                prompt=prompt,
                last_result_file_id=result_file_id,
                file_path=file_path,
            )
            return

        # ✅ Режим edit - отправляем photo с кнопками (превью)
        result_msg = await safe_send_photo(
            bot,
            chat_id,
            FSInputFile(preview_path),  # ✅ Используем превью
            caption="<b>Если хотите что-то изменить или добавить напишите в чат ⬇️</b>",
            reply_markup=kb_final_result(),
        )

        result_file_id = None
        if result_msg and result_msg.photo:
            result_file_id = result_msg.photo[-1].file_id

        photos = data.get("photos", [])
        base_prompt = data.get("base_prompt") or prompt
        edits = data.get("edits") or []
        
        await state.clear()
        await state.set_state(GenStates.final_menu)
        await state.update_data(
            mode="edit",
            prompt=prompt,
            base_prompt=base_prompt,
            edits=edits,
            photos=photos,
            last_result_file_id=result_file_id,
            file_path=file_path,
        )
        
        # ✅ ДОБАВЛЕНО: Удаляем preview после отправки (если это не оригинал)
        if preview_path != file_path and os.path.exists(preview_path):
            try:
                os.unlink(preview_path)
            except Exception:
                pass
    
    finally:
        await redis_cli.aclose()