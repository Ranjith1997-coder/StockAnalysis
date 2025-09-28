import requests, time, pandas as pd
from .base import PostMarketSource

class SectorPerformanceSource(PostMarketSource):
    source_name = "sector_performance"
    URL = "https://api.stockedge.com/Api/SectorDashboardApi/GetAllSectorsWithRespectiveIndustriesAndMcap?sectorSort=3&lang=en"
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
        raise RuntimeError(f"Failed to fetch sector performance: {last_err}")

    def normalize(self, raw):
        # raw is list of sector dicts
        return pd.DataFrame(raw)