import time
import asyncio
import logging
from typing import Callable, Dict, Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import Message
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramBadRequest,
)

logger = logging.getLogger("app")


class ErrorLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        start = time.time()
        try:
            return await handler(event, data)

        # Пользователь заблокировал бота — НИКАКИХ ответов не шлём
        except TelegramForbiddenError:
            logger.info(
                "handler_forbidden",
                extra={"chat_id": getattr(event.from_user, "id", None)},
            )
            return

        # Любая другая ошибка хендлера
        except Exception:
            logger.exception(
                "handler_error", extra={"chat_id": getattr(event.from_user, "id", None)}
            )
            # Пытаемся мягко уведомить пользователя, но безопасно
            try:
                await event.answer("⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
            except TelegramRetryAfter as e:
                try:
                    await event.bot.send_chat_action(event.chat.id, "typing")
                    await asyncio.sleep(e.retry_after)
                    await event.answer("⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")
                except Exception:
                    pass
            except (TelegramForbiddenError, TelegramBadRequest):
                # Сообщение удалить/изменить нельзя или нас блокнули — молча игнорируем
                pass
        # ❌ УБРАНО: finally блок с logger.info("message_processed") - слишком много логов


# class RateLimitMiddleware(BaseMiddleware):
#     def __init__(self, redis_client, limit_per_min: int):
#         self.redis = redis_client
#         self.limit = limit_per_min

#     async def __call__(self, handler, event: Message, data):
#         try:
#             key = f"rl:{event.from_user.id}"
#             count = await self.redis.incr(key)
#             if count == 1:
#                 await self.redis.expire(key, 60)
#             if count > self.limit:
#                 try:
#                     await event.answer("Слишком много запросов. Попробуйте через минуту.")
#                 except (TelegramForbiddenError, TelegramBadRequest):
#                     pass
#                 return
#         except Exception:
#             # ✅ ЕСЛИ REDIS УПАЛ - ПРОПУСКАЕМ БЕЗ RATE LIMIT
#             logger.warning(f"Rate limit Redis error for user {event.from_user.id}")  # ✅ ИСПРАВЛЕНО: log → logger
#             pass
        
#         return await handler(event, data)

class RateLimitMiddleware(BaseMiddleware):
    def __init__(self, redis_client, limit_per_min: int):
        self.redis = redis_client
        self.limit = limit_per_min

    async def __call__(self, handler, event: Message, data):
        redis_conn = None  # ✅ Локальная переменная
        try:
            # ✅ Используем connection pool, а не создаем новый
            key = f"rl:{event.from_user.id}"
            count = await self.redis.incr(key)
            if count == 1:
                await self.redis.expire(key, 60)
            if count > self.limit:
                try:
                    await event.answer("Слишком много запросов. Попробуйте через минуту.")
                except (TelegramForbiddenError, TelegramBadRequest):
                    pass
                return
        except Exception as e:
            # ✅ Логируем, но пропускаем запрос
            logger.warning(f"Rate limit Redis error for user {event.from_user.id}: {e}")
            pass
        
        return await handler(event, data)