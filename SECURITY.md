# Security Policy

## Secrets

Never commit real API keys, Telegram bot tokens, chat IDs, `.env` files, logs, or generated runtime state.

Required secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `OPENAI_API_KEY`

Use `.env.example` as the template and keep the real `.env` file local.

## If a Secret Is Exposed

1. Revoke or rotate the exposed credential immediately.
2. Remove the secret from the working tree.
3. Purge the secret from Git history if the repository is public or shared.
4. Force-push the cleaned history only after coordinating with anyone else using the repository.
