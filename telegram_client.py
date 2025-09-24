import time
import asyncio
import logging
from telegram import Bot
from telegram.error import RetryAfter
from settings import BOT_TOKEN, MIN_REQUEST_INTERVAL

logger = logging.getLogger(__name__)

bot: Bot = None
BOT_ID: int = None
LAST_REQUEST_TIME = 0

async def init_bot():
    global bot, BOT_ID
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set")
    
    bot_instance = Bot(token=BOT_TOKEN)
    me = await bot_instance.get_me()
    BOT_ID = me.id
    bot = bot_instance
    logger.info("Bot initialized: %s", me.username)
    return bot

async def rate_limit():
    """Add small delay between requests to avoid rate limiting"""
    global LAST_REQUEST_TIME
    current_time = time.time()
    elapsed = current_time - LAST_REQUEST_TIME
    if elapsed < MIN_REQUEST_INTERVAL:
        await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)
    LAST_REQUEST_TIME = time.time()

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
                raise
            logger.info("Telegram API timeout, retrying...")
            await asyncio.sleep(1)
        except Exception as e:
            if attempt == max_retries:
                logger.warning("Telegram API error: %s (attempt %s)", e, attempt + 1)
                raise
            logger.info("Telegram API error, retrying...")
            await asyncio.sleep(1)