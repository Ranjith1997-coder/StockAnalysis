import logging
import os
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

load_dotenv()

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FORMAT = "%(levelname)s:%(name)s:%(asctime)s:%(filename)s:%(lineno)d:%(funcName)s:%(message)s"

# LOG_LEVEL env var controls verbosity across all handlers.
# Valid values: DEBUG, INFO, WARNING, ERROR  (default: WARNING)
_level_name = os.environ.get("LOG_LEVEL", "WARNING").upper()
_level = getattr(logging, _level_name, logging.WARNING)

logger = logging.getLogger('StockMonitor')
logger.setLevel(_level)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(_level)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

# POSITIONAL=1 (set by the systemd positional service) → separate log file
_is_positional = os.environ.get("POSITIONAL", "0") == "1" or os.environ.get("DEV_POSITIONAL", "0") == "1"
_log_filename = "stock_monitor_positional.log" if _is_positional else "stock_monitor.log"

# File handler — rotates at 10 MB, keeps last 5 files
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, _log_filename),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
)
file_handler.setLevel(_level)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

logger.addHandler(console_handler)
logger.addHandler(file_handler)
