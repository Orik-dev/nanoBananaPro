# # from fastapi import APIRouter
# # from fastapi.responses import PlainTextResponse

# # router = APIRouter()

# # @router.get("/healthz")
# # async def healthz():
# #     return PlainTextResponse("ok")

# from fastapi import APIRouter
# from fastapi.responses import JSONResponse
# import redis.asyncio as redis
# from sqlalchemy import text

# from core.config import settings
# from db.engine import SessionLocal

# router = APIRouter()

# @router.get("/healthz")
# async def healthz():
#     """Простая проверка"""
#     return {"status": "ok"}

# @router.get("/health/deep")
# async def health_deep():
#     """Глубокая проверка всех сервисов"""
#     status = {"overall": "ok", "services": {}}
    
#     # Проверка БД
#     try:
#         async with SessionLocal() as s:
#             await s.execute(text("SELECT 1"))
#         status["services"]["database"] = "ok"
#     except Exception as e:
#         status["services"]["database"] = f"error: {str(e)[:100]}"
#         status["overall"] = "degraded"
    
#     # Проверка Redis FSM
#     try:
#         r = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_FSM)
#         await r.ping()
#         await r.aclose()
#         status["services"]["redis_fsm"] = "ok"
#     except Exception as e:
#         status["services"]["redis_fsm"] = f"error: {str(e)[:100]}"
#         status["overall"] = "degraded"
    
#     # Проверка Redis Cache
#     try:
#         r = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
#         await r.ping()
#         await r.aclose()
#         status["services"]["redis_cache"] = "ok"
#     except Exception as e:
#         status["services"]["redis_cache"] = f"error: {str(e)[:100]}"
#         status["overall"] = "degraded"
    
#     return JSONResponse(status, status_code=200 if status["overall"] == "ok" else 503)

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import redis.asyncio as redis
from sqlalchemy import text
from pathlib import Path
import httpx

from core.config import settings
from db.engine import SessionLocal

router = APIRouter()

@router.get("/healthz")
async def healthz():
    """Простая проверка"""
    return {"status": "ok"}

@router.get("/health/deep")
async def health_deep():
    """Глубокая проверка всех сервисов"""
    status = {"overall": "ok", "services": {}}
    
    # Проверка БД
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
        status["services"]["database"] = "ok"
    except Exception as e:
        status["services"]["database"] = f"error: {str(e)[:100]}"
        status["overall"] = "degraded"
    
    # Проверка Redis FSM
    try:
        r = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_FSM)
        await r.ping()
        await r.aclose()
        status["services"]["redis_fsm"] = "ok"
    except Exception as e:
        status["services"]["redis_fsm"] = f"error: {str(e)[:100]}"
        status["overall"] = "degraded"
    
    # Проверка Redis Cache
    try:
        r = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
        await r.ping()
        await r.aclose()
        status["services"]["redis_cache"] = "ok"
    except Exception as e:
        status["services"]["redis_cache"] = f"error: {str(e)[:100]}"
        status["overall"] = "degraded"
    
    # ✅ НОВОЕ: Проверка прокси изображений
    try:
        temp_dir = Path("/app/temp_inputs")
        if temp_dir.exists():
            files = list(temp_dir.glob("*.*"))
            status["services"]["image_proxy"] = f"ok (files: {len(files)})"
        else:
            status["services"]["image_proxy"] = "dir_not_exist"
            status["overall"] = "degraded"
    except Exception as e:
        status["services"]["image_proxy"] = f"error: {str(e)[:100]}"
        status["overall"] = "degraded"
    
    return JSONResponse(status, status_code=200 if status["overall"] == "ok" else 503)


@router.get("/health/proxy-test")
async def health_proxy_test():
    """✅ НОВОЕ: Тест доступности прокси извне"""
    temp_dir = Path("/app/temp_inputs")
    
    if not temp_dir.exists():
        return {"status": "error", "message": "temp dir not exist"}
    
    files = list(temp_dir.glob("*.*"))
    
    if not files:
        return {"status": "warning", "message": "no files in temp dir", "files_count": 0}
    
    # Берём первый файл для теста
    test_file = files[0]
    test_url = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/proxy/image/{test_file.name}"
    
    # Пытаемся скачать изображение через публичный URL
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(test_url)
            
            if resp.status_code == 200:
                return {
                    "status": "ok",
                    "message": "proxy accessible",
                    "test_url": test_url,
                    "content_type": resp.headers.get("content-type"),
                    "content_length": resp.headers.get("content-length"),
                    "files_count": len(files)
                }
            else:
                return {
                    "status": "error",
                    "message": f"proxy returned {resp.status_code}",
                    "test_url": test_url
                }
    except Exception as e:
        return {
            "status": "error",
            "message": f"cannot access proxy: {str(e)[:200]}",
            "test_url": test_url
        }