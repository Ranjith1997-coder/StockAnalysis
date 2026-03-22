"""
LLM client abstraction — pluggable interface with Gemini Flash as primary.

Free tier: 15 RPM, 1M tokens/day.
Typical usage: ~20-30 calls/day × 1.3K tokens = ~35K tokens (3.5% of budget).
"""

from __future__ import annotations
import os
import json
import requests
from abc import ABC, abstractmethod
from datetime import date
from threading import Lock

from common.logging_util import logger


class LLMClient(ABC):
    """Interface for LLM providers."""

    @abstractmethod
    def generate(self, system: str, prompt: str) -> str | None:
        """Generate a response. Returns None on failure (caller should handle gracefully)."""
        ...


class GeminiClient(LLMClient):
    """
    Google Gemini Flash — free tier via Google AI Studio.

    Requires GEMINI_API_KEY in .env (get from https://aistudio.google.com/apikey).
    """

    MODEL = "gemini-2.5-flash"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
    TIMEOUT = 15  # seconds
    MAX_OUTPUT_TOKENS = 6000
    DAILY_TOKEN_LIMIT = 900_000  # leave 10% buffer

    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY", "")
        self._daily_tokens = 0
        self._daily_date = date.today()
        self._lock = Lock()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def generate(self, system: str, prompt: str) -> str | None:
        if not self.available:
            return None

        # Reset daily counter at midnight
        with self._lock:
            if date.today() != self._daily_date:
                self._daily_tokens = 0
                self._daily_date = date.today()
            if self._daily_tokens >= self.DAILY_TOKEN_LIMIT:
                logger.warning("[Gemini] Daily token limit reached, skipping")
                return None

        url = f"{self.ENDPOINT}/{self.MODEL}:generateContent?key={self.api_key}"

        payload = {
            "system_instruction": {
                "parts": [{"text": system}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "maxOutputTokens": self.MAX_OUTPUT_TOKENS,
                "temperature": 0.3,
            },
        }

        try:
            resp = requests.post(url, json=payload, timeout=self.TIMEOUT)
            if resp.status_code != 200:
                logger.error(f"[Gemini] API error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            candidate = data["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            finish_reason = candidate.get("finishReason", "UNKNOWN")

            # Track token usage
            usage = data.get("usageMetadata", {})
            total_tokens = usage.get("totalTokenCount", 0)
            prompt_tokens = usage.get("promptTokenCount", 0)
            output_tokens = usage.get("candidatesTokenCount", 0)
            with self._lock:
                self._daily_tokens += total_tokens

            logger.info(f"[Gemini] finish={finish_reason} prompt={prompt_tokens} output={output_tokens} daily={self._daily_tokens}")
            if finish_reason == "MAX_TOKENS":
                logger.warning("[Gemini] Response hit MAX_TOKENS limit — consider increasing MAX_OUTPUT_TOKENS")
            return text.strip()

        except requests.Timeout:
            logger.warning("[Gemini] Request timed out")
            return None
        except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"[Gemini] Request failed: {e}")
            return None

    @property
    def daily_tokens_used(self) -> int:
        return self._daily_tokens
