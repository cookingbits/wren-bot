"""
Crypto payment verification.

Flow:
  1. User runs /upgrade. We create a pending payment row with a unique
     memo code (e.g. TRK-A7K2PQ9X).
  2. We tell the user: "Send $X USDC to <treasury> on Solana, and include
     this memo in the transaction: TRK-A7K2PQ9X".
  3. A background task polls the treasury wallet's parsed tx history from
     Helius. For each incoming USDC transfer, we look at the memo string
     and check if it matches a pending payment.
  4. If the amount >= required and memo matches, we mark the payment
     verified and upgrade the user to paid tier.

Why memos: multiple users share one treasury wallet, and the memo lets
us attribute each incoming payment to a specific user without per-user
deposit addresses (which would require custody of derived keys).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from config import (
    HELIUS_API_KEY,
    HELIUS_BASE_URL,
    TREASURY_WALLET_ADDRESS,
    USDC_MINT,
)
from database import (
    find_payment_by_memo,
    get_pending_payments,
    grant_paid_tier,
    mark_payment_verified,
)

log = logging.getLogger(__name__)

# Small tolerance for stablecoin micro-fluctuations
PRICE_TOLERANCE = 0.05  # USDC

# Track which treasury tx signatures we've processed so we don't reprocess
_processed_signatures: set[str] = set()


async def _fetch_treasury_history(
    client: httpx.AsyncClient, limit: int = 50
) -> list[dict[str, Any]]:
    url = f"{HELIUS_BASE_URL}/addresses/{TREASURY_WALLET_ADDRESS}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": limit}
    try:
        resp = await client.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning("Failed to fetch treasury history: %s", e)
        return []


def _extract_memos(tx: dict[str, Any]) -> list[str]:
    """
    Extract memo strings from a parsed Helius transaction.

    Memos appear in `instructions` under the SPL Memo program
    (Memo1...program ID or MemoSq...). Helius sometimes decodes them
    into the `description` too, but we parse instructions to be safe.
    """
    memos: list[str] = []
    MEMO_PROGRAMS = {
        "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    }
    for instr in tx.get("instructions", []) or []:
        program_id = instr.get("programId") or instr.get("program") or ""
        if program_id in MEMO_PROGRAMS:
            parsed = instr.get("parsed") or instr.get("data")
            if isinstance(parsed, dict):
                memo = parsed.get("memo") or parsed.get("data")
                if memo:
                    memos.append(str(memo))
            elif isinstance(parsed, str):
                memos.append(parsed)
        # Helius sometimes puts inner memos into innerInstructions
        for inner in instr.get("innerInstructions", []) or []:
            inner_pid = inner.get("programId") or inner.get("program") or ""
            if inner_pid in MEMO_PROGRAMS:
                parsed = inner.get("parsed") or inner.get("data")
                if isinstance(parsed, dict):
                    memo = parsed.get("memo") or parsed.get("data")
                    if memo:
                        memos.append(str(memo))
                elif isinstance(parsed, str):
                    memos.append(parsed)

    # Fallback: Helius sometimes surfaces memos via `events.memo` or description
    events = tx.get("events") or {}
    memo_event = events.get("memo") if isinstance(events, dict) else None
    if isinstance(memo_event, str):
        memos.append(memo_event)

    return memos


def _find_incoming_usdc(tx: dict[str, Any]) -> float:
    """Sum USDC amount received by our treasury wallet in this tx."""
    total = 0.0
    for transfer in tx.get("tokenTransfers", []) or []:
        if transfer.get("mint") != USDC_MINT:
            continue
        if transfer.get("toUserAccount") != TREASURY_WALLET_ADDRESS:
            continue
        amt = transfer.get("tokenAmount") or 0
        try:
            total += float(amt)
        except (TypeError, ValueError):
            pass
    return total


async def check_pending_payments(
    notifier: Optional[callable] = None,
) -> list[dict[str, Any]]:
    """
    Poll treasury wallet, verify any pending payments, return list of
    {user_id, memo, amount, tx_signature, new_expiry} for notification.

    `notifier` is an optional async callable: `await notifier(user_id, info)`.
    """
    pending = await get_pending_payments()
    if not pending:
        return []

    newly_verified: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        txs = await _fetch_treasury_history(client)

    for tx in txs:
        sig = tx.get("signature")
        if not sig or sig in _processed_signatures:
            continue
        _processed_signatures.add(sig)

        amount = _find_incoming_usdc(tx)
        if amount <= 0:
            continue

        memos = _extract_memos(tx)
        if not memos:
            continue

        for memo in memos:
            memo = memo.strip()
            payment = await find_payment_by_memo(memo)
            if not payment:
                continue
            if payment.get("status") != "pending":
                continue
            required = float(payment["amount_usdc"]) - PRICE_TOLERANCE
            if amount < required:
                log.info(
                    "Payment memo %s matched but amount %.2f < required %.2f",
                    memo, amount, payment["amount_usdc"],
                )
                continue

            # Verify it!
            await mark_payment_verified(payment["id"], sig)
            new_exp = await grant_paid_tier(payment["user_id"])
            info = {
                "user_id": payment["user_id"],
                "memo": memo,
                "amount": amount,
                "tx_signature": sig,
                "new_expiry": new_exp,
            }
            newly_verified.append(info)
            if notifier:
                try:
                    await notifier(payment["user_id"], info)
                except Exception as e:
                    log.exception("Notifier failed for user %s: %s", payment["user_id"], e)

    return newly_verified
