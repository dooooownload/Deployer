# HusteRIX Cloudflare Telegram deployer

Private Telegram bot that deploys the included `worker.js` as the Cloudflare Worker `husterix`, with the existing D1 database `zeus-db-eu7n21` bound as `DB`. It offers inline-keyboard Deploy and delete/recreate Redeploy controls.

## Setup

1. Use Python 3.10+ and create a Telegram bot with BotFather.
2. Create a Cloudflare API token scoped to the target account. Grant only what the bot needs: Workers Scripts Read/Edit (including delete for Redeploy), D1 Read, and account settings/subdomain read/write as requested by Cloudflare. The bot checks the account and D1 names; it does not create or delete the database.
3. Set `TELEGRAM_ADMIN_ID` in the host environment. Paste a newly generated BotFather token into the `TELEGRAM_BOT_TOKEN` constant near the top of `bot.py` if you want the token inside the source. The token previously shared in chat must be revoked and must not be reused. Alternatively, set `TELEGRAM_BOT_TOKEN` in the host environment.
4. Install and run:

   ```sh
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   python bot.py
   ```

5. Open the bot in a private chat as the configured Telegram admin. Send `/start`, enter the Cloudflare API token, then use the inline buttons.

## Important behavior

- Cloudflare API tokens are held in bot memory only, not written to disk or D1. They are lost when the process exits. The bot tries to delete the Telegram message containing the token immediately, but Telegram is not an end-to-end encrypted secret manager.
- The requested address requires the Cloudflare account's workers.dev subdomain to already be `freebirds22`. The bot checks and stops rather than changing that account-wide setting. An email address does not automatically determine a workers.dev subdomain.
- The bot checks for the exact D1 name in the selected account and uses `DB` as the binding name expected by the source. It never creates or deletes the D1 database.
- Redeploy has an extra inline confirmation. It deletes the existing Worker script, then uploads the source again; this can briefly interrupt traffic. D1 data is not deleted. If upload fails after deletion, redeploy manually from the Cloudflare dashboard or rerun Deploy.
- Subscription paths use the Worker application's D1 username, not the Cloudflare or Telegram panel login. The bot displays `/sub/USERNAME_IN_D1` by default. Set `SUBSCRIPTION_USERNAME` in `bot.py` to show a specific existing D1 username as a clickable link.

## Source review note

The supplied Worker source contains a self-update feature that can fetch JavaScript from `hoplimit.shop` or a GitHub repository when its panel's update function is used. Review and trust those upstreams before enabling that feature. This bot deploys the provided source as-is and does not invoke that updater.
