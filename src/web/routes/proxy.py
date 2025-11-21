from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
import logging
import os

router = APIRouter()
log = logging.getLogger("proxy")

TEMP_DIR = Path("/app/temp_inputs")


@router.get("/proxy/image/{filename}")
async def proxy_image(filename: str):
    """
    ✅ УЛУЧШЕНО: Прокси для раздачи файлов с правильными заголовками для KIE AI
    """
    # Защита от path traversal
    if ".." in filename or "/" in filename:
        raise HTTPException(403, "Invalid filename")
    
    filepath = TEMP_DIR / filename
    
    if not filepath.exists():
        log.warning(f"File not found: {filename}")
        raise HTTPException(404, "File not found")
    
    # Определяем media type
    ext = filepath.suffix.lower()
    media_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    media_type = media_type_map.get(ext, "application/octet-stream")
    
    # ✅ Получаем размер файла
    file_size = os.path.getsize(filepath)
    
    return FileResponse(
        filepath,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "Accept-Ranges": "bytes",  # ✅ для поддержки частичной загрузки
            "Content-Length": str(file_size),  # ✅ явный размер файла
            "Access-Control-Allow-Origin": "*",  # ✅ CORS для внешних API
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )


@router.options("/proxy/image/{filename}")
async def proxy_image_options(filename: str):
    """✅ CORS preflight для внешних API"""
    return {
        "Allow": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }