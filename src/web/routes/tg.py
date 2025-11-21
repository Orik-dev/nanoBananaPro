from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import JSONResponse
from aiogram.types import Update

router = APIRouter()

@router.post("/tg/webhook")
async def tg_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != request.app.state.webhook_secret:
        raise HTTPException(403, "forbidden")
    update = Update.model_validate(await request.json(), context={"bot": request.app.state.bot})
    await request.app.state.dp.feed_update(request.app.state.bot, update)
    return JSONResponse({"ok": True})
