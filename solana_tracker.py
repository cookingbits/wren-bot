"""
Solana wallet tracker via Helius.

For each tracked wallet we:
  1. Fetch the last N parsed transactions from Helius
  2. Skip ones we've already alerted on (seen_transactions table)
  3. For swap-style transactions, format a human-readable alert
  4. Mark as seen and return the alerts

We use Helius's enhanced "Parse Transaction History" endpoint, which gives us
structured swap events (tokenTransfers + nativeTransfers + description).

Docs: https://docs.helius.dev/solana-apis/enhanced-transactions-api
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from config import HELIUS_API_KEY, HELIUS_BASE_URL
from database import (
    count_seen_for_address,
    get_all_tracked_addresses,
    is_tx_seen,
    mark_tx_seen,
)

log = logging.getLogger(__name__)

# Max transactions to fetch per wallet per poll (Helius max is 100)
TX_PER_POLL = 25

# How many transactions to always keep in DB even if not alerting
# (first-run behavior: skip alerts for pre-existing tx, just mark seen)


@dataclass
class WalletAlert:
    """Something happened on a tracked wallet — tell the subscribed users."""
    address: str
    signature: str
    timestamp: int
    description: str
    subscribed_user_ids: list[int]


async def _fetch_address_history(
    client: httpx.AsyncClient, address: str, limit: int = TX_PER_POLL
) -> list[dict[str, Any]]:
    """Fetch parsed transaction history for one address from Helius."""
    url = f"{HELIUS_BASE_URL}/addresses/{address}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": limit}
    try:
        resp = await client.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        log.warning("Unexpected Helius response shape for %s: %r", address, data)
        return []
    except httpx.HTTPStatusError as e:
        log.warning("Helius HTTP %s for %s: %s", e.response.status_code, address, e)
        return []
    except Exception as e:
        log.exception("Helius fetch failed for %s: %s", address, e)
        return []


def _format_transfer(t: dict[str, Any]) -> str:
    """Format a single token transfer line."""
    amount = t.get("tokenAmount") or t.get("amount") or 0
    symbol = t.get("tokenStandard") or t.get("mint", "")[:6] or "?"
    # Helius sometimes provides tokenSymbol in richer data sources — fall back.
    return f"{amount} {symbol}"


def _summarize_tx(tx: dict[str, Any], watched_address: str) -> str:
    """
    Build a one-line human summary of a parsed transaction.

    Helius provides `description` for common types, but it's not always
    present and sometimes refers to counterparties not the watched wallet,
    so we add our own fallback.
    """
    description = (tx.get("description") or "").strip()
    tx_type = tx.get("type") or "UNKNOWN"
    fee_payer = tx.get("feePayer", "")

    # Try to infer what happened to OUR watched address from tokenTransfers
    received: list[str] = []
    sent: list[str] = []
    for transfer in tx.get("tokenTransfers", []) or []:
        if transfer.get("toUserAccount") == watched_address:
            received.append(_format_transfer(transfer))
        elif transfer.get("fromUserAccount") == watched_address:
            sent.append(_format_transfer(transfer))

    for transfer in tx.get("nativeTransfers", []) or []:
        amount_sol = (transfer.get("amount") or 0) / 1_000_000_000
        if amount_sol == 0:
            continue
        if transfer.get("toUserAccount") == watched_address:
            received.append(f"{amount_sol:.4f} SOL")
        elif transfer.get("fromUserAccount") == watched_address:
            sent.append(f"{amount_sol:.4f} SOL")

    parts: list[str] = []
    if sent and received:
        parts.append(f"SWAP — sent {', '.join(sent)} → got {', '.join(received)}")
    elif sent:
        parts.append(f"SENT {', '.join(sent)}")
    elif received:
        parts.append(f"RECEIVED {', '.join(received)}")
    elif description:
        parts.append(description)
    else:
        parts.append(f"{tx_type} transaction")

    if fee_payer and fee_payer != watched_address:
        parts.append(f"(fee payer: {fee_payer[:6]}…{fee_payer[-4:]})")

    return " ".join(parts)


def format_alert_message(alert: WalletAlert, label: Optional[str] = None) -> str:
    """Pretty-print an alert for Telegram (MarkdownV2 is finicky — we use plain text + simple markdown)."""
    short_addr = f"{alert.address[:4]}…{alert.address[-4:]}"
    header = f"🔔 *{label or short_addr}*"
    solscan = f"https://solscan.io/tx/{alert.signature}"
    return (
        f"{header}\n"
        f"{alert.description}\n\n"
        f"[View on Solscan]({solscan})"
    )


async def poll_once() -> list[WalletAlert]:
    """
    Run one polling pass over every tracked wallet. Returns a list of
    new alerts that the caller (bot.py) should forward to users.
    """
    tracked = await get_all_tracked_addresses()
    if not tracked:
        return []

    alerts: list[WalletAlert] = []
    async with httpx.AsyncClient() as client:
        for entry in tracked:
            address = entry["address"]
            chain = entry["chain"]
            user_ids = entry["user_ids"]

            if chain != "solana":
                continue  # BSC/ETH come in a future version

            # First-poll priming: if we've never seen any tx for this address,
            # mark the current batch as seen without alerting. This prevents
            # spamming users with ~25 "alerts" for historical activity the
            # moment they add a new wallet.
            first_poll = (await count_seen_for_address(address)) == 0

            txs = await _fetch_address_history(client, address)
            for tx in txs:
                sig = tx.get("signature")
                ts = tx.get("timestamp") or 0
                if not sig:
                    continue

                if first_poll:
                    # Just record history so future polls can diff against it.
                    await mark_tx_seen(sig, address, ts, json.dumps(tx))
                    continue

                if await is_tx_seen(sig, address):
                    continue

                await mark_tx_seen(sig, address, ts, json.dumps(tx))

                # Only alert on meaningful tx types
                tx_type = (tx.get("type") or "").upper()
                has_token = bool(tx.get("tokenTransfers"))
                has_native = bool(tx.get("nativeTransfers"))
                if not (has_token or has_native or tx_type in {"SWAP", "TRANSFER"}):
                    continue

                summary = _summarize_tx(tx, address)
                alerts.append(
                    WalletAlert(
                        address=address,
                        signature=sig,
                        timestamp=ts,
                        description=summary,
                        subscribed_user_ids=user_ids,
                    )
                )

            # Gentle pacing to stay under Helius free-tier rate limits
            await asyncio.sleep(0.15)

    return alerts
