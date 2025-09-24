import asyncio
import logging
from telegram import Bot
from telegram.error import RetryAfter
from settings import BOT_TOKEN, WEBHOOK_URL, WEBHOOK_SECRET_TOKEN, MIN_REQUEST_INTERVAL

logger = logging.getLogger(__name__)

bot: Bot | None = None
BOT_ID = None
LAST_REQUEST_TIME = 0


async def rate_limit():
    global LAST_REQUEST_TIME
    import time
    current_time = time.time()
    elapsed = current_time - LAST_REQUEST_TIME
    if elapsed < MIN_REQUEST_INTERVAL:
        await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)
    LAST_REQUEST_TIME = time.time()


async def init_bot():
    global bot, BOT_ID
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set")
        return None

    bot_instance = Bot(token=BOT_TOKEN)
    me = await bot_instance.get_me()
    BOT_ID = me.id
    bot = bot_instance

    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
        if WEBHOOK_SECRET_TOKEN:
            await bot_instance.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET_TOKEN)
        else:
            await bot_instance.set_webhook(webhook_url)
        logger.info("Webhook set: %s", webhook_url)

    logger.info("Bot initialized: %s", me.username)
    return bot


async def close_bot():
    global bot
    if bot:
        await bot.close()


async def safe_telegram_call(coro, timeout=15, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            await rate_limit()
            return await asyncio.wait_for(coro, timeout=timeout)

        except RetryAfter as e:
            if attempt == max_retries:
                logger.warning("Telegram rate limit exceeded, retry after %s seconds", e.retry_after)
                raise
            logger.info("Telegram rate limit, waiting %s seconds", e.retry_after)
            await asyncio.sleep(e.retry_after)

        except asyncio.TimeoutError:
            if attempt == max_retries:
                logger.warning("Telegram API timeout after %s seconds (attempt %s)", timeout, attempt + 1)
            await asyncio.sleep(1)
