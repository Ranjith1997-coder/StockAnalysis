import os
import json
import hashlib
import feedparser
import threading
from datetime import datetime
from services.common.logging import get_logger
logger = get_logger("sentiment")

from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch
import numpy as np

class FinBertSentiment:
    def __init__(self):
        self.tokenizer = AutoTokenizer.from_pretrained("yiyanghkust/finbert-tone")
        self.model = AutoModelForSequenceClassification.from_pretrained("yiyanghkust/finbert-tone")
        self.labels = ["negative", "neutral", "positive"]

    def polarity_scores(self, text):
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True)
        with torch.no_grad():
            outputs = self.model(**inputs)
            scores = torch.softmax(outputs.logits, dim=1).numpy()[0]
        # Map to VADER-like keys for compatibility
        return {
            "neg": float(scores[0]),
            "neu": float(scores[1]),
            "pos": float(scores[2]),
            "compound": float(scores[2]) - float(scores[0])  # positive - negative
        }

finbert_sia = FinBertSentiment()

class NewsSentimentManager:
    def __init__(self, seen_file="seen_news_hashes.json"):
        self.seen_file = seen_file
        self.seen_hashes = self._load_seen_hashes()
        self.new_hashes = set()  # Hashes seen in this session
        self.lock = threading.Lock()  # For thread safety

    def _load_seen_hashes(self):
        if os.path.exists(self.seen_file):
            with open(self.seen_file, "r") as f:
                logger.info(f"Loaded seen news hashes from {self.seen_file}")
                return set(json.load(f))
        logger.info("No seen news hash file found, starting fresh.")
        return set()

    def save_session_hashes(self):
        # Merge new hashes into seen_hashes and save at end of session
        all_hashes = self.seen_hashes.union(self.new_hashes)
        with open(self.seen_file, "w") as f:
            json.dump(list(all_hashes), f)
        logger.info(f"Saved {len(all_hashes)} news hashes to {self.seen_file}")

    @staticmethod
    def fetch_news(ticker, max_articles=10):
        url = f"https://news.google.com/rss/search?q={ticker}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(url)
        fresh = []
        for entry in getattr(feed, "entries", [])[:max_articles]:
            h = hashlib.md5((entry.title + entry.link).encode()).hexdigest()
            sentiment = finbert_sia.polarity_scores(entry.title)
            fresh.append({
                "ticker": ticker,
                "title": entry.title,
                "link": entry.link,
                "published": getattr(entry, "published", ""),
                "sentiment": sentiment,
                "hash": h
            })
        return fresh

    @staticmethod
    def summarize_sentiment(news_items):
        if not news_items:
            return {"avg_compound": 0.0, "count": 0}
        compounds = [item["sentiment"]["compound"] for item in news_items]
        avg = sum(compounds) / len(compounds)
        return {"avg_compound": avg, "count": len(compounds)}

    def process_stock_news(self, stock):
        """Call this from each stock thread. Updates stock with news & sentiment, logs, and returns new strong news."""
        news_items = self.fetch_news(stock.stock_symbol)
        new_news = []
        with self.lock:
            for n in news_items:
                if n['hash'] not in self.seen_hashes and n['hash'] not in self.new_hashes:
                    self.new_hashes.add(n['hash'])
                    new_news.append(n)
        if news_items:
            sentiment_summary = self.summarize_sentiment(news_items)
            stock.set_news_sentiment(sentiment_summary, news_items)
            logger.info(f"[{stock.stock_symbol}] News sentiment updated: avg_compound={sentiment_summary['avg_compound']:+.2f} ({sentiment_summary['count']} articles)")
        for n in new_news:
            if abs(n['sentiment']['compound']) > 0.5:
                logger.info(f"[{stock.stock_symbol}] Strong news: {n['title']} | Sentiment: {n['sentiment']['compound']:+.2f}")
        return new_news  # List of new news dicts with strong sentiment

    def end_of_day_summary(self, stock_list):
        """Return a summary of average sentiment for each stock."""
        summary = []
        for stock in stock_list:
            news_items = getattr(stock, "news_items", [])
            sentiment_summary = self.summarize_sentiment(news_items)
            summary.append({
                "symbol": stock.stock_symbol,
                "avg_compound": sentiment_summary["avg_compound"],
                "count": sentiment_summary["count"]
            })
        return summary