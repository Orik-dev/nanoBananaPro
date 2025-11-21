📘 README.md (архитектура и запуск)
Что это

Нано Банана — телеграм-бот для генерации текста/картинок через RunBlob/Gemini. Баланс в кредитах, пополнение через YooKassa и Telegram Stars. UX: минимум спама, всё через понятные кнопки.

Архитектура

src/core — конфиг/логирование.

src/db — движок и ORM-модели.

src/vendors — внешние клиенты (RunBlob).

src/services — бизнес-логика: тарифы, генерация, платежи, пользователи.

src/bot — клавиатуры, middleware, FSM, routers (команды/звёзды).

src/web — FastAPI: webhook Telegram, webhook YooKassa, health, return page.

db/create.sql — миграция MySQL 5.7.

Dockerfile / compose / nginx / gunicorn — продакшен-сборка.

nanobanana/
├─ src/
│  ├─ core/
│  │  ├─ config.py               # настройки/ENV
│  │  └─ logging.py              # JSON-логгер
│  ├─ db/
│  │  ├─ engine.py               # async engine + session
│  │  └─ models.py               # SQLAlchemy ORM (users, payments, credit_ledger, tasks)
│  ├─ vendors/
│  │  └─ runblob.py              # клиент RunBlob/Gemini
│  ├─ services/
│  │  ├─ pricing.py              # тарифы/пакеты/конверсия
│  │  ├─ users.py                # учётка/баланс
│  │  ├─ generation.py           # генерация, списание
│  │  └─ payments.py             # YooKassa create + webhook
│  ├─ bot/
│  │  ├─ keyboards.py            # инлайн-клавиатуры
│  │  ├─ middlewares.py          # логирование, rate-limit
│  │  ├─ states.py               # FSM состояния
│  │  └─ routers/
│  │     ├─ commands.py          # /start /help /balance /gen /history /topup
│  │     └─ stars.py             # /topup_stars (Telegram Stars)
│  └─ web/
│     ├─ server.py               # FastAPI приложение, webhook setup
│     └─ routes/
│        ├─ tg.py                # /tg/webhook
│        ├─ yookassa.py          # /yookassa/callback
│        ├─ health.py            # /healthz
│        └─ misc.py              # /pay/return
├─ db/
│  └─ create.sql                 # миграция БД (MySQL 5.7)
├─ deploy/
│  └─ nginx.conf                 # nginx (проксирование)
├─ Dockerfile
├─ docker-compose.yml
├─ gunicorn.conf.py
├─ requirements.txt
└─ README.md
