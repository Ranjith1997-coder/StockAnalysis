from .fii_dii import FiiDiiActivitySource
from .sector_performance import SectorPerformanceSource
from post_market_analysis.fo_participant_oi import FoParticipantOISource
from .index_returns import IndexReturnsSource

SOURCE_CLASSES = [SectorPerformanceSource, 
                  FiiDiiActivitySource,
                  FoParticipantOISource,
                  IndexReturnsSource]

def load_sources():
    return [cls() for cls in SOURCE_CLASSES]