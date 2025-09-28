from .fii_dii import FiiDiiActivitySource
from .sector_performance import SectorPerformanceSource

SOURCE_CLASSES = [SectorPerformanceSource, FiiDiiActivitySource]

def load_sources():
    return [cls() for cls in SOURCE_CLASSES]