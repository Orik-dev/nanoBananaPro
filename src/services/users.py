from sqlalchemy import select
from db.engine import SessionLocal
from db.models import User

async def ensure_user(telegram_user) -> User:
    async with SessionLocal() as s:
        u = (await s.execute(select(User).where(User.chat_id == telegram_user.id))).scalar_one_or_none()
        if u:
            return u
        u = User(
            chat_id=telegram_user.id,
            username=telegram_user.username or "",
            balance_credits=5,
            receipt_opt_out=False,
            email=None,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u
