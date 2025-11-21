# services/payments.py
from __future__ import annotations
from uuid import uuid4
import logging
from decimal import Decimal
from typing import Optional
from requests.exceptions import HTTPError
from aiogram import Bot
from yookassa import Configuration, Payment
from yookassa.domain.exceptions.api_error import ApiError
from sqlalchemy import select

from core.config import settings
from db.engine import SessionLocal
from db.models import Payment as PayModel, User
from services.pricing import credits_for_rub

log = logging.getLogger("payments")

Configuration.account_id = settings.YOOKASSA_SHOP_ID
Configuration.secret_key = settings.YOOKASSA_SECRET_KEY

# ✅ Технический email для чеков когда пользователь выбирает "Чек не нужен"
TECH_EMAIL = "grok.smirnov@gmail.com"

YOOKASSA_TAX_SYSTEM_CODE = getattr(settings, "YOOKASSA_TAX_SYSTEM_CODE", 2) 
YOOKASSA_VAT_CODE = getattr(settings, "YOOKASSA_VAT_CODE", 1)               

def _assert_yookassa_creds():
    if not (settings.YOOKASSA_SHOP_ID and settings.YOOKASSA_SECRET_KEY):
        raise RuntimeError("YooKassa credentials missing")

def _build_receipt(*, email: str, plan: str, amount_rub: int | float | Decimal) -> dict:
    """Создает чек для YooKassa"""
    amount_rub = float(amount_rub)
    
    return {
        "customer": {"email": email.strip()},
        "items": [
            {
                "description": f"Тариф {plan}"[:128],
                "quantity": "1.00",
                "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
                "vat_code": int(YOOKASSA_VAT_CODE),     
                "payment_subject": "service",           
                "payment_mode": "full_prepayment",      
                "measure": "piece",
            }
        ],
        "tax_system_code": int(YOOKASSA_TAX_SYSTEM_CODE),  
    }

async def create_topup_payment(chat_id: int, rub_amount: int) -> str:
    """
    Создаёт платёж в YooKassa и сохраняет его в БД.
    
    ✅ Чек отправляется ВСЕГДА (обязательно для ИП по 54-ФЗ):
    - Если пользователь хочет чек → на его email
    - Если НЕ хочет → на grok.smirnov@gmail.com
    """
    _assert_yookassa_creds()

    credits = credits_for_rub(rub_amount)
    if credits <= 0:
        raise ValueError(f"Unsupported RUB pack: {rub_amount}")

    async with SessionLocal() as s:
        user: User = (await s.execute(select(User).where(User.chat_id == chat_id))).scalar_one()
        pay = PayModel(user_id=user.id, rub_amount=rub_amount, amount=credits, status="pending")
        s.add(pay)
        await s.commit()
        await s.refresh(pay)

    description = f"Topup chat:{chat_id} payment:{pay.id}"
    if len(description) > 128:
        description = description[:128]

    # ✅ Определяем email для чека
    if user.email and not user.receipt_opt_out:
        # ✅ УЛУЧШЕНА ВАЛИДАЦИЯ EMAIL
        email = user.email.strip()
        
        # Проверка на базовые ошибки
        if "@" not in email or email.count("@") != 1:
            log.warning(f"Invalid email format (no @): {email}")
            receipt_email = TECH_EMAIL
        elif "." not in email.split("@")[1]:  # домен должен содержать точку
            log.warning(f"Invalid email format (domain): {email}")
            receipt_email = TECH_EMAIL
        elif email.endswith(",com") or email.endswith(",ru"):  # запятая вместо точки
            log.warning(f"Invalid email format (comma): {email}")
            receipt_email = TECH_EMAIL
        else:
            receipt_email = email
            log.info(f"Payment with user receipt: user={chat_id}, email={receipt_email}")
    else:
        receipt_email = TECH_EMAIL
        log.info(f"Payment with tech receipt: user={chat_id}, email={TECH_EMAIL}")

    plan = f"{rub_amount} ₽ → {credits} генераций"
    receipt = _build_receipt(email=receipt_email, plan=plan, amount_rub=rub_amount)

    body = {
        "amount": {"value": f"{rub_amount:.2f}", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": settings.TOPUP_RETURN_URL},
        "capture": True,
        "description": description,
        "receipt": receipt,  # ✅ Чек ВСЕГДА присутствует
    }

    idem_key = str(uuid4())
    try:
        p = Payment.create(body, idem_key)
    except ApiError as e:
        log.error(
            "YooKassa ApiError: type=%s code=%s param=%s desc=%s details=%s body=%s",
            getattr(e, "type", None),
            getattr(e, "code", None),
            getattr(e, "parameter", None),
            getattr(e, "description", str(e)),
            getattr(e, "details", None),
            body,
        )
        raise
    except HTTPError as e:
        resp = getattr(e, "response", None)
        log.error(
            "YooKassa HTTPError: status=%s reason=%s body=%s request_body=%s",
            getattr(resp, "status_code", None),
            getattr(resp, "reason", None),
            getattr(resp, "text", None),
            body,
        )
        raise
    except Exception:
        log.exception("YooKassa create() failed (unknown)")
        raise

    async with SessionLocal() as s:
        dbp = await s.get(PayModel, pay.id)
        dbp.ext_payment_id = p.id
        dbp.confirmation_url = p.confirmation.confirmation_url
        await s.commit()

    return p.confirmation.confirmation_url


async def handle_yookassa_webhook(payload: dict):
    if payload.get("event") != "payment.succeeded":
        return

    ext_id = payload["object"]["id"]

    async with SessionLocal() as s:
        pay = (await s.execute(
            select(PayModel).where(PayModel.ext_payment_id == ext_id)
        )).scalar_one_or_none()

        if not pay or pay.status == "succeeded":
            return

        pay.status = "succeeded"
        user = await s.get(User, pay.user_id)
        user.balance_credits += int(pay.amount)
        await s.commit()

    text = (
        f"Платёж на {pay.rub_amount:.2f}₽ прошёл успешно!✅ \n"
        f"Баланс пополнен на {pay.amount} генераций.\n\n"
        "Теперь попробуйте:\n"
        "1️⃣ /gen — начать генерацию\n"
        "2️⃣ Пришлите 1–4 фото одним сообщением\n"
        "3️⃣ Опишите желаемый результат\n\n"
        "✅ Бот пришлёт готовое фото!"
    )

    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(user.chat_id, text)
    finally:
        await bot.session.close()