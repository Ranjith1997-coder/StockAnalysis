"""
Scoring-based notification system for stock analysis.

This module provides a dynamic scoring mechanism to determine notification priority
based on the strength and alignment of various analysis signals.
"""

import common.constants as constants
from common.logging_util import logger
from enum import Enum
from typing import Dict, Tuple, Optional
from dataclasses import dataclass


class NotificationPriority(Enum):
    """Notification priority levels."""
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class ScoreResult:
    """Result of score calculation."""
    total_score: float
    base_score: float
    alignment_bonus: float
    priority: NotificationPriority
    signal_alignment: str
    breakdown: Dict[str, float]
    dominant_sentiment: str
    confidence_pct: float


def get_analysis_weight(analysis_type: str) -> int:
    """
    Get the weight for a given analysis type.
    
    Args:
        analysis_type: The type of analysis (e.g., 'RSI', 'MACD', 'MAX_PAIN')
    
    Returns:
        Weight value for the analysis type
    """
    return constants.ANALYSIS_WEIGHTS.get(
        analysis_type, 
        constants.ANALYSIS_WEIGHTS.get("DEFAULT", 10)
    )


def calculate_alignment_bonus(analysis: Dict) -> Tuple[float, str]:
    """
    Calculate bonus multiplier based on signal alignment.
    
    Args:
        analysis: Stock analysis dictionary containing BULLISH, BEARISH, NEUTRAL
    
    Returns:
        Tuple of (multiplier, alignment_type)
    """
    bullish_count = len(analysis.get("BULLISH", {}))
    bearish_count = len(analysis.get("BEARISH", {}))
    neutral_count = len(analysis.get("NEUTRAL", {}))
    
    total_directional = bullish_count + bearish_count
    
    if total_directional == 0:
        return 1.0, "NONE"
    
    # Check for all-aligned signals
    if bullish_count > 0 and bearish_count == 0:
        # Check if technical and options signals both present
        has_technical = any(
            key in constants.TECHNICAL_ANALYSES 
            for key in analysis.get("BULLISH", {}).keys()
        )
        has_options = any(
            key in constants.OPTIONS_ANALYSES 
            for key in analysis.get("BULLISH", {}).keys()
        )
        
        if has_technical and has_options:
            return constants.SIGNAL_ALIGNMENT_BONUS["CONFIRMATION"], "CONFIRMATION_BULLISH"
        return constants.SIGNAL_ALIGNMENT_BONUS["ALL_BULLISH"], "ALL_BULLISH"
    
    elif bearish_count > 0 and bullish_count == 0:
        has_technical = any(
            key in constants.TECHNICAL_ANALYSES 
            for key in analysis.get("BEARISH", {}).keys()
        )
        has_options = any(
            key in constants.OPTIONS_ANALYSES 
            for key in analysis.get("BEARISH", {}).keys()
        )
        
        if has_technical and has_options:
            return constants.SIGNAL_ALIGNMENT_BONUS["CONFIRMATION"], "CONFIRMATION_BEARISH"
        return constants.SIGNAL_ALIGNMENT_BONUS["ALL_BEARISH"], "ALL_BEARISH"
    
    return constants.SIGNAL_ALIGNMENT_BONUS["MIXED"], "MIXED"


def determine_priority(score: float) -> NotificationPriority:
    """
    Determine notification priority based on score.
    
    Args:
        score: Total calculated score
    
    Returns:
        NotificationPriority enum value
    """
    if score >= constants.NOTIFICATION_PRIORITY["CRITICAL"]:
        return NotificationPriority.CRITICAL
    elif score >= constants.NOTIFICATION_PRIORITY["HIGH"]:
        return NotificationPriority.HIGH
    elif score >= constants.NOTIFICATION_PRIORITY["MEDIUM"]:
        return NotificationPriority.MEDIUM
    elif score >= constants.NOTIFICATION_PRIORITY["LOW"]:
        return NotificationPriority.LOW
    return NotificationPriority.NONE


def calculate_score(analysis: Dict) -> ScoreResult:
    """
    Calculate the total score for a stock's analysis.
    
    Args:
        analysis: Stock analysis dictionary with structure:
                  {
                      "BULLISH": {analysis_type: data, ...},
                      "BEARISH": {analysis_type: data, ...},
                      "NEUTRAL": {analysis_type: data, ...},
                      "NoOfTrends": int
                  }
    
    Returns:
        ScoreResult with detailed breakdown
    """
    breakdown = {}
    base_score = 0.0
    bullish_score = 0.0
    bearish_score = 0.0
    
    # Calculate scores for BULLISH and BEARISH signals
    for sentiment in ["BULLISH", "BEARISH"]:
        if sentiment not in analysis:
            continue
            
        for analysis_type, data in analysis[sentiment].items():
            weight = get_analysis_weight(analysis_type)
            
            # Handle list of multiple signals of same type
            if isinstance(data, list):
                # Multiple signals of same type - diminishing returns
                count = len(data)
                score = weight * (1 + 0.3 * (count - 1))  # Each additional adds 30%
            else:
                score = weight
            
            breakdown[f"{sentiment}:{analysis_type}"] = score
            base_score += score
            
            if sentiment == "BULLISH":
                bullish_score += score
            elif sentiment == "BEARISH":
                bearish_score += score
    
    # Calculate scores for NEUTRAL signals (excluding uncertain/divergent ones)
    if "NEUTRAL" in analysis:
        for analysis_type, data in analysis["NEUTRAL"].items():
            # Skip signals that indicate uncertainty
            if analysis_type in constants.NEUTRAL_EXCLUDE_FROM_SCORE:
                continue
            
            weight = get_analysis_weight(analysis_type)
            
            if isinstance(data, list):
                count = len(data)
                score = weight * (1 + 0.3 * (count - 1))
            else:
                score = weight
            
            breakdown[f"NEUTRAL:{analysis_type}"] = score
            base_score += score
    
    # Calculate alignment bonus
    alignment_multiplier, alignment_type = calculate_alignment_bonus(analysis)
    alignment_bonus = base_score * (alignment_multiplier - 1)
    
    total_score = base_score * alignment_multiplier
    
    # Determine dominant sentiment
    if bullish_score > bearish_score:
        dominant_sentiment = "BULLISH"
        confidence_pct = (bullish_score / (bullish_score + bearish_score) * 100) if (bullish_score + bearish_score) > 0 else 0
    elif bearish_score > bullish_score:
        dominant_sentiment = "BEARISH"
        confidence_pct = (bearish_score / (bullish_score + bearish_score) * 100) if (bullish_score + bearish_score) > 0 else 0
    else:
        dominant_sentiment = "NEUTRAL"
        confidence_pct = 50.0
    
    priority = determine_priority(total_score)
    
    return ScoreResult(
        total_score=round(total_score, 2),
        base_score=round(base_score, 2),
        alignment_bonus=round(alignment_bonus, 2),
        priority=priority,
        signal_alignment=alignment_type,
        breakdown=breakdown,
        dominant_sentiment=dominant_sentiment,
        confidence_pct=round(confidence_pct, 1)
    )


def should_notify(analysis: Dict, min_priority: NotificationPriority = NotificationPriority.LOW) -> Tuple[bool, ScoreResult]:
    """
    Determine if a notification should be sent based on analysis score.
    
    Args:
        analysis: Stock analysis dictionary
        min_priority: Minimum priority level required to notify
    
    Returns:
        Tuple of (should_notify: bool, score_result: ScoreResult)
    """
    score_result = calculate_score(analysis)
    
    # Check minimum score threshold
    if score_result.total_score < constants.MIN_NOTIFICATION_SCORE:
        return False, score_result
    
    # Check priority threshold
    priority_order = [
        NotificationPriority.NONE,
        NotificationPriority.LOW,
        NotificationPriority.MEDIUM,
        NotificationPriority.HIGH,
        NotificationPriority.CRITICAL
    ]
    
    if priority_order.index(score_result.priority) >= priority_order.index(min_priority):
        return True, score_result
    
    return False, score_result


def should_notify_legacy(analysis: Dict) -> bool:
    """
    Legacy notification check using REQUIRED_TRENDS.
    Maintained for backward compatibility.
    
    Args:
        analysis: Stock analysis dictionary
    
    Returns:
        True if NoOfTrends >= REQUIRED_TRENDS
    """
    return analysis.get("NoOfTrends", 0) >= constants.REQUIRED_TRENDS


def format_score_message(stock_symbol: str, score_result: ScoreResult) -> str:
    """
    Format a score-based notification message.
    
    Args:
        stock_symbol: Symbol of the stock
        score_result: ScoreResult from calculate_score
    
    Returns:
        Formatted message string
    """
    priority_emoji = {
        NotificationPriority.LOW: "📊",
        NotificationPriority.MEDIUM: "📈",
        NotificationPriority.HIGH: "🔥",
        NotificationPriority.CRITICAL: "🚨"
    }
    
    emoji = priority_emoji.get(score_result.priority, "📊")
    
    message_parts = [
        f"{emoji} [{score_result.priority.value}] {stock_symbol}",
        f"Score: {score_result.total_score} (Base: {score_result.base_score}, Bonus: +{score_result.alignment_bonus})",
        f"Sentiment: {score_result.dominant_sentiment} ({score_result.confidence_pct}% confidence)",
        f"Signal Alignment: {score_result.signal_alignment}"
    ]
    
    return "\n".join(message_parts)
