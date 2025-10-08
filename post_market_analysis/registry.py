from .fii_dii import FiiDiiActivitySource
from .sector_performance import SectorPerformanceSource
from post_market_analysis.fo_participant_oi import FoParticipantOISource

SOURCE_CLASSES = [SectorPerformanceSource, 
                  FiiDiiActivitySource,
                  FoParticipantOISource]

def load_sources():
    return [cls() for cls in SOURCE_CLASSES]