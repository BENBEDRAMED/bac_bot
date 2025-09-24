import gc
import time
import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
import uvicorn

from settings import PORT, MAX_CONCURRENT, PROCESSING_SEMAPHORE_TIMEOUT, WEBHOOK_SECRET_TOKEN, WEBHOOK_URL, BOT_TOKEN, DATABASE_URL
from database import init_pg_pool, init_db_schema_and_defaults, check_db_health
from telegram_client import init_bot, get_bot, get_bot_id
from handlers import process_text_message, handle_callback_query
from telegram import Bot

logger = logging.getLogger(__name__)

app = FastAPI(docs_url=None, redoc_url=None)

PROCESSED_UPDATES = deque(maxlen=200)
PROCESSING_SEMAPHORE = asyncio.BoundedSemaphore(MAX_CONCURRENT)
ACTIVE_REQUESTS = 0
REQUEST_HISTORY = deque(maxlen=20)

# Heartbeat imports
import threading
import sys

def cleanup_memory():
    gc.collect()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Missing required environment variables")
        yield
        return

# Heartbeat logger
def heartbeat():
    while True:
        logger.info("[HEARTBEAT] Bot process alive")
        time.sleep(30)

# Start heartbeat in a background thread
heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
heartbeat_thread.start()

# Global exception handler for uncaught exceptions
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


    try:
        await init_pg_pool()
        await init_db_schema_and_defaults()
        await init_bot()  # This sets the global bot instance in telegram_client
        
        bot_instance = get_bot()
        if WEBHOOK_URL and bot_instance:
            webhook_url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            if WEBHOOK_SECRET_TOKEN:
                await bot_instance.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET_TOKEN)
            else:
                await bot_instance.set_webhook(webhook_url)
            logger.info("Webhook set: %s", webhook_url)
            
    except Exception as e:
        logger.error("Startup failed: %s", e)
        yield
        return

    yield
    
    logger.info("Shutting down...")
    from database import pg_pool
    if pg_pool:
        await pg_pool.close()
    bot_instance = get_bot()
    if bot_instance:
        await bot_instance.close()

app.router.lifespan_context = lifespan

@app.get("/")
async def root():
    return HTMLResponse("""
    <html>
        <head>
            <title>Telegram Bot</title>
        </head>
        <body>
            <h1>🤖 Telegram Bot is Running</h1>
            <p>Bot is active and ready to receive webhook calls.</p>
            <p><a href="/health">Check Health</a></p>
            <p><a href="/ping">Ping</a></p>
        </body>
    </html>
    """)

@app.get("/health")
async def health_check():
    global ACTIVE_REQUESTS
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

@app.get("/ping")
async def ping():
    return PlainTextResponse("pong")

@app.post("/webhook")
async def webhook(request: Request):
    global ACTIVE_REQUESTS
    acquired = False
    start_time = time.time()
    update_id = None
    
    if WEBHOOK_SECRET_TOKEN:
        token = request.headers.get("x-telegram-bot-api-secret-token")
        if token != WEBHOOK_SECRET_TOKEN:
            raise HTTPException(403, "Invalid token")

    ACTIVE_REQUESTS += 1
    REQUEST_HISTORY.append((time.time(), "start"))
    
    try:
        logger.debug(f"Waiting for semaphore. Available: {PROCESSING_SEMAPHORE._value}")
        await asyncio.wait_for(PROCESSING_SEMAPHORE.acquire(), timeout=PROCESSING_SEMAPHORE_TIMEOUT)
        acquired = True
        ACTIVE_REQUESTS -= 1
        logger.debug(f"Semaphore acquired. Available: {PROCESSING_SEMAPHORE._value}")
        REQUEST_HISTORY.append((time.time(), "acquired"))
    except asyncio.TimeoutError:
        ACTIVE_REQUESTS -= 1
        logger.warning("Server busy, rejecting request - semaphore timeout")
        REQUEST_HISTORY.append((time.time(), "timeout"))
        return JSONResponse({"ok": False, "error": "busy"}, status_code=429)
    except Exception as e:
        ACTIVE_REQUESTS -= 1
        logger.error(f"Unexpected error acquiring semaphore: {e}")
        REQUEST_HISTORY.append((time.time(), "error"))
        return JSONResponse({"ok": False, "error": "internal"}, status_code=500)

    try:
        update = await request.json()
        update_id = update.get("update_id")
        
        logger.info(f"Processing update {update_id}")
        
        if update_id and update_id in PROCESSED_UPDATES:
            logger.debug(f"Duplicate update {update_id}, skipping")
            return JSONResponse({"ok": True})
        PROCESSED_UPDATES.append(update_id)

        if len(PROCESSED_UPDATES) % 50 == 0:
            cleanup_memory()

        if "callback_query" in update:
            await handle_callback_query(update["callback_query"])

        elif "message" in update:
            await process_text_message(update["message"])
        elif "edited_message" in update:
            await process_text_message(update["edited_message"])

        processing_time = time.time() - start_time
        logger.info(f"Successfully processed update {update_id} in {processing_time:.2f}s")
        REQUEST_HISTORY.append((time.time(), "success"))
        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"Webhook error for update {update_id}: {e}")
        REQUEST_HISTORY.append((time.time(), f"error: {str(e)}"))
        return JSONResponse({"ok": False, "error": "internal"}, status_code=500)
        
    finally:
        if acquired:
            try:
                PROCESSING_SEMAPHORE.release()
                logger.debug(f"Semaphore released. Available: {PROCESSING_SEMAPHORE._value}")
                REQUEST_HISTORY.append((time.time(), "released"))
            except ValueError as e:
                logger.error(f"Error releasing semaphore: {e}")
                REQUEST_HISTORY.append((time.time(), "release_error"))