#!/usr/bin/env bash
#
# One-shot setup for the Stock Telegram Agent on a headless server (Oracle
# Cloud, etc.). Run it from the repository root on the server:
#
#   git clone https://github.com/shafeequealipt-dotcom/StockTelegramAgent.git
#   cd StockTelegramAgent
#   bash deploy/setup.sh
#
# It installs system deps, the Python venv, and the optional upstream CLIs
# (Exa via mcporter, Reddit via rdt). It does NOT install/auth Twitter (not
# recommended from a datacenter IP) and does NOT create your .env or transfer
# cookies — those steps are printed at the end.

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
echo "==> App directory: $APP_DIR"

# --- system packages ---------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip pipx nodejs npm
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip pipx nodejs npm
else
    echo "!! Unsupported package manager. Install python3-venv, pipx, nodejs, npm manually."
fi
pipx ensurepath || true
export PATH="$HOME/.local/bin:$PATH"

# --- python venv -------------------------------------------------------------
echo "==> Creating Python venv and installing requirements"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

# --- Exa web search (free, no key, no IP risk) -------------------------------
echo "==> Installing mcporter + Exa (web search)"
sudo npm install -g mcporter
mkdir -p "$HOME/.mcporter"
mcporter config add exa https://mcp.exa.ai/mcp || true

# --- Reddit CLI (optional; needs a credential.json copied from your laptop) --
echo "==> Installing rdt-cli (Reddit)"
pipx install 'git+https://github.com/public-clis/rdt-cli.git' || \
    echo "!! rdt install skipped/failed — Reddit will be skipped until installed."

# --- systemd unit ------------------------------------------------------------
echo "==> Rendering systemd unit"
UNIT_SRC="$APP_DIR/deploy/stock-agent.service"
UNIT_OUT="/tmp/stock-agent.service"
sed -e "s#__USER__#$(id -un)#g" \
    -e "s#__APP_DIR__#$APP_DIR#g" \
    -e "s#__HOME__#$HOME#g" \
    "$UNIT_SRC" > "$UNIT_OUT"

cat <<EOF

============================================================
Setup complete. Remaining manual steps:

1. Create your .env (never commit it):
     cp .env.example .env && \$EDITOR .env
   Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENROUTER_API_KEY.

2. (Optional) Enable Reddit — copy the cookie file from your LOCAL machine,
   where you ran 'rdt login' in a browser session:
     # on your laptop:
     scp ~/.config/rdt-cli/credential.json $(id -un)@<oracle-ip>:~/.config/rdt-cli/credential.json
   Use a THROWAWAY Reddit account — the cookie grants full account access.

3. Install and start the service:
     sudo cp $UNIT_OUT /etc/systemd/system/stock-agent.service
     sudo systemctl daemon-reload
     sudo systemctl enable --now stock-agent
     journalctl -u stock-agent -f

Verify in Telegram: /sources then /brief
============================================================
EOF
