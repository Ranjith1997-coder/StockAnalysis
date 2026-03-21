import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FORMAT = "%(levelname)s:%(name)s:%(asctime)s:%(filename)s:%(lineno)d:%(funcName)s:%(message)s"

logger = logging.getLogger('StockMonitor')
logger.setLevel(logging.WARNING)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

# File handler — rotates at 10 MB, keeps last 5 files
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "stock_monitor.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
)
file_handler.setLevel(logging.WARNING)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

logger.addHandler(console_handler)
logger.addHandler(file_handler)
