import asyncio
import redis.asyncio as redis

from fastapi import FastAPI
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from aiogram.exceptions import TelegramRetryAfter
import logging
from core.config import settings
from core.logging import configure_json_logging
from redis.asyncio.connection import ConnectionPool

from bot.middlewares import ErrorLoggingMiddleware, RateLimitMiddleware
from bot.routers import commands as r_cmd
from bot.routers import payments as r_payments
from bot.routers import generation as r_generation

from web.routes import tg as rt_tg
from web.routes import yookassa as rt_yk
from web.routes import health as rt_health
from web.routes import misc as rt_misc
# from web.routes import runblob as rt_rb
from bot.routers import voice as r_voice
from bot.routers import broadcast as r_broadcast 
# from web.routes import freepik as rt_freepik
from web.routes import kie as rt_kie
from web.routes import proxy as rt_proxy

async def migrate_fsm_states():
    """
    Очищает FSM состояния, которые несовместимы с новой версией.
    """
    r = redis.Redis(
        host=settings.REDIS_HOST, 
        port=settings.REDIS_PORT, 
        db=settings.REDIS_DB_FSM
    )
    
    try:
        log = logging.getLogger("migration")
        cleaned = 0
        checked = 0
        max_check = 1000  # ✅ ОГРАНИЧИВАЕМ КОЛИЧЕСТВО
        
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor, match="fsm:*:state", count=100)
            
            for state_key in keys:
                checked += 1
                
                # ✅ ОГРАНИЧЕНИЕ - НЕ ПРОВЕРЯЕМ БОЛЬШЕ 1000 КЛЮЧЕЙ
                if checked > max_check:
                    log.warning(f"⚠️ FSM migration stopped at {max_check} keys (too many)")
                    return
                
                try:
                    state_value = await r.get(state_key)
                    if not state_value:
                        continue
                    
                    state_str = state_value.decode('utf-8')
                    
                    if "final_menu" in state_str or "generating" in state_str:
                        data_key = state_key.decode('utf-8').replace(':state', ':data')
                        data_value = await r.get(data_key)
                        
                        if data_value:
                            import json
                            try:
                                data = json.loads(data_value)
                                
                                if not data.get("last_result_file_id") and "final_menu" in state_str:
                                    await r.delete(state_key)
                                    await r.delete(data_key)
                                    cleaned += 1
                            except json.JSONDecodeError:
                                pass
                
                except Exception as e:
                    log.warning(f"Error processing FSM key {state_key}: {e}")
            
            if cursor == 0:
                break
        
        log.info(f"✅ FSM migration complete: checked={checked}, cleaned={cleaned}")
    
    except Exception as e:
        log.error(f"❌ FSM migration failed: {e}")
    finally:
        await r.aclose()


configure_json_logging()
app = FastAPI(title="NanoBanana", version="1.0.0")

bot = Bot(token=settings.TELEGRAM_BOT_TOKEN,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))


redis_pool = ConnectionPool(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB_FSM,
    max_connections=200,        # увеличено с 50
    decode_responses=False,
)

redis_fsm = redis.Redis(connection_pool=redis_pool)
storage = RedisStorage(redis=redis_fsm, key_builder=DefaultKeyBuilder(with_bot_id=True))
# redis_fsm = redis.from_url(f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB_FSM}")
# storage = RedisStorage(redis=redis_fsm, key_builder=DefaultKeyBuilder(with_bot_id=True))
dp = Dispatcher(storage=storage)

# include routers
dp.include_router(r_voice.router)
dp.include_router(r_broadcast.router)
dp.include_router(r_cmd.router)
dp.include_router(r_payments.router)
dp.include_router(r_generation.router)



# middlewares
dp.message.middleware(ErrorLoggingMiddleware())
dp.message.middleware(
    RateLimitMiddleware(
        redis.from_url(f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB_CACHE}"),
        settings.RATE_LIMIT_PER_MIN,
    )
)

dp.callback_query.middleware(ErrorLoggingMiddleware())
dp.callback_query.middleware(
    RateLimitMiddleware(
        redis.from_url(f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB_CACHE}"),
        settings.RATE_LIMIT_PER_MIN,
    )
)

 
app.state.bot = bot
app.state.dp = dp
app.state.webhook_secret = settings.WEBHOOK_SECRET_TOKEN

# FastAPI routes
app.include_router(rt_tg.router)
app.include_router(rt_yk.router)
app.include_router(rt_health.router)
app.include_router(rt_misc.router)
# app.include_router(rt_rb.router)
# app.include_router(rt_freepik.router)
app.include_router(rt_kie.router)
app.include_router(rt_proxy.router)

@app.on_event("startup")
async def on_startup():
    
    
    if settings.ADMIN_ID:
        from core.telegram_logger import TelegramLogHandler
        telegram_handler = TelegramLogHandler(bot, settings.ADMIN_ID)
        logging.getLogger().addHandler(telegram_handler)
        
    if settings.WEBHOOK_USE:
        try:
            await bot.set_webhook(
                url=f"{settings.PUBLIC_BASE_URL}/tg/webhook",
                secret_token=settings.WEBHOOK_SECRET_TOKEN,
                drop_pending_updates=True,
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.set_webhook(
                    url=f"{settings.PUBLIC_BASE_URL}/tg/webhook",
                    secret_token=settings.WEBHOOK_SECRET_TOKEN,
                    drop_pending_updates=True,
                )
            except TelegramRetryAfter:
                pass
           
    try:
        await migrate_fsm_states()
    except Exception as e:
        logging.getLogger("startup").error(f"FSM migration failed: {e}")


@app.on_event("shutdown")
async def on_shutdown():
    await bot.session.close()
