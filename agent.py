#!/usr/bin/env python3
"""Stock and AI news Telegram agent.

The agent polls RSS feeds, uses an OpenRouter-hosted model to score relevant
items, sends Telegram alerts, and tracks short-term ticker sentiment in local
state files. The active model can be switched at runtime with the /model
Telegram command.
"""

from __future__ import annotations

import hashlib
import io
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

import social

load_dotenv()


APP_NAME = "StockTelegramAgent"
DEFAULT_USER_AGENT = "StockTelegramAgent/1.0 (+https://github.com/shafeequealipt-dotcom/StockTelegramAgent)"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Yahoo's crumb/quoteSummary endpoints rate-limit non-browser User-Agents, so
# the analyst calls present a browser UA (price/chart endpoints are unaffected).
YAHOO_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_MODEL_CHOICES = [
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-4.5",
    "google/gemini-2.5-flash",
    "deepseek/deepseek-chat-v3.1",
    "meta-llama/llama-3.3-70b-instruct",
    # Free-tier models (no OpenRouter credits required)
    "google/gemma-4-31b-it:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "openai/gpt-oss-120b:free",
]
DEFAULT_ANALYSIS_INSTRUCTIONS = (
    "You are a markets and macro analyst. Surface anything that can move US "
    "equities or the portfolio, whether or not it names a specific ticker. "
    "Prioritize: Federal Reserve decisions, interest rate moves, inflation and "
    "jobs data; US political and policy news (White House, Trump administration, "
    "tariffs, trade, regulation, fiscal policy); global and geopolitical events "
    "(wars, OPEC, China, trade disputes, elections) that affect markets; direct "
    "portfolio impact; competitive AI developments, model releases, semiconductor "
    "supply chain, cloud AI adoption, earnings, and major product announcements. "
    "Broad market-moving macro/political/global news with no specific ticker is "
    "still highly relevant — score it on market impact, not ticker mentions."
)
ANALYSIS_SYSTEM_PROMPT = (
    "You are a careful equity analyst and AI research tracker. "
    "Return valid JSON only. No markdown or commentary."
)
QA_SYSTEM_PROMPT = (
    "You are a sharp, honest equity and AI market assistant chatting on Telegram. "
    "Use the live market data, per-stock statistics, and news headlines supplied "
    "in the user message as your primary evidence. You may add well-known "
    "background knowledge (business model, segments, competitors), but clearly "
    "separate it from the supplied live data and never invent prices, numbers, or "
    "news. Answer directly and concisely in plain text suitable for Telegram: no "
    "markdown tables, no headers, short paragraphs or simple dashes for lists. "
    "This is not financial advice and you should note that only when the user "
    "asks for buy/sell decisions."
)
TICKER_EXTRACTION_PROMPT = (
    "List the stock ticker symbols for any public companies, ETFs, or market "
    "indexes mentioned in the question below. Resolve company names to their "
    "primary US-listed ticker (for example 'arm' or 'arm holdings' -> ARM, "
    "'palantir' -> PLTR, 'nasdaq index' -> ^IXIC). Ignore words that merely look "
    "like tickers but are used as plain English. Return a JSON array of ticker "
    "strings only, [] if none.\n\nQuestion: "
)
INSTITUTIONAL_REPORT_SYSTEM_PROMPT = (
    "Use only the evidence supplied in the user message. Do not invent prices, "
    "flows, filings, earnings details, or macro data. If a requested section is "
    "not supported by supplied evidence, say 'Not available from supplied evidence'. "
    "Separate facts from assumptions."
)
INSTITUTIONAL_REPORT_PROMPT = """
You are a Senior Hedge Fund Analyst, Macro Strategist, and Institutional Flow Analyst.

Your objective is to identify where institutional money is moving, which sectors are
gaining or losing leadership, and which stocks have the highest probability of
outperforming over the next 3-5 years.

Use only verifiable information from:
- Reuters
- SEC EDGAR Filings
- Federal Reserve releases
- Earnings reports and conference calls
- Company investor presentations
- Government economic data (BLS, EIA, Census, Treasury)
- Major fund flow reports
- Industry reports
- High-quality financial news sources

Ignore social media hype, influencer opinions, and unverified rumors.

==================================================
PART 1 - MARKET SUMMARY
==================================================

Provide a concise market summary including:
- Major index performance (S&P 500, Nasdaq, Dow, Russell 2000)
- Treasury yield movements
- Dollar Index movement
- Oil, Natural Gas, Copper, Gold, Silver performance
- Major macro events affecting markets
- Key economic releases
- Federal Reserve developments

Explain why the market moved.

==================================================
PART 2 - NEWS ANALYSIS
==================================================

Identify:
Top 10 Bullish News Events Today
Top 10 Bearish News Events Today

For each:
- Headline
- Stocks affected
- Sector affected
- Why it matters
- Expected impact (Low / Medium / High)

==================================================
PART 3 - SECTOR ROTATION ANALYSIS
==================================================

Identify sectors experiencing strong inflows, moderate inflows, neutral flows,
moderate outflows, and strong outflows. Include evidence, stocks benefiting or
affected, and institutional rationale where evidence supports it.

Determine whether money is rotating into growth, value, cyclicals, defensives,
small caps, or large caps. Explain why.

==================================================
PART 4 - SMART MONEY DETECTION
==================================================

Detect evidence of institutional accumulation, institutional distribution, profit
taking, hedge fund positioning, ETF inflows/outflows, insider buying, insider
selling, analyst upgrades, analyst downgrades, and unusual volume activity.
Rank conviction as Low, Medium, or High.

==================================================
PART 5 - AI THEME ANALYSIS
==================================================

Classify AI opportunities into:
- FIRST-ORDER BENEFICIARIES: direct AI winners such as AI chips, accelerators,
  GPU manufacturers, and AI software leaders.
- SECOND-ORDER BENEFICIARIES: infrastructure providers such as data centers,
  networking, power equipment, cooling systems, electrical equipment, and cloud
  infrastructure.
- THIRD-ORDER BENEFICIARIES: indirect beneficiaries such as copper, utilities,
  construction, industrial automation, engineering firms, and energy infrastructure.

For each category include top companies, current catalysts, revenue drivers,
risks, and estimated growth runway.

==================================================
PART 6 - EMERGING THEMES
==================================================

Compare today's information with the previous 30 days. Identify themes
accelerating, slowing, dying, and newly emerging. Score each theme:
0 = Dying, 1-3 = Weak, 4-6 = Stable, 7-8 = Strong, 9-10 = Explosive.

Examples: AI Infrastructure, Data Centers, Cloud, Cybersecurity, Nuclear Energy,
Utilities, Copper, Grid Modernization, Robotics, Defense, Healthcare AI,
Fintech, Industrial Automation.

==================================================
PART 7 - STOCK OPPORTUNITY SCREEN
==================================================

Identify top bullish and bearish stocks. For each bullish stock provide ticker,
sector, current catalyst, why institutions may be buying, valuation attractiveness,
3-5 year outlook, risk factors, and conviction score from 1-10. For each bearish
stock provide ticker, sector, reason for weakness, whether the issue is temporary
or structural, risk level, and probability of recovery.

==================================================
PART 8 - WINNERS AND LOSERS
==================================================

List top gainers and losers today and this week where evidence is available. For
every stock explain what happened, why it moved, and whether the move is likely
temporary or long-term.

==================================================
PART 9 - UNDERVALUED LONG-TERM OPPORTUNITIES
==================================================

Identify stocks where earnings growth may be underestimated, market sentiment is
excessively negative, long-term secular tailwinds exist, cash flow is strong, and
the balance sheet is healthy. Provide ticker, sector, reason, expected growth
drivers, risk, and conviction score.

==================================================
PART 10 - OUTPUT TABLE
==================================================

Create a table:
| Stock | Sector | Catalyst | Bullish/Bearish | Institutional Activity | Impact Score (1-10) | 3-5 Year Outlook |

==================================================
PART 11 - FINAL INVESTMENT CONCLUSION
==================================================

Conclude with:
1. Where smart money appears to be moving today.
2. Which sectors are becoming leaders.
3. Which sectors are losing leadership.
4. Best risk/reward opportunities.
5. Most overcrowded trades.
6. Top 10 stocks institutions appear to be accumulating.
7. Top 10 stocks institutions appear to be distributing.
8. Biggest opportunities created by recent profit taking.
9. Highest conviction ideas for the next 3-5 years.

Provide evidence for every conclusion. Do not speculate. Separate facts from
assumptions. Prioritize capital flow analysis over headlines.
""".strip()


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


# All feed URLs verified live on 2026-06-12. Reuters direct RSS, Anthropic News,
# MarketWatch Internet, Investors Business, and Papers With Code feeds are dead
# and were removed; Reuters is covered through the Google News RSS proxy below.
RSS_FEEDS: list[tuple[str, str]] = [
    # Official / primary sources
    ("Federal Reserve Press", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("SEC Press Releases", "https://www.sec.gov/news/pressreleases.rss"),
    ("EIA Today in Energy", "https://www.eia.gov/rss/todayinenergy.xml"),
    ("BEA News", "https://apps.bea.gov/rss/rss.xml"),
    ("Census Economic Indicators", "https://www.census.gov/economic-indicators/indicator.xml"),
    # Market news
    ("Reuters Markets (Google News)", "https://news.google.com/rss/search?q=site:reuters.com+markets&hl=en-US&gl=US&ceid=US:en"),
    ("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("WSJ US Business", "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Seeking Alpha Market Currents", "https://seekingalpha.com/market_currents.xml"),
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Earnings", "https://www.cnbc.com/id/15839135/device/rss/rss.html"),
    ("CNBC Tech", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("MarketWatch Top", "https://feeds.marketwatch.com/marketwatch/topstories"),
    ("Fortune", "https://fortune.com/feed/"),
    # Technology and AI
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("ZDNet AI", "https://www.zdnet.com/topic/artificial-intelligence/rss.xml"),
    ("HuggingFace Blog", "https://huggingface.co/blog/feed.xml"),
    ("Towards Data Science", "https://towardsdatascience.com/feed"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("OpenAI News", "https://openai.com/news/rss.xml"),
    ("IEEE Spectrum", "https://spectrum.ieee.org/feeds/feed.rss"),
    ("New Scientist Tech", "https://www.newscientist.com/subject/technology/feed/"),
    # Politics & policy (White House, Congress, regulation, Trump administration)
    ("Reuters Politics (Google News)", "https://news.google.com/rss/search?q=site:reuters.com+politics&hl=en-US&gl=US&ceid=US:en"),
    ("The Hill", "https://thehill.com/news/feed/"),
    ("Politico Politics", "https://rss.politico.com/politics-news.xml"),
    ("AP Politics", "https://news.google.com/rss/search?q=site:apnews.com+politics&hl=en-US&gl=US&ceid=US:en"),
    ("White House Trade & Economy (Google News)", "https://news.google.com/rss/search?q=White+House+OR+Trump+tariffs+OR+economy+OR+trade&hl=en-US&gl=US&ceid=US:en"),
    # Global & geopolitical (macro-moving: wars, trade, OPEC, China, elections)
    ("Reuters World (Google News)", "https://news.google.com/rss/search?q=site:reuters.com+world&hl=en-US&gl=US&ceid=US:en"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Reuters Business (Google News)", "https://news.google.com/rss/search?q=site:reuters.com+business&hl=en-US&gl=US&ceid=US:en"),
    ("Geopolitics & Markets (Google News)", "https://news.google.com/rss/search?q=geopolitics+OR+OPEC+OR+China+economy+OR+trade+war&hl=en-US&gl=US&ceid=US:en"),
]


# Default search queries fed to the social/web sources (X, Reddit, Exa).
# Tuned for macro + market-moving coverage rather than per-ticker spam.
DEFAULT_SOCIAL_QUERIES: list[str] = [
    "stock market today",
    "Federal Reserve interest rate decision",
    "Trump tariffs economy policy",
    "US economy inflation jobs report",
    "$NVDA OR $MSFT OR $AAPL OR $META OR $AMD",
]

# Subreddits the agent pulls hot posts from (in addition to the search queries).
DEFAULT_REDDIT_SUBS: list[str] = ["stocks", "wallstreetbets", "investing"]


MARKET_SYMBOLS: dict[str, str] = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq Composite",
    "^DJI": "Dow Jones Industrial Average",
    "^RUT": "Russell 2000",
    "^TNX": "10-Year Treasury Yield",
    "DX-Y.NYB": "U.S. Dollar Index",
    "CL=F": "WTI Crude Oil",
    "NG=F": "Natural Gas",
    "HG=F": "Copper",
    "GC=F": "Gold",
    "SI=F": "Silver",
}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    openrouter_api_key: str
    default_model: str
    model_choices: tuple[str, ...]
    poll_interval_seconds: int
    min_score: int
    bearish_threshold: int
    daily_digest_hour: int
    feed_lookback_hours: int
    request_timeout_seconds: int
    brief_interval_hours: int
    realtime_alerts: bool
    enable_social: bool
    social_queries: tuple[str, ...]
    reddit_subreddits: tuple[str, ...]
    social_timeout_seconds: int
    data_dir: Path
    log_dir: Path
    analysis_instructions: str

    @property
    def seen_cache(self) -> Path:
        return self.data_dir / "seen.json"

    @property
    def sentiment_cache(self) -> Path:
        return self.data_dir / "sentiment.json"

    @property
    def model_cache(self) -> Path:
        return self.data_dir / "model.json"


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


def _get_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    return raw_value.strip().lower() in ("1", "true", "yes", "on")


def _load_social_queries() -> tuple[str, ...]:
    raw = os.getenv("SOCIAL_QUERIES", "")
    queries = [query.strip() for query in raw.split("|") if query.strip()]
    if not queries:
        queries = list(DEFAULT_SOCIAL_QUERIES)
    return tuple(queries)


def _load_reddit_subs() -> tuple[str, ...]:
    raw = os.getenv("REDDIT_SUBREDDITS", "")
    subs = [sub.strip().lstrip("r/").strip("/") for sub in raw.split(",") if sub.strip()]
    if not subs:
        subs = list(DEFAULT_REDDIT_SUBS)
    return tuple(subs)


def _load_analysis_instructions() -> str:
    prompt_file = os.getenv("ANALYSIS_PROMPT_FILE", "").strip()
    inline_prompt = os.getenv("ANALYSIS_PROMPT", "").strip()
    if prompt_file and inline_prompt:
        raise ValueError("Set either ANALYSIS_PROMPT_FILE or ANALYSIS_PROMPT, not both")
    if prompt_file:
        path = Path(prompt_file).expanduser()
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Unable to read ANALYSIS_PROMPT_FILE: {path}") from exc
        if not content:
            raise ValueError("ANALYSIS_PROMPT_FILE must not be empty")
        return content
    if inline_prompt:
        return inline_prompt
    return DEFAULT_ANALYSIS_INSTRUCTIONS


def _load_model_choices(default_model: str) -> tuple[str, ...]:
    raw = os.getenv("OPENROUTER_MODELS", "")
    choices = [model.strip() for model in raw.split(",") if model.strip()]
    if not choices:
        choices = list(DEFAULT_MODEL_CHOICES)
    if default_model not in choices:
        choices.insert(0, default_model)
    return tuple(choices)


def load_settings() -> Settings:
    missing = [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENROUTER_API_KEY")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    default_model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
    return Settings(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        default_model=default_model,
        model_choices=_load_model_choices(default_model),
        poll_interval_seconds=_get_int_env("POLL_INTERVAL_SECONDS", 1800, minimum=60),
        min_score=_get_int_env("MIN_SCORE", 6, minimum=1, maximum=10),
        bearish_threshold=_get_int_env("BEARISH_THRESHOLD", 3, minimum=1),
        daily_digest_hour=_get_int_env("DAILY_DIGEST_HOUR", 8, minimum=0, maximum=23),
        feed_lookback_hours=_get_int_env("FEED_LOOKBACK_HOURS", 2, minimum=1),
        request_timeout_seconds=_get_int_env("REQUEST_TIMEOUT_SECONDS", 10, minimum=1),
        brief_interval_hours=_get_int_env("BRIEF_INTERVAL_HOURS", 4, minimum=1, maximum=24),
        realtime_alerts=_get_bool_env("REALTIME_ALERTS", False),
        enable_social=_get_bool_env("ENABLE_SOCIAL", True),
        social_queries=_load_social_queries(),
        reddit_subreddits=_load_reddit_subs(),
        social_timeout_seconds=_get_int_env("SOCIAL_TIMEOUT_SECONDS", 30, minimum=5, maximum=120),
        data_dir=Path(os.getenv("AGENT_DATA_DIR", "data")),
        log_dir=Path(os.getenv("AGENT_LOG_DIR", "logs")),
        analysis_instructions=_load_analysis_instructions(),
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
    redactor = RedactingFilter([settings.telegram_bot_token, settings.openrouter_api_key])

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
        self.llm = OpenAI(api_key=settings.openrouter_api_key, base_url=OPENROUTER_BASE_URL)
        self.model = self.load_model()
        self.paused_until: datetime | None = None
        self.paused_lock = threading.Lock()
        self.last_update_id = 0
        # Items accumulated since the last brief, sent as one consolidated message.
        self.brief_items: list[dict[str, Any]] = []
        self.last_brief_time = datetime.now(timezone.utc)
        self.last_morning_brief_date: str | None = None
        self.social_notes: list[str] = []
        self.latest_articles: list[dict[str, Any]] = []
        self.articles_lock = threading.Lock()
        self._yahoo_crumb_value: str | None = None

    def load_model(self) -> str:
        saved = load_json(self.settings.model_cache, {})
        model = saved.get("model") if isinstance(saved, dict) else None
        if isinstance(model, str) and model.strip():
            return model.strip()
        return self.settings.default_model

    def set_model(self, model: str) -> None:
        self.model = model
        save_json(self.settings.model_cache, {"model": model})

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

    @staticmethod
    def _entry_time(value: str) -> datetime:
        # Sentiment files written by older agent versions hold naive timestamps;
        # treat those as UTC so they compare cleanly with aware datetimes.
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def get_history(self, ticker: str, range_: str = "1y") -> dict[str, Any] | None:
        """Daily price history plus quote metadata for any Yahoo symbol."""
        try:
            response = self.http.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": range_},
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            result = (response.json().get("chart", {}).get("result") or [None])[0]
            if not result:
                return None
            quote = result.get("indicators", {}).get("quote", [{}])[0]
            points = [
                (ts, close)
                for ts, close in zip(result.get("timestamp", []), quote.get("close", []))
                if close is not None
            ]
            if not points:
                return None
            return {
                "meta": result.get("meta", {}),
                "timestamps": [p[0] for p in points],
                "closes": [p[1] for p in points],
                "volumes": [v for v in quote.get("volume", []) if v is not None],
            }
        except (requests.RequestException, ValueError, KeyError) as exc:
            self.log.warning("History fetch failed for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _pct(new: float, old: float) -> str:
        if not old:
            return "n/a"
        return f"{(new - old) / old * 100:+.1f}%"

    def _yahoo_crumb(self) -> str | None:
        """Yahoo's analyst/quote endpoints need a cookie + crumb token.

        Seed the session cookie jar once, fetch a crumb, and cache it. Works
        headlessly (no browser), so it runs fine on a server. Returns None if
        Yahoo blocks the handshake, in which case analyst data is just skipped.
        """
        if self._yahoo_crumb_value:
            return self._yahoo_crumb_value
        try:
            self.http.get(
                "https://fc.yahoo.com",
                headers=YAHOO_BROWSER_HEADERS,
                timeout=self.settings.request_timeout_seconds,
            )
            response = self.http.get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb",
                headers=YAHOO_BROWSER_HEADERS,
                timeout=self.settings.request_timeout_seconds,
            )
            crumb = (response.text or "").strip()
            # A real crumb is a short token with no whitespace; error bodies like
            # "Too Many Requests" or HTML must be rejected (and never cached).
            if crumb and len(crumb) <= 24 and not re.search(r"\s|<", crumb):
                self._yahoo_crumb_value = crumb
                return crumb
            self.log.warning("Yahoo crumb unavailable (response: %r)", crumb[:40])
        except requests.RequestException as exc:
            self.log.warning("Yahoo crumb fetch failed: %s", exc)
        return None

    def get_analyst(self, ticker: str) -> dict[str, Any] | None:
        """Analyst ratings and price targets from Yahoo's quoteSummary API."""
        crumb = self._yahoo_crumb()
        if not crumb:
            return None
        try:
            response = self.http.get(
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                params={"modules": "financialData,recommendationTrend", "crumb": crumb},
                headers=YAHOO_BROWSER_HEADERS,
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            result = (response.json().get("quoteSummary", {}).get("result") or [None])[0]
            if not result:
                return None
        except (requests.RequestException, ValueError) as exc:
            self.log.warning("Analyst fetch failed for %s: %s", ticker, exc)
            return None

        fin = result.get("financialData", {}) or {}
        trend = (result.get("recommendationTrend", {}).get("trend") or [{}])[0]

        def _raw(field: str) -> float | None:
            value = fin.get(field)
            return value.get("raw") if isinstance(value, dict) else None

        data = {
            "recommendation": fin.get("recommendationKey", ""),
            "num_analysts": _raw("numberOfAnalystOpinions"),
            "current": _raw("currentPrice"),
            "target_mean": _raw("targetMeanPrice"),
            "target_high": _raw("targetHighPrice"),
            "target_low": _raw("targetLowPrice"),
            "strong_buy": trend.get("strongBuy"),
            "buy": trend.get("buy"),
            "hold": trend.get("hold"),
            "sell": trend.get("sell"),
            "strong_sell": trend.get("strongSell"),
        }
        if data["target_mean"] is None and not data["recommendation"]:
            return None
        return data

    def analyst_summary(self, ticker: str) -> list[str]:
        """Human-readable analyst lines; empty list if no data available."""
        data = self.get_analyst(ticker)
        if not data:
            return []
        lines: list[str] = []
        rec = str(data.get("recommendation", "")).replace("_", " ").title()
        if rec and data.get("num_analysts"):
            lines.append(f"Analyst consensus: {rec} ({int(data['num_analysts'])} analysts)")
        elif rec:
            lines.append(f"Analyst consensus: {rec}")
        mean = data.get("target_mean")
        if mean:
            target = f"Price target: {mean:.2f} mean"
            if data.get("target_low") and data.get("target_high"):
                target += f" (range {data['target_low']:.0f}-{data['target_high']:.0f})"
            current = data.get("current")
            if current:
                target += f" | upside {self._pct(mean, current)} vs {current:.2f}"
            lines.append(target)
        counts = [data.get(k) for k in ("strong_buy", "buy", "hold", "sell", "strong_sell")]
        if any(isinstance(c, int) for c in counts):
            sb, b, h, s, ss = (c or 0 for c in counts)
            lines.append(f"Ratings: {sb} strong buy, {b} buy, {h} hold, {s} sell, {ss} strong sell")
        return lines

    def ticker_overview(self, ticker: str, history: dict[str, Any]) -> list[str]:
        """Human-readable stats computed from price history."""
        meta = history["meta"]
        closes = history["closes"]
        price = closes[-1]
        name = meta.get("longName") or meta.get("shortName") or ticker
        lines = [
            f"{ticker} ({name}) on {meta.get('fullExchangeName', meta.get('exchangeName', ''))}",
            f"Price: {price:.2f} {meta.get('currency', 'USD')} | Market: {meta.get('marketState', 'UNKNOWN')}",
        ]
        spans = [("1 day", 2), ("1 week", 6), ("1 month", 22), ("3 months", 64), ("6 months", 127), ("1 year", len(closes))]
        changes = [
            f"{label}: {self._pct(price, closes[-offset])}"
            for label, offset in spans
            if len(closes) >= offset
        ]
        if changes:
            lines.append("Returns: " + " | ".join(changes))
        high = meta.get("fiftyTwoWeekHigh") or max(closes)
        low = meta.get("fiftyTwoWeekLow") or min(closes)
        lines.append(f"52-week range: {low:.2f} - {high:.2f} ({self._pct(price, high)} from high)")
        if len(closes) >= 50:
            sma50 = sum(closes[-50:]) / 50
            lines.append(f"50-day average: {sma50:.2f} (price is {self._pct(price, sma50)} vs it)")
        if len(closes) >= 200:
            sma200 = sum(closes[-200:]) / 200
            lines.append(f"200-day average: {sma200:.2f} (price is {self._pct(price, sma200)} vs it)")
        volumes = history.get("volumes", [])
        if len(volumes) >= 20:
            avg_volume = sum(volumes[-20:]) / 20
            lines.append(f"Last volume: {volumes[-1]:,.0f} vs 20-day average {avg_volume:,.0f}")
        lines.extend(self.analyst_summary(ticker))
        return lines

    def fetch_ticker_news(self, query: str, limit: int = 8) -> list[str]:
        """Search the web for recent news about a ticker via Google News RSS."""
        url = (
            "https://news.google.com/rss/search?q="
            + requests.utils.quote(f"{query} stock")
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            response = self.http.get(url, timeout=self.settings.request_timeout_seconds)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            return [
                f"- {entry.get('title', '').strip()} ({entry.get('published', '')})"
                for entry in feed.entries[:limit]
            ]
        except requests.RequestException as exc:
            self.log.warning("News search failed for %s: %s", query, exc)
            return []

    def render_chart(self, ticker: str, history: dict[str, Any]) -> bytes | None:
        """1-year price chart PNG; returns None if matplotlib is unavailable."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            self.log.warning("matplotlib not installed; skipping chart for %s", ticker)
            return None

        closes = history["closes"]
        dates = [datetime.fromtimestamp(ts, tz=timezone.utc) for ts in history["timestamps"]]
        name = history["meta"].get("longName") or ticker
        color = "#16a34a" if closes[-1] >= closes[0] else "#dc2626"

        fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
        ax.plot(dates, closes, color=color, linewidth=1.6)
        ax.fill_between(dates, closes, min(closes), color=color, alpha=0.12)
        if len(closes) >= 50:
            sma50 = [sum(closes[max(0, i - 49) : i + 1]) / min(i + 1, 50) for i in range(len(closes))]
            ax.plot(dates, sma50, color="#2563eb", linewidth=1.0, linestyle="--", label="50-day avg")
            ax.legend(loc="upper left", frameon=False, fontsize=8)
        change = self._pct(closes[-1], closes[0])
        ax.set_title(f"{name} ({ticker}) - 1 year  |  {closes[-1]:.2f} {history['meta'].get('currency', 'USD')}  ({change})", fontsize=11)
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
        fig.autofmt_xdate()
        fig.tight_layout()

        buffer = io.BytesIO()
        fig.savefig(buffer, format="png")
        plt.close(fig)
        return buffer.getvalue()

    def send_telegram_photo(self, photo: bytes, caption: str = "") -> None:
        try:
            response = self.http.post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendPhoto",
                data={"chat_id": self.settings.telegram_chat_id, "caption": caption[:1024]},
                files={"photo": ("chart.png", photo, "image/png")},
                timeout=self.settings.request_timeout_seconds + 20,
            )
            response.raise_for_status()
            self.log.info("Telegram photo sent")
        except requests.RequestException as exc:
            self.log.error("Telegram photo send failed: %s", exc)

    def resolve_tickers(self, question: str) -> list[str]:
        """Find ticker symbols the question refers to, portfolio or not."""
        tickers: list[str] = []
        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                max_tokens=100,
                temperature=0,
                messages=[
                    {"role": "system", "content": "Return valid JSON only. No markdown or commentary."},
                    {"role": "user", "content": TICKER_EXTRACTION_PROMPT + question},
                ],
            )
            content = (response.choices[0].message.content or "[]").strip()
            content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(content)
            if isinstance(parsed, list):
                tickers = [
                    str(item).upper().strip()
                    for item in parsed
                    if isinstance(item, str) and re.fullmatch(r"[\^]?[A-Za-z0-9.=-]{1,10}", str(item).strip())
                ]
        except Exception as exc:
            self.log.warning("Ticker extraction failed (%s): %s", self.model, exc)

        if not tickers:
            # Fallback: explicit upper-case ticker-like tokens in the question.
            tickers = [
                token
                for token in re.findall(r"\b[A-Z]{1,5}\b", question)
                if token in PORTFOLIO or len(token) >= 2
            ]

        unique: list[str] = []
        for ticker in tickers:
            if ticker not in unique:
                unique.append(ticker)
        return unique[:3]

    def record_sentiment(self, tickers: list[str], impact: str) -> None:
        sentiment = self.load_sentiment()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=7)

        for ticker in tickers:
            entries = sentiment.setdefault(ticker, [])
            entries.append({"impact": impact, "time": now.isoformat()})
            kept = []
            for entry in entries:
                try:
                    if self._entry_time(entry["time"]) > cutoff:
                        kept.append(entry)
                except (KeyError, ValueError):
                    continue
            sentiment[ticker] = kept

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

    # Category groups shown in the consolidated brief, in display order.
    BRIEF_CATEGORIES: tuple[tuple[str, str], ...] = (
        ("FED_RATES", "Fed & Rates"),
        ("STOCKS", "US Stocks & Earnings"),
        ("POLITICS", "Politics & Policy"),
        ("GLOBAL", "Global & Geopolitics"),
        ("AI_TECH", "AI & Tech"),
        ("OTHER", "Other"),
    )

    def build_brief(self, items: list[dict[str, Any]], *, title: str, include_prices: bool) -> str:
        """Group analysed items by category into one consolidated message."""
        buckets: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in self.BRIEF_CATEGORIES}
        for item in items:
            category = str(item.get("category", "OTHER")).upper()
            buckets.setdefault(category if category in buckets else "OTHER", []).append(item)

        lines = [title, ""]
        for key, label in self.BRIEF_CATEGORIES:
            group = sorted(
                buckets.get(key, []),
                key=lambda entry: int(entry.get("relevance_score", 0)),
                reverse=True,
            )[:6]
            if not group:
                continue
            lines.append(f"=== {label} ===")
            for entry in group:
                tickers = " ".join(f"${ticker}" for ticker in entry.get("affected_tickers", []))
                tag = f" {tickers}" if tickers else ""
                lines.append(
                    f"[{entry.get('relevance_score')}/10] {entry.get('impact', 'NEUTRAL')}"
                    f"{tag} — {str(entry.get('title', ''))[:140]}"
                )
                one_line = str(entry.get("one_line", "")).strip()
                if one_line:
                    lines.append(f"   {one_line[:180]}")
                link = str(entry.get("link", "")).strip()
                if link:
                    lines.append(f"   {link}")
            lines.append("")

        if include_prices:
            lines.append("=== Market snapshot ===")
            lines.extend(self.market_snapshot())
            lines.append("")
            lines.append("Watchlist")
            for ticker in ["NVDA", "MSFT", "INTC", "META", "AMD", "NOW"]:
                price = self.price_line(ticker)
                if price:
                    lines.append(price)

        return "\n".join(lines).strip()

    def maybe_send_brief(self) -> None:
        """Send the morning edition at the digest hour, plus interval briefs."""
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        is_morning = (
            datetime.now().hour == self.settings.daily_digest_hour
            and self.last_morning_brief_date != today
        )
        interval_due = (now - self.last_brief_time) >= timedelta(hours=self.settings.brief_interval_hours)

        if not (is_morning or interval_due):
            return

        items = self.brief_items
        if is_morning:
            self.last_morning_brief_date = today
            title = f"Morning Market Intelligence Brief - {today}"
            if not items:
                self.send_telegram(f"{title}\n\nNo significant market-moving news overnight.")
                self.brief_items = []
                self.last_brief_time = now
                return
            self.send_telegram_chunks(self.build_brief(items, title=title, include_prices=True))
        else:
            # Interval brief: skip silently when nothing new accumulated.
            if not items:
                self.last_brief_time = now
                return
            stamp = datetime.now().strftime("%H:%M")
            title = f"Market Intelligence Brief - {stamp}"
            self.send_telegram_chunks(self.build_brief(items, title=title, include_prices=False))

        self.brief_items = []
        self.last_brief_time = now

    def send_brief_now(self) -> None:
        """On-demand brief: fetch fresh news + social, analyse, send immediately."""
        self.send_telegram("Building your combined market brief...")
        try:
            articles = self.fetch_articles()
            analysed = self.analyse(articles)
            relevant = [a for a in analysed if int(a.get("relevance_score", 0)) >= self.settings.min_score]
        except Exception as exc:
            self.log.error("On-demand brief failed: %s", exc)
            self.send_telegram("Brief failed. Check logs for details.")
            return
        if not relevant:
            self.send_telegram("No market-moving news right now across stocks, Fed, politics, or global feeds.")
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.send_telegram_chunks(
            self.build_brief(relevant, title=f"Market Intelligence Brief - {stamp}", include_prices=True)
        )

    def fetch_articles(self) -> list[dict[str, Any]]:
        articles: list[dict[str, Any]] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.settings.feed_lookback_hours)

        for source, url in RSS_FEEDS:
            try:
                response = self.http.get(url, timeout=self.settings.request_timeout_seconds)
                response.raise_for_status()
                feed = feedparser.parse(response.content)
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

        articles.extend(self.fetch_social_articles())

        if articles:
            with self.articles_lock:
                self.latest_articles = articles
        return articles

    def fetch_social_articles(self) -> list[dict[str, Any]]:
        """Pull recent items from X/Twitter, Reddit, and Exa (graceful degrade)."""
        if not self.settings.enable_social:
            return []
        try:
            articles, notes = social.gather(
                list(self.settings.social_queries),
                env=dict(os.environ),
                reddit_subs=list(self.settings.reddit_subreddits),
                timeout=self.settings.social_timeout_seconds,
                log=self.log,
            )
            self.social_notes = notes
            if articles:
                self.log.info("Social sources returned %s items (%s)", len(articles), "; ".join(notes))
            return articles
        except Exception as exc:
            self.log.warning("Social gather failed: %s", exc)
            return []

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
                f"{self.settings.analysis_instructions}\n\n"
                f"Portfolio:\n{portfolio}\n\n"
                f"Articles:\n{article_text}\n\n"
                "Output contract:\n"
                "Return a JSON array only. Each object must contain: index, relevance_score "
                "(integer 1-10 by market impact), affected_tickers (list from the portfolio, "
                "or empty list for broad macro/political/global news), impact (BULLISH, "
                "BEARISH, or NEUTRAL for the market or named tickers), one_line, urgency "
                "(HIGH, MEDIUM, or LOW), and category (STOCKS, FED_RATES, POLITICS, GLOBAL, "
                "AI_TECH, or OTHER). Score Fed/rate, political, and global market-movers on "
                "their market impact even when affected_tickers is empty. Only include "
                "articles with relevance_score >= 6."
            )

            try:
                response = self.llm.chat.completions.create(
                    model=self.model,
                    max_tokens=1500,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                )
                content = response.choices[0].message.content or "[]"
                for item in self._parse_analysis(content):
                    index = int(item.get("index", 0)) - 1
                    if 0 <= index < len(batch):
                        results.append({**batch[index], **item})
            except Exception as exc:
                self.log.error("OpenRouter analysis failed (%s): %s", self.model, exc)

        results.sort(key=lambda item: int(item.get("relevance_score", 0)), reverse=True)
        return results

    def _parse_analysis(self, content: str) -> list[dict[str, Any]]:
        cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            self.log.warning("Model returned invalid JSON: %s", exc)
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

    def send_telegram_chunks(self, message: str, chunk_size: int = 3900) -> None:
        chunk = ""
        for line in message.splitlines():
            pending = f"{chunk}\n{line}".strip() if chunk else line
            if len(pending) <= chunk_size:
                chunk = pending
                continue
            if chunk:
                self.send_telegram(chunk)
            while len(line) > chunk_size:
                self.send_telegram(line[:chunk_size])
                line = line[chunk_size:]
            chunk = line
        if chunk:
            self.send_telegram(chunk)

    def market_snapshot(self) -> list[str]:
        lines: list[str] = []
        for symbol, name in MARKET_SYMBOLS.items():
            price = self.get_price(symbol)
            if price.get("error"):
                lines.append(f"{name} ({symbol}): unavailable")
                continue
            sign = "+" if price["change"] >= 0 else ""
            lines.append(
                f"{name} ({symbol}): {price['price']} "
                f"({sign}{price['change_pct']}%) | {price['market']}"
            )
        return lines

    def portfolio_snapshot(self) -> list[str]:
        lines: list[str] = []
        for ticker, company in PORTFOLIO.items():
            price = self.get_price(ticker)
            if price.get("error"):
                lines.append(f"{ticker} ({company}): unavailable")
                continue
            sign = "+" if price["change"] >= 0 else ""
            lines.append(
                f"{ticker} ({company}): {price['price']} {price['currency']} "
                f"({sign}{price['change_pct']}%) | {price['market']}"
            )
        return lines

    def build_institutional_report_prompt(self, articles: list[dict[str, Any]], max_articles: int = 60) -> str:
        article_lines = []
        for index, article in enumerate(articles[:max_articles], 1):
            article_lines.append(
                f"[{index}] Source: {article.get('source', '')}\n"
                f"Title: {article.get('title', '')}\n"
                f"Published: {article.get('published', '')}\n"
                f"Summary: {article.get('summary', '')}\n"
                f"Link: {article.get('link', '')}"
            )

        return (
            f"{INSTITUTIONAL_REPORT_PROMPT}\n\n"
            "Available evidence collected by this bot follows. Base every conclusion "
            "only on this evidence. If the evidence does not support a requested item, "
            "write 'Not available from supplied evidence'. Keep the Telegram report "
            "concise but cover all parts.\n\n"
            "Market snapshot:\n"
            f"{chr(10).join(self.market_snapshot())}\n\n"
            "Portfolio snapshot:\n"
            f"{chr(10).join(self.portfolio_snapshot())}\n\n"
            "Recent articles:\n"
            f"{chr(10).join(article_lines) if article_lines else 'No recent articles available.'}"
        )

    def generate_institutional_report(self, articles: list[dict[str, Any]]) -> str:
        response = self.llm.chat.completions.create(
            model=self.model,
            max_tokens=3500,
            temperature=0,
            messages=[
                {"role": "system", "content": INSTITUTIONAL_REPORT_SYSTEM_PROMPT},
                {"role": "user", "content": self.build_institutional_report_prompt(articles)},
            ],
        )
        return response.choices[0].message.content or "No report generated."

    def send_institutional_report(self) -> None:
        self.send_telegram("Generating institutional market report...")
        try:
            report = self.generate_institutional_report(self.fetch_articles())
        except Exception as exc:
            self.log.error("Institutional report failed: %s", exc)
            self.send_telegram("Institutional report failed. Check logs for details.")
            return
        self.send_telegram_chunks(f"Institutional Market Report\n\n{report}")

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
                params={"offset": self.last_update_id + 1, "timeout": 25},
                timeout=self.settings.request_timeout_seconds + 30,
            )
            response.raise_for_status()
            updates = response.json().get("result", [])
        except requests.RequestException as exc:
            self.log.error("Telegram command polling failed: %s", exc)
            time.sleep(5)
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
            elif command.startswith("/analyst"):
                self.send_analyst_for_ticker(text)
            elif command.startswith("/model"):
                self.handle_model_command(text)
            elif command == "/q" or command.startswith("/q "):
                self.answer_question(text)
            elif command == "/report":
                self.send_institutional_report()
            elif command == "/brief":
                self.send_brief_now()
            elif command == "/sources":
                self.send_sources_status()
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

    def send_analyst_for_ticker(self, text: str) -> None:
        parts = text.split()
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            self.send_telegram("Usage: /analyst NVDA")
            return

        summary = self.analyst_summary(ticker)
        if not summary:
            self.send_telegram(
                f"No analyst data found for {ticker}. It may be an index/ETF, "
                "or Yahoo blocked the request — try again shortly."
            )
            return
        header = self.price_line(ticker) or ticker
        self.send_telegram("\n".join([f"Analyst view - ${ticker}", header, "", *summary]))

    def answer_question(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ""
        if not question:
            self.send_telegram("Usage: /q your question\nExample: /q why is NVDA down today?")
            return

        self.send_telegram("Thinking...")

        tickers = self.resolve_tickers(question)
        ticker_sections: list[str] = []
        charts: list[tuple[str, bytes]] = []
        for ticker in tickers:
            history = self.get_history(ticker)
            if history is None:
                ticker_sections.append(f"{ticker}: no price data found on Yahoo Finance.")
                continue
            section = "\n".join(self.ticker_overview(ticker, history))
            name = history["meta"].get("longName") or ticker
            news = self.fetch_ticker_news(name if len(name) < 40 else ticker)
            if news:
                section += "\nLatest news from the web:\n" + "\n".join(news)
            ticker_sections.append(section)
            chart = self.render_chart(ticker, history)
            if chart:
                charts.append((f"{ticker} - 1 year price chart", chart))

        with self.articles_lock:
            articles = list(self.latest_articles)
        headline_lines = [
            f"- [{article['source']}] {article['title']} ({article['published']})"
            for article in articles[: 25 if ticker_sections else 50]
        ]

        context = (
            f"Question from the user: {question}\n\n"
            + (
                "Live data and web news for stocks mentioned:\n\n"
                + "\n\n".join(ticker_sections)
                + "\n\n"
                if ticker_sections
                else ""
            )
            + "Live market snapshot:\n"
            f"{chr(10).join(self.market_snapshot())}\n\n"
            f"User's portfolio holdings: {', '.join(PORTFOLIO)}\n\n"
            "Recent general headlines collected by this bot:\n"
            f"{chr(10).join(headline_lines) if headline_lines else 'No recent headlines available.'}"
        )

        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                max_tokens=1200,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": QA_SYSTEM_PROMPT},
                    {"role": "user", "content": context},
                ],
            )
            answer = response.choices[0].message.content or "No answer generated."
        except Exception as exc:
            self.log.error("Question answering failed (%s): %s", self.model, exc)
            self.send_telegram("Sorry, I could not get an answer from the model. Try again or switch models with /model.")
            return

        # Telegram messages are sent as plain text; markdown markers would show literally.
        answer = answer.replace("**", "").replace("##", "").replace("__", "")
        self.send_telegram_chunks(answer)
        for caption, chart in charts:
            self.send_telegram_photo(chart, caption)

    def handle_model_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        choice = parts[1].strip() if len(parts) > 1 else ""

        if not choice:
            lines = [f"Current model: {self.model}", "", "Available models:"]
            for index, model in enumerate(self.settings.model_choices, 1):
                marker = " (active)" if model == self.model else ""
                lines.append(f"{index}. {model}{marker}")
            lines.extend(
                [
                    "",
                    "Switch with /model <number> or /model <openrouter-model-id>",
                    "Example: /model 2 or /model anthropic/claude-sonnet-4.5",
                ]
            )
            self.send_telegram("\n".join(lines))
            return

        if choice.isdigit():
            index = int(choice) - 1
            if not 0 <= index < len(self.settings.model_choices):
                self.send_telegram(
                    f"Invalid number. Pick 1-{len(self.settings.model_choices)} or send /model to see the list."
                )
                return
            model = self.settings.model_choices[index]
        else:
            model = choice
            if not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
                self.send_telegram("That does not look like a valid OpenRouter model id.")
                return

        self.set_model(model)
        self.log.info("Model switched to %s", model)
        self.send_telegram(f"Model switched to {model}. All analysis and reports now use this model.")

    def send_sources_status(self) -> None:
        ready = social.availability(dict(os.environ))
        lines = ["News sources", "", f"RSS feeds: {len(RSS_FEEDS)} active"]
        if self.settings.enable_social:
            lines.append("")
            lines.append("Social / web sources:")
            label = {"twitter": "X/Twitter", "reddit": "Reddit", "exa": "Exa web search"}
            for key, name in label.items():
                lines.append(f"  {name}: {'ready' if ready.get(key) else 'not configured'}")
            if self.social_notes:
                lines.append("")
                lines.append("Last fetch: " + "; ".join(self.social_notes))
        else:
            lines.append("Social sources: disabled (set ENABLE_SOCIAL=true)")
        lines.append("")
        lines.append(f"Brief every {self.settings.brief_interval_hours}h; morning edition at {self.settings.daily_digest_hour}:00.")
        self.send_telegram("\n".join(lines))

    def send_help(self) -> None:
        self.send_telegram(
            "AI Market Intelligence Agent commands\n"
            "/brief - combined news brief now (stocks, Fed, politics, global)\n"
            "/portfolio - live prices for holdings\n"
            "/news NVDA - latest news for a ticker\n"
            "/score NVDA - 7-day sentiment report\n"
            "/analyst NVDA - analyst ratings & price targets\n"
            "/report - institutional market report\n"
            "/sources - show which news sources are active\n"
            "/q your question - ask the AI anything about markets or your holdings\n"
            "/model - show or switch the AI model\n"
            "/pause - pause alerts for 2 hours\n"
            "/resume - resume alerts\n"
            "/help - show this menu"
        )

    def is_paused(self) -> bool:
        with self.paused_lock:
            return self.paused_until is not None and datetime.now(timezone.utc) < self.paused_until

    def news_cycle(self, seen: set[str]) -> None:
        self.log.info("Fetching news")
        articles = self.fetch_articles()
        new_articles = [article for article in articles if article["id"] not in seen]
        self.log.info("%s new articles", len(new_articles))

        if not new_articles:
            return

        alerts = self.analyse(new_articles)
        relevant = [
            alert
            for alert in alerts
            if int(alert.get("relevance_score", 0)) >= self.settings.min_score
        ]
        self.log.info("%s relevant items", len(relevant))
        # Accumulate for the next consolidated brief and keep sentiment state.
        self.brief_items.extend(relevant)
        for alert in relevant:
            self.record_sentiment(
                alert.get("affected_tickers", []),
                alert.get("impact", "NEUTRAL"),
            )
            # Real-time per-item alerts are opt-in; the brief is the default delivery.
            if self.settings.realtime_alerts:
                self.send_telegram(self.format_alert(alert))
                time.sleep(1.5)

        seen.update(article["id"] for article in new_articles)
        self.save_seen(seen)

    def news_loop(self) -> None:
        seen = self.load_seen()
        while True:
            try:
                self.maybe_send_brief()
                if self.is_paused():
                    time.sleep(60)
                    continue
                self.news_cycle(seen)
            except Exception:
                self.log.exception("News loop error")
            self.log.info("News loop sleeping %s seconds", self.settings.poll_interval_seconds)
            time.sleep(self.settings.poll_interval_seconds)

    def run(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.log.info("Stock Telegram Agent started")
        ready = social.availability(dict(os.environ))
        social_on = [name for name, ok in ready.items() if ok] if self.settings.enable_social else []
        social_line = (
            f"Social: {', '.join(social_on)}\n" if social_on
            else "Social: RSS only (run /sources for setup)\n"
        )
        self.send_telegram(
            f"AI Market Intelligence Agent is live\n"
            f"Watching {len(PORTFOLIO)} symbols across {len(RSS_FEEDS)} feeds "
            f"(stocks, Fed, politics, global).\n"
            f"{social_line}"
            f"Combined brief every {self.settings.brief_interval_hours}h + morning edition.\n"
            f"Model: {self.model} (via OpenRouter)\n"
            "Send /brief for a brief now, /help for commands."
        )

        news_thread = threading.Thread(target=self.news_loop, name="news-loop", daemon=True)
        news_thread.start()

        # The main thread long-polls Telegram so commands are answered within
        # seconds even while the news loop is sleeping between feed cycles.
        while True:
            try:
                self.handle_commands()
            except Exception:
                self.log.exception("Command loop error")
                time.sleep(5)


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
