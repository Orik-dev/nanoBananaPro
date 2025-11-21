# # src/web/routes/freepik.py
# from __future__ import annotations

# import asyncio
# import os

# import httpx
# from fastapi import APIRouter, Request, HTTPException
# from fastapi.responses import JSONResponse
# from sqlalchemy import select

# from vendors.freepik import verify_webhook
# from db.engine import SessionLocal
# from db.models import Task, User
# from services.telegram_safe import safe_send_text
# from bot.routers.generation import send_generation_result

# router = APIRouter()


# @router.post("/webhook/freepik")
# async def freepik_webhook(req: Request):
#     # 1) проверяем подпись
#     raw = await req.body()
#     if not verify_webhook(raw, req.headers):
#         raise HTTPException(401, "invalid signature")

#     # 2) читаем payload
#     payload = await req.json()

#     task_id = payload.get("task_id") or payload.get("id")
#     status = str(payload.get("status") or "").upper()
#     generated = payload.get("generated") or []

#     if not task_id:
#         return JSONResponse({"ok": False, "error": "no_task_id"}, status_code=400)

#     async with SessionLocal() as s:
#         task = (
#             await s.execute(
#                 select(Task).where(Task.task_uuid == task_id)
#             )
#         ).scalar_one_or_none()

#         if not task:
#             # задача в БД не найдена — считаем, что уже обработали/удалили
#             return JSONResponse({"ok": True})

#         # уже финализировано другим путём
#         if task.status == "completed":
#             return JSONResponse({"ok": True})

#         # обновляем статус задачи
#         task.status = status.lower()
#         await s.commit()

#         user = await s.get(User, task.user_id)
#         bot = req.app.state.bot

#         if status == "COMPLETED":
#             # списываем кредиты (идемпотентно)
#             user.balance_credits = max(0, int(user.balance_credits) - 1)
#             await s.commit()

#             if not generated:
#                 await safe_send_text(
#                     bot,
#                     user.chat_id,
#                     "Произошла ошибка при получении изображения. Команда уже разбирается."
#                 )
#                 return JSONResponse({"ok": False, "error": "no_generated"})

#             first = generated[0]
#             image_url = first.get("url") if isinstance(first, dict) else first
#             if not image_url:
#                 await safe_send_text(
#                     bot,
#                     user.chat_id,
#                     "Произошла ошибка при обработке результата. Команда уже разбирается."
#                 )
#                 return JSONResponse({"ok": False, "error": "bad_generated_item"})

#             out_dir = "/tmp/nanobanana"
#             os.makedirs(out_dir, exist_ok=True)
#             local_path = os.path.join(out_dir, f"{task_id}.png")

#             downloaded = False
#             async with httpx.AsyncClient() as client:
#                 for _ in range(3):
#                     try:
#                         r = await client.get(image_url, timeout=120)
#                         r.raise_for_status()
#                         with open(local_path, "wb") as f:
#                             f.write(r.content)
#                         downloaded = True
#                         break
#                     except Exception:
#                         await asyncio.sleep(2)

#             if not downloaded or not os.path.exists(local_path):
#                 await safe_send_text(
#                     bot,
#                     user.chat_id,
#                     "Произошла ошибка при скачивании результата. Попробуйте ещё раз или напишите @guard_gpt."
#                 )
#                 return JSONResponse({"ok": False, "error": "download_failed"})

#             # отправляем результат пользователю и обновляем FSM
#             await send_generation_result(
#                 user.chat_id,
#                 task_id,
#                 task.prompt,
#                 image_url,
#                 local_path,
#                 bot,
#             )

#         elif status in {"MODERATION_BLOCKED"}:
#             await safe_send_text(
#                 bot,
#                 user.chat_id,
#                 "❌ Не прошла проверку модерации. Попробуйте изменить фото или промт."
#             )
#         elif status in {"FAILED", "ERROR"}:
#             await safe_send_text(
#                 bot,
#                 user.chat_id,
#                 "Произошла ошибка при генерации. Команда уже разбирается."
#             )

#     return JSONResponse({"ok": True})



from __future__ import annotations
import asyncio
import os

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select

from vendors.freepik import verify_webhook
from db.engine import SessionLocal
from db.models import Task, User
from services.telegram_safe import safe_send_text
from bot.routers.generation import send_generation_result

router = APIRouter()


@router.post("/webhook/freepik")
async def freepik_webhook(req: Request):
    raw = await req.body()
    if not verify_webhook(raw, req.headers):
        raise HTTPException(401, "invalid signature")

    payload = await req.json()

    task_id = payload.get("task_id") or payload.get("id")
    status = str(payload.get("status") or "").upper()
    generated = payload.get("generated") or []

    if not task_id:
        return JSONResponse({"ok": False, "error": "no_task_id"}, status_code=400)

    async with SessionLocal() as s:
        task = (await s.execute(select(Task).where(Task.task_uuid == task_id))).scalar_one_or_none()
        if not task:
            return JSONResponse({"ok": True})  # идемпотентность

        if task.status == "completed":
            return JSONResponse({"ok": True})

        task.status = status.lower()
        await s.commit()

        user = await s.get(User, task.user_id)
        bot = req.app.state.bot

        if status == "COMPLETED":
            # списываем кредиты (идемпотентно)
            user.balance_credits = max(0, int(user.balance_credits) - 1)
            await s.commit()

            if not generated:
                await safe_send_text(bot, user.chat_id, "Произошла ошибка. Команда уже разбирается.")
                return JSONResponse({"ok": False, "error": "no_generated"})

            first = generated[0]
            image_url = first.get("url") if isinstance(first, dict) else first
            if not image_url:
                await safe_send_text(bot, user.chat_id, "Произошла ошибка. Команда уже разбирается.")
                return JSONResponse({"ok": False, "error": "bad_generated_item"})

            out_dir = "/tmp/nanobanana"
            os.makedirs(out_dir, exist_ok=True)
            local_path = os.path.join(out_dir, f"{task_id}.png")

            async with httpx.AsyncClient() as client:
                for _ in range(3):
                    try:
                        r = await client.get(image_url, timeout=120)
                        r.raise_for_status()
                        open(local_path, "wb").write(r.content)
                        break
                    except Exception:
                        await asyncio.sleep(2)

            await send_generation_result(user.chat_id, task_id, task.prompt, image_url, local_path, bot)

        elif status in {"MODERATION_BLOCKED"}:
            await safe_send_text(bot, user.chat_id, "❌ Не прошла проверку. Измените фото или промт.")
        elif status in {"FAILED", "ERROR"}:
            await safe_send_text(bot, user.chat_id, "Произошла ошибка. Команда уже разбирается.")

    return JSONResponse({"ok": True})
