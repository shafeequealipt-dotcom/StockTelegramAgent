# Stock Telegram Agent

A Python agent that watches market, Fed, political, global, and AI news (RSS plus optional X/Twitter, Reddit, and Exa web search), uses an OpenRouter-hosted AI model to score market-moving stories, and sends a consolidated Telegram *Market Intelligence Brief* for validating moves in a configured stock portfolio.

## Features

- Polls verified free, trustworthy sources across four themes: **US stocks & earnings**, **Fed & rates**, **politics & policy** (White House, Trump administration, tariffs, regulation), and **global & geopolitics** (wars, OPEC, China, trade) — plus tech/AI.
- Optional **social & web sources** via [Agent-Reach](https://github.com/Panniantong/Agent-Reach) upstream CLIs: X/Twitter, Reddit, and Exa web search. Each degrades gracefully — sources you have not configured are simply skipped (`/sources` shows status).
- **Consolidated brief**: instead of a stream of one-off alerts, the agent groups market-moving news by theme into a single *Market Intelligence Brief*, sent every few hours (`BRIEF_INTERVAL_HOURS`) with a fuller morning edition. Request one anytime with `/brief`. Set `REALTIME_ALERTS=true` to also get instant per-item alerts.
- Macro/political/global news that moves markets is scored on **market impact**, even when it names no portfolio ticker.
- Scores articles using any model available on [OpenRouter](https://openrouter.ai/models); switch it from Telegram with `/model` (persists across restarts).
- Ask the agent anything with `/q` — it answers using live market data, portfolio prices, and the latest headlines.
- Always responsive: news polling runs in a background thread while the bot long-polls Telegram, so commands are answered within seconds at any time.
- Analyst ratings and price targets (consensus, mean/high/low target, buy/hold/sell counts) via Yahoo Finance — fetched headlessly with a cookie+crumb handshake, so it works on a server with no API key. Use `/analyst NVDA`, and it is folded into `/q` answers automatically.
- Supports `/brief`, `/sources`, `/portfolio`, `/news`, `/score`, `/analyst`, `/report`, `/q`, `/model`, `/pause`, `/resume`, and `/help`.
- Stores runtime state under `data/` and logs under `logs/`; neither should be committed.
- Validates required secrets on startup and redacts tokens from logs.

## Setup

```powershell
cd C:\Projects\StockTelegramAgent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Create a Telegram Bot

Telegram bots are created and managed through the official `@BotFather` bot.

1. Open Telegram on desktop, mobile, or web.
2. Search for `@BotFather` and open the verified BotFather chat.
3. Send `/newbot`.
4. Enter a display name, for example `Stock Telegram Agent`.
5. Enter a unique username ending in `bot`, for example `my_stock_alert_bot`.
6. Copy the token BotFather returns. This is your `TELEGRAM_BOT_TOKEN`.

Keep this token private. Anyone with the token can control your bot.

## Get Your Telegram Chat ID

For a private chat with the bot:

1. Open your new bot in Telegram.
2. Press **Start** or send `/start`.
3. In PowerShell, replace `<your-telegram-bot-token>` with the token from BotFather and run:

```powershell
Invoke-RestMethod "https://api.telegram.org/bot<your-telegram-bot-token>/getUpdates"
```

4. In the response, find `message.chat.id`. That number is your `TELEGRAM_CHAT_ID`.

For a group chat:

1. Add the bot to the group.
2. Send a message in the group, such as `/start`.
3. Run the same `getUpdates` command.
4. Use the `message.chat.id` value for the group. Group IDs are often negative numbers.

Edit `.env` and set real values for:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OPENROUTER_API_KEY` (create one at https://openrouter.ai/keys)

Do not commit `.env`, logs, or runtime state files.

## Run

```powershell
python agent.py
```

## Configuration

Optional environment variables:

- `OPENROUTER_MODEL`: default model on startup; defaults to `openai/gpt-4o-mini`.
- `OPENROUTER_MODELS`: comma-separated list of model ids offered by the `/model` command. A sensible default list is built in.
- `POLL_INTERVAL_SECONDS`: defaults to `1800`.
- `MIN_SCORE`: defaults to `6`.
- `BEARISH_THRESHOLD`: defaults to `3`.
- `DAILY_DIGEST_HOUR`: local hour from `0` to `23`; defaults to `8`.
- `FEED_LOOKBACK_HOURS`: defaults to `2`.
- `REQUEST_TIMEOUT_SECONDS`: defaults to `10`.
- `AGENT_DATA_DIR`: defaults to `data`.
- `AGENT_LOG_DIR`: defaults to `logs`.
- `ANALYSIS_PROMPT`: optional custom analysis instructions for the AI agent.
- `ANALYSIS_PROMPT_FILE`: optional path to a UTF-8 text file containing custom analysis instructions. Use this instead of `ANALYSIS_PROMPT` for longer prompts.

The custom analysis prompt changes how the agent evaluates articles. The JSON output contract is still enforced in code because Telegram alerts, scoring, and sentiment tracking depend on those fields.

## Asking Questions from Telegram

Send `/q` followed by any question:

```
/q why is NVDA down today?
/q what is the status of arm now?
/q summarize what the Fed said this week
```

The agent resolves any stock, ETF, or index mentioned in the question — it does not have to be in your portfolio. For each one it fetches the live price and 1-year history from Yahoo Finance, computes returns (1d/1w/1m/3m/6m/1y), 52-week range, 50/200-day averages, and volume, and searches the web for its latest news via Google News. The answer comes from the active model grounded in that data, followed by a 1-year price chart image for each stock.

## Switching Models from Telegram

Send `/model` to the bot to see the current model and the numbered list of available models. Switch with either form:

```
/model 3
/model anthropic/claude-sonnet-4.5
```

Any valid OpenRouter model id is accepted, even if it is not in the list. The selection is saved to `data/model.json` and survives restarts.

The `/report` command uses the built-in institutional market report prompt. It generates a Telegram-ready report from the bot's current market snapshot, portfolio prices, and recent feed articles. Sections without enough evidence are reported as unavailable rather than inferred.

## Consolidated Brief & Social Sources

The agent's default delivery is a single *Market Intelligence Brief* that groups market-moving news into **Fed & Rates**, **US Stocks & Earnings**, **Politics & Policy**, **Global & Geopolitics**, and **AI & Tech**. It is sent every `BRIEF_INTERVAL_HOURS` (default 4) with a fuller morning edition at `DAILY_DIGEST_HOUR`. Send `/brief` for one on demand, or set `REALTIME_ALERTS=true` to also receive instant per-item alerts.

In addition to RSS, the agent can pull live signals from X/Twitter, Reddit, and Exa web search using [Agent-Reach](https://github.com/Panniantong/Agent-Reach)'s upstream CLIs. These are optional — the agent runs fully on RSS and silently skips any source that is not installed/authenticated. Run `/sources` in Telegram to see what is active. Setup commands for each source are documented in `.env.example`. The search queries fed to these sources are configurable via `SOCIAL_QUERIES`, and the subreddits via `REDDIT_SUBREDDITS`.

All market data (prices, history, charts, analyst ratings/targets) comes from Yahoo Finance with no API key. Analyst data uses Yahoo's cookie+crumb handshake, which the agent performs automatically and headlessly — no browser needed — so the full feature set works on a headless server. If Yahoo rate-limits the crumb handshake, analyst data is skipped for that call and retried later; prices and history are unaffected.

## Security Notes

This project uses local environment variables for credentials. If a token is ever committed or appears in logs pushed to GitHub, rotate it immediately in the provider dashboard. Removing it from the latest commit is not enough because Git history may still contain the old value.
