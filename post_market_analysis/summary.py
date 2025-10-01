import math

class BaseSummaryFormatter:
    source_name = None
    def format(self, analysis: dict) -> str:
        return f"{self.source_name}: {analysis}"

class FiiDiiSummaryFormatter(BaseSummaryFormatter):
    source_name = "fii_dii_activity"

    COLS = [
        ("date", "Date"),
        ("fii_cash", "FII"),
        ("dii_cash", "DII"),
        ("fii_index_fut", "IdxFut"),
        ("fii_index_opt", "IdxOpt"),
        ("nifty_fut_exposure", "NfFut"),
        ("banknifty_fut_exposure", "BnFut"),
        ("nifty_opt_exposure", "NfOpt"),
        ("banknifty_opt_exposure", "BnOpt"),
    ]

    def _fmt(self, v):
        if v is None or (isinstance(v, float) and (math.isnan(v))):
            return "NA"
        try:
            return f"{v:+.0f}"
        except Exception:
            return str(v)

    def format(self, analysis: dict) -> str:
        if not analysis:
            return "No FII/DII data"
        header_latest = (
            f"FII/DII Flows (Latest {analysis.get('date')})\n"
            f"FII Cash: {analysis.get('fii_cash_net')}  "
            f"DII Cash: {analysis.get('dii_cash_net')}  "
            f"5d FII: {analysis.get('fii_cash_5d_sum')}  "
            f"Idx Fut: {analysis.get('fii_index_fut_net')}  "
            f"Idx Opt: {analysis.get('fii_index_opt_net')}"
        )
        last5 = analysis.get("last5") or []
        if not last5:
            return header_latest

        # compute widths
        widths = {}
        for key, title in self.COLS:
            w = len(title)
            for r in last5:
                val = r.get(key) if key == "date" else self._fmt(r.get(key))
                w = max(w, len(val))
            widths[key] = w

        header_row = " ".join(title.ljust(widths[key]) for key, title in self.COLS)
        sep_row = " ".join("-" * widths[key] for key, _ in self.COLS)

        data_lines = []
        for r in last5:
            line = " ".join(
                (r.get("date") if key == "date" else self._fmt(r.get(key))).ljust(widths[key])
                for key, _ in self.COLS
            )
            data_lines.append(line)

        block = "\n".join([header_latest, "", header_row, sep_row, *data_lines])
        # length guard (Telegram)
        if len(block) > 3900:
            data_lines = data_lines[-5:]
            block = "\n".join([header_latest, "", header_row, sep_row, *data_lines])
        return block

class SectorSummaryFormatter(BaseSummaryFormatter):
    source_name = "sector_performance"

    def format(self, analysis: dict) -> str:
        if not analysis:
            return "No sector performance data"
        tg = analysis.get("top_gainers", [])
        tl = analysis.get("top_losers", [])
        # Helpers
        def fmt_chg(v):
            if v is None:
                return "NA"
            return f"{v:+.2f}%"
        def fmt_mcap(v):
            if v is None:
                return "NA"
            # Keep large numbers compact (cr -> crore approx) if huge? Just show full for now.
            return f"{int(v):,}"
        def build_table(title, rows):
            if not rows:
                return f"{title}: None"
            name_w = max(6, *(len(r["name"]) for r in rows))
            lines = [title,
                     f"{'Sector'.ljust(name_w)}  {'Chg%':>7}  {'Mcap':>10}  {'Stocks':>6}",
                     f"{'-'*name_w}  {'-'*7}  {'-'*10}  {'-'*6}"]
            for r in rows:
                lines.append(
                    f"{r['name'].ljust(name_w)}  {fmt_chg(r['chg']):>7}  {fmt_mcap(r['mcap']):>10}  {str(r['stocks']):>6}"
                )
            return "\n".join(lines)

        header = (
            f"Sectors ({analysis.get('as_of')}) "
            f"Adv:{analysis.get('advancing')}/Dec:{analysis.get('declining')}/Unch:{analysis.get('unchanged')} "
            f"Total:{analysis.get('total_sectors')}"
        )
        block = "\n".join([
            header,
            build_table("Top 5 Gaining Sectors", tg),
            "",
            build_table("Top 5 Losing Sectors", tl)
        ])
        if len(block) > 3900:
            block = block[:3900] + "\n(truncated)"
        return block

class PostMarketSummaryBuilder:
    """
    Collects per-source formatters and builds a combined summary.
    Easily extendable: add new formatter subclass & register.
    """
    def __init__(self):
        self.formatter_map = {
            FiiDiiSummaryFormatter.source_name: FiiDiiSummaryFormatter(),
            SectorSummaryFormatter.source_name: SectorSummaryFormatter(),
        }

    def build(self, outputs: list) -> list| None:
        parts = []
        for o in outputs:
            src = o.get("source")
            analysis = o.get("analysis")
            formatter = self.formatter_map.get(src)
            if not formatter:
                continue
            parts.append(formatter.format(analysis))
        return parts if parts else None