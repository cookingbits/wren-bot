"""
Config loader — reads values from the .env file (or the environment) into
simple Python constants the rest of the app imports.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root if it exists
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _required(name: str) -> str:
    """Read an env var and fail loudly if it's missing."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer, got: {raw!r}")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be a number, got: {raw!r}")


# --- Required ---
TELEGRAM_BOT_TOKEN: str = _required("TELEGRAM_BOT_TOKEN")
HELIUS_API_KEY: str = _required("HELIUS_API_KEY")
TREASURY_WALLET_ADDRESS: str = _required("TREASURY_WALLET_ADDRESS")

# --- Optional with defaults ---
SUBSCRIPTION_PRICE_USDC: float = _float("SUBSCRIPTION_PRICE_USDC", 9.99)
FREE_TIER_WALLET_LIMIT: int = _int("FREE_TIER_WALLET_LIMIT", 2)
PAID_TIER_WALLET_LIMIT: int = _int("PAID_TIER_WALLET_LIMIT", 50)
WALLET_POLL_INTERVAL: int = _int("WALLET_POLL_INTERVAL", 60)
PAYMENT_POLL_INTERVAL: int = _int("PAYMENT_POLL_INTERVAL", 30)
ADMIN_TELEGRAM_ID: int = _int("ADMIN_TELEGRAM_ID", 0)
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/bot.db")

# --- Constants (not user-configurable) ---
HELIUS_BASE_URL = "https://api.helius.xyz/v0"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# USDC mint on Solana mainnet
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# How long a paid subscription lasts after successful payment
SUBSCRIPTION_DAYS = 30

# --- Promo codes ---
# code → {"days": <int>, "max_uses": <int|None>}
# - days: how many days of paid tier this code grants
# - max_uses: TOTAL redemption cap across all users (None = unlimited)
# Per-user uniqueness (one redemption per user per code) is enforced separately
# in the redemptions DB table. Codes are case-insensitive (handler upper-cases).
PROMO_CODES: dict[str, dict[str, int]] = {
    # First-20 early-bird promo for @cookingbits channel members
    "COOKINGBITS3": {"days": 90, "max_uses": 20},
}

# Channel users are asked to post a review in (shown in the /redeem reply)
REVIEW_CHANNEL: str = os.getenv("REVIEW_CHANNEL", "@cookingbits")
