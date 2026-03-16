import sys
import os
sys.path.append(os.getcwd())
from nse.nse_utils import nse_urlfetch
import json

from zerodha.zerodha_connect import KiteConnect


DUMMY_API_KEY_ZERODHA = "dummy_api_key"
NSE_FNO_LIST_URL= "https://www.nseindia.com/api/underlying-information"
NSE_ORIGINAL_URL = "https://www.nseindia.com/products-services/equity-derivatives-list-underlyings-information"


# Load the existing final_derivatives_list.json file
with open("final_derivatives_list.json", "r") as f:
    data = json.load(f)

# Fetch the latest UnderlyingList from NSE
latest_data = nse_urlfetch(NSE_FNO_LIST_URL, NSE_ORIGINAL_URL).json()
stock_list = latest_data["data"]["UnderlyingList"]

key_map = {
    "underlying": "name",
    "symbol": "tradingsymbol",
    "serialNumber": "instrument_token"
}


new_stock_list = [
    {key_map[k]: v for k, v in item.items()} for item in stock_list
]

kc = KiteConnect(DUMMY_API_KEY_ZERODHA)

zerodha_instruments = kc.instruments()
# Create a dictionary mapping tradingsymbol to instrument_token (NSE equity segment)
instrument_token_map = {item['tradingsymbol']: item['instrument_token'] for item in zerodha_instruments}


# Update the instrument_token in new_stock_list
for item in new_stock_list:
    if item['tradingsymbol'] in instrument_token_map:
        item['instrument_token'] = instrument_token_map[item['tradingsymbol']]

data["data"]["UnderlyingList"] = new_stock_list


# ── Update IndexList instrument tokens from Zerodha INDICES segment ──
# Zerodha uses different tradingsymbols for indices than our JSON
# Map: our tradingsymbol → Zerodha tradingsymbol in INDICES segment
INDEX_ZERODHA_SYMBOL_MAP = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "SENSEX": "SENSEX",
    "NIFTYNXT50": "NIFTY NEXT 50",
    "INDIA_VIX": "INDIA VIX",
}

# Build token map for INDICES segment
indices_instruments = [item for item in zerodha_instruments if item.get('segment') == 'INDICES']
indices_token_map = {item['tradingsymbol']: item['instrument_token'] for item in indices_instruments}

# Update index tokens
for index_item in data["data"]["IndexList"]:
    zerodha_symbol = INDEX_ZERODHA_SYMBOL_MAP.get(index_item["tradingsymbol"])
    if zerodha_symbol and zerodha_symbol in indices_token_map:
        old_token = index_item["instrument_token"]
        index_item["instrument_token"] = indices_token_map[zerodha_symbol]
        print(f"  {index_item['tradingsymbol']}: {old_token} → {index_item['instrument_token']} ({zerodha_symbol})")
    else:
        print(f"  WARNING: No Zerodha token found for {index_item['tradingsymbol']} (looked up: {zerodha_symbol})")


with open("final_derivatives_list.json", "w") as f:
    json.dump(data, f, indent=4)

print("\nfinal_derivatives_list.json updated successfully.")
