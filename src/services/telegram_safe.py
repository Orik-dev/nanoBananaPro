# services/telegram_safe.py (полный файл с исправлением)

from __future__ import annotations
import asyncio
import logging
import tempfile
from typing import Optional, Union
from io import BytesIO
import os
from aiogram import Bot
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramServerError,
    TelegramNetworkError,# ✅ ДОБАВЛЕНО
)

from db.engine import SessionLocal
from db.models import User
from sqlalchemy import delete

log = logging.getLogger("tg_safe")

# ---------------------- внутренние утилиты ----------------------
async def _maybe_delete_user(chat_id: int):
    try:
        async with SessionLocal() as s:
            await s.execute(delete(User).where(User.chat_id == chat_id))
            await s.commit()
        log.info("user_deleted_due_block chat_id=%s", chat_id)
    except Exception:
        log.exception("failed to delete user on forbidden chat_id=%s", chat_id)

def _is_not_modified(err: Exception) -> bool:
    txt = str(err).lower()
    return "message is not modified" in txt or "message can't be edited" in txt

# ---------------------- публичные safe-обёртки ----------------------
async def safe_answer(cb: CallbackQuery):
    try:
        await cb.answer(cache_time=60)
    except TelegramBadRequest:
        pass
    except Exception:
        log.exception("cb.answer failed")

async def safe_send_text(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = False,
):
    """✅ УЛУЧШЕНО: добавлена обработка TelegramServerError"""
    max_attempts = 3
    
    for attempt in range(1, max_attempts + 1):
        try:
            return await bot.send_message(
                chat_id, 
                text, 
                reply_markup=reply_markup, 
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            
        except TelegramServerError as e:
            # ✅ Обработка 502 Bad Gateway и других 5xx
            if attempt < max_attempts:
                wait_time = 2 * attempt
                log.warning(f"TelegramServerError (attempt {attempt}/{max_attempts}), retry in {wait_time}s: {e}")
                await asyncio.sleep(wait_time)
                continue
            else:
                log.error(f"TelegramServerError after {max_attempts} attempts: {e}")
                return None
                
        except TelegramRetryAfter as e:
            if attempt < max_attempts:
                await asyncio.sleep(e.retry_after)
                continue
            else:
                log.exception(f"send_message failed after retry chat_id={chat_id}")
                return None
                
        except TelegramForbiddenError:
            await _maybe_delete_user(chat_id)
            return None
            
        except Exception as e:
            log.exception(f"send_message failed chat_id={chat_id}")
            return None
    
    return None

# async def safe_send_photo(
#     bot: Bot,
#     chat_id: int,
#     photo: Union[FSInputFile, bytes],
#     caption: Optional[str] = None,
#     reply_markup: Optional[InlineKeyboardMarkup] = None,
#     parse_mode: str = "HTML",
# ) -> Optional[Message]:
#     """✅ УЛУЧШЕНО: с retry и обработкой TelegramServerError"""
#     if isinstance(photo, FSInputFile):
#         try:
#             file_size = os.path.getsize(photo.path)
#             file_size_mb = file_size / (1024 * 1024)
            
#             if file_size_mb > 10:
#                 log.warning(f"Photo too large ({file_size_mb:.2f} MB), sending as document")
#                 return await safe_send_document(bot, chat_id, photo.path, caption=caption)
#         except Exception:
#             pass
    
#     for attempt in range(1, 4):
#         try:
#             return await bot.send_photo(
#                 chat_id, 
#                 photo=photo, 
#                 caption=caption, 
#                 reply_markup=reply_markup, 
#                 parse_mode=parse_mode
#             )
            
#         except TelegramServerError as e:
#             # ✅ Обработка 502 Bad Gateway
#             if attempt < 3:
#                 wait_time = 3 * attempt
#                 log.warning(f"TelegramServerError sending photo, retry {attempt}/3 in {wait_time}s")
#                 await asyncio.sleep(wait_time)
#                 continue
            
#             if attempt == 3:
#                 log.error(f"TelegramServerError after 3 attempts, trying as document")
#                 if isinstance(photo, FSInputFile):
#                     try:
#                         return await safe_send_document(bot, chat_id, photo.path, caption=caption)
#                     except Exception:
#                         pass
#                 return None
            
#         except TelegramBadRequest as e:
#             error_msg = str(e).lower()
            
#             if "internal" in error_msg and attempt < 3:
#                 wait_time = 3 * attempt
#                 log.warning(f"Telegram internal error, retry {attempt}/3 in {wait_time}s for chat {chat_id}")
#                 await asyncio.sleep(wait_time)
#                 continue
            
#             if attempt == 3:
#                 log.error(f"Failed to send photo after 3 attempts, trying as document: {error_msg[:100]}")
                
#                 if isinstance(photo, FSInputFile):
#                     try:
#                         return await safe_send_document(bot, chat_id, photo.path, caption=caption)
#                     except Exception as doc_err:
#                         log.error(f"Failed to send as document too: {doc_err}")
                
#                 log.exception(f"send_photo failed chat_id={chat_id}")
#                 return None
        
#         except TelegramRetryAfter as e:
#             if attempt < 3:
#                 await asyncio.sleep(e.retry_after)
#                 continue
#             else:
#                 log.exception(f"send_photo failed after retry chat_id={chat_id}")
#                 return None
        
#         except TelegramForbiddenError:
#             await _maybe_delete_user(chat_id)
#             return None
        
#         except Exception as e:
#             if "timeout" in str(e).lower() and attempt < 3:
#                 wait_time = 5 * attempt
#                 log.warning(f"Timeout, retry {attempt}/3 in {wait_time}s for chat {chat_id}")
#                 await asyncio.sleep(wait_time)
#                 continue
            
#             log.exception(f"send_photo failed chat_id={chat_id}")
#             return None
    
#     return None

async def safe_send_photo(
    bot: Bot,
    chat_id: int,
    photo: Union[FSInputFile, bytes],
    caption: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
) -> Optional[Message]:
    """✅ ИСПРАВЛЕНО: Убран fallback на document (дублирование)"""
    
    # ❌ УДАЛЕНО: Проверка размера и fallback на document
    # if isinstance(photo, FSInputFile):
    #     try:
    #         file_size = os.path.getsize(photo.path)
    #         file_size_mb = file_size / (1024 * 1024)
    #         
    #         if file_size_mb > 10:
    #             log.warning(f"Photo too large ({file_size_mb:.2f} MB), sending as document")
    #             return await safe_send_document(bot, chat_id, photo.path, caption=caption)
    #     except Exception:
    #         pass
    
    for attempt in range(1, 4):
        try:
            return await bot.send_photo(
                chat_id, 
                photo=photo, 
                caption=caption, 
                reply_markup=reply_markup, 
                parse_mode=parse_mode
            )
            
        except TelegramServerError as e:
            if attempt < 3:
                wait_time = 3 * attempt
                log.warning(f"TelegramServerError sending photo, retry {attempt}/3 in {wait_time}s")
                await asyncio.sleep(wait_time)
                continue
            
            # ❌ УДАЛЕНО: fallback на document
            # if attempt == 3:
            #     log.error(f"TelegramServerError after 3 attempts, trying as document")
            #     if isinstance(photo, FSInputFile):
            #         try:
            #             return await safe_send_document(bot, chat_id, photo.path, caption=caption)
            #         except Exception:
            #             pass
            #     return None
            
            # ✅ Просто возвращаем None если не удалось
            log.error(f"TelegramServerError after 3 attempts, failed to send photo")
            return None
            
        except TelegramBadRequest as e:
            error_msg = str(e).lower()
            
            if "internal" in error_msg and attempt < 3:
                wait_time = 3 * attempt
                log.warning(f"Telegram internal error, retry {attempt}/3 in {wait_time}s for chat {chat_id}")
                await asyncio.sleep(wait_time)
                continue
            
            # ❌ УДАЛЕНО: fallback на document
            # if attempt == 3:
            #     log.error(f"Failed to send photo after 3 attempts, trying as document: {error_msg[:100]}")
            #     if isinstance(photo, FSInputFile):
            #         try:
            #             return await safe_send_document(bot, chat_id, photo.path, caption=caption)
            #         except Exception as doc_err:
            #             log.error(f"Failed to send as document too: {doc_err}")
            #     log.exception(f"send_photo failed chat_id={chat_id}")
            #     return None
            
            # ✅ Просто логируем ошибку
            if attempt == 3:
                log.exception(f"send_photo failed chat_id={chat_id}")
                return None
        
        except TelegramRetryAfter as e:
            if attempt < 3:
                await asyncio.sleep(e.retry_after)
                continue
            else:
                log.exception(f"send_photo failed after retry chat_id={chat_id}")
                return None
        
        except TelegramForbiddenError:
            await _maybe_delete_user(chat_id)
            return None
        
        except Exception as e:
            if "timeout" in str(e).lower() and attempt < 3:
                wait_time = 5 * attempt
                log.warning(f"Timeout, retry {attempt}/3 in {wait_time}s for chat {chat_id}")
                await asyncio.sleep(wait_time)
                continue
            
            log.exception(f"send_photo failed chat_id={chat_id}")
            return None
    
    return None

async def safe_send_document(
    bot: Bot,
    chat_id: int,
    file_path: str,
    caption: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,  # ✅ ДОБАВЛЕНО
):
    """✅ ИСПРАВЛЕНО: добавлен параметр reply_markup"""
    if not os.path.exists(file_path):
        log.error(f"File not found: {file_path}")
        return None
    
    try:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            log.error(f"File is empty: {file_path}")
            return None
    except Exception as e:
        log.error(f"Cannot check file size {file_path}: {e}")
        return None
    
    for attempt in range(1, 4):
        try:
            return await bot.send_document(
                chat_id, 
                document=FSInputFile(file_path), 
                caption=caption,
                reply_markup=reply_markup,  # ✅ ДОБАВЛЕНО
                request_timeout=120
            )
            
        except TelegramServerError as e:
            if attempt < 3:
                wait_time = 3 * attempt
                log.warning(f"TelegramServerError sending document, retry {attempt}/3 in {wait_time}s")
                await asyncio.sleep(wait_time)
                continue
            else:
                log.exception(f"send_document failed after 3 attempts chat_id={chat_id}")
                return None
            
        except TelegramBadRequest as e:
            error_msg = str(e).lower()
            
            if "file must be non-empty" in error_msg:
                log.error(f"File is empty or corrupted: {file_path}")
                return None
            
            if "internal" in error_msg and attempt < 3:
                wait_time = 3 * attempt
                log.warning(f"Telegram internal error, retry {attempt}/3 in {wait_time}s")
                await asyncio.sleep(wait_time)
                continue
            
            if attempt == 3:
                log.exception(f"send_document failed chat_id={chat_id}")
                return None
        
        except TelegramRetryAfter as e:
            if attempt < 3:
                await asyncio.sleep(e.retry_after)
                continue
            else:
                log.exception(f"send_document failed after retry chat_id={chat_id}")
                return None
        
        except TelegramForbiddenError:
            await _maybe_delete_user(chat_id)
            return None
        
        except Exception as e:
            if "timeout" in str(e).lower() and attempt < 3:
                wait_time = 5 * attempt
                log.warning(f"Timeout, retry {attempt}/3 in {wait_time}s")
                await asyncio.sleep(wait_time)
                continue
            
            log.exception(f"send_document failed chat_id={chat_id}")
            return None
    
    return None

async def safe_edit_text(
    message: Message,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = False,
):
    try:
        return await message.edit_text(
            text, 
            reply_markup=reply_markup, 
            parse_mode=parse_mode, 
            disable_web_page_preview=disable_web_page_preview
        )
    except TelegramBadRequest as e:
        if _is_not_modified(e):
            if reply_markup is not None:
                try:
                    return await message.edit_reply_markup(reply_markup=reply_markup)
                except TelegramBadRequest as e2:
                    if _is_not_modified(e2):
                        return message
                    log.exception("edit_reply_markup bad request (not 'modified')")
                except Exception:
                    log.exception("edit_reply_markup failed")
            return message
        log.exception("edit_text bad request")
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            return await message.edit_text(
                text, 
                reply_markup=reply_markup, 
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview
            )
        except Exception:
            log.exception("edit_text failed after retry")
    except TelegramForbiddenError:
        return None
    except Exception:
        log.exception("edit_text failed")
    return None

async def safe_edit_reply_markup(
    message: Message,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    try:
        return await message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if _is_not_modified(e):
            return message
        log.exception("edit_reply_markup bad request")
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            return await message.edit_reply_markup(reply_markup=reply_markup)
        except Exception:
            log.exception("edit_reply_markup failed after retry")
    except TelegramForbiddenError:
        return None
    except Exception:
        log.exception("edit_reply_markup failed")
    return None

async def safe_delete_message(bot: Bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass
    except TelegramForbiddenError:
        await _maybe_delete_user(chat_id)
    except Exception:
        log.exception("delete_message failed chat_id=%s", chat_id)

async def safe_send_video(
    bot: Bot,
    chat_id: int,
    video: Union[FSInputFile, bytes],
    caption: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
) -> Optional[Message]:
    try:
        if isinstance(video, BytesIO):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
                temp_file.write(video.getvalue())
                video_file = FSInputFile(temp_file.name)
        else:
            video_file = video
        return await bot.send_video(chat_id, video=video_file, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            return await bot.send_video(chat_id, video=video_file, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
        except TelegramForbiddenError:
            await _maybe_delete_user(chat_id)
        except Exception:
            log.exception("send_video failed after retry chat_id=%s", chat_id)
    except TelegramForbiddenError:
        await _maybe_delete_user(chat_id)
    except Exception as e:
        log.exception("send_video failed chat_id=%s: %s", chat_id, str(e))
    finally:
        if isinstance(video, BytesIO) and 'temp_file' in locals():
            os.unlink(temp_file.name)
    return None