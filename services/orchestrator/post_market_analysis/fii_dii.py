import requests, time, datetime, pandas as pd
from .base import PostMarketSource

class FiiDiiActivitySource(PostMarketSource):
    source_name = "fii_dii_activity"
    URL = "https://api.stockedge.com/Api/FIIDashboardApi/GetLatestFIIActivities?lang=en"
    RETRIES = 3
    SLEEP = 2
    TIMEOUT = 15

    def fetch_raw(self):
        last_err = None
        for _ in range(self.RETRIES):
            try:
                r = requests.get(self.URL, timeout=self.TIMEOUT)
                if r.status_code == 200:
                    return r.json()
                last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
            time.sleep(self.SLEEP)
        raise RuntimeError(f"Failed to fetch FII/DII data: {last_err}")

    def normalize(self, raw):
        # raw = list of days
        records = []
        for day in raw:
            date = day.get("Date")
            date = datetime.datetime.fromisoformat(date).date() if date else None
            buckets = day.get("FIIDIIData", [])
            # Top-level categories
            for b in buckets:
                cat_name = b.get("Name")
                short = b.get("ShortName")
                val = b.get("Value")
                child = b.get("ChildData") or []
                if not child:
                    records.append({
                        "date": date,
                        "level": "category",
                        "category": cat_name,
                        "category_short": short,
                        "instrument": None,
                        "instrument_short": None,
                        "value": val
                    })
                else:
                    # Parent aggregate
                    records.append({
                        "date": date,
                        "level": "category",
                        "category": cat_name,
                        "category_short": short,
                        "instrument": None,
                        "instrument_short": None,
                        "value": val
                    })
                    # Child breakdown
                    for c in child:
                        records.append({
                            "date": date,
                            "level": "instrument",
                            "category": cat_name,
                            "category_short": short,
                            "instrument": c.get("Name"),
                            "instrument_short": c.get("ShortName"),
                            "value": c.get("Value")
                        })
            # Close prices
            for px in day.get("ClosePrice", []):
                records.append({
                    "date": date,
                    "level": "close_price",
                    "category": "CLOSE_PRICE",
                    "category_short": px.get("Symbol"),
                    "instrument": None,
                    "instrument_short": None,
                    "value": px.get("C"),
                    "change": px.get("CZ"),
                    "change_pct": px.get("CZG")
                })
        df = pd.DataFrame(records)
        return df