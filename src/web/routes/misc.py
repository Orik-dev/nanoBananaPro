from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

@router.get("/pay/return")
async def pay_return():
    return HTMLResponse("<h3>Спасибо! Если оплата прошла, баланс пополнится в течение минуты.</h3>")
