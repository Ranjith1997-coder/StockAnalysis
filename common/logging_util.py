import logging

# Configure logging with a detailed format
logger = logging.getLogger('StockMonitor')
logging.basicConfig(
    format="%(levelname)s:%(name)s:%(asctime)s:%(filename)s:%(lineno)d:%(funcName)s:%(message)s",
    style="%",
    level=logging.INFO
)
