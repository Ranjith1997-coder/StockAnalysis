"""Tests for analyser/LiveAlertFormatter.py."""
from analyser.LiveAlertFormatter import LiveAlertFormatter, F


class TestHeader:
    def test_contains_symbol(self):
        result = F.header("NIFTY", "PCR Crossover", "📈")
        assert "NIFTY" in result

    def test_contains_title(self):
        result = F.header("NIFTY", "PCR Crossover", "📈")
        assert "PCR Crossover" in result

    def test_contains_emoji(self):
        result = F.header("NIFTY", "PCR Crossover", "📈")
        assert "📈" in result

    def test_bold_tags_present(self):
        result = F.header("NIFTY", "PCR Crossover", "📈")
        assert "<b>" in result and "</b>" in result

    def test_timestamp_present(self):
        # Time formatted as HH:MM:SS — simple bracket check
        result = F.header("NIFTY", "PCR Crossover", "📈")
        assert "[" in result and "]" in result


class TestKv:
    def test_label_present(self):
        assert "PCR" in F.kv("PCR", "1.23")

    def test_value_wrapped_in_code(self):
        result = F.kv("PCR", "1.23")
        assert "<code>1.23</code>" in result


class TestKvPair:
    def test_both_labels_present(self):
        result = F.kv_pair("CE OI", "100", "PE OI", "200")
        assert "CE OI" in result
        assert "PE OI" in result

    def test_both_values_in_code(self):
        result = F.kv_pair("CE OI", "100", "PE OI", "200")
        assert "<code>100</code>" in result
        assert "<code>200</code>" in result

    def test_separator_pipe(self):
        result = F.kv_pair("CE OI", "100", "PE OI", "200")
        assert "|" in result


class TestKvBold:
    def test_value_wrapped_in_bold(self):
        result = F.kv_bold("Signal", "BULLISH")
        assert "<b>BULLISH</b>" in result

    def test_label_present(self):
        assert "Signal" in F.kv_bold("Signal", "BULLISH")


class TestSignal:
    def test_arrow_prefix(self):
        result = F.signal("Buy now")
        assert result.startswith("→")

    def test_text_included(self):
        assert "Buy now" in F.signal("Buy now")


class TestNote:
    def test_italic_tags(self):
        result = F.note("Context here")
        assert "<i>" in result and "</i>" in result

    def test_text_included(self):
        assert "Context here" in F.note("Context here")


class TestBuild:
    def test_joins_non_empty_lines(self):
        result = F.build("line1", "line2", "line3")
        assert result == "line1\nline2\nline3"

    def test_filters_empty_strings(self):
        result = F.build("line1", "", "line3")
        assert result == "line1\nline3"

    def test_filters_none(self):
        result = F.build("line1", None, "line3")
        assert result == "line1\nline3"

    def test_all_empty_returns_empty_string(self):
        result = F.build("", None, "")
        assert result == ""

    def test_single_line_no_newline(self):
        result = F.build("only")
        assert result == "only"

    def test_f_alias_is_same_class(self):
        assert F is LiveAlertFormatter
