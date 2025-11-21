from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from services.payments import handle_yookassa_webhook

router = APIRouter()


@router.post("/webhook/yookassa")
async def yk_callback_nginx(req: Request):
    data = await req.json()
    await handle_yookassa_webhook(data)
    return JSONResponse({"ok": True})