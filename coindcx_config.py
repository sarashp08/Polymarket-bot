"""
CoinDCX Futures Bot — Configuration
====================================
All settings read from environment variables / .env file.
Copy .env.example to .env and fill in your values.
"""

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class DCXConfig:
    # ── Mode ──────────────────────────────────────────────────────────────────
    paper_trading: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"

    # ── CoinDCX API ───────────────────────────────────────────────────────────
    api_key: str = os.getenv("COINDCX_API_KEY", "")
    api_secret: str = os.getenv("COINDCX_API_SECRET", "")

    # ── Symbols (CoinDCX perpetual futures pair names) ────────────────────────
    symbols: List[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv(
            "DCX_SYMBOLS",
            "B-ETH_USDT,B-SOL_USDT,B-HYPE_USDT,B-AAVE_USDT,B-ZEC_USDT,B-UNI_USDT",
        ).split(",")
    ])

    # ── Strategy Parameters ───────────────────────────────────────────────────
    ema_period: int = int(os.getenv("EMA_PERIOD", "50"))
    atr_period: int = int(os.getenv("ATR_PERIOD", "20"))
    atr_sl_mult: float = float(os.getenv("ATR_SL_MULT", "1.5"))   # SL = entry ± mult*ATR
    atr_tp_mult: float = float(os.getenv("ATR_TP_MULT", "3.0"))   # TP = entry ∓ mult*ATR
    primary_tf: str = os.getenv("PRIMARY_TF", "4h")               # signal timeframe
    candles_limit: int = 300                                       # bars to fetch per symbol

    # ── Risk Management ───────────────────────────────────────────────────────
    capital_usdt: float = float(os.getenv("CAPITAL_USDT", "1000"))
    risk_per_trade_pct: float = float(os.getenv("RISK_PCT", "1.0"))  # % of capital at risk
    max_open_positions: int = int(os.getenv("MAX_POSITIONS", "4"))   # across all symbols
    leverage: int = int(os.getenv("LEVERAGE", "3"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))

    # ── Polling ───────────────────────────────────────────────────────────────
    # Check for new 4H candles every 5 min (4H = 240 min)
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL", "300"))

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Storage ───────────────────────────────────────────────────────────────
    trades_file: str = "dcx_trades.json"
    state_file: str = "dcx_state.json"
    backtest_file: str = "dcx_backtest.json"


dcx_config = DCXConfig()
