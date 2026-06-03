#!/usr/bin/env python3
"""Stock and AI news Telegram agent.

The agent polls RSS feeds, uses OpenAI to score relevant items, sends Telegram
alerts, and tracks short-term ticker sentiment in local state files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


APP_NAME = "StockTelegramAgent"
DEFAULT_USER_AGENT = "StockTelegramAgent/1.0 (+https://github.com/shafeequealipt-dotcom/StockTelegramAgent)"


PORTFOLIO: dict[str, str] = {
    "TQQQ": "ProShares UltraPro QQQ",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet / Google",
    "CRM": "Salesforce",
    "PATH": "UiPath",
    "PYPL": "PayPal",
    "PLTR": "Palantir",
    "NFLX": "Netflix",
    "NOW": "ServiceNow",
    "INTC": "Intel",
    "MU": "Micron",
    "AAPL": "Apple",
    "TWLO": "Twilio",
    "META": "Meta",
    "ORCL": "Oracle",
    "SNDK": "SanDisk",
    "QQQ": "Invesco QQQ ETF",
    "PANW": "Palo Alto Networks",
    "TEAM": "Atlassian",
    "SAP": "SAP",
    "BABA": "Alibaba",
    "AMD": "AMD",
}


RSS_FEEDS: list[tuple[str, str]] = [
    ("Reuters Business", "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Tech", "https://feeds.reuters.com/reuters/technologyNews"),
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Tech", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("MarketWatch Top", "https://feeds.marketwatch.com/marketwatch/topstories"),
    ("MarketWatch Internet", "https://feeds.marketwatch.com/marketwatch/internet"),
    ("Investors Business", "https://www.investors.com/feed/"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("ZDNet AI", "https://www.zdnet.com/topic/artificial-intelligence/rss.xml"),
    ("HuggingFace Blog", "https://huggingface.co/blog/feed.xml"),
    ("Papers With Code", "https://paperswithcode.com/latest.rss"),
    ("Towards Data Science", "https://towardsdatascience.com/feed"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("OpenAI News", "https://openai.com/news/rss.xml"),
    ("Anthropic News", "https://www.anthropic.com/news/rss.xml"),
    ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss"),
    ("New Scientist Tech", "https://www.newscientist.com/subject/technology/feed/"),
]


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    openai_api_key: str
    openai_model: str
    poll_interval_seconds: int
    min_score: int
    bearish_threshold: int
    daily_digest_hour: int
    feed_lookback_hours: int
    request_timeout_seconds: int
    data_dir: Path
    log_dir: Path

    @property
    def seen_cache(self) -> Path:
        return self.data_dir / "seen.json"

    @property
    def sentiment_cache(self) -> Path:
        return self.data_dir / "sentiment.json"


def _get_int_env(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def load_settings() -> Settings:
    missing = [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return Settings(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        poll_interval_seconds=_get_int_env("POLL_INTERVAL_SECONDS", 1800, minimum=60),
        min_score=_get_int_env("MIN_SCORE", 6, minimum=1, maximum=10),
        bearish_threshold=_get_int_env("BEARISH_THRESHOLD", 3, minimum=1),
        daily_digest_hour=_get_int_env("DAILY_DIGEST_HOUR", 8, minimum=0, maximum=23),
        feed_lookback_hours=_get_int_env("FEED_LOOKBACK_HOURS", 2, minimum=1),
        request_timeout_seconds=_get_int_env("REQUEST_TIMEOUT_SECONDS", 10, minimum=1),
        data_dir=Path(os.getenv("AGENT_DATA_DIR", "data")),
        log_dir=Path(os.getenv("AGENT_LOG_DIR", "logs")),
    )


class RedactingFilter(logging.Filter):
    """Remove credentials from log messages before handlers write them."""

    TOKEN_PATTERNS = [
        re.compile(r"/bot[0-9]{6,}:[A-Za-z0-9_-]+"),
        re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    ]

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        for pattern in self.TOKEN_PATTERNS:
            message = pattern.sub("/bot[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def setup_logging(settings: Settings) -> logging.Logger:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.log_dir / "agent.log"

    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    redactor = RedactingFilter([settings.telegram_bot_token, settings.openai_api_key])

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redactor)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp_file:
        json.dump(data, tmp_file, indent=2, sort_keys=True)
        tmp_name = tmp_file.name
    Path(tmp_name).replace(path)


def article_id(entry: Any) -> str:
    raw_id = entry.get("id") or entry.get("link") or entry.get("title") or ""
    return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()


class StockTelegramAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = setup_logging(settings)
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        self.openai = OpenAI(api_key=settings.openai_api_key)
        self.paused_until: datetime | None = None
        self.paused_lock = threading.Lock()
        self.last_update_id = 0
        self.digest_articles: list[dict[str, Any]] = []
        self.last_digest_date: str | None = None

    def load_seen(self) -> set[str]:
        return set(load_json(self.settings.seen_cache, []))

    def save_seen(self, seen: set[str]) -> None:
        save_json(self.settings.seen_cache, sorted(seen)[-2000:])

    def load_sentiment(self) -> dict[str, list[dict[str, str]]]:
        return load_json(self.settings.sentiment_cache, {})

    def save_sentiment(self, sentiment: dict[str, list[dict[str, str]]]) -> None:
        save_json(self.settings.sentiment_cache, sentiment)

    def get_price(self, ticker: str) -> dict[str, Any]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        try:
            response = self.http.get(
                url,
                params={"interval": "1d", "range": "2d"},
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            result = data.get("chart", {}).get("result") or []
            if not result:
                return {"error": True}
            meta = result[0].get("meta", {})
            price = round(float(meta.get("regularMarketPrice", 0)), 2)
            previous = round(float(meta.get("previousClose", price)), 2)
            change = round(price - previous, 2)
            change_pct = round((change / previous) * 100, 2) if previous else 0
            return {
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "currency": meta.get("currency", "USD"),
                "market": meta.get("marketState", "UNKNOWN"),
                "error": False,
            }
        except requests.RequestException as exc:
            self.log.warning("Price fetch failed for %s: %s", ticker, exc)
            return {"error": True}

    def price_line(self, ticker: str) -> str:
        price = self.get_price(ticker)
        if price.get("error"):
            return ""
        sign = "+" if price["change"] >= 0 else ""
        market = "After-hours" if price["market"] != "REGULAR" else "Market open"
        return f"{ticker}: ${price['price']} ({sign}{price['change_pct']}%) | {market}"

    def record_sentiment(self, tickers: list[str], impact: str) -> None:
        sentiment = self.load_sentiment()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=7)

        for ticker in tickers:
            entries = sentiment.setdefault(ticker, [])
            entries.append({"impact": impact, "time": now.isoformat()})
            sentiment[ticker] = [
                entry
                for entry in entries
                if datetime.fromisoformat(entry["time"]) > cutoff
            ]

        self.save_sentiment(sentiment)

    def sentiment_warnings(self, tickers: list[str]) -> list[tuple[str, int]]:
        sentiment = self.load_sentiment()
        warnings: list[tuple[str, int]] = []
        for ticker in tickers:
            entries = sentiment.get(ticker, [])
            bearish = sum(1 for entry in entries if entry.get("impact") == "BEARISH")
            if bearish >= self.settings.bearish_threshold:
                warnings.append((ticker, bearish))
        return warnings

    def maybe_send_digest(self) -> None:
        now = datetime.now()
        today = now.date().isoformat()
        if now.hour != self.settings.daily_digest_hour or self.last_digest_date == today:
            return

        self.last_digest_date = today
        if not self.digest_articles:
            self.send_telegram("Daily AI Market Digest\nNo significant news in the past 24 hours.")
            return

        top_articles = sorted(
            self.digest_articles,
            key=lambda item: int(item.get("relevance_score", 0)),
            reverse=True,
        )[:7]
        lines = [f"Daily AI Market Digest - {today}", ""]
        for article in top_articles:
            tickers = " ".join(f"${ticker}" for ticker in article.get("affected_tickers", []))
            lines.append(
                f"{article.get('impact', 'NEUTRAL')} | {article.get('relevance_score')}/10 | "
                f"{tickers or 'General/AI'}"
            )
            lines.append(str(article.get("title", ""))[:120])
            lines.append(str(article.get("one_line", ""))[:160])
            lines.append("")

        lines.append("Morning price snapshot")
        for ticker in ["NVDA", "MSFT", "INTC", "META", "AMD", "NOW"]:
            price = self.price_line(ticker)
            if price:
                lines.append(price)

        self.send_telegram("\n".join(lines).strip())
        self.digest_articles = []

    def fetch_articles(self) -> list[dict[str, Any]]:
        articles: list[dict[str, Any]] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.settings.feed_lookback_hours)

        for source, url in RSS_FEEDS:
            try:
                feed = feedparser.parse(url)
                if getattr(feed, "bozo", False):
                    self.log.warning("Feed parse warning for %s", source)

                for entry in feed.entries[:10]:
                    published = self._published_at(entry)
                    if published and published < cutoff:
                        continue
                    articles.append(
                        {
                            "id": article_id(entry),
                            "source": source,
                            "title": entry.get("title", "").strip(),
                            "summary": entry.get("summary", "").strip()[:400],
                            "link": entry.get("link", "").strip(),
                            "published": published.isoformat() if published else "unknown",
                        }
                    )
            except Exception as exc:
                self.log.warning("Feed fetch failed for %s: %s", source, exc)

        return articles

    @staticmethod
    def _published_at(entry: Any) -> datetime | None:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed:
            return None
        return datetime(*parsed[:6], tzinfo=timezone.utc)

    def analyse(self, articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not articles:
            return []

        portfolio = "\n".join(f"- {ticker}: {name}" for ticker, name in PORTFOLIO.items())
        results: list[dict[str, Any]] = []

        for start in range(0, len(articles), 10):
            batch = articles[start : start + 10]
            article_text = "\n\n".join(
                f"[{index + 1}] {article['source']}: {article['title']}\n{article['summary']}"
                for index, article in enumerate(batch)
            )
            prompt = (
                "You are an equity analyst and AI research tracker.\n\n"
                f"Portfolio:\n{portfolio}\n\n"
                f"Articles:\n{article_text}\n\n"
                "Return a JSON array only. Each object must contain: index, relevance_score "
                "(integer 1-10), affected_tickers (list from the portfolio or empty list), "
                "impact (BULLISH, BEARISH, or NEUTRAL), one_line, urgency "
                "(HIGH, MEDIUM, or LOW), and category (MARKET, AI_LLM, HUGGINGFACE, "
                "INVENTION, or OTHER). Only include articles with relevance_score >= 6."
            )

            try:
                response = self.openai.chat.completions.create(
                    model=self.settings.openai_model,
                    max_tokens=1500,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": "Return valid JSON only. No markdown."},
                        {"role": "user", "content": prompt},
                    ],
                )
                content = response.choices[0].message.content or "[]"
                for item in self._parse_analysis(content):
                    index = int(item.get("index", 0)) - 1
                    if 0 <= index < len(batch):
                        results.append({**batch[index], **item})
            except Exception as exc:
                self.log.error("OpenAI analysis failed: %s", exc)

        results.sort(key=lambda item: int(item.get("relevance_score", 0)), reverse=True)
        return results

    def _parse_analysis(self, content: str) -> list[dict[str, Any]]:
        cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            self.log.warning("OpenAI returned invalid JSON: %s", exc)
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    def send_telegram(self, message: str) -> None:
        try:
            response = self.http.post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": message[:4096],
                    "disable_web_page_preview": False,
                },
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            self.log.info("Telegram message sent")
        except requests.RequestException as exc:
            self.log.error("Telegram send failed: %s", exc)

    def format_alert(self, article: dict[str, Any]) -> str:
        tickers = article.get("affected_tickers", [])
        ticker_text = " ".join(f"${ticker}" for ticker in tickers) or "General/AI"

        lines = [
            f"AI STOCK & TECH ALERT - {article.get('urgency', 'LOW')}",
            f"{article.get('title', '')}",
            f"Source: {article.get('source', '')}",
            f"Impact: {article.get('impact', 'NEUTRAL')} | Score: {article.get('relevance_score')}/10",
            f"Holdings: {ticker_text}",
            f"Summary: {article.get('one_line', '')}",
        ]

        price_lines = [line for ticker in tickers[:3] if (line := self.price_line(ticker))]
        if price_lines:
            lines.extend(["", "Prices", *price_lines])

        warning_lines = [
            f"Sentiment warning: ${ticker} has {count} bearish alerts this week."
            for ticker, count in self.sentiment_warnings(tickers)
        ]
        if warning_lines:
            lines.extend(["", *warning_lines])

        lines.extend(["", f"Read more: {article.get('link', '')}", f"Published: {article.get('published', '')}"])
        return "\n".join(lines)

    def handle_commands(self) -> None:
        try:
            response = self.http.get(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/getUpdates",
                params={"offset": self.last_update_id + 1, "timeout": 5},
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            updates = response.json().get("result", [])
        except requests.RequestException as exc:
            self.log.error("Telegram command polling failed: %s", exc)
            return

        for update in updates:
            self.last_update_id = update.get("update_id", self.last_update_id)
            message = update.get("message", {})
            text = message.get("text", "").strip()
            command = text.lower()
            chat_id = str(message.get("chat", {}).get("id", ""))

            if chat_id != self.settings.telegram_chat_id:
                continue

            if command == "/portfolio":
                self.send_portfolio()
            elif command.startswith("/news"):
                self.send_news_for_ticker(text)
            elif command.startswith("/score"):
                self.send_score_for_ticker(text)
            elif command.startswith("/pause"):
                with self.paused_lock:
                    self.paused_until = datetime.now(timezone.utc) + timedelta(hours=2)
                self.send_telegram("Alerts paused for 2 hours. Send /resume to restart early.")
            elif command == "/resume":
                with self.paused_lock:
                    self.paused_until = None
                self.send_telegram("Alerts resumed.")
            elif command in ("/help", "/start"):
                self.send_help()

    def send_portfolio(self) -> None:
        lines = ["Your portfolio - live prices"]
        for ticker in list(PORTFOLIO.keys())[:15]:
            lines.append(self.price_line(ticker) or f"{ticker}: price unavailable")
        self.send_telegram("\n".join(lines))

    def send_news_for_ticker(self, text: str) -> None:
        parts = text.split()
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            self.send_telegram("Usage: /news NVDA")
            return

        self.send_telegram(f"Fetching latest news for {ticker}...")
        articles = self.fetch_articles()
        relevant = [
            article
            for article in articles
            if ticker.lower() in (article["title"] + article["summary"]).lower()
        ]
        if not relevant:
            self.send_telegram(f"No recent news found for {ticker} in current feeds.")
            return

        scored = self.analyse(relevant[:5])
        self.send_telegram(self.format_alert(scored[0]) if scored else f"Latest: {relevant[0]['title']}\n{relevant[0]['link']}")

    def send_score_for_ticker(self, text: str) -> None:
        parts = text.split()
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            self.send_telegram("Usage: /score NVDA")
            return

        entries = self.load_sentiment().get(ticker, [])
        bullish = sum(1 for entry in entries if entry.get("impact") == "BULLISH")
        bearish = sum(1 for entry in entries if entry.get("impact") == "BEARISH")
        neutral = sum(1 for entry in entries if entry.get("impact") == "NEUTRAL")
        mood = "Bullish" if bullish > bearish else ("Bearish" if bearish > bullish else "Neutral")
        warning = "WARNING: High bearish activity." if bearish >= self.settings.bearish_threshold else "Sentiment normal."

        lines = [
            f"Sentiment report - ${ticker}",
            self.price_line(ticker),
            f"Last 7 days: {len(entries)} alerts",
            f"Bullish: {bullish} | Bearish: {bearish} | Neutral: {neutral}",
            f"Overall mood: {mood}",
            warning,
        ]
        self.send_telegram("\n".join(line for line in lines if line))

    def send_help(self) -> None:
        self.send_telegram(
            "AI Stock & Tech News Agent commands\n"
            "/portfolio - live prices for holdings\n"
            "/news NVDA - latest news for a ticker\n"
            "/score NVDA - 7-day sentiment report\n"
            "/pause - pause alerts for 2 hours\n"
            "/resume - resume alerts\n"
            "/help - show this menu"
        )

    def is_paused(self) -> bool:
        with self.paused_lock:
            return self.paused_until is not None and datetime.now(timezone.utc) < self.paused_until

    def run(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.log.info("Stock Telegram Agent started")
        self.send_telegram(
            f"Stock Telegram Agent is live\n"
            f"Watching {len(PORTFOLIO)} symbols across {len(RSS_FEEDS)} feeds.\n"
            "Send /help to see commands."
        )

        seen = self.load_seen()
        while True:
            self.handle_commands()
            self.maybe_send_digest()

            if self.is_paused():
                self.log.info("Agent is paused; sleeping 60 seconds")
                time.sleep(60)
                continue

            self.log.info("Fetching news")
            articles = self.fetch_articles()
            new_articles = [article for article in articles if article["id"] not in seen]
            self.log.info("%s new articles", len(new_articles))

            if new_articles:
                alerts = self.analyse(new_articles)
                relevant = [
                    alert
                    for alert in alerts
                    if int(alert.get("relevance_score", 0)) >= self.settings.min_score
                ]
                self.log.info("%s alerts to send", len(relevant))
                self.digest_articles.extend(relevant)

                for alert in relevant:
                    self.record_sentiment(
                        alert.get("affected_tickers", []),
                        alert.get("impact", "NEUTRAL"),
                    )
                    self.send_telegram(self.format_alert(alert))
                    time.sleep(1.5)

                seen.update(article["id"] for article in new_articles)
                self.save_seen(seen)

            self.log.info("Sleeping %s seconds", self.settings.poll_interval_seconds)
            time.sleep(self.settings.poll_interval_seconds)


def main() -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    StockTelegramAgent(settings).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
