import sys
import os
sys.path.append(os.getcwd())
from nse.nse_utils import nse_urlfetch
import json

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

data["data"]["IndexList"] = new_index_list
data["data"]["UnderlyingList"] = new_stock_list

with open("fnolist.json", "w") as f:
    json.dump(data, f, indent=4)


