# from __future__ import annotations

# from pydantic_settings import BaseSettings
# from pydantic import computed_field


# class Settings(BaseSettings):

#     ENV: str = "prod"
#     TZ: str = "Asia/Baku"

#     TELEGRAM_BOT_TOKEN: str
#     WEBHOOK_USE: bool = True
#     PUBLIC_BASE_URL: str
#     WEBHOOK_SECRET_TOKEN: str
#     ADMIN_ID: int | None = None 
#     FFMPEG_PATH: str = "/usr/bin/ffmpeg"
    
#     FREEPIK_API_KEY: str
#     FREEPIK_BASE: str = "https://api.freepik.com/v1/ai/gemini-2-5-flash-image-preview"
#     FREEPIK_WEBHOOK_SECRET: str

#     # ✅ ОБНОВЛЕНО: добавлены модели и 4k по умолчанию
#     # KIE_API_KEY: str
#     # KIE_BASE: str = "https://api.kie.ai/api/v1"
#     # KIE_MODEL_CREATE: str = "google/nano-banana"
#     # KIE_MODEL_EDIT: str = "google/nano-banana-edit"
#     # KIE_MODEL_PRO_CREATE: str = "google/nano-banana-pro"  # ✅ ДОБАВЛЕНО
#     # KIE_MODEL_PRO_EDIT: str = "google/nano-banana-pro-edit"  # ✅ ДОБАВЛЕНО
#     # KIE_OUTPUT_FORMAT: str = "png"
#     # KIE_IMAGE_SIZE: str = "4k"  # ✅ ИЗМЕНЕНО: было "auto"
    
#     # ✅ ИСПРАВЛЕНО: Правильные названия моделей из документации
#     KIE_API_KEY: str
#     KIE_BASE: str = "https://api.kie.ai/api/v1"
#     KIE_MODEL_CREATE: str = "google/nano-banana"
#     KIE_MODEL_EDIT: str = "google/nano-banana-edit"
#     KIE_MODEL_PRO_CREATE: str = "nano-banana-pro"       # ✅ БЕЗ google/
#     KIE_MODEL_PRO_EDIT: str = "nano-banana-pro"         # ✅ БЕЗ google/ (может быть одна модель)
#     KIE_OUTPUT_FORMAT: str = "png"
#     KIE_IMAGE_SIZE: str = "auto"  # ✅ НЕ ТРОГАЕМ - это aspect ratio
    
#     RUNBLOB_API_KEY: str
#     RUNBLOB_BASE: str = "https://api.runblob.io/api/v1/gemini"

#     YOOKASSA_SHOP_ID: str
#     YOOKASSA_SECRET_KEY: str
#     CURRENCY: str = "RUB"
#     TOPUP_RETURN_URL: str

#     DB_HOST: str
#     DB_PORT: int = 3310
#     DB_USER: str
#     DB_PASSWORD: str
#     DB_NAME: str
    
#     BROADCAST_RPS: int = 10
#     BROADCAST_CONCURRENCY: int = 5
#     BROADCAST_BATCH: int = 100

#     REDIS_HOST: str = "redis"
#     REDIS_PORT: int = 6379
#     REDIS_DB_FSM: int = 1
#     REDIS_DB_CACHE: int = 2
#     RATE_LIMIT_PER_MIN: int = 30
#     REDIS_PASSWORD: str | None = None
#     REDIS_DB_BROADCAST: int = 3 
    
#     MAX_TASK_WAIT_S: int = 150
#     ARQ_JOB_TIMEOUT_OFFSET_S: int = 60

#     @computed_field
#     @property
#     def ARQ_JOB_TIMEOUT_S(self) -> int:
#         return max(self.MAX_TASK_WAIT_S + self.ARQ_JOB_TIMEOUT_OFFSET_S, 360)
    

#     @computed_field
#     @property
#     def DB_DSN(self) -> str:
#         return (
#             f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}@"
#             f"{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
#         )
    
# settings = Settings()

# ===============================
# ✅ ИСПРАВЛЕННЫЙ config.py
# ===============================

from pydantic_settings import BaseSettings
from pydantic import computed_field


class Settings(BaseSettings):
    ENV: str = "prod"
    TZ: str = "Asia/Baku"

    TELEGRAM_BOT_TOKEN: str
    WEBHOOK_USE: bool = True
    PUBLIC_BASE_URL: str
    WEBHOOK_SECRET_TOKEN: str
    ADMIN_ID: int | None = None 
    FFMPEG_PATH: str = "/usr/bin/ffmpeg"
    
    FREEPIK_API_KEY: str
    FREEPIK_BASE: str = "https://api.freepik.com/v1/ai/gemini-2-5-flash-image-preview"
    FREEPIK_WEBHOOK_SECRET: str

    # ✅ ИСПРАВЛЕНО: Правильные названия моделей
    KIE_API_KEY: str
    KIE_BASE: str = "https://api.kie.ai/api/v1"
    
    # Standard модели (старая логика)
    KIE_MODEL_CREATE: str = "google/nano-banana"
    KIE_MODEL_EDIT: str = "google/nano-banana-edit"
    
    # ✅ Pro модель (БЕЗ разделения на create/edit - одна модель)
    KIE_MODEL_PRO: str = "nano-banana-pro"  # ✅ ИСПРАВЛЕНО
    
    KIE_OUTPUT_FORMAT: str = "png"
    KIE_IMAGE_SIZE: str = "auto"  # ✅ aspect_ratio для standard (НЕ ТРОГАТЬ)
    
    RUNBLOB_API_KEY: str
    RUNBLOB_BASE: str = "https://api.runblob.io/api/v1/gemini"

    YOOKASSA_SHOP_ID: str
    YOOKASSA_SECRET_KEY: str
    CURRENCY: str = "RUB"
    TOPUP_RETURN_URL: str

    DB_HOST: str
    DB_PORT: int = 3310
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str
    
    BROADCAST_RPS: int = 10
    BROADCAST_CONCURRENCY: int = 5
    BROADCAST_BATCH: int = 100

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB_FSM: int = 1
    REDIS_DB_CACHE: int = 2
    RATE_LIMIT_PER_MIN: int = 30
    REDIS_PASSWORD: str | None = None
    REDIS_DB_BROADCAST: int = 3 
    
    MAX_TASK_WAIT_S: int = 150
    ARQ_JOB_TIMEOUT_OFFSET_S: int = 60

    @computed_field
    @property
    def ARQ_JOB_TIMEOUT_S(self) -> int:
        return max(self.MAX_TASK_WAIT_S + self.ARQ_JOB_TIMEOUT_OFFSET_S, 360)
    
    @computed_field
    @property
    def DB_DSN(self) -> str:
        return (
            f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}@"
            f"{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )
    
settings = Settings()
