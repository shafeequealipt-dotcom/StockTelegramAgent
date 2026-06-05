# Stock Telegram Agent

A Python agent that watches market, technology, and AI RSS feeds, uses OpenAI to score relevant stories, and sends Telegram alerts for a configured stock portfolio.

## Features

- Polls finance, technology, AI, and research RSS feeds.
- Scores articles against a portfolio using OpenAI.
- Sends Telegram alerts and supports `/portfolio`, `/news`, `/score`, `/report`, `/pause`, `/resume`, and `/help`.
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
- `OPENAI_API_KEY`

Do not commit `.env`, logs, or runtime state files.

## Run

```powershell
python agent.py
```

## Configuration

Optional environment variables:

- `OPENAI_MODEL`: defaults to `gpt-4o-mini`.
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

The `/report` command uses the built-in institutional market report prompt. It generates a Telegram-ready report from the bot's current market snapshot, portfolio prices, and recent feed articles. Sections without enough evidence are reported as unavailable rather than inferred.

## Security Notes

This project uses local environment variables for credentials. If a token is ever committed or appears in logs pushed to GitHub, rotate it immediately in the provider dashboard. Removing it from the latest commit is not enough because Git history may still contain the old value.
