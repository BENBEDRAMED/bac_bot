import logging
import time
import asyncio
import gc
from collections import deque
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
import uvicorn

from settings import LOG_LEVEL, PORT, MAX_CONCURRENT, PROCESSING_SEMAPHORE_TIMEOUT, WEBHOOK_SECRET_TOKEN
from database import init_pg_pool, init_db_schema_and_defaults, check_db_health
from telegram_client import init_bot, close_bot, bot, BOT_ID, safe_telegram_call
from handlers import process_text_message, check_user_membership
from ui import build_main_menu, missing_chats_markup

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)

# Globals
PROCESSED_UPDATES = deque(maxlen=200)
admin_state = {}
PROCESSING_SEMAPHORE = asyncio.BoundedSemaphore(MAX_CONCURRENT)
ACTIVE_REQUESTS = 0
REQUEST_HISTORY = deque(maxlen=20)


def cleanup_memory():
    gc.collect()


async def lifespan(app: FastAPI):
    """Startup/shutdown tasks"""
    logger.info("Starting up...")

    try:
        await init_pg_pool()
        await init_db_schema_and_defaults()
        await init_bot()
    except Exception as e:
        logger.error("Startup failed: %s", e)
        yield
        return

    yield  # app running

    logger.info("Shutting down...")
    try:
        await close_bot()
    except Exception as e:
        logger.error("Error closing bot: %s", e)


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


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
    bot_healthy = bot is not None
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
        await asyncio.wait_for(PROCESSING_SEMAPHORE.acquire(), timeout=PROCESSING_SEMAPHORE_TIMEOUT)
        acquired = True
        ACTIVE_REQUESTS -= 1
        REQUEST_HISTORY.append((time.time(), "acquired"))
    except asyncio.TimeoutError:
        ACTIVE_REQUESTS -= 1
        REQUEST_HISTORY.append((time.time(), "timeout"))
        return JSONResponse({"ok": False, "error": "busy"}, status_code=429)
    except Exception as e:
        ACTIVE_REQUESTS -= 1
        logger.error("Semaphore acquire error: %s", e)
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

        # Handle callback queries
        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data")
            user_id = cq.get("from", {}).get("id")
            message = cq.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")

            logger.debug(f"Callback query: {data} from user {user_id}")

            if bot and cq.get("id"):
                await safe_telegram_call(bot.answer_callback_query(callback_query_id=cq["id"]))

            # membership check
            if data == "check_membership":
                ok, missing, _ = await check_user_membership(user_id)
                if ok:
                    markup = await build_main_menu()
                    if bot and chat_id and message_id:
                        await safe_telegram_call(bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text="تم التحقق — اختر القسم:",
                            reply_markup=markup
                        ))
                else:
                    if bot and chat_id and message_id:
                        await safe_telegram_call(bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text="لا زلت تحتاج للانضمام",
                            reply_markup=missing_chats_markup()
                        ))

            # admin panel
            elif data == "admin_panel" and user_id in __import__('settings').ADMIN_IDS:
                if bot and chat_id and message_id:
                    from ui import admin_panel_markup
                    await safe_telegram_call(bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="لوحة التحكم:",
                        reply_markup=admin_panel_markup()
                    ))

            elif data == "back_to_main":
                markup = await build_main_menu()
                if bot and chat_id and message_id:
                    await safe_telegram_call(bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="اختر القسم:",
                        reply_markup=markup
                    ))

            elif data in ["admin_add_button", "admin_remove_button", "admin_upload_to_button"]:
                if user_id in __import__('settings').ADMIN_IDS:
                    admin_state[user_id] = {"action": data}
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id=chat_id, text="أرسل البيانات المطلوبة"))

            elif data == "admin_list_buttons" and user_id in __import__('settings').ADMIN_IDS:
                try:
                    rows = await __import__('database').db_fetchall(
                        "SELECT id, name, callback_data FROM buttons ORDER BY id"
                    )
                    text = "\n".join(f"{r['id']}: {r['name']} ({r['callback_data']})" for r in rows)
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id=chat_id, text=text or "لا توجد أزرار"))
                except Exception as e:
                    logger.error("Failed to list buttons: %s", e)

            else:
                row = await __import__('database').db_fetchone(
                    "SELECT content_type, file_id FROM buttons WHERE callback_data = $1",
                    data
                )
                if row and row["content_type"] and row["file_id"]:
                    ctype, fid = row["content_type"], row["file_id"]
                    if bot and chat_id:
                        if ctype == "document":
                            await safe_telegram_call(bot.send_document(chat_id=chat_id, document=fid))
                        elif ctype == "photo":
                            await safe_telegram_call(bot.send_photo(chat_id=chat_id, photo=fid))
                        elif ctype == "video":
                            await safe_telegram_call(bot.send_video(chat_id=chat_id, video=fid))
                        else:
                            await safe_telegram_call(bot.send_message(chat_id=chat_id, text=str(fid)))
                else:
                    parent = await __import__('database').db_fetchone(
                        "SELECT id FROM buttons WHERE callback_data = $1", data
                    )
                    if parent:
                        subs = await __import__('database').db_fetchall(
                            "SELECT name, callback_data FROM buttons WHERE parent_id = $1 ORDER BY id",
                            parent["id"]
                        )
                        if subs:
                            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                            keyboard = [[InlineKeyboardButton(s['name'], callback_data=s['callback_data'])] for s in subs]
                            keyboard.append([InlineKeyboardButton("العودة", callback_data="back_to_main")])
                            markup = InlineKeyboardMarkup(keyboard)
                            if bot and chat_id and message_id:
                                await safe_telegram_call(
                                    bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="اختر:", reply_markup=markup)
                                )
                        else:
                            if bot and chat_id:
                                await safe_telegram_call(bot.send_message(chat_id=chat_id, text="لا محتوى"))

        # Handle normal messages
        elif "message" in update:
            await process_text_message(update["message"], admin_state)
        elif "edited_message" in update:
            await process_text_message(update["edited_message"], admin_state)

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
                REQUEST_HISTORY.append((time.time(), "released"))
            except ValueError as e:
                logger.error(f"Error releasing semaphore: {e}")
                REQUEST_HISTORY.append((time.time(), "release_error"))


def main():
    logger.info("Starting server on port %s with max_concurrent=%s", PORT, MAX_CONCURRENT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", workers=1)
