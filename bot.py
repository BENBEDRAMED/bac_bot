import os
import logging
from settings import LOG_LEVEL, PORT, MAX_CONCURRENT

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

from server import app

def main():
    logger.info("Starting server on port %s with max_concurrent=%s", PORT, MAX_CONCURRENT)
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=PORT, 
        log_level="info",
        workers=1
    )

if __name__ == "__main__":
    main()