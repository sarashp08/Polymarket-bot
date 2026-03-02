import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Mode ──────────────────────────────────────────────────────────
    paper_trading: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"

    # ── Bankroll & Risk ───────────────────────────────────────────────
    bankroll: float = float(os.getenv("BANKROLL", "1000"))
    risk_per_trade_pct: float = float(os.getenv("RISK_PCT", "2.0"))
    max_open_trades: int = int(os.getenv("MAX_OPEN_TRADES", "3"))

    # ── Signal Settings ───────────────────────────────────────────────
    timeframes: List[str] = field(
        default_factory=lambda: [
            tf.strip() for tf in os.getenv("TIMEFRAMES", "15m").split(",")
        ]
    )
    min_confluence: int = 2                          # signals needed to fire a trade
    cvd_threshold: float = float(os.getenv("CVD_THRESHOLD", "5.0"))     # BTC units
    ob_imbalance_threshold: float = 0.60            # bid ratio above this = bullish
    volume_spike_multiplier: float = 2.0            # Nx rolling average = spike
    whale_usd_threshold: float = float(os.getenv("WHALE_THRESHOLD", "50000"))
    signal_cooldown_seconds: int = int(os.getenv("SIGNAL_COOLDOWN", "900"))

    # ── Binance ───────────────────────────────────────────────────────
    binance_symbol: str = "BTCUSDT"
    binance_symbol_lower: str = "btcusdt"

    # ── Polymarket ────────────────────────────────────────────────────
    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_api_key: Optional[str] = os.getenv("POLY_API_KEY")
    polymarket_api_secret: Optional[str] = os.getenv("POLY_API_SECRET")
    polymarket_api_passphrase: Optional[str] = os.getenv("POLY_PASSPHRASE")
    polymarket_private_key: Optional[str] = os.getenv("POLY_PRIVATE_KEY")
    chain_id: int = 137  # Polygon mainnet

    # ── Telegram ──────────────────────────────────────────────────────
    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Risk Guards ───────────────────────────────────────────────────
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "10.0"))

    # ── Logging ───────────────────────────────────────────────────────
    dashboard_interval_minutes: int = 60
    log_file: str = "trades.json"


config = Config()
