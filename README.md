# Wallet Tracker Bot — Solana v1

A paid Telegram bot that tracks Solana wallets and sends real-time alerts when they buy, sell, or transfer. Paid users also get tax CSV export (Koinly-compatible). Payments are accepted in USDC on Solana — no Stripe, no banks, no chargebacks.

This is v1. Discord, BSC, Ethereum, smart-money discovery, and PnL leaderboards are on the roadmap (see "Next versions" at the bottom).

---

## What it does

- Users run `/add <wallet>` to track any Solana address.
- Every ~60 seconds the bot polls Helius for new transactions on every tracked wallet.
- New transactions get formatted as alerts and pushed to every user tracking that wallet.
- Free tier: 2 wallets. Paid tier ($9.99/month USDC): 50 wallets + tax CSV export.
- Users upgrade by sending USDC with a unique memo the bot generates — a background task watches the treasury wallet and auto-upgrades matching users.

---

## You'll need (all free or ~$5)

1. A **Telegram account** (you already have one).
2. A **Helius account** (free tier) — https://www.helius.dev. Sign up, create a project, copy the API key.
3. A **Solana wallet** you control (Phantom is easiest) — this is the *treasury wallet* where paying users send USDC. Copy the public address only. **Never share the seed phrase.**
4. A **GitHub account** (free) — for deploying to Railway.
5. A **Railway account** (free trial; ~$5/month after) — https://railway.app.

Total monthly cost to run the bot: roughly $5 (Railway). Everything else has a free tier you won't outgrow until you have real users.

---

## Setup — step by step

### Step 1. Create your Telegram bot

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot`.
3. Pick a display name (e.g., "Wallet Tracker") and a username (must end in `bot`, e.g., `wallet_tracker_v1_bot`).
4. BotFather will reply with a **token** that looks like `123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`.
5. Copy that token. You'll paste it into `.env` in a minute.

Also run `/setcommands` in BotFather and paste this block so the autocomplete menu works:

```
start - Welcome + command list
add - Track a new Solana wallet
remove - Stop tracking a wallet
list - Show your tracked wallets
status - Show your plan & usage
upgrade - Upgrade to paid tier
export - Download tax CSV (paid only)
help - Show help
```

### Step 2. Get a Helius API key

1. Go to https://www.helius.dev and sign up.
2. Create a new project (any name).
3. Copy the API key from the dashboard.

### Step 3. Create your treasury wallet

1. Install **Phantom** (browser extension or mobile app) if you don't have it.
2. Create a *new* wallet — do **not** reuse your personal trading wallet. This wallet will be public-facing.
3. Copy the public address (starts with a number or letter, 32–44 characters).
4. Write down the seed phrase and store it somewhere safe and offline. You'll need it to withdraw subscription revenue.

### Step 4. Find your own Telegram user ID

Message `@userinfobot` on Telegram — it'll reply with your numeric user ID. Save this.

### Step 5. Get the code running locally (test it works before deploying)

Open a terminal. If you're on Windows, use PowerShell or install Git Bash.

```bash
# Clone or download this folder, then:
cd wallet-tracker-bot

# Install Python 3.11+ if you don't have it: https://www.python.org/downloads/

# Create a virtual environment (keeps dependencies isolated)
python -m venv .venv
# On macOS/Linux:
source .venv/bin/activate
# On Windows:
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy the config template and fill it in
cp .env.example .env
# (on Windows: copy .env.example .env)

# Open .env in any text editor and paste in:
#   TELEGRAM_BOT_TOKEN, HELIUS_API_KEY, TREASURY_WALLET_ADDRESS, ADMIN_TELEGRAM_ID

# Run the bot
python bot.py
```

You should see log lines like `Starting Telegram bot` and `Wallet tracker loop started`.

Now open Telegram, find your bot (search the username you picked), send `/start`, then `/add <some_wallet>` and watch the magic happen.

### Step 6. Deploy to Railway (so it runs 24/7)

Running the bot on your laptop works for testing, but it'll die when you close it. Railway runs it in the cloud for ~$5/month.

1. Push this folder to a private GitHub repo. (If you don't know Git yet, GitHub Desktop is the easiest way.)
2. Go to https://railway.app and sign up with your GitHub account.
3. Click **New Project → Deploy from GitHub repo** and pick your repo.
4. Railway will detect Python and the `Procfile`.
5. In the Railway dashboard, go to **Variables** and add each variable from your local `.env` (everything except the example values). **Important**: use your real values, not the placeholder text.
6. Railway will build and start the bot. Check the **Deployments → Logs** tab — you should see `Starting Telegram bot` within a minute.
7. You're live. The bot will keep running and auto-redeploy whenever you push to the repo.

---

## How to test end-to-end before you charge anyone

1. `/start` — should show the welcome message.
2. `/add <any real Solana wallet>` — pick a known active wallet. Try one of these:
   - `5tzFkiKscXHK5ZXCGbXbH4eLTtQ1fFkA3YpzBsqnWXJz` (Jupiter aggregator, very active — lots of test alerts)
3. Wait 60–90 seconds. You should get at least one alert.
4. `/list` — should show the wallet.
5. `/status` — should show "Free tier, 1/2 wallets".
6. `/upgrade` — should give you a memo code and your treasury address.
7. **Real payment test**: send exactly 9.99 USDC to your treasury with the memo attached (Phantom: Send → USDC → Advanced → Memo). Wait 1–2 minutes. You should get a "Payment confirmed" message and `/status` should flip to paid.
8. `/export` — should now work and give you a CSV.
9. `/remove <address>` — should remove the wallet.

If any of these fail, check the Railway logs first.

---

## Going to market — how to get paying users

The product is technically easy; distribution is the whole game. Rough plan:

1. **Seed your tracked-wallet library with 20–30 known "smart money" Solana wallets** (find them on Birdeye's top traders page, Nansen, or X posts). Make the bot useful out of the box for free users.
2. **Post in 5–10 Solana-focused Telegram and Discord communities** — lead with the tax CSV feature, that's the hook for people who *already* trade a lot.
3. **Offer a 7-day free trial of paid tier** for early users in exchange for a testimonial. Manually flip them to paid in the DB if you want.
4. **Give 30% lifetime referral commissions** to the first 10 users who bring paying subscribers. Track this manually for now (you'll build it into the bot in v2).
5. **Target SERP for "Solana wallet tracker telegram"** with a simple landing page that funnels to your bot link (`https://t.me/your_bot_username`).

Realistic trajectory: 10–30 paying users in the first 60 days if you grind distribution, $100–300 MRR to start, growing based on how much of the above you do.

---

## Architecture (so you can debug it)

```
┌─────────────────────────────────────────────────┐
│                    bot.py                       │
│  (entry point — starts 3 concurrent pieces)     │
└──────┬──────────────────┬──────────────┬────────┘
       │                  │              │
       ▼                  ▼              ▼
┌─────────────┐   ┌──────────────┐  ┌──────────────┐
│ Telegram    │   │ Wallet       │  │ Payment      │
│ commands    │   │ tracker loop │  │ verifier loop│
│ (handlers.py)│   │ polls Helius │  │ polls treasury│
└──────┬──────┘   └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       └─────────────────┴─────────────────┘
                         │
                         ▼
                ┌─────────────────┐
                │  database.py    │
                │  (SQLite file)  │
                └─────────────────┘
```

- `config.py` — loads env vars
- `database.py` — SQLite schema + CRUD
- `solana_tracker.py` — Helius polling + alert formatting
- `payments.py` — treasury polling + memo matching
- `handlers.py` — Telegram command logic
- `tax_export.py` — Koinly-format CSV
- `bot.py` — wires everything together

---

## Common things that will break (and how to fix)

**"Missing required environment variable"**
Your `.env` file is missing or incomplete. Double-check `TELEGRAM_BOT_TOKEN`, `HELIUS_API_KEY`, `TREASURY_WALLET_ADDRESS`.

**Alerts not arriving**
- First-run behavior: the bot *skips alerts for pre-existing transactions* the first time it sees a wallet, so it doesn't spam you with old history. You need to wait for a new transaction. Test by adding an active wallet like the Jupiter example above.
- Check Railway logs for `Helius HTTP 429` — you've hit the free-tier rate limit. Back off, or upgrade Helius.
- Check that the wallet is actually transacting on Solscan.

**Payment didn't auto-verify**
- The user didn't include the memo, or included the wrong one. Memos must match exactly.
- The user sent SOL instead of USDC. Must be USDC (mint `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`).
- The user sent less than the required amount (we allow a 5-cent tolerance).
- Manually verify in Solscan, then run a DB update by hand if needed — or add an admin `/grant <user_id>` command in v1.1.

**Bot stops responding**
Railway deployment crashed or ran out of free credit. Check Dashboard → Deployments → Logs.

---

## Security notes (read before going live)

- The treasury wallet's **public address** is in your `.env` and on Railway — that's fine, it's public by design.
- The treasury wallet's **seed phrase** is NOT in this repo and never should be. Only you should have it.
- Railway environment variables are encrypted at rest. Don't paste real values into Git.
- This bot has no admin backdoor by default. If you need to manually grant a user paid status, open the SQLite DB with `sqlite3 data/bot.db` and run `UPDATE users SET tier='paid', subscription_expires='2026-12-31T00:00:00+00:00' WHERE telegram_id=<id>;`. Future versions should add a proper admin command gated by `ADMIN_TELEGRAM_ID`.
- The bot never holds user funds. It never asks for seed phrases. Neither should you.

---

## Roadmap (what comes next if v1 gets traction)

- **v2 (weeks 4–5):** Discord bot sharing the same backend and DB.
- **v3 (weeks 6–8):** BSC support via Moralis; basic PnL calculation per wallet.
- **v4 (weeks 9–12):** Ethereum support, smart-money discovery (surface wallets with best 30-day PnL), public PnL leaderboards for viral loops.
- **v5:** Referral program automation, Solana Pay QR codes for upgrades, premium alert filters (only alert on >$N trades).

Each version is funded by revenue from the previous one. Don't start v2 until you have at least 5 paying users on v1.
