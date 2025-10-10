import gc
import time
import asyncio
import threading
import sys
import requests  # <-- ADD THIS IMPORT
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from contextlib import asynccontextmanager

from database import init_pg_pool, init_db_schema_and_defaults, check_db_health, pg_pool
from telegram_client import init_bot, get_bot
from settings import BOT_TOKEN, DATABASE_URL, WEBHOOK_URL, WEBHOOK_SECRET_TOKEN, MAX_CONCURRENT
from handlers import process_text_message
import logging

logger = logging.getLogger(__name__)

app = FastAPI()

PROCESSED_UPDATES = []
REQUEST_HISTORY = []
ACTIVE_REQUESTS = 0
PROCESSING_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

# Add your Render app URL (replace with your actual URL)
RENDER_APP_URL = "https://your-app-name.onrender.com"  # <-- ADD THIS

# ---- Wakeup route ----
@app.get("/wakeup")  # <-- ADD THIS ROUTE
async def wakeup():
    return {"status": "awake", "timestamp": time.time()}

def keep_alive():  # <-- ADD THIS FUNCTION
    """Keep the app awake by pinging itself periodically"""
    def run_ping():
        while True:
            try:
                logger.info("Pinging to keep awake...")
                response = requests.get(f"{RENDER_APP_URL.rstrip('/')}/wakeup", timeout=10)
                logger.info(f"Wakeup ping successful: {response.status_code}")
            except Exception as e:
                logger.error(f"Wakeup ping failed: {e}")
            time.sleep(300)  # Ping every 5 minutes
    
    thread = threading.Thread(target=run_ping, daemon=True)
    thread.start()
    logger.info("Keep-alive thread started!")

# ---- Webhook route ----
@app.post("/webhook")
async def webhook(request: Request):
    update_id = None
    await PROCESSING_SEMAPHORE.acquire()
    try:
        update = await request.json()
        update_id = update.get("update_id")
        logger.info(f"Processing update {update_id}")

        if update_id and update_id in PROCESSED_UPDATES:
            logger.debug(f"Duplicate update {update_id}, skipping")
            return {"ok": True}
        PROCESSED_UPDATES.append(update_id)

        if "message" in update:
            try:
                await process_text_message(update["message"])
            except Exception as e:
                logger.error(f"process_text_message failed: {e}")

        elif "edited_message" in update:
            try:
                await process_text_message(update["edited_message"])
            except Exception as e:
                logger.error(f"process_text_message failed: {e}")

        return {"ok": True}

    except Exception as e:
        logger.error(f"Webhook handler error: {e}")
        return {"ok": True}

    finally:
        PROCESSING_SEMAPHORE.release()


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
        
        # Start keep-alive thread  # <-- ADD THIS
        keep_alive()
        
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
    return HTMLResponse("<h1>🤖 Bot is Running</h1><p><a href='/health'>Check Health</a></p><p><a href='/wakeup'>Wakeup Check</a></p>")  # <-- Updated

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
        "request_history": list(REQUEST_HISTORY),
        "wakeup_endpoint": f"{RENDER_APP_URL}/wakeup"  # <-- Added wakeup info
    }, status_code=status_code)