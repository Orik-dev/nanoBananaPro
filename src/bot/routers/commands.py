from __future__ import annotations

import os
import time
import redis.asyncio as aioredis

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from arq import create_pool
from arq.connections import RedisSettings

from bot.states import CreateStates
from bot.routers.generation import cmd_gen
from bot.keyboards import kb_topup_methods, kb_aspect_ratio_selector, validate_aspect_ratio,kb_model_selector
from services.users import ensure_user
from services.telegram_safe import safe_answer, safe_send_text,safe_edit_text
from core.config import settings
from db.engine import SessionLocal
from db.models import User
from services.queue import enqueue_generation

router = Router()


def get_asset_path(filename: str) -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(base_dir, "assets", filename)


# ======================= /create (режим 2025) =======================

@router.message(Command("create"))
async def cmd_create(m: Message, state: FSMContext):
    # чистый режим генерации по тексту, без исходных фото
    await state.clear()
    # await state.set_state(CreateStates.waiting_prompt)
    # await safe_send_text(m.bot, m.chat.id, "✍️ Введите промт для генерации изображения:")
    await state.set_state(CreateStates.selecting_aspect_ratio)
    await state.update_data(mode="create", photos=[], edits=[])
    await safe_send_text(
        m.bot, m.chat.id,
        "Выберите соотношение сторон для изображения:",
        reply_markup=kb_aspect_ratio_selector()
    )

@router.callback_query(CreateStates.selecting_aspect_ratio, F.data.startswith("ar_"))
async def handle_create_aspect_ratio(c: CallbackQuery, state: FSMContext):
    ar = c.data.replace("ar_", "")
    
    # ❌ УБРАЛИ обработку "skip" - теперь пользователь ОБЯЗАН выбрать
    if ar.startswith("header_"):
        await safe_answer(c)
        return  # заголовки не кликабельны
    elif not validate_aspect_ratio(ar):
        await safe_answer(c, "❌ Неверное соотношение")
        return
    
    # ✅ Сохраняем выбранное соотношение
    await state.update_data(aspect_ratio=ar)
    await state.set_state(CreateStates.waiting_prompt)
    await safe_edit_text(c.message, "Введите промт для генерации изображения:")
    await safe_answer(c)


# FIX: в aiogram 3.20.0 нельзя писать Command() без аргументов.
# В режиме ожидания промта — перехватываем ЛЮБУЮ команду, чтобы не принять её как промт.
@router.message(CreateStates.waiting_prompt, F.text.startswith("/"))
async def create_state_commands(m: Message, state: FSMContext):
    cmd = (m.text or "").split(maxsplit=1)[0].lower()

    if cmd in ("/gen", "/edit"):
        await state.clear()
        await cmd_gen(m, state, show_intro=True)
        return
    if cmd == "/start":
        await state.clear()
        await cmd_start(m)
        return
    if cmd == "/help":
        await state.clear()
        await cmd_help(m)
        return
    if cmd == "/buy":
        await state.clear()
        await cmd_buy(m, state)
        return
    if cmd == "/example":
        await state.clear()
        await cmd_example(m)
        return
    if cmd == "/bots":
        await state.clear()
        await show_other_bots(m, state)
        return
    if cmd == "/live":
        await state.clear()
        await cmd_live(m)
        return
    # прочие команды поймают глобальные хэндлеры
    return


@router.message(CreateStates.waiting_prompt, F.text)
async def create_got_prompt(m: Message, state: FSMContext) -> None:
    prompt = (m.text or "").strip()

    # FIX: дополнительная защита — если вдруг снова команда
    if prompt.startswith("/"):
        await create_state_commands(m, state)
        return

    if len(prompt) < 3:
        await safe_send_text(m.bot, m.chat.id, "Промт слишком короткий. Опишите задачу минимум в 3 символах 🙂")
        return
    if len(prompt) > 2000:
        prompt = prompt[:2000]
        
    data = await state.get_data()
    aspect_ratio = data.get("aspect_ratio")    

    await state.set_state(CreateStates.generating)
    wait_msg = await safe_send_text(m.bot, m.chat.id, "Генерирую…")
    await state.update_data(mode="create", wait_msg_id=getattr(wait_msg, "message_id", None))
    await enqueue_generation(m.from_user.id, prompt, [],aspect_ratio=aspect_ratio)


# ======================= /start =======================

@router.message(Command("start"))
async def cmd_start(m: Message):
    await ensure_user(m.from_user)
    img_path = get_asset_path("nanobanana.png")

    caption = (
        "🍌 <b>Добро пожаловать в Nano Banana</b> — Ai фотошоп от Google в удобном телеграм-боте:\n\n"
        "🎁 У вас есть <b>5 бесплатных генераций</b>\n\n"
        "💰 Тариф: <b>1 генерация</b> = <b>1 изображение</b>.\n\n"
        "Рекомендуем изучить инструкцию перед использованием!\n"
        "📖 <a href=\"https://t.me/nano_banana_examples\">Инструкция и примеры</a>\n\n"
        "Чтобы воспользоваться ботом, нажмите «Меню» в левом нижнем углу или кнопку «Создать»👇\n\n"
        "Пользуясь ботом, Вы принимаете наше "
        "<a href=\"https://docs.google.com/document/d/139A-rEgNeA6CrcOaOsOergVVx4bUq8NFlTLx4eD4MfE/edit?usp=drivesdk\">пользовательское соглашение</a> "
        "и <a href=\"https://telegram.org/privacy-tpa\">политику конфиденциальности</a>."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✨ Создать", callback_data="run_gen")]]
    )

    if os.path.exists(img_path):
        await m.answer_photo(
            photo=FSInputFile(img_path),
            caption=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    else:
        await safe_send_text(m.bot, m.chat.id, caption, reply_markup=keyboard)


@router.callback_query(F.data == "run_gen")
async def cb_run_gen(c: CallbackQuery, state: FSMContext):
    await safe_answer(c)
    await ensure_user(c.from_user)
    await cmd_gen(c.message, state, user_id=c.from_user.id)


# ======================= Прочие команды =======================

@router.message(Command("help"))
async def cmd_help(m: Message):
    text = (
        "❓ <b>Помощь</b>\n\n"
        "Вот что я умею:\n\n"
        "🚀 <b>/start</b> — запуск и краткое введение\n"
        "📸 <b>/gen</b> — загрузите фото + запрос → получайте изображение\n"
        "🎨 <b>/model</b> — выбор модели (обычная / Pro 4K)\n"  # ✅ ДОБАВЛЕНО
        "💳 <b>/buy</b> — баланс и пополнение (₽/⭐)\n"
        "🎥 <b>/example</b> — посмотреть примеры работ\n"
        "🤖 <b>/bots</b> — другие наши проекты\n"
        "❓ <b>/help</b> — эта справка\n\n"
        "✉️ Вопросы? Напишите: @guard_gpt"
    )
    await safe_send_text(m.bot, m.chat.id, text)


@router.message(Command("buy"))
async def cmd_buy(m: Message, state: FSMContext):
    try:
        user = await ensure_user(m.from_user)
        await state.clear()
        await safe_send_text(
            m.bot,
            m.chat.id,
            (
                f"Ваш баланс: <b>{user.balance_credits}</b> генераций.\n"
                f"Тариф: 1 генерация — 1 изображение.\n\n"
                "Выберите способ оплаты:"
            ),
            reply_markup=kb_topup_methods(),
        )
    except Exception:
        await safe_send_text(m.bot, m.chat.id, "⚠️ Произошла ошибка.\nНапишите в поддержку: @guard_gpt")


@router.message(Command("example"))
async def cmd_example(m: Message):
    caption = (
        "📌 <b>Примеры работ Nano Banana</b>\n\n"
        "Хотите увидеть, как выглядит результат генерации? "
        "Нажмите кнопку ниже и перейдите в наш канал 👇"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📂 Примеры", url="https://t.me/nano_banana_examples")]
        ]
    )
    await safe_send_text(m.bot, m.chat.id, caption, reply_markup=keyboard)


@router.message(Command("bots"))
async def show_other_bots(m: Message, state: FSMContext):
    await state.clear()
    text = (
        "🔗 <b>Ознакомьтесь с нашими другими полезными ботами:</b>\n\n"
        "🎥 <b>Sora 2 · Создать видео</b> — создавайте супер реалистичные, захватывающие 10 секундные видео с озвучкой в нейросети от создателей ChatGPT.\n"
        "👉 <a href='https://t.me/sora_ai_ibot'>@sora_ai_ibot</a>\n\n"
        "🤖 <b>DeepSeek</b> — лучшая китайская нейросеть. Официальный API. Голосовое общение.\n"
        "👉 <a href='https://t.me/DeepSeek_telegram_bot'>@DeepSeek_telegram_bot</a>\n\n"
        "🍔 <b>КБЖУ по фото</b> — считает калории по фото или голосовому.\n"
        "👉 <a href='https://t.me/calories_by_photo_bot'>@calories_by_photo_bot</a>\n\n"
        "🎥 <b>Google Veo AI</b> — генерация видео с помощью ИИ от Google.\n"
        "👉 <a href='https://t.me/veo_google_ai_bot'>@veo_google_ai_bot</a>\n\n"
        "🖼 <b>Реалистичное оживление фото</b> — оживляет статичные фотографии, превращая их в видео.\n"
        "👉 <a href='https://t.me/Ozhivlenie_foto_bot'>@Ozhivlenie_foto_bot</a>\n\n"
        "✨ <b>Seedream 4.0 · Редактирование и создание фото</b> — китайская нейросеть для редактирования и создания фотографий.\n"
        "👉 <a href='https://t.me/seedream_ibot'>@seedream_ibot</a>"
    )
    await safe_send_text(m.bot, m.chat.id, text, disable_web_page_preview=True)


@router.message(Command("live"))
async def cmd_live(m: Message):
    text = (
        "<b>Рекомендуем эти боты для оживления фото</b>\n\n"
        "🖼 <b>Реалистичное оживление фото</b>\n"
        "Реалистично оживляет фотографии, превращая их в видео.\n"
        "👉 <a href='https://t.me/Ozhivlenie_foto_bot'>@Ozhivlenie_foto_bot</a>\n\n"
        "🎥 <b>Sora 2 · Создать видео</b> — создавайте супер реалистичные, захватывающие 10 секундные видео с озвучкой в нейросети от создателей ChatGPT.\n"
        "👉 <a href='https://t.me/sora_ai_ibot'>@sora_ai_ibot</a>\n\n"
        "🎥 <b>Google Veo 3</b> — генерация видео от Google. Может оживить со звуком. 8 секунд.\n"
        "👉 <a href='https://t.me/veo_google_ai_bot'>@veo_google_ai_bot</a>\n\n"
        
    )
    await safe_send_text(m.bot, m.chat.id, text,disable_web_page_preview=True)


@router.message(Command("model"))
async def cmd_model(m: Message, state: FSMContext):
    """Выбор модели генерации"""
    await state.clear()
    
    async with SessionLocal() as s:
        user = (await s.execute(select(User).where(User.chat_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            await safe_send_text(m.bot, m.chat.id, "Нажмите /start для инициализации.")
            return
        
        current_model = user.model_preference or "standard"
        
        text = (
            "🤖 <b>Выбор модели генерации</b>\n\n"
            f"Текущая модель: <b>{'Pro' if current_model == 'pro' else 'Standard'}</b>\n\n"
            "📊 <b>Standard</b> (Nano Banana)\n"
            "• Стоимость: <b>1 генерация</b>\n"
            "• Качество: хорошее\n"
            "• Скорость: быстрая\n\n"
            "⭐ <b>Pro</b> (Nano Banana Pro)\n"
            "• Стоимость: <b>5 генераций</b>\n"
            "• Разрешение: 4K\n"
            "• Максимальное качество\n"
            "• Промт до 5000 символов\n\n"
            "• Лучше понимает текст\n\n"
            f"💰 Ваш баланс: <b>{user.balance_credits}</b> генераций"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Standard (1 ген)" if current_model == "standard" else "Standard (1 ген)",
                callback_data="model_standard"
            )],
            [InlineKeyboardButton(
                text="✅ Pro (5 ген)" if current_model == "pro" else "⭐ Pro (5 ген)",
                callback_data="model_pro"
            )],
        ])
        
        await safe_send_text(m.bot, m.chat.id, text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("model_"))
async def cb_model_select(c: CallbackQuery, state: FSMContext):
    """Обработка выбора модели"""
    await safe_answer(c)
    
    model = c.data.replace("model_", "")
    
    if model not in ("standard", "pro"):
        return
    
    async with SessionLocal() as s:
        user = (await s.execute(select(User).where(User.chat_id == c.from_user.id))).scalar_one_or_none()
        if not user:
            return
        
        user.model_preference = model
        await s.commit()
        
        model_name = "Pro" if model == "pro" else "Standard"
        cost = 5 if model == "pro" else 1
        
        text = (
            f"✅ <b>Модель изменена на {model_name}</b>\n\n"
            f"Стоимость генерации: <b>{cost} {'генерация' if cost == 1 else 'генераций'}</b>\n"
            f"💰 Ваш баланс: <b>{user.balance_credits}</b> генераций"
        )
        
        await safe_edit_text(c.message, text)
