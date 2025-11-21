import logging
import json
import sys

def configure_json_logging(bot=None, admin_id=None):  # ✅ Добавили параметры
    class JsonFormatter(logging.Formatter):
        def format(self, record):
            d = {
                "lvl": record.levelname,
                "msg": record.getMessage(),
                "logger": record.name,
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            }
            if record.exc_info:
                d["exc"] = self.formatException(record.exc_info)
            return json.dumps(d, ensure_ascii=False)
    
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(logging.WARNING)
    
    # ✅ ДОБАВЛЯЕМ Telegram handler если передан bot и admin_id
    if bot and admin_id:
        from core.telegram_logger import TelegramLogHandler
        telegram_handler = TelegramLogHandler(bot, admin_id)
        root.addHandler(telegram_handler)
    
    # Отключаем шумные логи
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("arq.worker").setLevel(logging.WARNING)
    logging.getLogger("arq.jobs").setLevel(logging.WARNING)