"""
Telegram command handlers.

Each function is registered with the python-telegram-bot Application in bot.py.
All handlers are beginner-friendly — they guide the user with examples.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import (
    ADMIN_TELEGRAM_ID,
    FREE_TIER_WALLET_LIMIT,
    PAID_TIER_WALLET_LIMIT,
    PROMO_CODES,
    REVIEW_CHANNEL,
    SUBSCRIPTION_DAYS,
    SUBSCRIPTION_PRICE_USDC,
    TREASURY_WALLET_ADDRESS,
)
from database import (
    add_tracked_wallet,
    count_user_wallets,
    create_pending_payment,
    ensure_user,
    grant_paid_tier,
    is_paid,
    list_user_wallets,
    record_redemption,
    remove_tracked_wallet,
)
from tax_export import export_user_csv

log = logging.getLogger(__name__)

# Loose Solana address validation: base58, 32-44 chars.
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


async def _register(update: Update) -> None:
    user = update.effective_user
    if user:
        await ensure_user(user.id, user.username)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _register(update)
    text = (
        "👋 *Welcome to Wallet Tracker*\n\n"
        "Track Solana wallets and get real-time alerts when they buy or sell.\n\n"
        "*Commands*\n"
        "`/add <address> [label]` — track a new wallet\n"
        "`/list` — see your tracked wallets\n"
        "`/remove <address>` — stop tracking\n"
        "`/status` — show your plan\n"
        "`/upgrade` — upgrade to paid tier\n"
        "`/redeem <CODE>` — redeem a promo code\n"
        "`/export` — download tax CSV (paid only)\n"
        "`/help` — show this message\n\n"
        f"Free tier: track up to *{FREE_TIER_WALLET_LIMIT}* wallets.\n"
        f"Paid: *{PAID_TIER_WALLET_LIMIT}* wallets + tax CSV export + priority alerts, "
        f"*${SUBSCRIPTION_PRICE_USDC}/month*."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, ctx)


async def add_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _register(update)
    user_id = update.effective_user.id

    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/add <solana_address> [label]`\n\n"
            "Example: `/add 5tzFkiKscXHK5ZXCGbXbH4eLTtQ1fFkA3YpzBsqnWXJz whale_1`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    address = ctx.args[0].strip()
    label = " ".join(ctx.args[1:]).strip() or None

    if not SOLANA_ADDRESS_RE.match(address):
        await update.message.reply_text(
            "❌ That doesn't look like a valid Solana address. "
            "Addresses are 32–44 characters, base58 (no 0, O, I, or l)."
        )
        return

    # Tier limits
    current = await count_user_wallets(user_id)
    paid = await is_paid(user_id)
    limit = PAID_TIER_WALLET_LIMIT if paid else FREE_TIER_WALLET_LIMIT
    if current >= limit:
        if paid:
            msg = f"❌ You've reached the {limit}-wallet limit for paid users."
        else:
            msg = (
                f"❌ Free tier is limited to {limit} wallets.\n\n"
                f"Upgrade with /upgrade to track up to {PAID_TIER_WALLET_LIMIT}."
            )
        await update.message.reply_text(msg)
        return

    added = await add_tracked_wallet(user_id, address, "solana", label)
    if not added:
        await update.message.reply_text("That wallet is already in your list.")
        return

    short = f"{address[:4]}…{address[-4:]}"
    await update.message.reply_text(
        f"✅ Now tracking *{label or short}*.\n"
        f"You'll get alerts when this wallet transacts.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def remove_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _register(update)
    user_id = update.effective_user.id

    if not ctx.args:
        await update.message.reply_text("Usage: `/remove <solana_address>`", parse_mode=ParseMode.MARKDOWN)
        return

    address = ctx.args[0].strip()
    ok = await remove_tracked_wallet(user_id, address)
    if ok:
        await update.message.reply_text("✅ Removed.")
    else:
        await update.message.reply_text("That wallet wasn't in your list.")


async def list_wallets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _register(update)
    user_id = update.effective_user.id
    wallets = await list_user_wallets(user_id)
    if not wallets:
        await update.message.reply_text("You're not tracking any wallets yet. Try /add.")
        return

    lines = ["*Your tracked wallets:*"]
    for w in wallets:
        short = f"{w['address'][:4]}…{w['address'][-4:]}"
        name = w.get("label") or short
        lines.append(f"• *{name}* — `{w['address']}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_row = await ensure_user(update.effective_user.id, update.effective_user.username)
    paid = await is_paid(update.effective_user.id)
    count = await count_user_wallets(update.effective_user.id)
    limit = PAID_TIER_WALLET_LIMIT if paid else FREE_TIER_WALLET_LIMIT

    if paid:
        exp = user_row.get("subscription_expires")
        try:
            exp_dt = datetime.fromisoformat(exp) if exp else None
        except ValueError:
            exp_dt = None
        exp_str = exp_dt.strftime("%Y-%m-%d") if exp_dt else "unknown"
        tier_line = f"💎 *Paid* — expires {exp_str}"
    else:
        tier_line = "🆓 *Free tier*"

    await update.message.reply_text(
        f"{tier_line}\n"
        f"Wallets: {count} / {limit}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _register(update)
    user_id = update.effective_user.id

    payment = await create_pending_payment(user_id, SUBSCRIPTION_PRICE_USDC)
    memo = payment["memo"]

    msg = (
        f"💎 *Upgrade to Paid* — `${SUBSCRIPTION_PRICE_USDC}` USDC for *{SUBSCRIPTION_DAYS} days*\n\n"
        "*How to pay:*\n"
        f"1. Send *{SUBSCRIPTION_PRICE_USDC} USDC* on *Solana* to:\n"
        f"   `{TREASURY_WALLET_ADDRESS}`\n\n"
        f"2. *IMPORTANT:* include this memo in the transaction:\n"
        f"   `{memo}`\n\n"
        "3. Wait ~1 minute. I'll confirm automatically once the payment lands.\n\n"
        "💡 Phantom wallet: tap *Send → USDC → Advanced → Memo*, paste the code above.\n"
        "💡 Solflare: tap *Send → Advanced → Memo*.\n\n"
        "⚠️ Must be USDC (not SOL). Must include the memo above — we use it to identify your payment."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Redeem a promo code for free paid days. One use per user per code."""
    await _register(update)
    user = update.effective_user
    user_id = user.id

    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/redeem <CODE>`\n\n"
            f"Got a promo code from {REVIEW_CHANNEL}? Type it after /redeem.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    code = ctx.args[0].strip().upper()
    days = PROMO_CODES.get(code)
    if not days:
        await update.message.reply_text(
            "❌ That code isn't valid. Double-check spelling — codes are case-insensitive."
        )
        return

    recorded = await record_redemption(user_id, code, days)
    if not recorded:
        await update.message.reply_text(
            "⚠️ You've already redeemed that code. One per customer!"
        )
        return

    new_exp = await grant_paid_tier(user_id, days=days)
    log.info(
        "Promo redemption: user_id=%s username=%s code=%s days=%s new_expiry=%s",
        user_id, user.username, code, days, new_exp.isoformat(),
    )

    months = days // 30
    await update.message.reply_text(
        f"🎉 *Promo redeemed!*\n\n"
        f"You're now on the *paid plan* for *{months} months* "
        f"(until *{new_exp.strftime('%Y-%m-%d')}*).\n\n"
        f"📣 In return — please post a short honest review in {REVIEW_CHANNEL}. "
        f"It's the single biggest way you can help this bot grow. Thanks 🙏\n\n"
        f"Try `/add <wallet>` to start tracking up to {PAID_TIER_WALLET_LIMIT} wallets, "
        f"or `/status` to see your plan.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Notify admin (so you can spot-check redemptions)
    if ADMIN_TELEGRAM_ID:
        try:
            handle = f"@{user.username}" if user.username else f"id={user_id}"
            await ctx.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=(
                    f"🎟 Redemption: {handle} used `{code}` "
                    f"(+{days}d, expires {new_exp.strftime('%Y-%m-%d')})"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.warning("Could not notify admin of redemption: %s", e)


async def export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _register(update)
    user_id = update.effective_user.id

    if not await is_paid(user_id):
        await update.message.reply_text(
            "📄 Tax CSV export is a *paid* feature.\n\n"
            "Upgrade with /upgrade to generate Koinly-compatible CSVs of all your tracked wallets.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    wallets = await list_user_wallets(user_id)
    if not wallets:
        await update.message.reply_text("No wallets tracked yet — add some with /add first.")
        return

    await update.message.reply_text("📄 Generating your tax CSV…")
    try:
        csv_text = await export_user_csv(user_id, wallets)
    except Exception as e:
        log.exception("Export failed for %s: %s", user_id, e)
        await update.message.reply_text("❌ Export failed. Try again in a minute, or contact support.")
        return

    buf = io.BytesIO(csv_text.encode("utf-8"))
    buf.name = f"wallet_tracker_export_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"

    await update.message.reply_document(
        document=buf,
        filename=buf.name,
        caption=(
            "📄 Here's your tax CSV (Koinly-compatible).\n"
            "Import into Koinly, CoinTracker, or CoinLedger to generate tax reports."
        ),
    )
