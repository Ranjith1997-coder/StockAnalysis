from Analyser import BaseAnalyzer
from common.Stock import Stock

class FuturesAnalyser(BaseAnalyzer):

    def __init__(self) -> None:
        super().__init__()
    
    def analyse_futures_and_volume(self, stock: Stock):
        futures_data = stock.futures_data

        




