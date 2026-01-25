import pandas as pd
from datetime import date
from common.logging_util import logger

class PostMarketAnalyzer:
    def analyse_fii_dii_activity(self, df: pd.DataFrame):
        if df.empty:
            return {}
        # --- Category level (aggregated) ---
        cat_df = df[df["level"] == "category"].copy()
        latest_date = cat_df["date"].max()
        latest = cat_df[cat_df["date"] == latest_date]

        def pick(short, frame):
            row = frame[frame["category_short"] == short]
            return float(row["value"].iloc[0]) if not row.empty else None

        fii_cash = pick("FII CM*", latest)
        dii_cash = pick("DII CM*", latest)
        index_fut = pick("FII Idx Fut", latest)
        index_opt = pick("FII Idx Opt", latest)
        stock_fut = pick("FII Stk Fut", latest)
        stock_opt = pick("FII Stk Opt", latest)

        # Rolling sums (5 day) for FII cash
        recent_fii_cash = (cat_df[cat_df["category_short"] == "FII CM*"]
                           .sort_values("date")
                           .tail(5))
        fii_cash_5d_sum = recent_fii_cash["value"].sum() if not recent_fii_cash.empty else None

        # --- Instrument level (child breakdown for indices) ---
        instr_df = df[(df["level"] == "instrument") &
                      (df["instrument_short"].isin(["NIFTY", "BANKNIFTY"])) &
                      (df["category_short"].isin(["FII Idx Fut", "FII Idx Opt"]))]

        # Last 5 unique dates available (based on category data)
        last5_dates = sorted(cat_df["date"].unique())[-5:]
        cat_last5 = cat_df[cat_df["date"].isin(last5_dates)]
        instr_last5 = instr_df[instr_df["date"].isin(last5_dates)]

        # Helper to get value for a given date & key
        def val_cat(date, short):
            row = cat_last5[(cat_last5["date"] == date) & (cat_last5["category_short"] == short)]
            return float(row["value"].iloc[0]) if not row.empty else None

        def val_instr(date, instr_short, parent_short):
            row = instr_last5[(instr_last5["date"] == date) &
                              (instr_last5["instrument_short"] == instr_short) &
                              (instr_last5["category_short"] == parent_short)]
            return float(row["value"].iloc[0]) if not row.empty else None

        # Build last 5 summary rows
        last5_rows = []
        for d in last5_dates:
            row_dict = {
                "date": str(d),
                "fii_cash": val_cat(d, "FII CM*"),
                "dii_cash": val_cat(d, "DII CM*"),
                "fii_index_fut": val_cat(d, "FII Idx Fut"),
                "fii_index_opt": val_cat(d, "FII Idx Opt"),
                "nifty_fut_exposure": val_instr(d, "NIFTY", "FII Idx Fut"),
                "banknifty_fut_exposure": val_instr(d, "BANKNIFTY", "FII Idx Fut"),
                "nifty_opt_exposure": val_instr(d, "NIFTY", "FII Idx Opt"),
                "banknifty_opt_exposure": val_instr(d, "BANKNIFTY", "FII Idx Opt"),
            }
            last5_rows.append(row_dict)

        # Log formatted lines
        logger.info("===== FII / DII Last 5 Days (Cash / Index Futures & Options) =====")
        for r in last5_rows:
            logger.info(
                f"{r['date']} | FII Cash {r['fii_cash']:+.0f} | DII Cash {r['dii_cash']:+.0f} | "
                f"Idx Fut {r['fii_index_fut']:+.0f} | Idx Opt {r['fii_index_opt']:+.0f} | "
                f"NIFTY Fut {r['nifty_fut_exposure']:+.0f} | BANKNIFTY Fut {r['banknifty_fut_exposure']:+.0f} | "
                f"NIFTY Opt {r['nifty_opt_exposure']:+.0f} | BANKNIFTY Opt {r['banknifty_opt_exposure']:+.0f}"
            )

        return {
            "date": str(latest_date),
            "fii_cash_net": fii_cash,
            "dii_cash_net": dii_cash,
            "fii_cash_5d_sum": fii_cash_5d_sum,
            "fii_index_fut_net": index_fut,
            "fii_index_opt_net": index_opt,
            "fii_stock_fut_net": stock_fut,
            "fii_stock_opt_net": stock_opt,
            "bias_cash": "INFLOW" if (fii_cash and fii_cash > 0) else ("OUTFLOW" if (fii_cash and fii_cash < 0) else "NEUTRAL"),
            "bias_index_fut": "LONG" if (index_fut and index_fut > 0) else ("SHORT" if (index_fut and index_fut < 0) else "FLAT"),
            "last5": last5_rows
        }
    
    def analyse_sector_performance(self, df: pd.DataFrame):
        """
        df: DataFrame created from Sector API:
            Columns expected: ID, Name, Mcap, McapZG, StocksCount, IndustriesCount, Slug, IndustriesForSector
        McapZG = percent change (already sorted by API by change).
        Returns top 5 gaining & losing sectors.
        """
        if df is None or df.empty:
            return {}
        work = df.copy()

        # Ensure numeric
        work["McapZG"] = pd.to_numeric(work["McapZG"], errors="coerce")
        work["Mcap"] = pd.to_numeric(work["Mcap"], errors="coerce")
        work["StocksCount"] = pd.to_numeric(work["StocksCount"], errors="coerce")
        work = work.dropna(subset=["McapZG"])

        # Sort descending by change to be safe (API may already sort)
        work = work.sort_values("McapZG", ascending=False)

        # Split positive / negative
        pos = work[work["McapZG"] > 0]
        neg = work[work["McapZG"] < 0].sort_values("McapZG")  # most negative first

        top_gainers = pos.head(5)
        top_losers = neg.head(5)

        def pack(rows):
            out = []
            for _, r in rows.iterrows():
                out.append({
                    "name": r["Name"],
                    "chg": float(r["McapZG"]),
                    "mcap": float(r["Mcap"]) if pd.notna(r["Mcap"]) else None,
                    "stocks": int(r["StocksCount"]) if pd.notna(r["StocksCount"]) else None
                })
            return out

        result = {
            "as_of": str(date.today()),
            "total_sectors": int(len(work)),
            "advancing": int((work["McapZG"] > 0).sum()),
            "declining": int((work["McapZG"] < 0).sum()),
            "unchanged": int((work["McapZG"] == 0).sum()),
            "top_gainers": pack(top_gainers),
            "top_losers": pack(top_losers)
        }

        logger.info("Sector performance summary: %s", result)
        return result
    
    def analyse_fo_participant_oi(self, df):
        """
        Expects DataFrame with columns: Date, FoParticipantTypeName, Net, Long, Short
        Returns: dict with last 5 days, each day a dict of participant rows
        """
        if df is None or df.empty:
            return {}

        days = sorted(df["Date"].unique(), reverse=True)[:5]
        out = []
        for d in days:
            day_df = df[df["Date"] == d]
            day = {"date": str(d)}
            for _, row in day_df.iterrows():
                name = row["FoParticipantTypeName"]
                day[name] = {
                    "Net": int(row["Net"]),
                    "Long": int(row["Long"]),
                    "Short": int(row["Short"])
                }
            out.append(day)
        return {"last5": out}
    
    def analyse_index_returns(self, df: pd.DataFrame):
        """
        Expects DataFrame with columns: SecurityName, ChangePercentage, Change, Close, etc.
        Returns: dict with top 10 gainers and losers
        """
        if df is None or df.empty:
            return {}
        
        work = df.copy()
        
        # Ensure numeric
        work["ChangePercentage"] = pd.to_numeric(work["ChangePercentage"], errors="coerce")
        work["Change"] = pd.to_numeric(work["Change"], errors="coerce")
        work["Close"] = pd.to_numeric(work["Close"], errors="coerce")
        work = work.dropna(subset=["ChangePercentage"])
        
        # Sort by percentage change
        work = work.sort_values("ChangePercentage", ascending=False)
        
        # Get top 10 gainers and losers
        top_gainers = work.head(10)
        top_losers = work.tail(10).sort_values("ChangePercentage")
        
        def pack(rows):
            out = []
            for _, r in rows.iterrows():
                out.append({
                    "name": r["SecurityName"],
                    "chg_pct": float(r["ChangePercentage"]),
                    "chg_pts": float(r["Change"]) if pd.notna(r["Change"]) else None,
                    "close": float(r["Close"]) if pd.notna(r["Close"]) else None
                })
            return out
        
        result = {
            "as_of": str(date.today()),
            "total_indices": int(len(work)),
            "advancing": int((work["ChangePercentage"] > 0).sum()),
            "declining": int((work["ChangePercentage"] < 0).sum()),
            "unchanged": int((work["ChangePercentage"] == 0).sum()),
            "top_gainers": pack(top_gainers),
            "top_losers": pack(top_losers)
        }
        
        logger.info("Index returns summary: %d indices, %d advancing, %d declining", 
                    result["total_indices"], result["advancing"], result["declining"])
        return result

    def dispatch(self, source_name: str, df: pd.DataFrame):
        if source_name == "fii_dii_activity":
            return self.analyse_fii_dii_activity(df)
        if source_name == "sector_performance":
            return self.analyse_sector_performance(df)
        if source_name == "fo_participant_oi":
            return self.analyse_fo_participant_oi(df)
        if source_name == "index_returns":
            return self.analyse_index_returns(df)
        return {}