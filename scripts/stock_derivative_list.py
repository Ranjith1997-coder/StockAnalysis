import sys
import os
sys.path.append(os.getcwd())
from nse.nse_utils import nse_urlfetch
import json

from zerodha.zerodha_connect import KiteConnect


DUMMY_API_KEY_ZERODHA = "dummy_api_key"
NSE_FNO_LIST_URL= "https://www.nseindia.com/api/underlying-information"
NSE_ORIGINAL_URL = "https://www.nseindia.com/products-services/equity-derivatives-list-underlyings-information"

data = nse_urlfetch(NSE_FNO_LIST_URL, NSE_ORIGINAL_URL).json()

index_list = data["data"]["IndexList"]
stock_list = data["data"]["UnderlyingList"]

key_map = {
    "underlying": "name",
    "symbol": "tradingsymbol",
    "serialNumber": "instrument_token"
}

new_index_list = [
    {key_map[k]: v for k, v in item.items()} for item in index_list
]
new_stock_list = [
    {key_map[k]: v for k, v in item.items()} for item in stock_list
]

kc = KiteConnect(DUMMY_API_KEY_ZERODHA)

zerodha_instruments = kc.instruments()
# Create a dictionary mapping tradingsymbol to instrument_token
instrument_token_map = {item['tradingsymbol']: item['instrument_token'] for item in zerodha_instruments}

# Update the instrument_token in new_stock_list
for item in new_stock_list:
    if item['tradingsymbol'] in instrument_token_map:
        item['instrument_token'] = instrument_token_map[item['tradingsymbol']]

# Similarly, update the new_index_list
for item in new_index_list:
    if item['tradingsymbol'] in instrument_token_map:
        item['instrument_token'] = instrument_token_map[item['tradingsymbol']]

data["data"]["IndexList"] = new_index_list
data["data"]["UnderlyingList"] = new_stock_list

with open("fnolist.json", "w") as f:
    json.dump(data, f, indent=4)


