"""Tests for common/scoring.py."""
import pytest
from common.scoring import (
    NotificationPriority,
    ScoreResult,
    get_analysis_weight,
    calculate_alignment_bonus,
    determine_priority,
    calculate_score,
    should_notify,
    format_score_message,
)
import common.constants as constants


# ── get_analysis_weight ───────────────────────────────────────────────────────

class TestGetAnalysisWeight:
    def test_known_key_rsi(self):
        assert get_analysis_weight("RSI") == constants.ANALYSIS_WEIGHTS["RSI"]

    def test_known_key_macd(self):
        assert get_analysis_weight("MACD") == constants.ANALYSIS_WEIGHTS["MACD"]

    def test_known_key_max_pain(self):
        assert get_analysis_weight("MAX_PAIN") == constants.ANALYSIS_WEIGHTS["MAX_PAIN"]

    def test_unknown_key_returns_default(self):
        assert get_analysis_weight("COMPLETELY_UNKNOWN_SIGNAL") == constants.ANALYSIS_WEIGHTS["DEFAULT"]

    def test_default_is_10(self):
        assert get_analysis_weight("NOT_A_REAL_KEY") == 10


# ── calculate_alignment_bonus ─────────────────────────────────────────────────

class TestCalculateAlignmentBonus:
    def test_empty_analysis_returns_no_bonus(self):
        analysis = {"BULLISH": {}, "BEARISH": {}, "NEUTRAL": {}}
        multiplier, label = calculate_alignment_bonus(analysis)
        assert multiplier == 1.0
        assert label == "NONE"

    def test_all_bullish_no_options_returns_all_bullish(self):
        analysis = {"BULLISH": {"RSI": {}, "MACD": {}}, "BEARISH": {}, "NEUTRAL": {}}
        multiplier, label = calculate_alignment_bonus(analysis)
        assert multiplier == constants.SIGNAL_ALIGNMENT_BONUS["ALL_BULLISH"]
        assert "BULLISH" in label

    def test_all_bearish_no_options_returns_all_bearish(self):
        analysis = {"BULLISH": {}, "BEARISH": {"RSI": {}, "MACD": {}}, "NEUTRAL": {}}
        multiplier, label = calculate_alignment_bonus(analysis)
        assert multiplier == constants.SIGNAL_ALIGNMENT_BONUS["ALL_BEARISH"]
        assert "BEARISH" in label

    def test_confirmation_bullish_tech_and_options(self):
        # RSI is TECHNICAL_ANALYSES, MAX_PAIN is OPTIONS_ANALYSES
        analysis = {
            "BULLISH": {"RSI": {}, "MAX_PAIN": {}},
            "BEARISH": {},
            "NEUTRAL": {},
        }
        multiplier, label = calculate_alignment_bonus(analysis)
        assert multiplier == constants.SIGNAL_ALIGNMENT_BONUS["CONFIRMATION"]
        assert "CONFIRMATION" in label

    def test_confirmation_bearish_tech_and_options(self):
        analysis = {
            "BULLISH": {},
            "BEARISH": {"MACD": {}, "PCR_EXTREME": {}},
            "NEUTRAL": {},
        }
        multiplier, label = calculate_alignment_bonus(analysis)
        assert multiplier == constants.SIGNAL_ALIGNMENT_BONUS["CONFIRMATION"]
        assert "CONFIRMATION" in label

    def test_mixed_signals_returns_mixed(self):
        analysis = {"BULLISH": {"RSI": {}}, "BEARISH": {"MACD": {}}, "NEUTRAL": {}}
        multiplier, label = calculate_alignment_bonus(analysis)
        assert multiplier == constants.SIGNAL_ALIGNMENT_BONUS["MIXED"]
        assert label == "MIXED"

    def test_only_neutral_returns_no_bonus(self):
        analysis = {"BULLISH": {}, "BEARISH": {}, "NEUTRAL": {"VOLUME": {}}}
        multiplier, label = calculate_alignment_bonus(analysis)
        assert multiplier == 1.0
        assert label == "NONE"


# ── determine_priority ────────────────────────────────────────────────────────

class TestDeterminePriority:
    def test_score_below_low_threshold(self):
        assert determine_priority(0) == NotificationPriority.NONE
        assert determine_priority(34) == NotificationPriority.NONE

    def test_score_at_low_threshold(self):
        assert determine_priority(constants.NOTIFICATION_PRIORITY["LOW"]) == NotificationPriority.LOW

    def test_score_below_medium(self):
        assert determine_priority(59) == NotificationPriority.LOW

    def test_score_at_medium_threshold(self):
        assert determine_priority(constants.NOTIFICATION_PRIORITY["MEDIUM"]) == NotificationPriority.MEDIUM

    def test_score_below_high(self):
        assert determine_priority(89) == NotificationPriority.MEDIUM

    def test_score_at_high_threshold(self):
        assert determine_priority(constants.NOTIFICATION_PRIORITY["HIGH"]) == NotificationPriority.HIGH

    def test_score_below_critical(self):
        assert determine_priority(129) == NotificationPriority.HIGH

    def test_score_at_critical_threshold(self):
        assert determine_priority(constants.NOTIFICATION_PRIORITY["CRITICAL"]) == NotificationPriority.CRITICAL

    def test_very_high_score_is_critical(self):
        assert determine_priority(999) == NotificationPriority.CRITICAL


# ── calculate_score ───────────────────────────────────────────────────────────

class TestCalculateScore:
    def test_single_bullish_signal_base_score(self):
        weight = constants.ANALYSIS_WEIGHTS["RSI"]
        analysis = {"BULLISH": {"RSI": {"value": 70}}, "BEARISH": {}, "NEUTRAL": {}}
        result = calculate_score(analysis)
        # ALL_BULLISH bonus applied (only technical, no options)
        expected_base = weight
        assert result.base_score == pytest.approx(expected_base)

    def test_list_of_signals_applies_diminishing_returns(self):
        weight = constants.ANALYSIS_WEIGHTS["RSI"]
        analysis = {
            "BULLISH": {"RSI": [{"v": 1}, {"v": 2}]},
            "BEARISH": {},
            "NEUTRAL": {},
        }
        result = calculate_score(analysis)
        expected = weight * (1 + 0.3 * (2 - 1))   # 2 signals
        assert result.base_score == pytest.approx(expected)

    def test_three_signals_diminishing_returns(self):
        weight = constants.ANALYSIS_WEIGHTS["MACD"]
        analysis = {
            "BULLISH": {"MACD": [{}, {}, {}]},
            "BEARISH": {},
            "NEUTRAL": {},
        }
        result = calculate_score(analysis)
        expected = weight * (1 + 0.3 * (3 - 1))
        assert result.base_score == pytest.approx(expected)

    def test_alignment_bonus_applied_to_total_score(self):
        analysis = {"BULLISH": {"RSI": {}}, "BEARISH": {}, "NEUTRAL": {}}
        result = calculate_score(analysis)
        # ALL_BULLISH → multiplier 1.3
        expected_total = result.base_score * 1.3
        assert result.total_score == pytest.approx(expected_total, rel=1e-3)

    def test_breakdown_keys_prefixed_correctly(self):
        analysis = {"BULLISH": {"RSI": {}}, "BEARISH": {"MACD": {}}, "NEUTRAL": {}}
        result = calculate_score(analysis)
        assert "BULLISH:RSI" in result.breakdown
        assert "BEARISH:MACD" in result.breakdown

    def test_dominant_sentiment_bullish_when_bullish_greater(self):
        analysis = {
            "BULLISH": {"RSI": {}, "MACD": {}},
            "BEARISH": {},
            "NEUTRAL": {},
        }
        result = calculate_score(analysis)
        assert result.dominant_sentiment == "BULLISH"

    def test_dominant_sentiment_bearish_when_bearish_greater(self):
        analysis = {
            "BULLISH": {},
            "BEARISH": {"RSI": {}, "MACD": {}},
            "NEUTRAL": {},
        }
        result = calculate_score(analysis)
        assert result.dominant_sentiment == "BEARISH"

    def test_dominant_neutral_when_equal(self):
        # Same weight both sides
        analysis = {
            "BULLISH": {"RSI": {}},
            "BEARISH": {"RSI": {}},
            "NEUTRAL": {},
        }
        result = calculate_score(analysis)
        assert result.dominant_sentiment == "NEUTRAL"
        assert result.confidence_pct == pytest.approx(50.0)

    def test_neutral_excluded_signal_does_not_score(self):
        # MAX_PAIN_ALIGNMENT is in NEUTRAL_EXCLUDE_FROM_SCORE
        analysis = {
            "BULLISH": {},
            "BEARISH": {},
            "NEUTRAL": {"MAX_PAIN_ALIGNMENT": {}},
        }
        result = calculate_score(analysis)
        assert result.base_score == 0.0

    def test_neutral_included_signal_does_score(self):
        # VOLUME is NOT in NEUTRAL_EXCLUDE_FROM_SCORE
        weight = constants.ANALYSIS_WEIGHTS["VOLUME"]
        analysis = {
            "BULLISH": {},
            "BEARISH": {},
            "NEUTRAL": {"VOLUME": {}},
        }
        result = calculate_score(analysis)
        assert result.base_score == pytest.approx(weight)

    def test_empty_analysis_returns_zero_score(self):
        analysis = {"BULLISH": {}, "BEARISH": {}, "NEUTRAL": {}}
        result = calculate_score(analysis)
        assert result.total_score == 0.0
        assert result.base_score == 0.0

    def test_score_result_is_dataclass(self):
        analysis = {"BULLISH": {"RSI": {}}, "BEARISH": {}, "NEUTRAL": {}}
        result = calculate_score(analysis)
        assert isinstance(result, ScoreResult)

    def test_total_score_rounded_to_2dp(self):
        analysis = {"BULLISH": {"RSI": {}}, "BEARISH": {}, "NEUTRAL": {}}
        result = calculate_score(analysis)
        # Should be rounded to 2 decimal places (no more than 2 places after dot)
        assert result.total_score == round(result.total_score, 2)


# ── should_notify ─────────────────────────────────────────────────────────────

class TestShouldNotify:
    def _analysis_with_score(self, target_score: float):
        """Build an analysis that produces approximately target_score via RSI only."""
        # Force a specific score by mocking calculate_score
        pass

    def test_below_min_score_returns_false(self):
        # Empty analysis → score 0
        analysis = {"BULLISH": {}, "BEARISH": {}, "NEUTRAL": {}}
        notify, result = should_notify(analysis)
        assert notify is False
        assert result.total_score < constants.MIN_NOTIFICATION_SCORE

    def test_below_65_pct_confidence_returns_false(self):
        # Nearly equal bullish/bearish → low confidence
        analysis = {
            "BULLISH": {"RSI": {}},       # RSI weight 15
            "BEARISH": {"RSI": {}},       # same weight → 50/50
            "NEUTRAL": {},
        }
        notify, result = should_notify(analysis)
        # confidence_pct = 50 < 65 → False
        assert notify is False

    def test_neutral_dominant_bypasses_confidence_gate(self):
        # score=0 → below MIN_NOTIFICATION_SCORE anyway; just confirm no TypeError
        analysis = {"BULLISH": {}, "BEARISH": {}, "NEUTRAL": {}}
        notify, _ = should_notify(analysis)
        assert isinstance(notify, bool)

    def test_meets_threshold_and_priority_returns_true(self):
        # Build a high-enough score by using heavy weight signals
        # PANIC_EXHAUSTION=25, RSI_DIVERGENCE=18, MACD=15 + alignment → >75
        analysis = {
            "BULLISH": {
                "PANIC_EXHAUSTION": {},    # 25 (OPTIONS)
                "RSI_DIVERGENCE": {},      # 18 (TECHNICAL)
                "MACD": {},                # 15 (TECHNICAL)
                "MAX_PAIN": {},            # 15 (OPTIONS)
            },
            "BEARISH": {},
            "NEUTRAL": {},
        }
        notify, result = should_notify(analysis)
        # With CONFIRMATION bonus (tech+options) → large score; should notify
        assert notify is True
        assert result.total_score >= constants.MIN_NOTIFICATION_SCORE

    def test_min_priority_high_blocks_low_score(self):
        # A minimal analysis that only reaches LOW priority
        analysis = {
            "BULLISH": {"RSI": {}},   # weight=15, ALL_BULLISH → 15*1.3=19.5 (LOW threshold=35 not met)
            "BEARISH": {},
            "NEUTRAL": {},
        }
        notify, _ = should_notify(analysis, min_priority=NotificationPriority.HIGH)
        assert notify is False


# ── format_score_message ──────────────────────────────────────────────────────

class TestFormatScoreMessage:
    def _make_result(self, priority=NotificationPriority.HIGH, score=95.0,
                     sentiment="BULLISH", confidence=80.0):
        return ScoreResult(
            total_score=score,
            base_score=70.0,
            alignment_bonus=25.0,
            priority=priority,
            signal_alignment="CONFIRMATION_BULLISH",
            breakdown={},
            dominant_sentiment=sentiment,
            confidence_pct=confidence,
        )

    def test_contains_stock_symbol(self):
        result = format_score_message("RELIANCE", self._make_result())
        assert "RELIANCE" in result

    def test_contains_score_value(self):
        result = format_score_message("NIFTY", self._make_result(score=95.0))
        assert "95" in result

    def test_contains_sentiment(self):
        result = format_score_message("HDFC", self._make_result(sentiment="BEARISH"))
        assert "BEARISH" in result

    def test_high_priority_has_fire_emoji(self):
        result = format_score_message("X", self._make_result(priority=NotificationPriority.HIGH))
        assert "🔥" in result

    def test_critical_priority_has_siren_emoji(self):
        result = format_score_message("X", self._make_result(priority=NotificationPriority.CRITICAL))
        assert "🚨" in result

    def test_medium_priority_has_chart_emoji(self):
        result = format_score_message("X", self._make_result(priority=NotificationPriority.MEDIUM))
        assert "📈" in result

    def test_low_priority_has_bar_chart_emoji(self):
        result = format_score_message("X", self._make_result(priority=NotificationPriority.LOW))
        assert "📊" in result

    def test_contains_priority_label(self):
        result = format_score_message("X", self._make_result(priority=NotificationPriority.CRITICAL))
        assert "CRITICAL" in result

    def test_contains_confidence_value(self):
        result = format_score_message("X", self._make_result(confidence=80.0))
        assert "80.0" in result
