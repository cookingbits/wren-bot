"""
Tax CSV export.

We emit rows in Koinly's "Universal" CSV format, which CoinTracker,
Koinly, and CoinLedger all import cleanly. Columns:

    Date, Sent Amount, Sent Currency, Received Amount, Received Currency,
    Fee Amount, Fee Currency, Net Worth Amount, Net Worth Currency,
    Label, Description, TxHash

We classify transactions as:
  - Trade  : both sent and received in the same tx (swap)
  - Deposit: only received
  - Withdrawal: only sent
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from database import get_transactions_for_address


KOINLY_HEADERS = [
    "Date",
    "Sent Amount",
    "Sent Currency",
    "Received Amount",
    "Received Currency",
    "Fee Amount",
    "Fee Currency",
    "Net Worth Amount",
    "Net Worth Currency",
    "Label",
    "Description",
    "TxHash",
]


def _fmt_date(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _wallet_flows(
    parsed_tx: dict[str, Any], address: str
) -> tuple[list[tuple[float, str]], list[tuple[float, str]], float]:
    """
    Returns (sent, received, fee_sol) for the given watched address.
    Each side is a list of (amount, currency) tuples.
    """
    sent: list[tuple[float, str]] = []
    received: list[tuple[float, str]] = []

    for t in parsed_tx.get("tokenTransfers", []) or []:
        amt = t.get("tokenAmount") or 0
        try:
            amt_f = float(amt)
        except (TypeError, ValueError):
            continue
        if amt_f == 0:
            continue
        mint = t.get("mint", "")
        symbol = mint[:4] + "…" + mint[-4:] if mint else "TOKEN"
        if t.get("toUserAccount") == address:
            received.append((amt_f, symbol))
        elif t.get("fromUserAccount") == address:
            sent.append((amt_f, symbol))

    for t in parsed_tx.get("nativeTransfers", []) or []:
        lamports = t.get("amount") or 0
        sol = lamports / 1_000_000_000
        if sol == 0:
            continue
        if t.get("toUserAccount") == address:
            received.append((sol, "SOL"))
        elif t.get("fromUserAccount") == address:
            sent.append((sol, "SOL"))

    # Fee: only applicable if the watched address is fee payer
    fee_lamports = parsed_tx.get("fee") or 0
    fee_sol = 0.0
    if parsed_tx.get("feePayer") == address:
        fee_sol = fee_lamports / 1_000_000_000

    return sent, received, fee_sol


def _classify(sent: list, received: list) -> str:
    if sent and received:
        return "Trade"
    if received:
        return "Deposit"
    if sent:
        return "Withdrawal"
    return "Other"


async def export_wallet_csv(address: str, since_ts: int | None = None) -> str:
    """Build a CSV string for a wallet. Returns the CSV contents."""
    rows = await get_transactions_for_address(address, since_ts=since_ts)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(KOINLY_HEADERS)

    for row in rows:
        raw = row.get("raw_json")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}

        sent, received, fee_sol = _wallet_flows(parsed, address)
        label = _classify(sent, received)

        # Koinly handles one sent/one received per row. If there are
        # multiple of each (e.g. multi-hop), we write one row per pair
        # and subsequent rows as separate deposits/withdrawals.
        if sent and received:
            sent_amt, sent_cur = sent[0]
            recv_amt, recv_cur = received[0]
            writer.writerow([
                _fmt_date(row["timestamp"]),
                f"{sent_amt:.8f}",
                sent_cur,
                f"{recv_amt:.8f}",
                recv_cur,
                f"{fee_sol:.8f}" if fee_sol else "",
                "SOL" if fee_sol else "",
                "", "",
                label,
                parsed.get("description", "")[:200],
                row["signature"],
            ])
            # Emit extras as separate rows
            for amt, cur in sent[1:]:
                writer.writerow([
                    _fmt_date(row["timestamp"]),
                    f"{amt:.8f}", cur, "", "", "", "", "", "",
                    "Withdrawal", "(multi-hop)", row["signature"],
                ])
            for amt, cur in received[1:]:
                writer.writerow([
                    _fmt_date(row["timestamp"]),
                    "", "", f"{amt:.8f}", cur, "", "", "", "",
                    "Deposit", "(multi-hop)", row["signature"],
                ])
        elif sent:
            for amt, cur in sent:
                writer.writerow([
                    _fmt_date(row["timestamp"]),
                    f"{amt:.8f}", cur, "", "",
                    f"{fee_sol:.8f}" if fee_sol else "",
                    "SOL" if fee_sol else "",
                    "", "",
                    "Withdrawal",
                    parsed.get("description", "")[:200],
                    row["signature"],
                ])
                fee_sol = 0  # only attach fee once
        elif received:
            for amt, cur in received:
                writer.writerow([
                    _fmt_date(row["timestamp"]),
                    "", "", f"{amt:.8f}", cur,
                    "", "", "", "",
                    "Deposit",
                    parsed.get("description", "")[:200],
                    row["signature"],
                ])

    return buf.getvalue()


async def export_user_csv(user_id: int, wallet_rows: Iterable[dict]) -> str:
    """Concatenate CSVs for all of a user's wallets into one file."""
    wallets = list(wallet_rows)
    if not wallets:
        return ",".join(KOINLY_HEADERS) + "\n"

    buf = io.StringIO()
    buf.write(",".join(KOINLY_HEADERS) + "\n")
    for w in wallets:
        csv_text = await export_wallet_csv(w["address"])
        # Drop header from subsequent files
        lines = csv_text.splitlines()
        if len(lines) > 1:
            buf.write("\n".join(lines[1:]) + "\n")
    return buf.getvalue()
