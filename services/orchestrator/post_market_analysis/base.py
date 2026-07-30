import abc, pandas as pd

class PostMarketSource(abc.ABC):
    source_name: str = "base"

    @abc.abstractmethod
    def fetch_raw(self):
        pass

    @abc.abstractmethod
    def normalize(self, raw) -> pd.DataFrame:
        pass

    def run(self) -> pd.DataFrame:
        raw = self.fetch_raw()
        df = self.normalize(raw)
        df["source"] = self.source_name
        return df