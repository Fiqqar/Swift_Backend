import os
os.environ["APP_ENV"] = "development"

from app.core.logging import setup_logging, get_logger

setup_logging()

logger = get_logger("system")
# This used to throw TypeError with stdlib logger
logger.info("system.startup", version="1.0.0", env=os.environ.get("APP_ENV", "development"))
print("SUCCESS: logger.info with kwargs works!")