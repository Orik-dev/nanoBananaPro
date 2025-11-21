import multiprocessing
from core.logging import configure_json_logging


bind = "0.0.0.0:8000"


# Для 4 ядер: workers = 8 (2*CPU)
_cpu = multiprocessing.cpu_count()
workers = 8


threads = 1
worker_class = "uvicorn.workers.UvicornWorker"


# Тайминги
timeout = 120
graceful_timeout = 30
keepalive = 10


# Логи
accesslog = "-"
errorlog = "-"
loglevel = "info"


worker_tmp_dir = "/dev/shm"
preload_app = False


def post_fork(server, worker):
    configure_json_logging()