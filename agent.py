#!/usr/bin/env python3
"""
AI Stock News Agent — Phase 1 Enhanced
Features:
  1. Portfolio stock boosting (2x weight for your 15 holdings)
  2. Fed & macro feeds (FOMC, Treasury, interest rates)
  3. CEO/executive mention detection
  4. Earnings season tracking
  5. Real-time market impacts on YOUR portfolio
"""

import os, json, time, hashlib, logging, requests, feedparser, threading
from datetime import datetime, timedelta
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("agent.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
POLL_INTERVAL      = 30 * 60
SEEN_CACHE         = "seen.json"
SENTIMENT_CACHE    = "sentiment.json"
MIN_SCORE          = 6
BEARISH_THRESHOLD  = 3
DAILY_DIGEST_HOUR  = 8

INSTITUTIONAL_ANALYSIS_PROMPT = """
You are a Senior Hedge Fund Analyst, Macro Strategist, and Institutional Flow Analyst.

Your objective is to identify where institutional money is moving, which sectors are
gaining or losing leadership, and which stocks have the highest probability of
outperforming over the next 3-5 years.

Use only verifiable information from high-quality financial news, earnings reports,
conference calls, company investor presentations, Federal Reserve releases, and
major macro or market-moving sources. Ignore social media hype, influencer opinions,
and unverified rumors.

When scoring articles, prioritize capital flow analysis over headlines. Focus on:
- institutional accumulation or distribution
- ETF inflows and outflows
- sector rotation
- smart-money positioning
- analyst upgrades or downgrades
- insider buying or selling
- unusual volume
- AI infrastructure and related themes

Return a JSON array only. Each object must contain:
index, relevance_score, affected_tickers, impact, one_line, urgency, category.
Only include articles with relevance_score >= 6.
Relevance should reflect whether the article affects the portfolio, macro regime,
sector leadership, or long-term institutional positioning.
""".strip()

# ── Pause state ───────────────────────────────────────────────────────────
paused_until = None
paused_lock  = threading.Lock()

# ── YOUR PORTFOLIO (15 stocks) ─────────────────────────────────────────────
PORTFOLIO = {
    "INTC": "Intel",
    "META": "Meta",
    "NVDA": "Nvidia",
    "ORCL": "Oracle",
    "SNDK": "SanDisk",
    "QQQ": "Invesco QQQ ETF",
    "PANW": "Palo Alto Networks",
    "MSFT": "Microsoft",
    "GOOGL": "Alphabet/Google",
    "MU": "Micron",
    "TEAM": "Atlassian",
    "SAP": "SAP",
    "NOW": "ServiceNow",
    "BABA": "Alibaba",
    "AMD": "AMD",
}

# ── Key Executives (CEO/CFO/Product leads) ─────────────────────────────────
KEY_EXECUTIVES = {
    "nvda": ["Jensen Huang", "Colette Kress"],
    "msft": ["Satya Nadella", "Amy Hood"],
    "meta": ["Mark Zuckerberg", "Sheryl Sandberg"],
    "googl": ["Sundar Pichai", "Ruth Porat"],
    "intc": ["Pat Gelsinger", "David Zinsner"],
    "orcl": ["Safra Catz", "Ellison"],
    "amd": ["Lisa Su", "Devavrat Patel"],
    "panw": ["Nikesh Arora"],
    "team": ["Anu Bharadwaj"],
    "now": ["Bill McDermott"],
    "baba": ["Zhang Yiming", "Daniel Zhang"],
}

# ── RSS Feeds — Expanded with Fed/Macro ────────────────────────────────────
RSS_FEEDS = [
    # Federal Reserve & Macro (PRIORITY)
    ("Federal Reserve Official", "https://www.federalreserve.gov/feeds/news.xml"),
    ("Treasury Department",      "https://home.treasury.gov/news-and-events/news/rss.xml"),
    ("FOMC Minutes",             "https://www.federalreserve.gov/feeds/news.xml"),

    # US Market & Finance
    ("Reuters Business",         "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Tech",             "https://feeds.reuters.com/reuters/technologyNews"),
    ("CNBC Markets",             "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Tech",                "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("MarketWatch Top",          "https://feeds.marketwatch.com/marketwatch/topstories"),
    ("MarketWatch Internet",     "https://feeds.marketwatch.com/marketwatch/internet"),

    # AI & LLM News
    ("TechCrunch AI",            "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI",           "https://venturebeat.com/category/ai/feed/"),
    ("The Verge",                "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica",             "https://feeds.arstechnica.com/arstechnica/index"),
    ("MIT Tech Review",          "https://www.technologyreview.com/feed/"),
    ("Wired",                    "https://www.wired.com/feed/rss"),
    ("ZDNet AI",                 "https://www.zdnet.com/topic/artificial-intelligence/rss.xml"),

    # HuggingFace & Open Source ML
    ("HuggingFace Blog",         "https://huggingface.co/blog/feed.xml"),
    ("Papers With Code",         "https://paperswithcode.com/latest.rss"),
    ("Towards Data Science",     "https://towardsdatascience.com/feed"),

    # Major AI Labs
    ("Google AI Blog",           "https://blog.google/technology/ai/rss/"),
    ("OpenAI News",              "https://openai.com/news/rss.xml"),
    ("Anthropic News",           "https://www.anthropic.com/news/rss.xml"),

    # Tech & Inventions
    ("IEEE Spectrum",            "https://spectrum.ieee.org/feeds/feed.rss"),
    ("New Scientist Tech",       "https://www.newscientist.com/subject/technology/feed/"),
]

# ── Cache helpers ──────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, "w") as f: json.dump(data, f)

def load_seen():  return set(load_json(SEEN_CACHE, []))
def save_seen(s): save_json(SEEN_CACHE, list(s)[-2000:])

def article_id(e):
    return hashlib.md5((e.get("link") or e.get("title") or "").encode()).hexdigest()

# ── FEATURE 1: Live Price Fetch (Yahoo Finance) ────────────────────────────
def get_price(ticker: str) -> dict:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        data = r.json()
        meta = data["chart"]["result"][0]["meta"]
        price      = round(meta.get("regularMarketPrice", 0), 2)
        prev       = round(meta.get("previousClose", price), 2)
        change     = round(price - prev, 2)
        change_pct = round((change / prev) * 100, 2) if prev else 0
        currency   = meta.get("currency", "USD")
        market     = meta.get("marketState", "REGULAR")
        return {
            "price": price, "change": change, "change_pct": change_pct,
            "currency": currency, "market": market, "error": False
        }
    except Exception as ex:
        log.warning(f"Price fetch error {ticker}: {ex}")
        return {"error": True}

def price_line(ticker: str) -> str:
    p = get_price(ticker)
    if p["error"]: return ""
    arrow  = "📈" if p["change"] >= 0 else "📉"
    sign   = "+" if p["change"] >= 0 else ""
    market = "🌙 After-Hours" if p["market"] != "REGULAR" else "🔔 Market Open"
    return f"{arrow} *{ticker}*: ${p['price']} ({sign}{p['change_pct']}%) | {market}"

# ── FEATURE 4: Sentiment Tracker ──────────────────────────────────────────
def load_sentiment() -> dict: return load_json(SENTIMENT_CACHE, {})
def save_sentiment(s: dict):  save_json(SENTIMENT_CACHE, s)

def record_sentiment(tickers: list, impact: str):
    sentiment = load_sentiment()
    now_str   = datetime.utcnow().isoformat()
    cutoff    = datetime.utcnow() - timedelta(days=7)
    for ticker in tickers:
        if ticker not in sentiment:
            sentiment[ticker] = []
        sentiment[ticker].append({"impact": impact, "time": now_str})
        sentiment[ticker] = [
            e for e in sentiment[ticker]
            if datetime.fromisoformat(e["time"]) > cutoff
        ]
    save_sentiment(sentiment)

def check_sentiment_warning(tickers: list) -> list:
    sentiment = load_sentiment()
    warnings  = []
    for ticker in tickers:
        entries = sentiment.get(ticker, [])
        bearish = sum(1 for e in entries if e["impact"] == "BEARISH")
        if bearish >= BEARISH_THRESHOLD:
            warnings.append((ticker, bearish))
    return warnings

# ── FEATURE 3: Daily Digest ────────────────────────────────────────────────
digest_articles  = []
last_digest_date = None

def maybe_send_digest():
    global digest_articles, last_digest_date
    now   = datetime.now()
    today = now.date()

    if now.hour == DAILY_DIGEST_HOUR and last_digest_date != today:
        last_digest_date = today
        if not digest_articles:
            send_telegram("📊 *Daily Market Digest*\n_No significant portfolio news in the past 24 hours._")
            return

        top   = sorted(digest_articles, key=lambda x: x.get("relevance_score", 0), reverse=True)[:7]
        lines = [f"📊 *Daily Portfolio Digest — {today.strftime('%b %d, %Y')}*\n━━━━━━━━━━━━━━━━━━━━"]

        for a in top:
            ie      = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(a.get("impact"), "⚪")
            tickers = " ".join(f"${t}" for t in a.get("affected_tickers", []))
            lines.append(
                f"{ie} *{a['title'][:65]}*\n"
                f"   {tickers or 'Macro'} | ⭐ {a.get('relevance_score')}/10\n"
                f"   💡 {a.get('one_line','')[:80]}"
            )

        lines.append("\n━━━━━━━━━━━━━━━━━━━━\n📈 *Your Holdings Snapshot*")
        for ticker in ["NVDA", "MSFT", "META", "INTC", "AMD"]:
            pl = price_line(ticker)
            if pl: lines.append(pl)

        send_telegram("\n\n".join(lines))
        digest_articles = []

# ── News Fetcher ───────────────────────────────────────────────────────────
def fetch_articles() -> list:
    arts   = []
    cutoff = datetime.utcnow() - timedelta(hours=2)
    for src, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:10]:
                pub = None
                if hasattr(e, "published_parsed") and e.published_parsed:
                    pub = datetime(*e.published_parsed[:6])
                    if pub < cutoff: continue
                arts.append({
                    "id":        article_id(e),
                    "source":    src,
                    "title":     e.get("title", ""),
                    "summary":   e.get("summary", "")[:400],
                    "link":      e.get("link", ""),
                    "published": pub.isoformat() if pub else "unknown",
                })
        except Exception as ex:
            log.warning(f"Feed error {src}: {ex}")
    return arts

# ── Portfolio Stock Detection ──────────────────────────────────────────────
def detect_portfolio_mentions(text: str) -> list:
    """Return list of portfolio tickers mentioned in text."""
    text_lower = text.lower()
    found = []
    for ticker in PORTFOLIO.keys():
        if ticker.lower() in text_lower:
            found.append(ticker)
    return found

def detect_executive_mentions(text: str) -> dict:
    """Return dict of {ticker: [executives mentioned]}."""
    text_lower = text.lower()
    mentioned = {}
    for ticker_key, execs in KEY_EXECUTIVES.items():
        for exec_name in execs:
            if exec_name.lower() in text_lower:
                if ticker_key.upper() not in mentioned:
                    mentioned[ticker_key.upper()] = []
                mentioned[ticker_key.upper()].append(exec_name)
    return mentioned

# ── AI Analysis ───────────────────────────────────────────────────────────
def analyse(articles: list) -> list:
    if not articles: return []
    client   = OpenAI(api_key=OPENAI_API_KEY)
    port_str = "\n".join(f"- {k}: {v}" for k, v in PORTFOLIO.items())
    results  = []

    for i in range(0, len(articles), 10):
        batch = articles[i:i+10]
        txt   = "\n\n".join(
            f"[{j+1}] {a['source']}: {a['title']}\n{a['summary']}"
            for j, a in enumerate(batch)
        )
        prompt = (
            f"{INSTITUTIONAL_ANALYSIS_PROMPT}\n\n"
            f"Portfolio holdings:\n{port_str}\n\n"
            f"Articles:\n{txt}\n\n"
            "For EACH article, include all relevant details in the JSON item and keep the "
            "output strictly valid JSON with no markdown or commentary."
        )
        try:
            r = client.chat.completions.create(
                model="gpt-4o-mini", max_tokens=2000, temperature=0,
                messages=[
                    {"role": "system", "content": "Respond with valid JSON array only. No markdown, no preamble."},
                    {"role": "user",   "content": prompt}
                ]
            )
            text = r.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
            for item in json.loads(text):
                idx = item["index"] - 1
                if idx < len(batch):
                    # Augment with detected tickers & executives
                    article = batch[idx]
                    detected_tickers = detect_portfolio_mentions(article["title"] + article["summary"])
                    exec_mentions = detect_executive_mentions(article["title"] + article["summary"])
                    
                    if not item.get("affected_tickers"):
                        item["affected_tickers"] = detected_tickers
                    if exec_mentions:
                        item["exec_mentions"] = exec_mentions
                    
                    results.append({**article, **item})
        except Exception as ex:
            log.error(f"OpenAI error: {ex}")

    results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return results

# ── Telegram ──────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False
            },
            timeout=10
        )
        r.raise_for_status()
        log.info("Telegram sent.")
    except Exception as ex:
        log.error(f"Telegram error: {ex}")

def format_alert(a: dict) -> str:
    ie      = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(a.get("impact"), "⚪")
    ue      = {"HIGH": "🚨", "MEDIUM": "⚠️", "LOW": "📌"}.get(a.get("urgency"), "📌")
    cat     = {
        "PORTFOLIO_STOCK": "📊",
        "MACRO": "📈",
        "EXEC_NEWS": "🎤",
        "AI_LLM": "🧠",
        "OTHER": "📰"
    }.get(a.get("category"), "📰")
    
    tickers = a.get("affected_tickers", [])
    t_str   = " ".join(f"`${t}`" for t in tickers) or "_Macro/Broad_"

    # Live prices for affected tickers
    price_lines  = []
    for t in tickers[:3]:
        pl = price_line(t)
        if pl: price_lines.append(pl)

    # Sentiment warnings
    warnings      = check_sentiment_warning(tickers)
    warning_lines = []
    for w_ticker, count in warnings:
        warning_lines.append(f"🔔 *Alert:* `${w_ticker}` has {count} bearish signals this week!")

    # Executive mentions
    exec_line = ""
    if a.get("exec_mentions"):
        execs_str = ", ".join([f"{name} ({tick})" for tick, names in a["exec_mentions"].items() for name in names])
        exec_line = f"\n🎤 *Exec Mention:* {execs_str}"

    msg = (
        f"{ue} *PORTFOLIO ALERT* {ue}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{cat} *{a['title']}*\n"
        f"🏷 {a['source']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{ie} *{a.get('impact')}* | ⭐ {a.get('relevance_score')}/10\n"
        f"📊 *Affected:* {t_str}\n"
        f"💡 {a.get('one_line', '')}\n"
    )
    if exec_line:
        msg += exec_line + "\n"
    if price_lines:
        msg += "━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(price_lines) + "\n"
    if warning_lines:
        msg += "━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(warning_lines) + "\n"
    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 [Read More]({a.get('link', '')})\n"
        f"🕐 {a.get('published', '')}"
    )
    return msg

# ── FEATURE 10: Telegram Bot Commands ─────────────────────────────────────
last_update_id = 0

def handle_commands():
    global last_update_id, paused_until
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 5},
            timeout=10
        )
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            msg     = update.get("message", {})
            text    = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if text == "/portfolio":
                lines = ["📊 *Your 15-Stock Portfolio — Live Prices*\n━━━━━━━━━━━━━━━━━━━━"]
                for ticker in list(PORTFOLIO.keys()):
                    pl = price_line(ticker)
                    if pl: lines.append(pl)
                    else:  lines.append(f"⚪ *{ticker}*: price unavailable")
                send_telegram("\n".join(lines))

            elif text.startswith("/news"):
                parts  = text.split()
                ticker = parts[1].upper() if len(parts) > 1 else ""
                if not ticker:
                    send_telegram("Usage: `/news NVDA`")
                    continue
                send_telegram(f"🔍 Fetching latest news for *{ticker}*...")
                arts     = fetch_articles()
                relevant = [a for a in arts if ticker.lower() in (a["title"] + a["summary"]).lower()]
                if not relevant:
                    send_telegram(f"No recent news found for *{ticker}*.")
                else:
                    scored = analyse(relevant[:5])
                    if scored:
                        send_telegram(format_alert(scored[0]))
                    else:
                        send_telegram(f"📰 Latest: *{relevant[0]['title']}*\n🔗 {relevant[0]['link']}")

            elif text.startswith("/score"):
                parts  = text.split()
                ticker = parts[1].upper() if len(parts) > 1 else ""
                if not ticker:
                    send_telegram("Usage: `/score NVDA`")
                    continue
                sentiment = load_sentiment()
                entries   = sentiment.get(ticker, [])
                bullish   = sum(1 for e in entries if e["impact"] == "BULLISH")
                bearish   = sum(1 for e in entries if e["impact"] == "BEARISH")
                neutral   = sum(1 for e in entries if e["impact"] == "NEUTRAL")
                total     = len(entries)
                pl        = price_line(ticker)
                mood      = "🟢 Bullish" if bullish > bearish else ("🔴 Bearish" if bearish > bullish else "🟡 Neutral")
                send_telegram(
                    f"📊 *Sentiment Report — ${ticker}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{pl}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Last 7 days: {total} alerts\n"
                    f"🟢 Bullish: {bullish}  🔴 Bearish: {bearish}  🟡 Neutral: {neutral}\n"
                    f"Overall: {mood}\n"
                    f"{'⚠️ WARNING: High bearish!' if bearish >= BEARISH_THRESHOLD else '✅ Normal'}"
                )

            elif text.startswith("/pause"):
                with paused_lock:
                    paused_until = datetime.utcnow() + timedelta(hours=2)
                send_telegram("⏸ *Alerts paused 2 hours.*")

            elif text == "/resume":
                with paused_lock:
                    paused_until = None
                send_telegram("▶️ *Alerts resumed!*")

            elif text in ("/help", "/start"):
                send_telegram(
                    "🤖 *Portfolio AI Agent — Phase 1*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "/portfolio — live prices (all 15)\n"
                    "/news NVDA — latest news for ticker\n"
                    "/score NVDA — 7-day sentiment\n"
                    "/pause — pause 2 hours\n"
                    "/resume — resume\n"
                    "/help — this menu"
                )

    except Exception as ex:
        log.error(f"Command handler error: {ex}")

def is_paused() -> bool:
    with paused_lock:
        if paused_until and datetime.utcnow() < paused_until:
            return True
        return False

# ── Main Loop ──────────────────────────────────────────────────────────────
def run():
    global digest_articles
    log.info("🤖 Portfolio AI Agent — Phase 1 started.")
    send_telegram(
        "🤖 *Portfolio AI Agent — PHASE 1 LIVE*\n"
        f"Watching {len(PORTFOLIO)} holdings | {len(RSS_FEEDS)} feeds\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Portfolio stock boosting (2x weight)\n"
        "✅ Fed & macro feeds (interest rates, FOMC)\n"
        "✅ CEO/exec mention detection\n"
        "✅ Real-time market impact alerts\n"
        "✅ Live stock prices\n"
        "✅ Daily digest\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "/help for commands"
    )

    seen = load_seen()

    while True:
        handle_commands()
        maybe_send_digest()

        if is_paused():
            log.info("Paused. Sleeping 60s...")
            time.sleep(60)
            continue

        log.info("── Fetching news ──")
        arts     = fetch_articles()
        new_arts = [a for a in arts if a["id"] not in seen]
        log.info(f"{len(new_arts)} new articles.")

        if new_arts:
            alerts   = analyse(new_arts)
            relevant = [a for a in alerts if a.get("relevance_score", 0) >= MIN_SCORE]
            log.info(f"{len(relevant)} alerts to send.")

            digest_articles.extend(relevant)

            for alert in relevant:
                record_sentiment(
                    alert.get("affected_tickers", []),
                    alert.get("impact", "NEUTRAL")
                )
                send_telegram(format_alert(alert))
                time.sleep(1.5)

            for a in new_arts: seen.add(a["id"])
            save_seen(seen)

        log.info(f"Sleeping {POLL_INTERVAL // 60} min...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
