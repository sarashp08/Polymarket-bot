"""
Order Flow Analyzer.

Maintains rolling windows of trade data for each timeframe and computes:
  - Cumulative Volume Delta (CVD) in BTC
  - Buy/Sell volume ratio
  - Order Book Imbalance (top-10 levels, bid vs ask qty)
  - Whale order count + dominant direction
  - Volume spike detection vs rolling baseline
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from data.binance_feed import OrderBook, Trade

# Rolling window lengths per timeframe (seconds)
WINDOW_SECONDS: Dict[str, int] = {
    "5m": 300,
    "15m": 900,
}


# ── Rolling trade window ────────────────────────────────────────────────────────

@dataclass
class TradeWindow:
    window_seconds: int
    _trades: deque = field(default_factory=deque, init=False)

    def add(self, trade: Trade):
        self._trades.append(trade)
        self._prune()

    def _prune(self):
        cutoff = time.time() - self.window_seconds
        while self._trades and (self._trades[0].timestamp_ms / 1000) < cutoff:
            self._trades.popleft()

    # ── Metrics ───────────────────────────────────────────────────────────────

    @property
    def cvd(self) -> float:
        """Cumulative Volume Delta in BTC (positive = net buying)."""
        return sum(t.signed_quantity for t in self._trades)

    @property
    def buy_volume_usd(self) -> float:
        return sum(t.usd_value for t in self._trades if not t.is_buyer_maker)

    @property
    def sell_volume_usd(self) -> float:
        return sum(t.usd_value for t in self._trades if t.is_buyer_maker)

    @property
    def total_volume_usd(self) -> float:
        return sum(t.usd_value for t in self._trades)

    @property
    def buy_sell_ratio(self) -> float:
        total = self.total_volume_usd
        return self.buy_volume_usd / total if total > 0 else 0.5

    @property
    def price_now(self) -> Optional[float]:
        return self._trades[-1].price if self._trades else None

    @property
    def trade_count(self) -> int:
        return len(self._trades)


# ── Snapshot returned to signal engine ─────────────────────────────────────────

@dataclass
class OrderFlowSnapshot:
    timeframe: str
    cvd: float                  # BTC net buy/sell
    buy_sell_ratio: float       # 0-1; >0.5 = more buying
    total_volume_usd: float
    ob_imbalance: float         # 0-1; >0.6 = bid-heavy (bullish)
    whale_count: int
    whale_direction: str        # "BUY" | "SELL" | "NEUTRAL"
    price: float
    timestamp: float


# ── Main analyzer ───────────────────────────────────────────────────────────────

class OrderFlowAnalyzer:
    def __init__(self, whale_threshold: float = 50_000.0):
        self.whale_threshold = whale_threshold

        self.windows: Dict[str, TradeWindow] = {
            tf: TradeWindow(secs) for tf, secs in WINDOW_SECONDS.items()
        }

        # (timestamp, usd_value, direction)
        self._whale_trades: deque = deque()

        # Latest order book snapshot
        self._ob: Optional[OrderBook] = None

    # ── WebSocket callbacks ────────────────────────────────────────────────────

    async def on_trade(self, trade: Trade):
        for window in self.windows.values():
            window.add(trade)

        if trade.usd_value >= self.whale_threshold:
            self._whale_trades.append((time.time(), trade.usd_value, trade.direction))
            self._prune_whales()

    async def on_orderbook(self, ob: OrderBook):
        self._ob = ob

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _prune_whales(self):
        cutoff = time.time() - WINDOW_SECONDS["15m"]
        while self._whale_trades and self._whale_trades[0][0] < cutoff:
            self._whale_trades.popleft()

    def ob_imbalance(self) -> float:
        """
        Ratio of bid qty to total qty across top-10 price levels.
        >0.60 → bids dominating (bullish pressure)
        <0.40 → asks dominating (bearish pressure)
        """
        if not self._ob:
            return 0.5
        try:
            bid_vol = sum(float(b[1]) for b in self._ob.bids[:10])
            ask_vol = sum(float(a[1]) for a in self._ob.asks[:10])
            total = bid_vol + ask_vol
            return bid_vol / total if total > 0 else 0.5
        except Exception:
            return 0.5

    def whale_pressure(self, window_seconds: int) -> Tuple[int, str]:
        """
        Returns (count, dominant_direction) for whale trades in the last
        `window_seconds` seconds.
        """
        cutoff = time.time() - window_seconds
        recent: List[Tuple] = [w for w in self._whale_trades if w[0] >= cutoff]

        if not recent:
            return 0, "NEUTRAL"

        buys = sum(1 for _, _, d in recent if d == "BUY")
        sells = sum(1 for _, _, d in recent if d == "SELL")

        if buys > sells:
            dominant = "BUY"
        elif sells > buys:
            dominant = "SELL"
        else:
            dominant = "NEUTRAL"

        return len(recent), dominant

    # ── Public API ─────────────────────────────────────────────────────────────

    def snapshot(self, timeframe: str) -> OrderFlowSnapshot:
        window = self.windows[timeframe]
        w_secs = WINDOW_SECONDS[timeframe]
        whale_count, whale_dir = self.whale_pressure(w_secs)

        return OrderFlowSnapshot(
            timeframe=timeframe,
            cvd=window.cvd,
            buy_sell_ratio=window.buy_sell_ratio,
            total_volume_usd=window.total_volume_usd,
            ob_imbalance=self.ob_imbalance(),
            whale_count=whale_count,
            whale_direction=whale_dir,
            price=window.price_now or 0.0,
            timestamp=time.time(),
        )

    def latest_price(self) -> float:
        # Check 15m window first (primary timeframe), fall back to 5m
        for tf in ("15m", "5m"):
            if tf in self.windows:
                p = self.windows[tf].price_now
                if p:
                    return p
        return 0.0
