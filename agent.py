#!/usr/bin/env python3
"""
AI Stock News Agent — Enhanced Edition
Features:
  1. Live price fetch (Yahoo Finance)
  3. Daily 8 AM market digest
  4. Sentiment trend tracking (3+ bearish = warning)
  10. Telegram bot commands (/portfolio /news /score /pause /resume)
"""

import os, json, time, hashlib, logging, requests, feedparser, threading
from datetime import datetime, timedelta
from openai import OpenAI

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
BEARISH_THRESHOLD  = 3       # alerts in 7 days to trigger warning
DAILY_DIGEST_HOUR  = 8       # 8 AM local time

# ── Pause state ───────────────────────────────────────────────────────────
paused_until = None
paused_lock  = threading.Lock()

# ── Your Portfolio ─────────────────────────────────────────────────────────
PORTFOLIO = {
    "TQQQ": "ProShares UltraPro QQQ",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "AMZN": "Amazon",
    "GOOGL": "Alphabet / Google",
    "CRM":  "Salesforce",
    "PATH": "UiPath",
    "PYPL": "PayPal",
    "PLTR": "Palantir",
    "NFLX": "Netflix",
    "NOW":  "ServiceNow",
    "INTC": "Intel",
    "MU":   "Micron",
    "AAPL": "Apple",
    "TWLO": "Twilio",
    "META": "Meta",
    "ORCL": "Oracle",
    "SNDK": "SanDisk",
    "QQQ":  "Invesco QQQ ETF",
    "PANW": "Palo Alto Networks",
    "TEAM": "Atlassian",
    "SAP":  "SAP",
    "BABA": "Alibaba",
    "AMD":  "AMD",
}

RSS_FEEDS = [
    ("Reuters Tech",   "https://feeds.reuters.com/reuters/technologyNews"),
    ("TechCrunch AI",  "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge",      "https://www.theverge.com/rss/index.xml"),
    ("CNBC Tech",      "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("MarketWatch",    "https://feeds.marketwatch.com/marketwatch/internet"),
    ("ZDNet",          "https://www.zdnet.com/topic/enterprise-software/rss.xml"),
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
        price     = round(meta.get("regularMarketPrice", 0), 2)
        prev      = round(meta.get("previousClose", price), 2)
        change    = round(price - prev, 2)
        change_pct= round((change / prev) * 100, 2) if prev else 0
        currency  = meta.get("currency", "USD")
        market    = meta.get("marketState", "REGULAR")
        return {
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "currency": currency,
            "market": market,
            "error": False
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

def save_sentiment(s: dict): save_json(SENTIMENT_CACHE, s)

def record_sentiment(tickers: list, impact: str):
    sentiment = load_sentiment()
    now_str   = datetime.utcnow().isoformat()
    cutoff    = datetime.utcnow() - timedelta(days=7)

    for ticker in tickers:
        if ticker not in sentiment:
            sentiment[ticker] = []
        # Append new entry
        sentiment[ticker].append({"impact": impact, "time": now_str})
        # Prune older than 7 days
        sentiment[ticker] = [
            e for e in sentiment[ticker]
            if datetime.fromisoformat(e["time"]) > cutoff
        ]
    save_sentiment(sentiment)

def check_sentiment_warning(tickers: list) -> list:
    """Returns list of tickers with >= BEARISH_THRESHOLD bearish alerts in 7 days."""
    sentiment = load_sentiment()
    warnings  = []
    for ticker in tickers:
        entries  = sentiment.get(ticker, [])
        bearish  = sum(1 for e in entries if e["impact"] == "BEARISH")
        if bearish >= BEARISH_THRESHOLD:
            warnings.append((ticker, bearish))
    return warnings

# ── FEATURE 3: Daily Digest ────────────────────────────────────────────────
digest_articles = []   # accumulate articles during the day
last_digest_date = None

def maybe_send_digest():
    global digest_articles, last_digest_date
    now = datetime.now()
    today = now.date()

    if now.hour == DAILY_DIGEST_HOUR and last_digest_date != today:
        last_digest_date = today
        if not digest_articles:
            send_telegram("📊 *Daily AI Market Digest*\n_No significant news in the past 24 hours._")
            return

        # Sort by score
        top = sorted(digest_articles, key=lambda x: x.get("relevance_score", 0), reverse=True)[:7]
        lines = [f"📊 *Daily AI Market Digest — {today.strftime('%b %d, %Y')}*\n━━━━━━━━━━━━━━━━━━━━"]

        for a in top:
            ie     = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(a.get("impact"), "⚪")
            tickers= " ".join(f"${t}" for t in a.get("affected_tickers", []))
            lines.append(
                f"{ie} *{a['title'][:65]}*\n"
                f"   {tickers or 'General'} | ⭐ {a.get('relevance_score')}/10\n"
                f"   💡 {a.get('one_line','')[:80]}"
            )

        # Price snapshot for top holdings
        lines.append("\n━━━━━━━━━━━━━━━━━━━━\n📈 *Morning Price Snapshot*")
        for ticker in ["NVDA", "MSFT", "INTC", "META", "AMD", "NOW"]:
            pl = price_line(ticker)
            if pl: lines.append(pl)

        send_telegram("\n\n".join(lines))
        digest_articles = []   # reset for next day

# ── News Fetcher ───────────────────────────────────────────────────────────
def fetch_articles() -> list:
    arts   = []
    cutoff = datetime.utcnow() - timedelta(hours=1)
    for src, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:8]:
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
            f"You are an equity analyst. Portfolio:\n{port_str}\n\n"
            f"Articles:\n{txt}\n\n"
            "Return JSON array only. Each item needs: index, relevance_score(1-10), "
            "affected_tickers(list), impact(BULLISH/BEARISH/NEUTRAL), one_line, "
            "urgency(HIGH/MEDIUM/LOW). Only include score>=6."
        )
        try:
            r    = client.chat.completions.create(
                model="gpt-4o-mini", max_tokens=1000, temperature=0,
                messages=[
                    {"role": "system", "content": "Respond with valid JSON array only."},
                    {"role": "user",   "content": prompt}
                ]
            )
            text = r.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
            for item in json.loads(text):
                idx = item["index"] - 1
                if idx < len(batch):
                    results.append({**batch[idx], **item})
        except Exception as ex:
            log.error(f"OpenAI error: {ex}")

    results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return results

# ── Telegram ──────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=10
        )
        r.raise_for_status()
        log.info("Telegram sent.")
    except Exception as ex:
        log.error(f"Telegram error: {ex}")

def format_alert(a: dict) -> str:
    ie      = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(a.get("impact"), "⚪")
    ue      = {"HIGH": "🚨", "MEDIUM": "⚠️", "LOW": "📌"}.get(a.get("urgency"), "📌")
    tickers = a.get("affected_tickers", [])
    t_str   = " ".join(f"`${t}`" for t in tickers) or "_None_"

    # Feature 1: live prices for affected tickers
    price_lines = []
    for t in tickers[:3]:   # max 3 to keep message compact
        pl = price_line(t)
        if pl: price_lines.append(pl)

    # Feature 4: sentiment warnings
    warnings     = check_sentiment_warning(tickers)
    warning_lines= []
    for w_ticker, count in warnings:
        warning_lines.append(f"🔔 *Sentiment Warning:* `${w_ticker}` has {count} bearish alerts this week!")

    msg = (
        f"{ue} *AI STOCK ALERT* {ue}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📰 *{a['title']}*\n"
        f"🏷 {a['source']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{ie} *{a.get('impact')}* | ⭐ {a.get('relevance_score')}/10\n"
        f"📊 *Holdings:* {t_str}\n"
        f"💡 {a.get('one_line', '')}\n"
    )
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
            msg = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if chat_id != TELEGRAM_CHAT_ID:
                continue

            # /portfolio — show all holdings with live prices
            if text == "/portfolio":
                lines = ["📊 *Your Portfolio — Live Prices*\n━━━━━━━━━━━━━━━━━━━━"]
                for ticker in list(PORTFOLIO.keys())[:15]:   # first 15
                    pl = price_line(ticker)
                    if pl: lines.append(pl)
                    else:  lines.append(f"⚪ *{ticker}*: price unavailable")
                send_telegram("\n".join(lines))

            # /news TICKER — on-demand news for a ticker
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
                    send_telegram(f"No recent news found for *{ticker}* in current feeds.")
                else:
                    scored = analyse(relevant[:5])
                    if scored:
                        send_telegram(format_alert(scored[0]))
                    else:
                        send_telegram(f"📰 Latest: *{relevant[0]['title']}*\n🔗 {relevant[0]['link']}")

            # /score TICKER — sentiment score for a ticker
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
                    f"Overall mood: {mood}\n"
                    f"{'⚠️ WARNING: High bearish activity!' if bearish >= BEARISH_THRESHOLD else '✅ Sentiment normal'}"
                )

            # /pause — pause alerts for 2 hours
            elif text.startswith("/pause"):
                with paused_lock:
                    paused_until = datetime.utcnow() + timedelta(hours=2)
                send_telegram("⏸ *Alerts paused for 2 hours.*\nSend /resume to restart early.")

            # /resume — resume alerts
            elif text == "/resume":
                with paused_lock:
                    paused_until = None
                send_telegram("▶️ *Alerts resumed!*")

            # /help
            elif text == "/help" or text == "/start":
                send_telegram(
                    "🤖 *AI Stock News Agent — Commands*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "/portfolio — live prices for all holdings\n"
                    "/news NVDA — latest news for a ticker\n"
                    "/score NVDA — 7-day sentiment report\n"
                    "/pause — pause alerts for 2 hours\n"
                    "/resume — resume alerts\n"
                    "/help — show this menu"
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
    log.info("🤖 Enhanced AI Stock News Agent started.")
    send_telegram(
        "🤖 *AI Stock News Agent LIVE — Enhanced*\n"
        f"Watching {len(PORTFOLIO)} stocks | {len(RSS_FEEDS)} feeds\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Live prices on alerts\n"
        "✅ Daily 8 AM digest\n"
        "✅ Sentiment trend tracking\n"
        "✅ Bot commands active\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Send /help to see all commands"
    )

    seen = load_seen()

    while True:
        # Always handle commands (even when paused)
        handle_commands()

        # Check daily digest
        maybe_send_digest()

        if is_paused():
            log.info("Agent is paused. Sleeping 60s...")
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

            # Accumulate for daily digest
            digest_articles.extend(relevant)

            for alert in relevant:
                # Record sentiment
                record_sentiment(
                    alert.get("affected_tickers", []),
                    alert.get("impact", "NEUTRAL")
                )
                send_telegram(format_alert(alert))
                time.sleep(1.5)

            for a in new_arts: seen.add(a["id"])
            save_seen(seen)

        log.info("Sleeping 30 min...")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
