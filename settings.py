import os
from typing import List, Optional

# ---------------- Config ----------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

BOT_TOKEN: Optional[str] = os.environ.get("BOT_TOKEN")
WEBHOOK_URL: Optional[str] = os.environ.get("WEBHOOK_URL")
WEBHOOK_SECRET_TOKEN: Optional[str] = os.environ.get("WEBHOOK_SECRET_TOKEN")
DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")

ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
try:
    ADMIN_IDS: List[int] = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []

REQUIRED_CHATS_RAW = os.environ.get("REQUIRED_CHATS", "")
REQUIRED_CHATS: List[str] = [c.strip() for c in REQUIRED_CHATS_RAW.split(",") if c.strip()]

PORT = int(os.environ.get("PORT", 10000))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 5))
MAX_CONCURRENT = 5
PROCESSING_SEMAPHORE_TIMEOUT = 10.0
MIN_REQUEST_INTERVAL = 0.2