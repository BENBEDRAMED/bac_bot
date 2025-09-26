import asyncpg
from typing import Optional
from settings import DATABASE_URL, DB_POOL_MAX
import logging

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
            command_timeout=30,
            timeout=10,
            max_inactive_connection_lifetime=60
        )
        logger.info("Postgres pool created successfully")
    except Exception as e:
        logger.error("Failed to create database pool: %s", e)
        pg_pool = None
        raise

async def db_fetchall(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            return await conn.fetch(query, *params)
    except Exception as e:
        logger.error("Database query failed: %s", e)
        raise

async def db_fetchone(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            return await conn.fetchrow(query, *params)
    except Exception as e:
        logger.error("Database query failed: %s", e)
        raise

async def db_execute(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            return await conn.execute(query, *params)
    except Exception as e:
        logger.error("Database execute failed: %s", e)
        raise

async def check_db_health():
    if not pg_pool:
        return False
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error("Database health check failed: %s", e)
        return False

async def init_db_schema_and_defaults():
    try:
        # Buttons table (menu items)
        await db_execute("""
            CREATE TABLE IF NOT EXISTS buttons (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                callback_data TEXT UNIQUE NOT NULL,
                parent_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Users table
        await db_execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                class_type TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Media files table (many files per button)
        await db_execute("""
            CREATE TABLE IF NOT EXISTS media_files (
                id SERIAL PRIMARY KEY,
                button_id INTEGER NOT NULL REFERENCES buttons(id) ON DELETE CASCADE,
                file_id TEXT NOT NULL,
                content_type TEXT NOT NULL,
                caption TEXT,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Insert defaults if not exist
        defaults = [
            ("العلمي", "science", 0),
            ("الأدبي", "literary", 0),
            ("الإدارة", "admin_panel", 0)
        ]
        for name, cb, parent in defaults:
            await db_execute(
                "INSERT INTO buttons (name, callback_data, parent_id) VALUES ($1,$2,$3) ON CONFLICT (callback_data) DO NOTHING",
                name, cb, parent
            )

        logger.info("DB schema initialized with media_files support")
    except Exception as e:
        logger.error("Failed to init DB schema: %s", e)
        raise
