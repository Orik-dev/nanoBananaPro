# FROM python:3.11-slim AS base
# ENV PYTHONDONTWRITEBYTECODE=1 \
#     PYTHONUNBUFFERED=1 \
#     PIP_NO_CACHE_DIR=1 \
#     PYTHONPATH=/app/src \
#     DEBIAN_FRONTEND=noninteractive

# ENV TZ=Asia/Baku

# RUN apt-get update && apt-get install -y --no-install-recommends \
#     curl tini build-essential tzdata ffmpeg \
#     && rm -rf /var/lib/apt/lists/*

# WORKDIR /app

# FROM base AS deps
# COPY requirements.txt .
# RUN pip install --upgrade pip && \
#     pip wheel --wheel-dir=/wheels -r requirements.txt

# FROM base AS runtime
# ENV PATH="/home/appuser/.local/bin:${PATH}"
# RUN useradd -m -u 10001 appuser

# # ✅ ДОБАВЛЕНО: создаем директорию с правами appuser
# RUN mkdir -p /app/temp_inputs && chown -R appuser:appuser /app/temp_inputs

# USER appuser

# COPY --from=deps /wheels /wheels
# RUN pip install --user /wheels/*

# COPY . /app

# ENTRYPOINT ["/usr/bin/tini","--"]
# CMD ["gunicorn","-c","gunicorn.conf.py","web.server:app"]

FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    DEBIAN_FRONTEND=noninteractive

ENV TZ=Asia/Baku

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl tini build-essential tzdata ffmpeg \
    default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

FROM base AS deps
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip wheel --wheel-dir=/wheels -r requirements.txt

FROM base AS runtime
ENV PATH="/home/appuser/.local/bin:${PATH}"
RUN useradd -m -u 10001 appuser

# ✅ ДОБАВЛЕНО: создаем директорию с правами appuser
RUN mkdir -p /app/temp_inputs && chown -R appuser:appuser /app/temp_inputs

USER appuser

COPY --from=deps /wheels /wheels
RUN pip install --user /wheels/*

COPY . /app

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["gunicorn","-c","gunicorn.conf.py","web.server:app"]