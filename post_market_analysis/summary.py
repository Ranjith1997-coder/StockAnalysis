import math

def _val_dot(v):
    """Return green/red dot emoji based on sign of value."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "\u26AA"
    return "\U0001F7E2" if v >= 0 else "\U0001F534"

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
            return "\U0001F4B0 <b>FII/DII Flows</b>: No data"

        fii_cash = analysis.get('fii_cash_net')
        dii_cash = analysis.get('dii_cash_net')

        header_latest = (
            f"\U0001F4B0 <b>FII/DII Flows</b> ({analysis.get('date')})\n"
            f"  {_val_dot(fii_cash)} FII Cash: <code>{fii_cash}</code>  "
            f"{_val_dot(dii_cash)} DII Cash: <code>{dii_cash}</code>\n"
            f"  5d FII: <code>{analysis.get('fii_cash_5d_sum')}</code>  "
            f"Idx Fut: <code>{analysis.get('fii_index_fut_net')}</code>  "
            f"Idx Opt: <code>{analysis.get('fii_index_opt_net')}</code>"
        )
        last5 = analysis.get("last5") or []
        if not last5:
            return header_latest
        last5 = list(reversed(last5)) 

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

        table = "\n".join([header_row, sep_row, *data_lines])
        block = header_latest + "\n\n<pre>" + table + "</pre>"
        # length guard (Telegram)
        if len(block) > 3900:
            data_lines = data_lines[-5:]
            table = "\n".join([header_row, sep_row, *data_lines])
            block = header_latest + "\n\n<pre>" + table + "</pre>"
        return block

class SectorSummaryFormatter(BaseSummaryFormatter):
    source_name = "sector_performance"

    def format(self, analysis: dict) -> str:
        if not analysis:
            return "\U0001F3ED <b>Sector Performance</b>: No data"
        tg = analysis.get("top_gainers", [])
        tl = analysis.get("top_losers", [])

        def fmt_chg(v):
            if v is None:
                return "NA"
            return f"{v:+.2f}%"
        def fmt_mcap(v):
            if v is None:
                return "NA"
            return f"{int(v):,}"

        def build_table(title, icon, rows):
            if not rows:
                return f"{icon} <b>{title}</b>: None"
            lines = [f"{icon} <b>{title}</b>"]
            for r in rows:
                chg = r.get('chg')
                dot = _val_dot(chg)
                lines.append(
                    f"  {dot} <b>{r['name']}</b>: {fmt_chg(chg)}  "
                    f"Mcap: <code>{fmt_mcap(r['mcap'])}</code>  Stocks: {r['stocks']}"
                )
            return "\n".join(lines)

        adv = analysis.get('advancing', 0)
        dec = analysis.get('declining', 0)
        header = (
            f"\U0001F3ED <b>Sector Performance</b> ({analysis.get('as_of')})\n"
            f"  \U0001F7E2 Advancing: {adv}  \U0001F534 Declining: {dec}  "
            f"\u26AA Unchanged: {analysis.get('unchanged')}  Total: {analysis.get('total_sectors')}"
        )
        block = "\n".join([
            header,
            "",
            build_table("Top 5 Gaining Sectors", "\U0001F4C8", tg),
            "",
            build_table("Top 5 Losing Sectors", "\U0001F4C9", tl)
        ])
        if len(block) > 3900:
            block = block[:3900] + "\n<i>(truncated)</i>"
        return block

class FoParticipantOISummaryFormatter(BaseSummaryFormatter):
    source_name = "fo_participant_oi"

    def format(self, analysis: dict) -> str:
        if not analysis or not analysis.get("last5"):
            return "\U0001F4CA <b>F&amp;O Participant OI</b>: No data"
        rows = analysis["last5"]
        participants = ["Client", "DII", "FII", "Pro"]

        lines = ["\U0001F4CA <b>F&amp;O Participant OI</b> (last 5 days)"]

        # Build monospace table
        header = "Date       " + "  ".join(f"{p:>8}" for p in participants)
        sep = "-" * len(header)
        table_lines = [header, sep]
        for day in rows:
            date = day["date"]
            nets = []
            for p in participants:
                v = day.get(p, {}).get("Net")
                nets.append(f"{v:+,}" if v is not None else "NA")
            table_lines.append(f"{date}  " + "  ".join(f"{n:>8}" for n in nets))

        lines.append("<pre>" + "\n".join(table_lines) + "</pre>")

        # Details for latest day
        latest = rows[0]
        lines.append("\n<b>Latest breakdown:</b>")
        for p in participants:
            d = latest.get(p, {})
            net_v = d.get('Net')
            dot = _val_dot(net_v)
            net_s = f"{net_v:>8}" if net_v is not None else "      NA"
            long_s = f"{d.get('Long','NA'):>8}"
            short_s = f"{d.get('Short','NA'):>8}"
            lines.append(f"  {dot} <b>{p}</b>: Net <code>{net_s}</code> | Long <code>{long_s}</code> | Short <code>{short_s}</code>")
        return "\n".join(lines)

class IndexReturnsSummaryFormatter(BaseSummaryFormatter):
    source_name = "index_returns"

    def format(self, analysis: dict) -> str:
        if not analysis:
            return "\U0001F4C8 <b>NSE Indices</b>: No data"
        
        tg = analysis.get("top_gainers", [])
        tl = analysis.get("top_losers", [])
        
        def build_table(title, icon, rows):
            if not rows:
                return f"{icon} <b>{title}</b>: None"
            
            lines = [f"{icon} <b>{title}</b>"]
            for r in rows:
                chg_pct = r.get('chg_pct')
                dot = _val_dot(chg_pct)
                chg_pct_s = f"{chg_pct:+.2f}%" if chg_pct is not None else "NA"
                chg_pts_s = f"{r['chg_pts']:+.2f}" if r.get('chg_pts') is not None else "NA"
                close_s = f"{r['close']:.2f}" if r.get('close') is not None else "NA"
                lines.append(
                    f"  {dot} <b>{r['name']}</b>: <code>{close_s}</code>  "
                    f"{chg_pct_s}  ({chg_pts_s} pts)"
                )
            return "\n".join(lines)

        adv = analysis.get('advancing', 0)
        dec = analysis.get('declining', 0)
        header = (
            f"\U0001F4C8 <b>NSE Indices</b> ({analysis.get('as_of')})\n"
            f"  \U0001F7E2 Advancing: {adv}  \U0001F534 Declining: {dec}  "
            f"\u26AA Unchanged: {analysis.get('unchanged')}  Total: {analysis.get('total_indices')}"
        )
        
        block = "\n".join([
            header,
            "",
            build_table("Top 10 Gaining Indices", "\U0001F4C8", tg),
            "",
            build_table("Top 10 Losing Indices", "\U0001F4C9", tl)
        ])
        
        if len(block) > 3900:
            block = block[:3900] + "\n<i>(truncated)</i>"
        
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
            FoParticipantOISummaryFormatter.source_name:  FoParticipantOISummaryFormatter(),
            IndexReturnsSummaryFormatter.source_name: IndexReturnsSummaryFormatter()
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