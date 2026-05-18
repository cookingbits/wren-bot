"""
Solana wallet tracker via Helius.

For each tracked wallet we:
  1. Fetch the last N parsed transactions from Helius
  2. Skip ones we've already alerted on (seen_transactions table)
  3. For swap-style transactions, format a human-readable alert
  4. Mark as seen and return the alerts

Token symbol resolution: Helius's parsed-transactions response returns
`tokenStandard: "Fungible"` rather than the actual token ticker, so we
batch-resolve mint addresses to symbols via Helius DAS `getAssetBatch`
and cache the results in-memory across polls.

Docs:
  - https://docs.helius.dev/solana-apis/enhanced-transactions-api
  - https://docs.helius.dev/compression-and-das-api/digital-asset-standard-das-api
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from config import HELIUS_API_KEY, HELIUS_BASE_URL, HELIUS_RPC_URL
from database import (
    count_seen_for_address,
    get_all_tracked_addresses,
    is_tx_seen,
    mark_tx_seen,
)

log = logging.getLogger(__name__)

# Max transactions to fetch per wallet per poll (Helius max is 100)
TX_PER_POLL = 25

# In-memory cache: mint address -> resolved symbol (or "" if Helius had nothing).
# Resets each deploy; warm-up is one extra Helius call per first-seen token.
_symbol_cache: dict[str, str] = {}


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


async def _fetch_symbols(
    client: httpx.AsyncClient, mints: list[str]
) -> dict[str, str]:
    """
    Resolve a batch of mint addresses to token symbols via Helius DAS
    `getAssetBatch`. Returns {mint: symbol}. Uses the module-level cache so
    repeat mints across polls don't re-hit Helius.

    Missing/empty symbols are cached as "" so we don't re-query forever.
    """
    if not mints:
        return {}

    # Split into cache hits and misses
    to_fetch: list[str] = []
    for m in mints:
        if m not in _symbol_cache:
            to_fetch.append(m)

    if to_fetch:
        # Helius getAssetBatch accepts up to 1000 ids per call; we batch in 100s
        # to keep response sizes sane and parallelism modest.
        for i in range(0, len(to_fetch), 100):
            chunk = to_fetch[i : i + 100]
            payload = {
                "jsonrpc": "2.0",
                "id": "wren-symbols",
                "method": "getAssetBatch",
                "params": {"ids": chunk},
            }
            try:
                resp = await client.post(HELIUS_RPC_URL, json=payload, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
                results = data.get("result") or []
                # Index by id so missing ones still get cached as ""
                by_id: dict[str, dict[str, Any]] = {}
                for asset in results:
                    if asset and asset.get("id"):
                        by_id[asset["id"]] = asset
                for mint in chunk:
                    asset = by_id.get(mint)
                    sym = ""
                    if asset:
                        meta = (asset.get("content") or {}).get("metadata") or {}
                        sym = (meta.get("symbol") or "").strip()
                    _symbol_cache[mint] = sym
            except Exception as e:
                log.warning("Helius getAssetBatch failed for %d mints: %s", len(chunk), e)
                # Cache as "" so we don't retry every poll on a transient failure;
                # next deploy will refresh. Acceptable tradeoff.
                for mint in chunk:
                    _symbol_cache.setdefault(mint, "")

    return {m: _symbol_cache.get(m, "") for m in mints}


def _short_mint(mint: str) -> str:
    """Fallback display when no symbol is known."""
    if not mint:
        return "?"
    return f"{mint[:4]}…"


def _format_transfer(t: dict[str, Any], symbols: dict[str, str]) -> str:
    """Format a single token transfer line, using resolved symbols where available."""
    amount = t.get("tokenAmount") or t.get("amount") or 0
    mint = t.get("mint", "")
    symbol = symbols.get(mint) or _short_mint(mint)
    return f"{amount} {symbol}"


def _summarize_tx(
    tx: dict[str, Any],
    watched_address: str,
    symbols: dict[str, str],
) -> str:
    """
    Build a human summary of a parsed transaction.

    Output includes:
      - SWAP / SENT / RECEIVED line with resolved token symbols
      - Optional fee-payer note
      - Token mint address(es) wrapped in backticks so Telegram renders them
        as inline code (tap-to-copy on mobile)
    """
    description = (tx.get("description") or "").strip()
    tx_type = tx.get("type") or "UNKNOWN"
    fee_payer = tx.get("feePayer", "")

    received: list[str] = []
    sent: list[str] = []
    mints: list[str] = []
    seen_mints: set[str] = set()

    for transfer in tx.get("tokenTransfers", []) or []:
        from_us = transfer.get("fromUserAccount") == watched_address
        to_us = transfer.get("toUserAccount") == watched_address
        mint = transfer.get("mint")
        if (from_us or to_us) and mint and mint not in seen_mints:
            mints.append(mint)
            seen_mints.add(mint)
        if to_us:
            received.append(_format_transfer(transfer, symbols))
        elif from_us:
            sent.append(_format_transfer(transfer, symbols))

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

    summary = " ".join(parts)

    if mints:
        mint_lines = "\n".join(f"`{m}`" for m in mints)
        summary = f"{summary}\n\n{mint_lines}"

    return summary


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

    # Per-cycle counters for the diagnostic log at the end
    wallets_polled = 0
    txs_fetched = 0
    primed = 0
    seen_skipped = 0
    filtered_out = 0
    alerts: list[WalletAlert] = []

    async with httpx.AsyncClient() as client:
        # First pass: fetch txs per wallet, collect mints to resolve in one batch.
        per_wallet: list[tuple[str, list[int], list[dict[str, Any]], bool]] = []
        all_mints: set[str] = set()

        for entry in tracked:
            address = entry["address"]
            chain = entry["chain"]
            user_ids = entry["user_ids"]

            if chain != "solana":
                continue  # BSC/ETH come in a future version

            wallets_polled += 1
            first_poll = (await count_seen_for_address(address)) == 0
            txs = await _fetch_address_history(client, address)
            txs_fetched += len(txs)
            per_wallet.append((address, user_ids, txs, first_poll))

            # Gentle pacing between wallets to stay under Helius free-tier limits
            await asyncio.sleep(0.15)

            # Collect mints from interesting transfers for symbol lookup
            for tx in txs:
                for transfer in tx.get("tokenTransfers", []) or []:
                    mint = transfer.get("mint")
                    if mint:
                        all_mints.add(mint)

        # Batch-resolve symbols once for everything we just fetched.
        symbols = await _fetch_symbols(client, sorted(all_mints))

        # Second pass: build alerts using resolved symbols.
        for address, user_ids, txs, first_poll in per_wallet:
            for tx in txs:
                sig = tx.get("signature")
                ts = tx.get("timestamp") or 0
                if not sig:
                    continue

                if first_poll:
                    await mark_tx_seen(sig, address, ts, json.dumps(tx))
                    primed += 1
                    continue

                if await is_tx_seen(sig, address):
                    seen_skipped += 1
                    continue

                await mark_tx_seen(sig, address, ts, json.dumps(tx))

                tx_type = (tx.get("type") or "").upper()
                has_token = bool(tx.get("tokenTransfers"))
                has_native = bool(tx.get("nativeTransfers"))
                if not (has_token or has_native or tx_type in {"SWAP", "TRANSFER"}):
                    filtered_out += 1
                    continue

                summary = _summarize_tx(tx, address, symbols)
                alerts.append(
                    WalletAlert(
                        address=address,
                        signature=sig,
                        timestamp=ts,
                        description=summary,
                        subscribed_user_ids=user_ids,
                    )
                )

    log.info(
        "Poll cycle: wallets=%d txs=%d primed=%d seen=%d filtered=%d alerts=%d mints_resolved=%d",
        wallets_polled, txs_fetched, primed, seen_skipped, filtered_out,
        len(alerts), len(all_mints),
    )
    return alerts
