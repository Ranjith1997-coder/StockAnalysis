"""Shared helper utilities used across multiple command modules."""
from __future__ import annotations

import common.shared as shared


def find_stock_by_symbol(symbol: str):
    """Look up a Stock object by symbol across all tracked dicts."""
    symbol_upper = symbol.upper().strip()
    for d in (
        shared.app_ctx.index_token_obj_dict,
        shared.app_ctx.stock_token_obj_dict,
        shared.app_ctx.commodity_token_obj_dict,
        shared.app_ctx.global_indices_token_obj_dict,
    ):
        for obj in d.values():
            if obj.stock_symbol.upper() == symbol_upper:
                return obj
    return None


def build_gainers_losers():
    """Compute top 5 gainers and losers from live stock data."""
    from common.helperFunctions import percentageChange

    gainers, losers = [], []
    for _, stock in shared.app_ctx.stock_token_obj_dict.items():
        try:
            if stock.ltp is not None and stock.prevDayOHLCV is not None:
                prev_close = stock.prevDayOHLCV.get("CLOSE")
                if prev_close and prev_close > 0:
                    change = percentageChange(stock.ltp, prev_close)
                    if isinstance(change, float) and change == change:  # NaN guard
                        if change > 0:
                            gainers.append((stock.stock_symbol, change))
                        else:
                            losers.append((stock.stock_symbol, change))
        except Exception:
            continue

    gainers.sort(key=lambda x: x[1], reverse=True)
    losers.sort(key=lambda x: x[1])
    return gainers[:5], losers[:5]
