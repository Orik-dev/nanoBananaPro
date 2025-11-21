import logging
import redis.asyncio as aioredis

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from services.pricing import credits_for_rub
from services.payments import create_topup_payment
from services.users import ensure_user
from db.engine import SessionLocal
from db.models import User
from bot.states import TopupStates
from bot.keyboards import kb_topup_packs, kb_topup_methods, kb_receipt_choice, kb_topup_stars
from services.telegram_safe import safe_answer, safe_edit_text, safe_send_text, safe_delete_message
from core.config import settings

router = Router()
log = logging.getLogger("payments")

# ====== ✅ FIX: новый helper для навигации (удаляет старое сообщение) ======
async def _send_with_delete(bot, chat_id: int, message_id: int, text: str, reply_markup):
    """Удаляет старое сообщение и отправляет новое - для исправления навигации"""
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass
    return await safe_send_text(bot, chat_id, text, reply_markup=reply_markup)

# ====== возврат к выбору способа оплаты ======
@router.callback_query(F.data.in_({"back_methods", "back_to_methods"}))
async def back_to_methods(c: CallbackQuery, state: FSMContext):
    log.info(f"🔙 Back to methods: user={c.from_user.id}")  # ✅ ДОБАВЛЕНО
    await safe_answer(c)
    await state.clear()
    user = await ensure_user(c.from_user)
    text = (f"Ваш баланс: <b>{user.balance_credits}</b> генераций.\n"
            f"Тариф: 1 генерация — 1 изображение.\n\n"
            "Выберите способ оплаты:")
    await _send_with_delete(c.bot, c.message.chat.id, c.message.message_id, text, kb_topup_methods())

# ====== RUB (ЮKassa) ======
@router.callback_query(F.data == "m_rub")
async def method_rub(c: CallbackQuery, state: FSMContext):
    log.info(f"💳 Method RUB selected: user={c.from_user.id}")  # ✅ ДОБАВЛЕНО
    await safe_answer(c)
    await state.clear()
    await state.set_state(TopupStates.choosing_amount)
    await _send_with_delete(c.bot, c.message.chat.id, c.message.message_id, 
                           "Выберите сумму для пополнения:", kb_topup_packs())

@router.callback_query(TopupStates.choosing_amount, F.data.startswith("pack_"))
async def choose_pack(c: CallbackQuery, state: FSMContext):
    log.info(f"📦 Pack callback: user={c.from_user.id}, data={c.data}")  # ✅ ДОБАВЛЕНО
    
    await safe_answer(c)
    token = c.data.split("_", 1)[1]
    
    log.info(f"📦 Pack token: {token}")  # ✅ ДОБАВЛЕНО
    
    try:
        rub = int(token)
    except ValueError:
        log.warning(f"⚠️ Invalid pack token: {token}, user={c.from_user.id}")  # ✅ ДОБАВЛЕНО
        await _send_with_delete(c.bot, c.message.chat.id, c.message.message_id,
                               "Выберите один из доступных пакетов.", kb_topup_packs())
        return

    cr = credits_for_rub(rub)
    if cr <= 0:
        log.warning(f"⚠️ Invalid rub amount: {rub}, user={c.from_user.id}")  # ✅ ДОБАВЛЕНО
        await _send_with_delete(c.bot, c.message.chat.id, c.message.message_id,
                               "Выберите один из доступных пакетов.", kb_topup_packs())
        return

    log.info(f"✅ Pack validated: user={c.from_user.id}, rub={rub}, credits={cr}")  # ✅ ДОБАВЛЕНО
    await state.update_data(rub=rub, credits=cr)

    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.chat_id == c.from_user.id))).scalar_one()
        already_has_pref = bool(u.email) or bool(u.receipt_opt_out)
        log.info(f"📧 User prefs: user={c.from_user.id}, email={bool(u.email)}, opt_out={u.receipt_opt_out}")  # ✅ ДОБАВЛЕНО

    if already_has_pref:
        try:
            log.info(f"💳 Creating payment: user={c.from_user.id}, rub={rub}")  # ✅ ДОБАВЛЕНО
            url = await create_topup_payment(c.from_user.id, rub)
            log.info(f"✅ Payment created: user={c.from_user.id}, url_prefix={url[:50]}...")  # ✅ ДОБАВЛЕНО
        except Exception as e:
            log.error(f"❌ Payment creation failed: user={c.from_user.id}, error={e}")  # ✅ ДОБАВЛЕНО
            await _send_with_delete(c.bot, c.message.chat.id, c.message.message_id,
                                   "⚠️ Не удалось создать счёт. Попробуйте позже или выберите другой способ оплаты.", 
                                   kb_topup_methods())
            await state.clear()
            return

        try:
            await c.message.delete()
        except Exception:
            pass
        await safe_send_text(c.bot, c.message.chat.id, f"Оплатите по ссылке:\n{url}")
        await state.clear()
        return

    await state.set_state(TopupStates.choosing_method)
    log.info(f"📝 Asking for receipt: user={c.from_user.id}")  # ✅ ДОБАВЛЕНО
    await _send_with_delete(c.bot, c.message.chat.id, c.message.message_id,
                           f"Сумма: <b>{rub} ₽</b> → {cr} генераций.\nНужен ли чек на e-mail?", 
                           kb_receipt_choice())

# ✅ FIX: перехват команд в состоянии choosing_amount
@router.message(TopupStates.choosing_amount, F.text.startswith("/"))
async def handle_commands_in_choosing(m: Message, state: FSMContext):
    """Обработка команд, когда пользователь выбирает пакет"""
    log.info(f"⚠️ Command in choosing_amount: user={m.from_user.id}, text={m.text}")  # ✅ ДОБАВЛЕНО
    await state.clear()
    cmd = (m.text or "").split()[0].lower()
    
    if cmd == "/start":
        from bot.routers.commands import cmd_start
        await cmd_start(m)
    elif cmd == "/gen":
        from bot.routers.generation import cmd_gen
        await cmd_gen(m, state, show_intro=True)
    elif cmd == "/create":
        from bot.routers.commands import cmd_create
        await cmd_create(m, state)
    elif cmd == "/buy":
        from bot.routers.commands import cmd_buy
        await cmd_buy(m, state)
    elif cmd == "/help":
        from bot.routers.commands import cmd_help
        await cmd_help(m)
    elif cmd == "/example":
        from bot.routers.commands import cmd_example
        await cmd_example(m)
    elif cmd == "/bots":
        from bot.routers.commands import show_other_bots
        await show_other_bots(m, state)
    # остальные команды обработаются глобально

@router.message(TopupStates.choosing_amount, lambda m: not m.text or not m.text.startswith("/"))
async def input_amount(m: Message, state: FSMContext):
    log.warning(f"⚠️ Text input in choosing_amount: user={m.from_user.id}")  # ✅ ДОБАВЛЕНО
    await safe_send_text(m.bot, m.chat.id, "Пожалуйста, выберите один из пакетов.", reply_markup=kb_topup_packs())

@router.callback_query(TopupStates.choosing_method, F.data == "receipt_skip")
async def receipt_skip(c: CallbackQuery, state: FSMContext):
    log.info(f"📧 Receipt skipped: user={c.from_user.id}")  # ✅ ДОБАВЛЕНО
    await safe_answer(c)
    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.chat_id == c.from_user.id))).scalar_one()
        u.receipt_opt_out = True
        await s.commit()

    rub = (await state.get_data())["rub"]
    url = await create_topup_payment(c.from_user.id, rub)
    
    try:
        await c.message.delete()
    except Exception:
        pass
    await safe_send_text(c.bot, c.message.chat.id, f"Оплатите по ссылке:\n{url}")
    await state.clear()

@router.callback_query(TopupStates.choosing_method, F.data == "receipt_need")
async def receipt_need(c: CallbackQuery, state: FSMContext):
    log.info(f"📧 Receipt requested: user={c.from_user.id}")  # ✅ ДОБАВЛЕНО
    await safe_answer(c)
    await state.set_state(TopupStates.waiting_email)
    await _send_with_delete(c.bot, c.message.chat.id, c.message.message_id,
                           "Введите e-mail для чека (один раз).", None)

# ✅ FIX: перехват команд в состоянии waiting_email
@router.message(TopupStates.waiting_email, F.text.startswith("/"))
async def handle_commands_in_email(m: Message, state: FSMContext):
    """Обработка команд, когда пользователь должен ввести email"""
    log.info(f"⚠️ Command in waiting_email: user={m.from_user.id}, text={m.text}")  # ✅ ДОБАВЛЕНО
    await state.clear()
    cmd = (m.text or "").split()[0].lower()
    
    if cmd == "/start":
        from bot.routers.commands import cmd_start
        await cmd_start(m)
    elif cmd == "/gen":
        from bot.routers.generation import cmd_gen
        await cmd_gen(m, state, show_intro=True)
    elif cmd == "/create":
        from bot.routers.commands import cmd_create
        await cmd_create(m, state)
    elif cmd == "/buy":
        from bot.routers.commands import cmd_buy
        await cmd_buy(m, state)
    elif cmd == "/help":
        from bot.routers.commands import cmd_help
        await cmd_help(m)
    elif cmd == "/example":
        from bot.routers.commands import cmd_example
        await cmd_example(m)
    elif cmd == "/bots":
        from bot.routers.commands import show_other_bots
        await show_other_bots(m, state)

@router.message(TopupStates.waiting_email, lambda m: not m.text or not m.text.startswith("/"))
async def waiting_email(m: Message, state: FSMContext):
    email = (m.text or "").strip()
    log.info(f"📧 Email input: user={m.from_user.id}, email={email[:20]}...")

    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.chat_id == m.from_user.id))).scalar_one()
        if email.lower() in {"не нужен", "ненужен", "skip"}:
            u.receipt_opt_out = True
            log.info(f"📧 Email skipped via text: user={m.from_user.id}")
        else:
            # ✅ УЛУЧШЕННАЯ ВАЛИДАЦИЯ EMAIL
            email_lower = email.lower()
            
            # Проверка 1: базовая структура
            if "@" not in email or len(email) < 5:
                log.warning(f"⚠️ Invalid email (no @): user={m.from_user.id}")
                await safe_send_text(m.bot, m.chat.id, "❌ Некорректный e-mail. Введите снова или напишите «не нужен».")
                return
            
            # Проверка 2: разделение на части
            parts = email.split("@")
            if len(parts) != 2:
                log.warning(f"⚠️ Invalid email (multiple @): user={m.from_user.id}")
                await safe_send_text(m.bot, m.chat.id, "❌ Некорректный e-mail. Введите снова или напишите «не нужен».")
                return
            
            local_part, domain_part = parts
            
            # Проверка 3: локальная часть не пустая
            if not local_part or len(local_part) < 1:
                log.warning(f"⚠️ Invalid email (empty local): user={m.from_user.id}")
                await safe_send_text(m.bot, m.chat.id, "❌ Некорректный e-mail. Введите снова или напишите «не нужен».")
                return
            
            # Проверка 4: домен должен содержать точку
            if "." not in domain_part:
                log.warning(f"⚠️ Invalid email (no dot in domain): user={m.from_user.id}")
                await safe_send_text(m.bot, m.chat.id, "❌ Некорректный e-mail. Домен должен содержать точку (например, gmail.com). Введите снова или напишите «не нужен».")
                return
            
            # Проверка 5: домен не должен начинаться или заканчиваться точкой
            if domain_part.startswith(".") or domain_part.endswith("."):
                log.warning(f"⚠️ Invalid email (domain starts/ends with dot): user={m.from_user.id}")
                await safe_send_text(m.bot, m.chat.id, "❌ Некорректный e-mail. Введите снова или напишите «не нужен».")
                return
            
            # Проверка 6: части домена не пустые
            domain_parts = domain_part.split(".")
            if any(len(part) < 1 for part in domain_parts):
                log.warning(f"⚠️ Invalid email (empty domain part): user={m.from_user.id}")
                await safe_send_text(m.bot, m.chat.id, "❌ Некорректный e-mail. Введите снова или напишите «не нужен».")
                return
            
            # Проверка 7: последняя часть домена (TLD) должна быть >= 2 символов
            if len(domain_parts[-1]) < 2:
                log.warning(f"⚠️ Invalid email (TLD too short): user={m.from_user.id}")
                await safe_send_text(m.bot, m.chat.id, "❌ Некорректный e-mail. Введите снова или напишите «не нужен».")
                return
            
            u.email = email
            log.info(f"✅ Email saved: user={m.from_user.id}")
        await s.commit()

    rub = (await state.get_data())["rub"]
    url = await create_topup_payment(m.from_user.id, rub)
    await safe_send_text(m.bot, m.chat.id, f"Оплатите по ссылке:\n{url}\nЕсли потеряете — используйте /buy.")
    await state.clear()

# ====== Stars (XTR) ======
@router.callback_query(F.data == "m_stars")
async def method_stars(c: CallbackQuery, state: FSMContext):
    log.info(f"⭐ Method Stars selected: user={c.from_user.id}")  # ✅ ДОБАВЛЕНО
    await safe_answer(c)
    await state.clear()
    await _send_with_delete(c.bot, c.message.chat.id, c.message.message_id,
                           "Выберите пакет звёзд ⭐:\n\n", kb_topup_stars())

@router.callback_query(F.data.startswith("stars_"))
async def cb_buy_stars(c: CallbackQuery, state: FSMContext):
    """✅ ИСПРАВЛЕНО: проверка типа сообщения"""
    log.info(f"⭐ Stars pack selected: user={c.from_user.id}, data={c.data}")
    await safe_answer(c)
    await state.clear()
    
    parts = c.data.split("_", 1)
    if len(parts) < 2 or not parts[1].isdigit():
        log.warning(f"⚠️ Invalid stars data: {c.data}")
        return

    from services.pricing import credits_for_rub
    stars = int(parts[1])
    cr = credits_for_rub(stars)
    if cr <= 0:
        log.warning(f"⚠️ Invalid stars amount: {stars}")
        return

    title = f"{stars} ⭐ → {cr} генераций"
    prices = [LabeledPrice(label=title, amount=stars)]

    # ✅ ИСПРАВЛЕНО: безопасное удаление сообщения
    try:
        # Проверяем тип сообщения перед удалением
        if hasattr(c.message, 'delete') and not isinstance(c.message, type(None)):
            await c.message.delete()
    except (TelegramBadRequest, AttributeError) as e:
        log.debug(f"Could not delete message: {e}")
    except Exception as e:
        log.warning(f"Unexpected error deleting message: {e}")

    try:
        await c.bot.send_invoice(
            chat_id=c.from_user.id,
            title=title,
            description="NanoBanana — пополнение звёздами",
            payload=f"stars:{stars}",
            provider_token="",
            currency="XTR",
            prices=prices,
        )
        log.info(f"✅ Stars invoice sent: user={c.from_user.id}, stars={stars}, cr={cr}")
    except TelegramForbiddenError:
        log.warning(f"⚠️ Stars invoice forbidden: user={c.from_user.id}")
    except Exception as e:
        log.exception(f"❌ Stars invoice error: user={c.from_user.id}, error={e}")

@router.pre_checkout_query()
async def stars_pre_checkout(q: PreCheckoutQuery):
    log.info(f"⭐ Pre-checkout: user={q.from_user.id}, payload={q.invoice_payload}")
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def stars_success(m: Message, state: FSMContext):
    """✅ Полная защита от ошибок + идемпотентность + логирование"""
    try:
        await state.clear()
        
        payload = m.successful_payment.invoice_payload or ""
        charge_id = m.successful_payment.telegram_payment_charge_id or ""
        
        log.info(f"⭐ Payment received: user={m.from_user.id}, payload={payload}, charge_id={charge_id}")
        
        if not payload.startswith("stars:"):
            log.warning(f"⚠️ Invalid payload: user={m.from_user.id}, payload={payload}")
            return
        
        try:
            stars = int(payload.split(":", 1)[1])
        except (ValueError, IndexError) as e:
            log.error(f"❌ Parse error: user={m.from_user.id}, payload={payload}, error={e}")
            return
        
        # Идемпотентность через Redis
        import redis.asyncio as aioredis
        from core.config import settings
        
        idempotency_key = f"stars:paid:{charge_id}"
        r = aioredis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB_CACHE)
        
        try:
            already_processed = await r.exists(idempotency_key)
            if already_processed:
                log.warning(f"⚠️ Duplicate payment: user={m.from_user.id}, charge_id={charge_id}")
                await safe_send_text(m.bot, m.chat.id, "✅ Баланс уже был пополнен ранее.")
                return
            
            await r.setex(idempotency_key, 604800, "1")
        except Exception as e:
            log.error(f"❌ Redis error: user={m.from_user.id}, error={e}")
        finally:
            try:
                await r.aclose()
            except Exception:
                pass
        
        async with SessionLocal() as s:
            try:
                user = await ensure_user(m.from_user)
                
                cr = credits_for_rub(stars)
                if cr <= 0:
                    log.error(f"❌ Invalid stars amount: user={m.from_user.id}, stars={stars}")
                    await safe_send_text(m.bot, m.chat.id, "❌ Ошибка: некорректная сумма звёзд.")
                    return
                
                result = await s.execute(
                    select(User).where(User.chat_id == m.from_user.id)
                )
                u = result.scalar_one_or_none()
                
                if not u:
                    log.error(f"❌ User not found: user={m.from_user.id}")
                    await safe_send_text(m.bot, m.chat.id, "❌ Ошибка: пользователь не найден. Напишите /start")
                    return
                
                old_balance = u.balance_credits
                u.balance_credits += cr
                await s.commit()
                
                log.info(f"✅ Balance updated: user={m.from_user.id}, stars={stars}, credits={cr}, old={old_balance}, new={u.balance_credits}")
                
                await safe_send_text(
                    m.bot,
                    m.chat.id,
                    f"✅ Оплата звёздами прошла!\n\n"
                    f"💰 Баланс пополнен на <b>{cr}</b> генераций.\n"
                    f"📊 Текущий баланс: <b>{u.balance_credits}</b> генераций.\n\n"
                    f"Начать генерацию: /gen или /create"
                )
                
            except Exception as e:
                log.exception(f"❌ DB error: user={m.from_user.id}, error={e}")
                await safe_send_text(
                    m.bot,
                    m.chat.id,
                    "⚠️ Платёж получен, но возникла ошибка при зачислении.\n"
                    "Напишите @guard_gpt с скриншотом оплаты - мы вручную пополним баланс."
                )
                
    except Exception as e:
        log.exception(f"❌ Critical error: user={m.from_user.id}, error={e}")
        try:
            await safe_send_text(
                m.bot,
                m.chat.id,
                "⚠️ Произошла ошибка при обработке платежа.\n"
                "Напишите @guard_gpt с скриншотом - разберёмся!"
            )
        except Exception:
            pass