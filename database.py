import asyncpg
import logging
from typing import Optional
from settings import DATABASE_URL, DB_POOL_MAX, DB_COMMAND_TIMEOUT

logger = logging.getLogger(__name__)

pg_pool: Optional[asyncpg.Pool] = None


async def init_pg_pool():
    global pg_pool
    if pg_pool:
        await pg_pool.close()

    if not DATABASE_URL:
        logger.error("DATABASE_URL not set, cannot create pool")
        return

    logger.info("Creating asyncpg pool max_size=%s", DB_POOL_MAX)
    try:
        pg_pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            max_size=DB_POOL_MAX,
            min_size=1,
            command_timeout=DB_COMMAND_TIMEOUT,
            timeout=10,
            max_inactive_connection_lifetime=60,
        )
        logger.info("Postgres pool created successfully")
    except Exception as e:
        logger.error("Failed to create database pool: %s", e)
        pg_pool = None
        raise


async def db_fetchall(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    async with pg_pool.acquire(timeout=5) as conn:
        return await conn.fetch(query, *params)


async def db_fetchone(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    async with pg_pool.acquire(timeout=5) as conn:
        return await conn.fetchrow(query, *params)


async def db_execute(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    async with pg_pool.acquire(timeout=5) as conn:
        return await conn.execute(query, *params)


async def check_db_health() -> bool:
    """Simple health check query to confirm DB connectivity."""
    if not pg_pool:
        return False
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            await conn.execute("SELECT 1;")
        return True
    except Exception as e:
        logger.error("DB health check failed: %s", e)
        return False
