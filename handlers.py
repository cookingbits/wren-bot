"""
Telegram command handlers.

Each function is registered with the python-telegram-bot Application in bot.py.
All handlers are beginner-friendly — they guide the user with examples.

UX patterns:
  - Commands accept inline arguments: `/redeem CODE` and `/add ADDR [LABEL]`.
  - If invoked with no arguments, the bot enters an "awaiting" state for that
    user and the next plain-text message is treated as the argument.
  - Awaiting state expires after AWAITING_TIMEOUT_SECONDS so stale prompts
    don't accidentally consume unrelated messages later.
  - /cancel clears any pending awaiting state.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta, timezone

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

# How long an "awaiting next message" prompt remains valid.
AWAITING_TIMEOUT_SECONDS = 5 * 60

# Keys used inside ctx.user_data
_AWAITING_KEY = "awaiting"
_AWAITING_SET_AT_KEY = "awaiting_set_at"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(update: Update) -> None:
    user = update.effective_user
    if user:
        await ensure_user(user.id, user.username)


def _set_awaiting(ctx: ContextTypes.DEFAULT_TYPE, what: str) -> None:
    """Mark this user as awaiting a follow-up message of a given kind."""
    ctx.user_data[_AWAITING_KEY] = what
    ctx.user_data[_AWAITING_SET_AT_KEY] = datetime.now(timezone.utc).isoformat()


def _consume_awaiting(ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Pop the awaiting state if present and not stale. Returns the kind or None."""
    what = ctx.user_data.pop(_AWAITING_KEY, None)
    set_at_raw = ctx.user_data.pop(_AWAITING_SET_AT_KEY, None)
    if not what:
        return None
    if set_at_raw:
        try:
            set_at = datetime.fromisoformat(set_at_raw)
            if datetime.now(timezone.utc) - set_at > timedelta(seconds=AWAITING_TIMEOUT_SECONDS):
                return None
        except ValueError:
            return None
    return what


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

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
        "`/cancel` — abort the current prompt\n"
        "`/help` — show this message\n\n"
        "💡 Tip: `/add` and `/redeem` also work without arguments — the bot "
        "will ask, and your next message becomes the answer.\n\n"
        f"Free tier: track up to *{FREE_TIER_WALLET_LIMIT}* wallets.\n"
        f"Paid: *{PAID_TIER_WALLET_LIMIT}* wallets + tax CSV export + priority alerts, "
        f"*${SUBSCRIPTION_PRICE_USDC}/month*."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, ctx)


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear any pending awaiting-state prompt."""
    what = ctx.user_data.pop(_AWAITING_KEY, None)
    ctx.user_data.pop(_AWAITING_SET_AT_KEY, None)
    if what:
        await update.message.reply_text("Cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


# ---------------------------------------------------------------------------
# /add — accept inline OR prompt for address
# ---------------------------------------------------------------------------

async def add_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _register(update)

    if ctx.args:
        address = ctx.args[0].strip()
        label = " ".join(ctx.args[1:]).strip() or None
        await _do_add(update, ctx, address, label)
        return

    _set_awaiting(ctx, "wallet_address")
    await update.message.reply_text(
        "Send me the *wallet address* you want to track in your next message "
        "(or /cancel).\n\n"
        "Tip: you can also do it in one shot — `/add <address> [optional label]`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _do_add(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    address: str,
    label: str | None,
) -> None:
    """Validate and insert a tracked wallet. Shared by /add and the awaiting flow."""
    user_id = update.effective_user.id

    if not SOLANA_ADDRESS_RE.match(address):
        await update.message.reply_text(
            "❌ That doesn't look like a valid Solana address. "
            "Addresses are 32–44 characters, base58 (no 0, O, I, or l).\n\n"
            "Try again with `/add <address>` or send `/cancel`.",
            parse_mode=ParseMode.MARKDOWN,
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


# ---------------------------------------------------------------------------
# /redeem — accept inline OR prompt for code
# ---------------------------------------------------------------------------

async def redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Redeem a promo code. Accepts inline arg or prompts for it."""
    await _register(update)

    if ctx.args:
        await _do_redeem(update, ctx, ctx.args[0].strip())
        return

    _set_awaiting(ctx, "redeem_code")
    await update.message.reply_text(
        f"Send me your *promo code* in your next message (or /cancel).\n\n"
        f"Got one from {REVIEW_CHANNEL}? Drop it here. "
        f"Tip: you can also do it in one shot — `/redeem <CODE>`.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _do_redeem(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    raw_code: str,
) -> None:
    """Process a redeem attempt. Shared by /redeem and the awaiting flow."""
    user = update.effective_user
    user_id = user.id

    code = raw_code.strip().upper()
    promo = PROMO_CODES.get(code)
    if not promo:
        await update.message.reply_text(
            "❌ That code isn't valid. Double-check spelling — codes are case-insensitive."
        )
        return

    days = promo["days"]
    max_uses = promo.get("max_uses")

    status_, count = await record_redemption(user_id, code, days, max_uses=max_uses)

    if status_ == "already_redeemed":
        await update.message.reply_text(
            "⚠️ You've already redeemed that code. One per customer!"
        )
        return

    if status_ == "sold_out":
        await update.message.reply_text(
            f"🥲 *Sorry — all {max_uses} early-bird spots have been claimed.*\n\n"
            f"Watch {REVIEW_CHANNEL} for the next promo, or `/upgrade` to subscribe "
            f"for *${SUBSCRIPTION_PRICE_USDC}/month*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # status_ == "ok"
    new_exp = await grant_paid_tier(user_id, days=days)
    log.info(
        "Promo redemption: user_id=%s username=%s code=%s spot=%s/%s days=%s new_expiry=%s",
        user_id, user.username, code, count, max_uses, days, new_exp.isoformat(),
    )

    months = days // 30
    spot_line = ""
    if max_uses is not None:
        remaining = max_uses - count
        spot_line = f"\n🎟 *Spot {count} of {max_uses}* — {remaining} left.\n"

    await update.message.reply_text(
        f"🎉 *Promo redeemed!*\n\n"
        f"You're now on the *paid plan* for *{months} months* "
        f"(until *{new_exp.strftime('%Y-%m-%d')}*)."
        f"{spot_line}\n"
        f"📣 In return — please post a short honest review in {REVIEW_CHANNEL}. "
        f"It's the single biggest way you can help this bot grow. Thanks 🙏\n\n"
        f"Try `/add <wallet>` to start tracking up to {PAID_TIER_WALLET_LIMIT} wallets, "
        f"or `/status` to see your plan.",
        parse_mode=ParseMode.MARKDOWN,
    )

    if ADMIN_TELEGRAM_ID:
        try:
            handle = f"@{user.username}" if user.username else f"id={user_id}"
            cap_str = f"{count}/{max_uses}" if max_uses is not None else f"#{count}"
            await ctx.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=(
                    f"🎟 Redemption {cap_str}: {handle} used `{code}` "
                    f"(+{days}d, expires {new_exp.strftime('%Y-%m-%d')})"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.warning("Could not notify admin of redemption: %s", e)


# ---------------------------------------------------------------------------
# Plain-text message router — consumes any pending awaiting state
# ---------------------------------------------------------------------------

async def text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all for non-command text. Only acts if the user previously triggered
    /add or /redeem without arguments and is still within the awaiting window.
    Otherwise silently ignored.
    """
    awaiting = _consume_awaiting(ctx)
    if not awaiting:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    if awaiting == "redeem_code":
        await _do_redeem(update, ctx, text)
    elif awaiting == "wallet_address":
        await _do_add(update, ctx, text, None)


# ---------------------------------------------------------------------------
# /export
# ---------------------------------------------------------------------------

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
