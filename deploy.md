# Deploying to a headless server (Oracle Cloud)

The agent runs fully headless with no session-bound dependencies. RSS, Exa web
search, Yahoo prices/charts, and Yahoo analyst data all work with no browser
and no API keys. Reddit works by transplanting a browser cookie. Twitter is
**not recommended** from a datacenter IP (suspension risk).

## 1. Get the code on the server

```bash
ssh <user>@<oracle-ip>          # opc on Oracle Linux, ubuntu on Ubuntu images
git clone https://github.com/shafeequealipt-dotcom/StockTelegramAgent.git
cd StockTelegramAgent
bash deploy/setup.sh
```

`setup.sh` installs system deps, the Python venv, `mcporter`+Exa, and `rdt-cli`,
then prints the remaining manual steps and renders a systemd unit to
`/tmp/stock-agent.service`.

## 2. Configure secrets

```bash
cp .env.example .env
$EDITOR .env     # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENROUTER_API_KEY
```

The `.env` is gitignored — never commit it.

## 3. (Optional) Enable Reddit

There is no browser on the server, so do not run `rdt login` there. Log in once
on your **laptop** (`rdt login` after signing into reddit.com), then copy the
resulting credential file up:

```bash
# on your laptop
scp ~/.config/rdt-cli/credential.json <user>@<oracle-ip>:~/.config/rdt-cli/credential.json
```

Use a **throwaway Reddit account** — that cookie grants full account access and
will live on the server. When it expires (~1–2 months), re-`rdt login` locally
and re-copy the file.

## 4. Run as a service

```bash
sudo cp /tmp/stock-agent.service /etc/systemd/system/stock-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now stock-agent
journalctl -u stock-agent -f      # follow logs
```

## 5. Verify

In Telegram:

- `/sources` — confirm RSS, Exa, and (if configured) Reddit are active
- `/brief` — request a combined brief immediately
- `/analyst NVDA` — confirm Yahoo analyst data works from the server IP

## Source behavior on a datacenter IP

| Source | Headless? | Notes |
|--------|-----------|-------|
| RSS (38 feeds) | yes | no auth |
| Exa web search | yes | plain HTTPS, no key, no IP risk |
| Yahoo price/history/charts | yes | no key |
| Yahoo analyst ratings/targets | yes | cookie+crumb handshake, automatic; skipped + retried if rate-limited |
| Reddit | yes | copy `credential.json`; low IP risk |
| X/Twitter | risky | datacenter IPs get flagged — use a residential proxy or a disposable account, or leave it off |

## Updating later

```bash
cd StockTelegramAgent
git pull
.venv/bin/pip install -r requirements.txt   # only if requirements changed
sudo systemctl restart stock-agent
```

## Firewall note

The agent makes only **outbound** HTTPS calls (Telegram long-poll, OpenRouter,
RSS, Yahoo, Exa, Reddit). It listens on no ports, so no Oracle ingress/security
list changes are needed.
