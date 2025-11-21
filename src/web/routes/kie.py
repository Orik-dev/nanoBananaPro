# from __future__ import annotations

# import asyncio
# import json
# import logging
# import os
# import re
# from pathlib import Path
# from typing import Optional, Tuple

# import httpx
# import redis.asyncio as aioredis
# from fastapi import APIRouter, Request
# from fastapi.responses import JSONResponse
# from sqlalchemy import select, update
# from sqlalchemy.exc import OperationalError

# from aiogram.fsm.context import FSMContext
# from aiogram.fsm.storage.base import StorageKey
# from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage

# from bot.routers.generation import send_generation_result
# from bot.states import CreateStates, GenStates
# from core.config import settings
# from db.engine import SessionLocal
# from db.models import Task, User
# from services.telegram_safe import safe_send_text

# router = APIRouter()
# log = logging.getLogger("kie")


# async def _acquire_webhook_lock(task_id: str, ttl: int = 180) -> Optional[Tuple[aioredis.Redis, str]]:
#     """
#     ✅ ИСПРАВЛЕНО: закрываем Redis если не получили lock
#     """
#     r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
#     key = f"wb:lock:kie:{task_id}"
#     try:
#         ok = await r.set(key, "1", nx=True, ex=ttl)
#         if ok:
#             return r, key
#         # ✅ Закрываем если не получили lock
#         await r.aclose()
#         return None
#     except Exception:
#         try:
#             await r.aclose()
#         except Exception:
#             pass
#         return None


# async def _release_webhook_lock(lock: Optional[Tuple[aioredis.Redis, str]]) -> None:
#     if not lock:
#         return
#     r, key = lock
#     try:
#         await r.delete(key)
#     except Exception:
#         pass
#     finally:
#         try:
#             await r.aclose()
#         except Exception:
#             pass


# async def _clear_pending_marker(task_id: str) -> None:
#     r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
#     try:
#         await r.delete(f"task:pending:{task_id}")
#     except Exception:
#         pass
#     finally:
#         await r.aclose()  # ✅ ДОБАВИТЬ


# async def _clear_wait_and_reset(bot, chat_id: int, *, back_to: str = "auto") -> None:
#     r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_FSM)
#     try:
#         storage = RedisStorage(redis=r, key_builder=DefaultKeyBuilder(with_bot_id=True))
#         me = await bot.get_me()
#         fsm = FSMContext(storage=storage, key=StorageKey(me.id, chat_id, chat_id))

#         data = await fsm.get_data()
#         wait_id = data.get("wait_msg_id")
#         if wait_id:
#             try:
#                 await bot.delete_message(chat_id, wait_id)
#             except Exception:
#                 pass
#             await fsm.update_data(wait_msg_id=None)

#         mode = (data.get("mode") or "").lower()
#         target = back_to
#         if target == "auto":
#             target = "create" if mode == "create" else "edit"

#         if target == "create":
#             await fsm.update_data(mode="create", edits=[], photos=[])
#             await fsm.set_state(CreateStates.waiting_prompt)
#         else:
#             await fsm.set_state(GenStates.waiting_prompt)
#     finally:
#         await r.aclose()


# async def _update_with_retry(session, stmt, max_retries=3) -> bool:
#     """
#     ✅ НОВОЕ: Выполнение UPDATE с retry для deadlock
    
#     Args:
#         session: SQLAlchemy async session
#         stmt: UPDATE statement для выполнения
#         max_retries: Максимальное количество попыток
        
#     Returns:
#         True если успешно, False если deadlock после всех попыток
#     """
#     for attempt in range(1, max_retries + 1):
#         try:
#             await session.execute(stmt)
#             await session.commit()
#             return True
            
#         except OperationalError as e:
#             await session.rollback()
#             error_code = getattr(e.orig, 'args', [None])[0] if hasattr(e, 'orig') else None
            
#             # 1213 = Deadlock
#             if error_code == 1213:
#                 if attempt < max_retries:
#                     wait_time = 0.5 * attempt  # 0.5s, 1s, 1.5s
#                     log.warning(json.dumps({
#                         "event": "kie_webhook.deadlock_retry",
#                         "attempt": attempt,
#                         "max_retries": max_retries,
#                         "wait_time": wait_time
#                     }, ensure_ascii=False))
#                     await asyncio.sleep(wait_time)
#                     continue
#                 else:
#                     log.error(json.dumps({
#                         "event": "kie_webhook.deadlock_failed",
#                         "attempts": max_retries
#                     }, ensure_ascii=False))
#                     return False
#             else:
#                 # Другая ошибка - пробросим
#                 raise
                
#         except Exception:
#             await session.rollback()
#             raise
            
#     return False


# @router.post("/webhook/kie")
# async def kie_callback(req: Request):
#     try:
#         payload = await req.json()
#     except Exception:
#         log.warning(json.dumps({"event": "kie_webhook.invalid_json"}, ensure_ascii=False))
#         return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

#     data = payload.get("data") or {}
#     task_id = data.get("taskId")
#     state = str(data.get("state") or "").lower()
#     result_json = data.get("resultJson") or "{}"
#     fail_code = data.get("failCode")
#     fail_msg = data.get("failMsg")

#     if not task_id:
#         return JSONResponse({"ok": False, "error": "no_task_id"}, status_code=400)

#     await _clear_pending_marker(task_id)

#     lock = await _acquire_webhook_lock(task_id, ttl=180)
#     if lock is None:
#         log.info(json.dumps({"event": "kie_webhook.skip_locked", "task_id": task_id}, ensure_ascii=False))
#         return JSONResponse({"ok": True})

#     try:
#         async with SessionLocal() as s:
#             task = (await s.execute(select(Task).where(Task.task_uuid == task_id))).scalar_one_or_none()
#             if not task:
#                 log.info(json.dumps({"event": "kie_webhook.no_task", "task_id": task_id}, ensure_ascii=False))
#                 return JSONResponse({"ok": True})

#             if getattr(task, "delivered", False):
#                 log.info(json.dumps({"event": "kie_webhook.already_delivered", "task_id": task_id}, ensure_ascii=False))
#                 return JSONResponse({"ok": True})

#             user = await s.get(User, task.user_id)
#             bot = req.app.state.bot

#             if state == "success":
#                 try:
#                     parsed = json.loads(result_json)
#                     result_urls = parsed.get("resultUrls") or []
#                 except Exception:
#                     result_urls = []

#                 if not result_urls:
#                     await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
#                     await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                    
#                     # ✅ UPDATE с retry
#                     success = await _update_with_retry(
#                         s,
#                         update(Task).where(Task.id == task.id).values(delivered=True, status="completed")
#                     )
#                     if not success:
#                         log.error(json.dumps({"event": "kie_webhook.update_failed_deadlock", "task_id": task_id}, ensure_ascii=False))
                    
#                     log.info(json.dumps({"event": "kie_webhook.no_urls", "task_id": task_id}, ensure_ascii=False))
#                     return JSONResponse({"ok": True})

#                 # Списание кредитов с retry
#                 from services.pricing import credits_per_generation
#                 user_model = user.model_preference or "standard"
#                 credits_used = credits_per_generation(user_model)
#                 before = int(user.balance_credits or 0)
#                 new_balance = max(0, before - credits_used)
                
#                 # ✅ UPDATE User с retry
#                 success = await _update_with_retry(
#                     s,
#                     update(User).where(User.id == user.id).values(balance_credits=new_balance)
#                 )
#                 if not success:
#                     log.error(json.dumps({"event": "kie_webhook.user_update_failed", "task_id": task_id}, ensure_ascii=False))
#                     await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
#                     await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
#                     return JSONResponse({"ok": True})
                
#                 # ✅ UPDATE Task с retry
#                 success = await _update_with_retry(
#                     s,
#                     update(Task).where(Task.id == task.id).values(status="completed", credits_used=credits_used)
#                 )
#                 if not success:
#                     log.error(json.dumps({"event": "kie_webhook.task_update_failed", "task_id": task_id}, ensure_ascii=False))
#                     await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
#                     await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
#                     return JSONResponse({"ok": True})

#                 # Маркер списания
#                 try:
#                     r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
#                     await r.setex(f"credits:debited:{task_id}", 86400, "1")
#                     await r.aclose()
#                 except Exception:
#                     pass

#                 # ✅ Скачивание с правильным Authorization header
#                 image_url = result_urls[0]
#                 out_dir = "/tmp/nanobanana"
#                 os.makedirs(out_dir, exist_ok=True)
#                 local_path = os.path.join(out_dir, f"{task_id}.png")
#                   # Скачивание файла (уже есть)
#                 async with httpx.AsyncClient() as client:
#                     last_exc = None
#                     for attempt in range(1, 4):
#                         try:
#                             headers = {"Authorization": f"Bearer {settings.KIE_API_KEY}"}
#                             r = await client.get(image_url, headers=headers, timeout=120)
#                             r.raise_for_status()
#                             with open(local_path, "wb") as f:
#                                 f.write(r.content)
#                             last_exc = None
#                             log.info(json.dumps({"event": "kie_webhook.download_ok", "task_id": task_id, "attempt": attempt}, ensure_ascii=False))
#                             break
#                         except Exception as e:
#                             last_exc = e
#                             log.warning(json.dumps({"event": "kie_webhook.download_retry", "task_id": task_id, "attempt": attempt, "error": str(e)[:200]}, ensure_ascii=False))
#                             if attempt < 3:
#                                 await asyncio.sleep(2)

#                     if last_exc:
#                         await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
#                         await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                        
#                         await _update_with_retry(
#                             s,
#                             update(Task).where(Task.id == task.id).values(delivered=True)
#                         )
                        
#                         log.warning(json.dumps({"event": "kie_webhook.download_failed", "task_id": task_id}, ensure_ascii=False))
#                         return JSONResponse({"ok": True})

#                 # ✅ ДОБАВЛЕНО: Создать сжатую версию для превью
#                 preview_path = None
#                 try:
#                     from PIL import Image
#                     import os
                    
#                     # Проверяем размер файла
#                     file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
                    
#                     # Если файл > 10 MB - создаём сжатую версию для превью
#                     if file_size_mb > 10:
#                         img = Image.open(local_path)
                        
#                         # Конвертируем RGBA → RGB если нужно
#                         if img.mode in ('RGBA', 'LA', 'P'):
#                             background = Image.new('RGB', img.size, (255, 255, 255))
#                             if img.mode == 'RGBA':
#                                 background.paste(img, mask=img.split()[3])
#                             else:
#                                 background.paste(img)
#                             img = background
                        
#                         # Уменьшаем размер (максимум 2048px по большей стороне)
#                         max_size = 2048
#                         if max(img.size) > max_size:
#                             ratio = max_size / max(img.size)
#                             new_size = tuple(int(dim * ratio) for dim in img.size)
#                             img = img.resize(new_size, Image.Resampling.LANCZOS)
                        
#                         # Сохраняем сжатую версию
#                         preview_path = local_path.replace('.png', '_preview.jpg')
#                         quality = 85
                        
#                         # Подбираем качество, чтобы файл был < 10 MB
#                         for q in [85, 80, 75, 70, 65]:
#                             img.save(preview_path, 'JPEG', quality=q, optimize=True)
#                             preview_size_mb = os.path.getsize(preview_path) / (1024 * 1024)
#                             if preview_size_mb < 9.5:  # Запас на всякий случай
#                                 quality = q
#                                 break
                        
#                         log.info(json.dumps({
#                             "event": "kie_webhook.preview_created",
#                             "task_id": task_id,
#                             "original_mb": round(file_size_mb, 2),
#                             "preview_mb": round(preview_size_mb, 2),
#                             "quality": quality
#                         }, ensure_ascii=False))
                    
#                     else:
#                         # Если файл < 10 MB - используем оригинал как превью
#                         preview_path = local_path
#                         log.info(json.dumps({
#                             "event": "kie_webhook.no_preview_needed",
#                             "task_id": task_id,
#                             "file_mb": round(file_size_mb, 2)
#                         }, ensure_ascii=False))

#                 except Exception as e:
#                     log.warning(json.dumps({
#                         "event": "kie_webhook.preview_failed",
#                         "task_id": task_id,
#                         "error": str(e)[:100]
#                     }, ensure_ascii=False))
#                     # Если не удалось создать превью - используем оригинал
#                     preview_path = local_path

#                 # ✅ Отправить результат (передаём оба пути)
#                 await send_generation_result(
#                     user.chat_id, 
#                     task_id, 
#                     task.prompt, 
#                     image_url, 
#                     local_path,      # ✅ Оригинал для document
#                     bot,
#                     preview_path     # ✅ Превью для photo
#                 )
#                 # ✅ Используем context manager для автоматического закрытия
#                 # async with httpx.AsyncClient() as client:
#                 #     last_exc = None
#                 #     for attempt in range(1, 4):
#                 #         try:
#                 #             headers = {"Authorization": f"Bearer {settings.KIE_API_KEY}"}
#                 #             r = await client.get(image_url, headers=headers, timeout=120)
#                 #             r.raise_for_status()
#                 #             with open(local_path, "wb") as f:
#                 #                 f.write(r.content)
#                 #             last_exc = None
#                 #             log.info(json.dumps({"event": "kie_webhook.download_ok", "task_id": task_id, "attempt": attempt}, ensure_ascii=False))
#                 #             break
#                 #         except Exception as e:
#                 #             last_exc = e
#                 #             log.warning(json.dumps({"event": "kie_webhook.download_retry", "task_id": task_id, "attempt": attempt, "error": str(e)[:200]}, ensure_ascii=False))
#                 #             if attempt < 3:
#                 #                 await asyncio.sleep(2)

#                 #     if last_exc:
#                 #         await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
#                 #         await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                        
#                 #         # ✅ UPDATE с retry
#                 #         await _update_with_retry(
#                 #             s,
#                 #             update(Task).where(Task.id == task.id).values(delivered=True)
#                 #         )
                        
#                 #         log.warning(json.dumps({"event": "kie_webhook.download_failed", "task_id": task_id}, ensure_ascii=False))
#                 #         return JSONResponse({"ok": True})

#                 # # Отправить результат
#                 # await send_generation_result(user.chat_id, task_id, task.prompt, image_url, local_path, bot)
                
#                 # ✅ UPDATE delivered с retry
#                 success = await _update_with_retry(
#                     s,
#                     update(Task).where(Task.id == task.id).values(delivered=True)
#                 )
#                 if not success:
#                     log.error(json.dumps({"event": "kie_webhook.delivered_update_failed", "task_id": task_id}, ensure_ascii=False))
                
#                 # ✅ Удаление временных файлов СРАЗУ после отправки
#                 try:
#                     # Извлечь filename из image_url
#                     match = re.search(r'/proxy/image/([^/]+)$', image_url)
#                     if match:
#                         temp_file = Path("/app/temp_inputs") / match.group(1)
#                         if temp_file.exists():
#                             temp_file.unlink()
#                             log.info(json.dumps({"event": "kie_webhook.temp_file_cleaned", "file": str(temp_file)}, ensure_ascii=False))
#                 except Exception as e:
#                     log.warning(json.dumps({"event": "kie_webhook.cleanup_failed", "error": str(e)[:100]}, ensure_ascii=False))
                
#                 log.info(json.dumps({"event": "kie_webhook.success", "task_id": task_id}, ensure_ascii=False))
#                 return JSONResponse({"ok": True})

#             if state == "fail":
#                 await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                
#                 try:
#                     rr = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
#                     shown = await rr.setnx(f"msg:fail:{task_id}", "1")
#                     if shown:
#                         await rr.expire(f"msg:fail:{task_id}", 86400)
                        
#                         error_msg = "⚠️ Не удалось сгенерировать изображение. Попробуйте снова чуть позже: /gen"
#                         if fail_msg:
#                             error_msg = f"⚠️ Ошибка: {fail_msg[:200]}\n\nПопробуйте изменить промт или фото."
                        
#                         await safe_send_text(bot, user.chat_id, error_msg)
#                     await rr.aclose()
#                 except Exception:
#                     pass

#                 # ✅ UPDATE с retry для failed task
#                 success = await _update_with_retry(
#                     s,
#                     update(Task).where(Task.id == task.id).values(
#                         delivered=True,
#                         status="failed"
#                     )
#                 )
#                 if not success:
#                     log.error(json.dumps({"event": "kie_webhook.fail_update_failed", "task_id": task_id}, ensure_ascii=False))
                
#                 log.info(json.dumps({
#                     "event": "kie_webhook.fail",
#                     "task_id": task_id,
#                     "fail_code": fail_code,
#                     "fail_msg": fail_msg
#                 }, ensure_ascii=False))
#                 return JSONResponse({"ok": True})

#             log.info(json.dumps({"event": "kie_webhook.waiting", "task_id": task_id}, ensure_ascii=False))
#             return JSONResponse({"ok": True})

#     finally:
#         await _release_webhook_lock(lock)

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

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
log = logging.getLogger("kie")


async def _acquire_webhook_lock(task_id: str, ttl: int = 180) -> Optional[Tuple[aioredis.Redis, str]]:
    """
    ✅ ИСПРАВЛЕНО: закрываем Redis если не получили lock
    """
    r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
    key = f"wb:lock:kie:{task_id}"
    try:
        ok = await r.set(key, "1", nx=True, ex=ttl)
        if ok:
            return r, key
        # ✅ Закрываем если не получили lock
        await r.aclose()
        return None
    except Exception:
        try:
            await r.aclose()
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
            await r.aclose()
        except Exception:
            pass


async def _clear_pending_marker(task_id: str) -> None:
    r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
    try:
        await r.delete(f"task:pending:{task_id}")
    except Exception:
        pass
    finally:
        await r.aclose()  # ✅ ДОБАВИТЬ


async def _clear_wait_and_reset(bot, chat_id: int, *, back_to: str = "auto") -> None:
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


async def _update_with_retry(session, stmt, max_retries=3) -> bool:
    """
    ✅ НОВОЕ: Выполнение UPDATE с retry для deadlock
    
    Args:
        session: SQLAlchemy async session
        stmt: UPDATE statement для выполнения
        max_retries: Максимальное количество попыток
        
    Returns:
        True если успешно, False если deadlock после всех попыток
    """
    for attempt in range(1, max_retries + 1):
        try:
            await session.execute(stmt)
            await session.commit()
            return True
            
        except OperationalError as e:
            await session.rollback()
            error_code = getattr(e.orig, 'args', [None])[0] if hasattr(e, 'orig') else None
            
            # 1213 = Deadlock
            if error_code == 1213:
                if attempt < max_retries:
                    wait_time = 0.5 * attempt  # 0.5s, 1s, 1.5s
                    log.warning(json.dumps({
                        "event": "kie_webhook.deadlock_retry",
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "wait_time": wait_time
                    }, ensure_ascii=False))
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    log.error(json.dumps({
                        "event": "kie_webhook.deadlock_failed",
                        "attempts": max_retries
                    }, ensure_ascii=False))
                    return False
            else:
                # Другая ошибка - пробросим
                raise
                
        except Exception:
            await session.rollback()
            raise
            
    return False


@router.post("/webhook/kie")
async def kie_callback(req: Request):
    try:
        payload = await req.json()
    except Exception:
        log.warning(json.dumps({"event": "kie_webhook.invalid_json"}, ensure_ascii=False))
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    data = payload.get("data") or {}
    task_id = data.get("taskId")
    state = str(data.get("state") or "").lower()
    result_json = data.get("resultJson") or "{}"
    fail_code = data.get("failCode")
    fail_msg = data.get("failMsg")

    if not task_id:
        return JSONResponse({"ok": False, "error": "no_task_id"}, status_code=400)

    await _clear_pending_marker(task_id)

    lock = await _acquire_webhook_lock(task_id, ttl=180)
    if lock is None:
        log.info(json.dumps({"event": "kie_webhook.skip_locked", "task_id": task_id}, ensure_ascii=False))
        return JSONResponse({"ok": True})

    try:
        async with SessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.task_uuid == task_id))).scalar_one_or_none()
            if not task:
                log.info(json.dumps({"event": "kie_webhook.no_task", "task_id": task_id}, ensure_ascii=False))
                return JSONResponse({"ok": True})

            if getattr(task, "delivered", False):
                log.info(json.dumps({"event": "kie_webhook.already_delivered", "task_id": task_id}, ensure_ascii=False))
                return JSONResponse({"ok": True})

            user = await s.get(User, task.user_id)
            bot = req.app.state.bot

            if state == "success":
                try:
                    parsed = json.loads(result_json)
                    result_urls = parsed.get("resultUrls") or []
                except Exception:
                    result_urls = []

                if not result_urls:
                    await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                    await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                    
                    # ✅ UPDATE с retry
                    success = await _update_with_retry(
                        s,
                        update(Task).where(Task.id == task.id).values(delivered=True, status="completed")
                    )
                    if not success:
                        log.error(json.dumps({"event": "kie_webhook.update_failed_deadlock", "task_id": task_id}, ensure_ascii=False))
                    
                    log.info(json.dumps({"event": "kie_webhook.no_urls", "task_id": task_id}, ensure_ascii=False))
                    return JSONResponse({"ok": True})

                # Списание кредитов с retry
                from services.pricing import credits_per_generation
                user_model = user.model_preference or "standard"
                credits_used = credits_per_generation(user_model)
                before = int(user.balance_credits or 0)
                new_balance = max(0, before - credits_used)
                
                # ✅ UPDATE User с retry
                success = await _update_with_retry(
                    s,
                    update(User).where(User.id == user.id).values(balance_credits=new_balance)
                )
                if not success:
                    log.error(json.dumps({"event": "kie_webhook.user_update_failed", "task_id": task_id}, ensure_ascii=False))
                    await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                    await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                    return JSONResponse({"ok": True})
                
                # ✅ UPDATE Task с retry
                success = await _update_with_retry(
                    s,
                    update(Task).where(Task.id == task.id).values(status="completed", credits_used=credits_used)
                )
                if not success:
                    log.error(json.dumps({"event": "kie_webhook.task_update_failed", "task_id": task_id}, ensure_ascii=False))
                    await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                    await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                    return JSONResponse({"ok": True})

                # Маркер списания
                try:
                    r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
                    await r.setex(f"credits:debited:{task_id}", 86400, "1")
                    await r.aclose()
                except Exception:
                    pass

                # ✅ Скачивание с правильным Authorization header
                image_url = result_urls[0]
                out_dir = "/tmp/nanobanana"
                os.makedirs(out_dir, exist_ok=True)
                local_path = os.path.join(out_dir, f"{task_id}.png")
                # Скачивание файла (уже есть)
                async with httpx.AsyncClient() as client:
                    last_exc = None
                    for attempt in range(1, 4):
                        try:
                            headers = {"Authorization": f"Bearer {settings.KIE_API_KEY}"}
                            r = await client.get(image_url, headers=headers, timeout=120)
                            r.raise_for_status()
                            with open(local_path, "wb") as f:
                                f.write(r.content)
                            last_exc = None
                            log.info(json.dumps({"event": "kie_webhook.download_ok", "task_id": task_id, "attempt": attempt}, ensure_ascii=False))
                            break
                        except Exception as e:
                            last_exc = e
                            log.warning(json.dumps({"event": "kie_webhook.download_retry", "task_id": task_id, "attempt": attempt, "error": str(e)[:200]}, ensure_ascii=False))
                            if attempt < 3:
                                await asyncio.sleep(2)

                    if last_exc:
                        await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                        await safe_send_text(bot, user.chat_id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                        
                        await _update_with_retry(
                            s,
                            update(Task).where(Task.id == task.id).values(delivered=True)
                        )
                        
                        log.warning(json.dumps({"event": "kie_webhook.download_failed", "task_id": task_id}, ensure_ascii=False))
                        return JSONResponse({"ok": True})

                # ✅ ДОБАВЛЕНО: Создать сжатую версию для превью
                preview_path = None
                try:
                    from PIL import Image
                    import os
                    
                    # Проверяем размер файла
                    file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
                    
                    # Если файл > 10 MB - создаём сжатую версию для превью
                    if file_size_mb > 10:
                        img = Image.open(local_path)
                        
                        # Конвертируем RGBA → RGB если нужно
                        if img.mode in ('RGBA', 'LA', 'P'):
                            background = Image.new('RGB', img.size, (255, 255, 255))
                            if img.mode == 'RGBA':
                                background.paste(img, mask=img.split()[3])
                            else:
                                background.paste(img)
                            img = background
                        
                        # Уменьшаем размер (максимум 2048px по большей стороне)
                        max_size = 2048
                        if max(img.size) > max_size:
                            ratio = max_size / max(img.size)
                            new_size = tuple(int(dim * ratio) for dim in img.size)
                            img = img.resize(new_size, Image.Resampling.LANCZOS)
                        
                        # Сохраняем сжатую версию
                        preview_path = local_path.replace('.png', '_preview.jpg')
                        quality = 85
                        
                        # Подбираем качество, чтобы файл был < 10 MB
                        for q in [85, 80, 75, 70, 65]:
                            img.save(preview_path, 'JPEG', quality=q, optimize=True)
                            preview_size_mb = os.path.getsize(preview_path) / (1024 * 1024)
                            if preview_size_mb < 9.5:  # Запас на всякий случай
                                quality = q
                                break
                        
                        log.info(json.dumps({
                            "event": "kie_webhook.preview_created",
                            "task_id": task_id,
                            "original_mb": round(file_size_mb, 2),
                            "preview_mb": round(preview_size_mb, 2),
                            "quality": quality
                        }, ensure_ascii=False))
                    
                    else:
                        # Если файл < 10 MB - используем оригинал как превью
                        preview_path = local_path
                        log.info(json.dumps({
                            "event": "kie_webhook.no_preview_needed",
                            "task_id": task_id,
                            "file_mb": round(file_size_mb, 2)
                        }, ensure_ascii=False))

                except Exception as e:
                    log.warning(json.dumps({
                        "event": "kie_webhook.preview_failed",
                        "task_id": task_id,
                        "error": str(e)[:100]
                    }, ensure_ascii=False))
                    # Если не удалось создать превью - используем оригинал
                    preview_path = local_path

                # ✅ Отправить результат (передаём оба пути)
                await send_generation_result(
                    user.chat_id, 
                    task_id, 
                    task.prompt, 
                    image_url, 
                    local_path,      # ✅ Оригинал для document
                    bot,
                    preview_path     # ✅ Превью для photo
                )
                
                # ✅ UPDATE delivered с retry
                success = await _update_with_retry(
                    s,
                    update(Task).where(Task.id == task.id).values(delivered=True)
                )
                if not success:
                    log.error(json.dumps({"event": "kie_webhook.delivered_update_failed", "task_id": task_id}, ensure_ascii=False))
                
                # ✅ Удаление временных файлов СРАЗУ после отправки
                try:
                    # Извлечь filename из image_url
                    match = re.search(r'/proxy/image/([^/]+)$', image_url)
                    if match:
                        temp_file = Path("/app/temp_inputs") / match.group(1)
                        if temp_file.exists():
                            temp_file.unlink()
                            log.info(json.dumps({"event": "kie_webhook.temp_file_cleaned", "file": str(temp_file)}, ensure_ascii=False))
                except Exception as e:
                    log.warning(json.dumps({"event": "kie_webhook.cleanup_failed", "error": str(e)[:100]}, ensure_ascii=False))
                
                log.info(json.dumps({"event": "kie_webhook.success", "task_id": task_id}, ensure_ascii=False))
                return JSONResponse({"ok": True})

            if state == "fail":
                await _clear_wait_and_reset(bot, user.chat_id, back_to="auto")
                
                try:
                    rr = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
                    shown = await rr.setnx(f"msg:fail:{task_id}", "1")
                    if shown:
                        await rr.expire(f"msg:fail:{task_id}", 86400)
                        
                        error_msg = "⚠️ Не удалось сгенерировать изображение. Попробуйте снова чуть позже: /gen"
                        if fail_msg:
                            error_msg = f"⚠️ Ошибка: {fail_msg[:200]}\n\nПопробуйте изменить промт или фото."
                        
                        await safe_send_text(bot, user.chat_id, error_msg)
                    await rr.aclose()
                except Exception:
                    pass

                # ✅ UPDATE с retry для failed task
                success = await _update_with_retry(
                    s,
                    update(Task).where(Task.id == task.id).values(
                        delivered=True,
                        status="failed"
                    )
                )
                if not success:
                    log.error(json.dumps({"event": "kie_webhook.fail_update_failed", "task_id": task_id}, ensure_ascii=False))
                
                log.info(json.dumps({
                    "event": "kie_webhook.fail",
                    "task_id": task_id,
                    "fail_code": fail_code,
                    "fail_msg": fail_msg
                }, ensure_ascii=False))
                return JSONResponse({"ok": True})

            log.info(json.dumps({"event": "kie_webhook.waiting", "task_id": task_id}, ensure_ascii=False))
            return JSONResponse({"ok": True})

    finally:
        await _release_webhook_lock(lock)