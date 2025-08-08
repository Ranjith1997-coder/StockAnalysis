from zerodha.zerodha_ticker import KiteTicker
import common.shared as shared
import time
from common.Stock import Stock
from common.logging_util import logger
import threading
import queue
from collections import defaultdict
from notification.Notification import TELEGRAM_NOTIFICATIONS
import requests

class ZerodhaTickerManager:
    def __init__(self, userName, password, encToken):
        self.username = userName
        self.password = password
        self.encToken = encToken
        self.apiKey = "kitefront"
        self.root = "wss://ws.zerodha.com"
        self.connected = False
        self._kt :KiteTicker | None= None
        self.max_retries = 3
        self.retry_delay = 5  # seconds
        self.tick_queue = queue.Queue()
        self.processor_thread = None
        self.stop_processor = False
        self.notification_cooldown = 300  # 5 minutes cooldown
        self.last_notification_time = defaultdict(float)
        self.is_enctoken_updated = False

    def initialize_kite_ticker(self):
        self._kt = KiteTicker(self.apiKey, self.username, self.encToken, root=self.root)
        self._kt.on_connect = self.on_connect
        self._kt.on_close = self.on_close
        self._kt.on_error = self.on_error
        self._kt.on_ticks = self.on_ticks

    def connect(self):
        for attempt in range(self.max_retries):
            try:
                logger.info(f"Attempting to connect (Attempt {attempt + 1}/{self.max_retries})")
                self.initialize_kite_ticker()
                self._kt.connect(threaded=True)
                 # Add a delay to allow the connection to establish
                connection_timeout = 10  # 10 seconds timeout
                start_time = time.time()
                
                while time.time() - start_time < connection_timeout:
                    if self._kt.is_connected():
                        logger.info("Successfully connected to Zerodha WebSocket")
                        return True
                    time.sleep(0.5)  # Check every 0.5 seconds
            except Exception as e:
                logger.error(f"Connection attempt {attempt + 1} failed: {str(e)}")
                if attempt < self.max_retries - 1:
                    logger.info(f"Retrying in {self.retry_delay} seconds...")
                    time.sleep(self.retry_delay)
                else:
                    logger.error("Max retries reached. Unable to connect.")
                    self.is_enctoken_updated = False
                    return False

    def close_connection(self):
        """
        Close the WebSocket connection.
        """
        if self._kt and self._kt.is_connected():
            try:
                self._kt.close()
                logger.info("WebSocket connection closed successfully")
                self.connected = False
                self.is_enctoken_updated = False
            except Exception as e:
                logger.error(f"Error while closing WebSocket connection: {str(e)}")
        else:
            logger.info("WebSocket connection is already closed or not initialized")


    def refresh_enctoken(self, twofa):
        # Implement the logic to get a new enctoken here
        # This might involve making an API call or running a script to get a new token
        # For now, we'll just simulate it with a placeholder function
        new_enctoken = self.get_new_enctoken(twofa)
        if new_enctoken:
            self.encToken = new_enctoken
            logger.info("Successfully refreshed enctoken.")
            self.is_enctoken_updated = True
            return True
        else:
            logger.error("Failed to get new enctoken.")
            self.is_enctoken_updated = False
            return False

    def get_new_enctoken(self, twofa):
        session = requests.Session()
        response = session.post('https://kite.zerodha.com/api/login', data={
            "user_id": self.username,
            "password": self.password
        })
        response = session.post('https://kite.zerodha.com/api/twofa', data={
            "request_id": response.json()['data']['request_id'],
            "twofa_value": twofa,
            "user_id": response.json()['data']['user_id']
        })
        enctoken = response.cookies.get('enctoken')
        if enctoken:
            return enctoken
        else:
            raise Exception("Enter valid details !!!!")
    
    def start_tick_processor(self):
        self.stop_processor = False
        self.processor_thread = threading.Thread(target=self.process_ticks)
        self.processor_thread.start()

    def stop_tick_processor(self):
        self.stop_processor = True
        if self.processor_thread:
            self.processor_thread.join()

    def process_ticks(self):
        while not self.stop_processor:
            try:
                tick = self.tick_queue.get(timeout=1)
                self.analyze_tick(tick)
            except queue.Empty:
                continue
    
    def analyze_tick(self, tick):
        stock_token = tick.get("instrument_token")
        if stock_token in shared.stock_token_obj_dict:
            stock = shared.stock_token_obj_dict[stock_token]
            stock.update_zerodha_data(tick)
            # buy_quantity = tick.get("total_buy_quantity", 0)
            # sell_quantity = tick.get("total_sell_quantity", 0)

            # current_time = time.time()
            # last_notification = stock.zerodha_ctx["last_notification_time"]

            # if current_time - last_notification >= self.notification_cooldown:
            #     if buy_quantity >= 2 * sell_quantity:
            #         self.send_notification(stock, "BUY", buy_quantity, sell_quantity)
            #         stock.zerodha_ctx["last_notification_time"] = current_time
            #     elif sell_quantity >= 2 * buy_quantity:
            #         self.send_notification(stock, "SELL", buy_quantity, sell_quantity)
            #         stock.zerodha_ctx["last_notification_time"] = current_time

    def send_notification(self, stock, direction, buy_quantity, sell_quantity):
        message = f"Alert for {stock.stockName} ({stock.stock_symbol}): "
        message += f"High {direction} pressure. "
        message += f"Buy Quantity: {buy_quantity}, Sell Quantity: {sell_quantity}"
        logger.info(message)
        TELEGRAM_NOTIFICATIONS.send_notification(message)
    def unsubscribe(self, instrument_tokens):
        """
        Unsubscribe from the given list of instrument tokens.

        Args:
            instrument_tokens (list): List of instrument tokens to unsubscribe from.
        """
        if not self._kt or not self._kt.is_connected():
            logger.error("Kite Ticker is not connected. Cannot unsubscribe.")
            return False

        try:
            self._kt.unsubscribe(instrument_tokens)
            logger.info(f"Successfully unsubscribed from instrument tokens: {instrument_tokens}")
            return True
        except Exception as e:
            logger.error(f"Error while unsubscribing: {str(e)}")
            return False
    
    def on_connect(self, ws, response):
        self.connected = True
        logger.info(f"Successfully connected. Response: {response}")
        instrument_tokens = list(shared.stock_token_obj_dict.keys())
        self.start_tick_processor()
        ws.subscribe(instrument_tokens)
        ws.set_mode(ws.MODE_FULL, instrument_tokens)

    def on_close(self, ws, code, reason):
        self.connected = False
        logger.info(f"Connection closed. Code: {code}, Reason: {reason}")
        self.stop_tick_processor() 

    def on_error(self, ws, code, reason):
        logger.error(f"Error in connection. Code: {code}, Reason: {reason}")
        self.stop_tick_processor() 

    def on_ticks(self, ws, ticks):
        # Implement your tick handling logic here
        logger.debug(f"Received ticks: {ticks}")
        for tick in ticks:
            self.tick_queue.put(tick)

def zerodha_init():
    manager = ZerodhaTickerManager(ZERODHA_USERNAME, ZERODHA_ENC_TOKEN)
    if manager.connect():
        logger.info("Zerodha initialized successfully")
        try:
            time.sleep(10)  # Run for 10 seconds
            manager.unsubscribe([128046084, 128046083])
            time.sleep(5)  # Wait for 5 seconds after unsubscribing
        finally:
            manager.close_connection()
    else:
        logger.error("Failed to initialize Zerodha")

if __name__ == "__main__":
    
    ticker =[{
                "name": "HDFC Bank Limited",
                "tradingsymbol": "HDFCBANK",
                "instrument_token": 341249
            },
            {
                "name": "RELIANCE",
                "tradingsymbol": "RELIANCE",
                "instrument_token": 738561
            },]
    
    for t in ticker:
        shared.stock_token_obj_dict[t["instrument_token"]] = Stock(t["name"], t["tradingsymbol"])
    zerodha_init()
    
    










