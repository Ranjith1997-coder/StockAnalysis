import requests, time, pandas as pd
from .base import PostMarketSource

class IndexReturnsSource(PostMarketSource):
    source_name = "index_returns"
    BASE_URL = "https://api.stockedge.com/Api/DailyDashboardApi/GetLatestIndexQuotes"
    PARAMS = {
        "pageSize": 20,
        "exchange": "NSE",
        "priceChangePeriodType": 1,
        "lang": "en"
    }
    RETRIES = 3
    SLEEP = 2
    TIMEOUT = 15

    def fetch_raw(self):
        """Fetch data from both page 1 and page 2"""
        all_data = []
        for page in [1, 2]:
            params = self.PARAMS.copy()
            params["page"] = page
            
            last_err = None
            for _ in range(self.RETRIES):
                try:
                    r = requests.get(self.BASE_URL, params=params, timeout=self.TIMEOUT)
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            all_data.extend(data)
                        break
                    last_err = f"HTTP {r.status_code}"
                except Exception as e:
                    last_err = str(e)
                time.sleep(self.SLEEP)
            else:
                raise RuntimeError(f"Failed to fetch index data page {page}: {last_err}")
            
            # Small delay between page requests
            time.sleep(0.5)
        
        return all_data

    def normalize(self, raw):
        """Convert raw JSON list to DataFrame"""
        if not raw:
            return pd.DataFrame()
        
        df = pd.DataFrame(raw)
        
        # Ensure numeric columns
        numeric_cols = ["Open", "High", "Low", "Close", "PreviousClose", "Change", "ChangePercentage"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        
        return df
