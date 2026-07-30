import requests
import pandas as pd
from .base import PostMarketSource

class FoParticipantOISource(PostMarketSource):
    source_name = "fo_participant_oi"
    URL = "https://api.stockedge.com/Api/FoParticipantOpenInterestsDashboardApi/GetFoParticipantWiseGrossOI?foParticipantUnderlyingType=1&lang=en"

    def fetch_raw(self):
        r = requests.get(self.URL, timeout=15)
        r.raise_for_status()
        return r.json()

    def normalize(self, raw):
        # raw is a list of dicts
        df = pd.DataFrame(raw)
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
        return df