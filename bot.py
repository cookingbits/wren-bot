"""
Main entry point.

Starts three concurrent pieces:
  1. Telegram bot (handles user commands)
  2. Wallet tracker loop (polls Helius for tracked-wallet activity)
  3. Payment verifier loop (polls treasury wallet for incoming USDC)

Run with: `python bot.py`
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import handlers
from config import (
    PAYMENT_POLL_INTERVAL,
    SUBSCRIPTION_PRICE_USDC,
    TELEGRAM_BOT_TOKEN,
    WALLET_POLL_INTERVAL,
)
from database import init_db, list_user_wallets
from payments import check_pending_payments
from solana_tracker import format_alert_message, poll_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs every Telegram API call at INFO with the full URL — and the URL
# contains the bot token. Bump to WARNING so the token never lands in logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def wallet_tracker_loop(bot: Bot) -> None:
    """Poll tracked wallets on a fixed interval and forward alerts to users."""
    log.info("Wallet tracker loop started (interval=%ss)", WALLET_POLL_INTERVAL)
    while True:
        try:
            alerts = await poll_once()
            for alert in alerts:
                # Each alert may be subscribed to by multiple users.
                for user_id in alert.subscribed_user_ids:
                    # Look up the user's label for this address (per-user)
                    label = None
                    try:
                        wallets = await list_user_wallets(user_id)
                        for w in wallets:
                            if w["address"] == alert.address:
                                label = w.get("label")
                                break
                    except Exception:
                        pass

                    text = format_alert_message(alert, label=label)
                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=True,
                        )
                    except Forbidden:
                        log.info("User %s blocked the bot; skipping", user_id)
                    except BadRequest as e:
                        log.warning("BadRequest for user %s: %s", user_id, e)
                    except Exception as e:
                        log.exception("Failed to send alert to %s: %s", user_id, e)
        except Exception as e:
            log.exception("Wallet tracker loop error: %s", e)

        await asyncio.sleep(WALLET_POLL_INTERVAL)


async def payment_verifier_loop(bot: Bot) -> None:
    """Poll the treasury wallet for incoming USDC and notify paying users."""
    log.info("Payment verifier loop started (interval=%ss)", PAYMENT_POLL_INTERVAL)

    async def notify(user_id: int, info: dict) -> None:
        expiry: datetime = info["new_expiry"]
        tx = info["tx_signature"]
        amount = info["amount"]
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"💎 *Payment confirmed!* Thanks — you're now on the paid plan.\n\n"
                    f"Amount: *{amount:.2f} USDC*\n"
                    f"Paid until: *{expiry.strftime('%Y-%m-%d')}*\n\n"
                    f"[View transaction](https://solscan.io/tx/{tx})"
                ),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.exception("Failed to notify %s of payment: %s", user_id, e)

    while True:
        try:
            verified = await check_pending_payments(notifier=notify)
            if verified:
                log.info("Verified %d payment(s) this cycle", len(verified))
        except Exception as e:
            log.exception("Payment verifier error: %s", e)
        await asyncio.sleep(PAYMENT_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Bootstrapping
# ---------------------------------------------------------------------------

async def _post_init(app: Application) -> None:
    """Called after the Application is initialized but before polling starts."""
    # Initialize DB inside PTB's event loop (avoids asyncio.run closing the loop)
    await init_db()
    # Launch background workers as asyncio tasks tied to the app's event loop
    app.create_task(wallet_tracker_loop(app.bot))
    app.create_task(payment_verifier_loop(app.bot))
    log.info("Background tasks scheduled. Subscription price: $%.2f USDC",
             SUBSCRIPTION_PRICE_USDC)


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # Register commands
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("add", handlers.add_wallet))
    app.add_handler(CommandHandler("remove", handlers.remove_wallet))
    app.add_handler(CommandHandler("list", handlers.list_wallets))
    app.add_handler(CommandHandler("status", handlers.status))
    app.add_handler(CommandHandler("upgrade", handlers.upgrade))
    app.add_handler(CommandHandler("redeem", handlers.redeem))
    app.add_handler(CommandHandler("cancel", handlers.cancel))
    app.add_handler(CommandHandler("export", handlers.export))

    # Plain-text catch-all: feeds the awaiting-state flow for /add and /redeem.
    # Only triggers on non-command text messages; silently no-ops if no pending prompt.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text_message))

    log.info("Starting Telegram bot — press Ctrl+C to stop")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
