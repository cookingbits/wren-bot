"""
Database layer — async SQLite access.

Tables:
  users             — one row per Telegram user
  tracked_wallets   — wallets a user wants to watch
  seen_transactions — every transaction we've already alerted on (dedup)
  payments          — pending & verified USDC payments
  redemptions       — promo code redemptions (one row per user/code)
"""
from __future__ import annotations

import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from config import DATABASE_PATH, SUBSCRIPTION_DAYS


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id          INTEGER PRIMARY KEY,
    username             TEXT,
    tier                 TEXT NOT NULL DEFAULT 'free',
    subscription_expires TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracked_wallets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    address     TEXT NOT NULL,
    chain       TEXT NOT NULL DEFAULT 'solana',
    label       TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(user_id, address, chain),
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tracked_wallets_address
    ON tracked_wallets(address);

CREATE TABLE IF NOT EXISTS seen_transactions (
    signature   TEXT NOT NULL,
    address     TEXT NOT NULL,
    timestamp   INTEGER NOT NULL,
    raw_json    TEXT,
    PRIMARY KEY (signature, address)
);

CREATE INDEX IF NOT EXISTS idx_seen_tx_address_ts
    ON seen_transactions(address, timestamp);

CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    memo          TEXT NOT NULL UNIQUE,
    amount_usdc   REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    tx_signature  TEXT,
    created_at    TEXT NOT NULL,
    verified_at   TEXT,
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);

CREATE TABLE IF NOT EXISTS redemptions (
    user_id      INTEGER NOT NULL,
    code         TEXT NOT NULL,
    days_granted INTEGER NOT NULL,
    redeemed_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, code),
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);
"""


async def init_db() -> None:
    """Create tables and the data directory if missing."""
    Path(os.path.dirname(DATABASE_PATH) or ".").mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

async def ensure_user(telegram_id: int, username: Optional[str]) -> dict[str, Any]:
    """Create user record if missing. Returns the current row."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, created_at) "
            "VALUES (?, ?, ?)",
            (telegram_id, username, _now_iso()),
        )
        # Always update username in case it changed
        await db.execute(
            "UPDATE users SET username = ? WHERE telegram_id = ?",
            (username, telegram_id),
        )
        await db.commit()
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_user(telegram_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def is_paid(telegram_id: int) -> bool:
    """True if user has an active paid subscription."""
    user = await get_user(telegram_id)
    if not user:
        return False
    if user.get("tier") != "paid":
        return False
    expires = user.get("subscription_expires")
    if not expires:
        return False
    try:
        expires_dt = datetime.fromisoformat(expires)
    except ValueError:
        return False
    return expires_dt > datetime.now(timezone.utc)


async def grant_paid_tier(telegram_id: int, days: Optional[int] = None) -> datetime:
    """Upgrade user to paid, extending subscription by `days` (default SUBSCRIPTION_DAYS)."""
    if days is None:
        days = SUBSCRIPTION_DAYS
    user = await get_user(telegram_id)
    now = datetime.now(timezone.utc)

    # If they already have time left, stack onto it
    current_exp = None
    if user and user.get("subscription_expires"):
        try:
            current_exp = datetime.fromisoformat(user["subscription_expires"])
        except ValueError:
            current_exp = None
    base = current_exp if current_exp and current_exp > now else now
    new_exp = base + timedelta(days=days)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE users SET tier = 'paid', subscription_expires = ? "
            "WHERE telegram_id = ?",
            (new_exp.isoformat(), telegram_id),
        )
        await db.commit()
    return new_exp


# ---------------------------------------------------------------------------
# Tracked wallets
# ---------------------------------------------------------------------------

async def add_tracked_wallet(
    user_id: int, address: str, chain: str = "solana", label: Optional[str] = None
) -> bool:
    """Returns True if inserted, False if it already existed."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO tracked_wallets (user_id, address, chain, label, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, address, chain, label, _now_iso()),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_tracked_wallet(user_id: int, address: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute(
            "DELETE FROM tracked_wallets WHERE user_id = ? AND address = ?",
            (user_id, address),
        )
        await db.commit()
        return cur.rowcount > 0


async def list_user_wallets(user_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tracked_wallets WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def count_user_wallets(user_id: int) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM tracked_wallets WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_all_tracked_addresses() -> list[dict[str, Any]]:
    """
    Returns distinct (address, chain) pairs with a list of subscribed user_ids.
    Used by the tracker so we only fetch each wallet once.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT address, chain, GROUP_CONCAT(user_id) AS user_ids "
            "FROM tracked_wallets GROUP BY address, chain"
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        user_ids = [int(u) for u in (r["user_ids"] or "").split(",") if u]
        result.append({"address": r["address"], "chain": r["chain"], "user_ids": user_ids})
    return result


# ---------------------------------------------------------------------------
# Seen transactions (dedup)
# ---------------------------------------------------------------------------

async def is_tx_seen(signature: str, address: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_transactions WHERE signature = ? AND address = ?",
            (signature, address),
        ) as cur:
            return (await cur.fetchone()) is not None


async def count_seen_for_address(address: str) -> int:
    """Used to detect whether this is the first poll for an address."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM seen_transactions WHERE address = ?",
            (address,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def mark_tx_seen(
    signature: str, address: str, timestamp: int, raw_json: Optional[str] = None
) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_transactions "
            "(signature, address, timestamp, raw_json) VALUES (?, ?, ?, ?)",
            (signature, address, timestamp, raw_json),
        )
        await db.commit()


async def get_transactions_for_address(
    address: str, since_ts: Optional[int] = None
) -> list[dict[str, Any]]:
    """Fetch cached transactions for tax export."""
    query = "SELECT * FROM seen_transactions WHERE address = ?"
    params: list[Any] = [address]
    if since_ts is not None:
        query += " AND timestamp >= ?"
        params.append(since_ts)
    query += " ORDER BY timestamp DESC"
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def _generate_memo() -> str:
    """Short random code the user must include as a Solana memo."""
    alphabet = string.ascii_uppercase + string.digits
    return "TRK-" + "".join(secrets.choice(alphabet) for _ in range(8))


async def create_pending_payment(user_id: int, amount_usdc: float) -> dict[str, Any]:
    """Create a new pending payment with a unique memo code."""
    memo = _generate_memo()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO payments (user_id, memo, amount_usdc, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, memo, amount_usdc, _now_iso()),
        )
        await db.commit()
    return {"memo": memo, "amount_usdc": amount_usdc, "user_id": user_id}


async def get_pending_payments() -> list[dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM payments WHERE status = 'pending'"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def find_payment_by_memo(memo: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM payments WHERE memo = ?", (memo,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def mark_payment_verified(payment_id: int, tx_signature: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE payments SET status = 'verified', tx_signature = ?, verified_at = ? "
            "WHERE id = ?",
            (tx_signature, _now_iso(), payment_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Promo redemptions
# ---------------------------------------------------------------------------

async def record_redemption(user_id: int, code: str, days_granted: int) -> bool:
    """Record a promo redemption. Returns False if user already redeemed this code."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO redemptions (user_id, code, days_granted, redeemed_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, code, days_granted, _now_iso()),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False
