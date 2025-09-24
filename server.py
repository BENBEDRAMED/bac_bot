import gc
import time
import asyncio
import threading
import sys
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from contextlib import asynccontextmanager

from database import init_pg_pool, init_db_schema_and_defaults, check_db_health, pg_pool
from telegram_client import init_bot, get_bot
from settings import BOT_TOKEN, DATABASE_URL, WEBHOOK_URL, WEBHOOK_SECRET_TOKEN, MAX_CONCURRENT
from handlers import handle_callback_query, process_text_message
import logging
logger = logging.getLogger(__name__)

app = FastAPI()

PROCESSED_UPDATES = []
REQUEST_HISTORY = []
ACTIVE_REQUESTS = 0
PROCESSING_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)


# ---- Webhook route ----
@app.post("/webhook")
async def webhook(request: Request):
    start_time = time.time()
    acquired = await PROCESSING_SEMAPHORE.acquire()
    update_id = None
    try:
        update = await request.json()
        update_id = update.get("update_id")

        logger.info(f"Processing update {update_id}")

        if update_id and update_id in PROCESSED_UPDATES:
            logger.debug(f"Duplicate update {update_id}, skipping")
            return JSONResponse({"ok": True})
        PROCESSED_UPDATES.append(update_id)

        if len(PROCESSED_UPDATES) % 50 == 0:
            gc.collect()

        if "callback_query" in update:
            await handle_callback_query(update["callback_query"])
        elif "message" in update:
            await process_text_message(update["message"])
        elif "edited_message" in update:
            await process_text_message(update["edited_message"])

        processing_time = time.time() - start_time
        logger.info(f"Processed update {update_id} in {processing_time:.2f}s")
        REQUEST_HISTORY.append((time.time(), "success"))
        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"Webhook error for update {update_id}: {e}")
        REQUEST_HISTORY.append((time.time(), f"error: {str(e)}"))
        return JSONResponse({"ok": False, "error": "internal"}, status_code=500)

    finally:
        if acquired:
            PROCESSING_SEMAPHORE.release()
            logger.debug(f"Semaphore released. Available: {PROCESSING_SEMAPHORE._value}")


# ---- Lifespan ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    try:
        await init_pg_pool()
        await init_db_schema_and_defaults()
        await init_bot()

        bot_instance = get_bot()
        if WEBHOOK_URL and bot_instance:
            webhook_url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            if WEBHOOK_SECRET_TOKEN:
                await bot_instance.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET_TOKEN)
            else:
                await bot_instance.set_webhook(webhook_url)
            logger.info(f"Webhook set: {webhook_url}")
    except Exception as e:
        logger.error(f"Startup failed: {e}")

    yield

    logger.info("Shutting down...")
    if pg_pool:
        await pg_pool.close()
    bot_instance = get_bot()
    if bot_instance:
        await bot_instance.close()


app.router.lifespan_context = lifespan


# ---- Health and test routes ----
@app.get("/")
async def root():
    return HTMLResponse("<h1>🤖 Bot is Running</h1><p><a href='/health'>Check Health</a></p>")

@app.get("/health")
async def health_check():
    db_healthy = await check_db_health()
    bot_healthy = get_bot() is not None
    status_code = 200 if db_healthy and bot_healthy else 503
    return JSONResponse({
        "status": "healthy" if status_code == 200 else "unhealthy",
        "database": "connected" if db_healthy else "disconnected",
        "bot": "connected" if bot_healthy else "disconnected",
        "active_requests": ACTIVE_REQUESTS,
        "max_concurrent": MAX_CONCURRENT,
        "semaphore_value": PROCESSING_SEMAPHORE._value,
        "processed_updates": len(PROCESSED_UPDATES),
        "request_history": list(REQUEST_HISTORY)
    }, status_code=status_code)
