import pandas as pd, os, datetime, json
from post_market_analysis.registry import load_sources
from post_market_analysis.analysis import PostMarketAnalyzer
from post_market_analysis.summary import PostMarketSummaryBuilder
from common.logging_util import logger

# DATA_DIR = "post_market/data"
# os.makedirs(DATA_DIR, exist_ok=True)

def run_post_market_pipeline():
    sources = load_sources()
    analyzer = PostMarketAnalyzer()
    outputs = []
    for src in sources:
        try:
            df = src.run()
            # save_path = os.path.join(
            #     DATA_DIR, f"{src.source_name}_{datetime.date.today().isoformat()}.csv"
            # )
            # df.to_csv(save_path, index=False)
            analysis = analyzer.dispatch(src.source_name, df)
            outputs.append({"source": src.source_name, "rows": len(df), "analysis": analysis})
        except Exception as e:
            logger.error(f"Post-market source {src.source_name} failed: {e}")
    return outputs, analyzer

def build_summary(outputs):
    builder = PostMarketSummaryBuilder()
    return builder.build(outputs)

def run_and_summarize():
    outputs, analyzer = run_post_market_pipeline()
    return build_summary(outputs)