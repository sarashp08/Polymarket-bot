"""
Signal Engine.

Evaluates four order-flow conditions every 30 seconds per timeframe.
When at least `min_confluence` conditions agree on the same direction,
a TradeSignal is emitted.

Conditions checked:
  1. CVD (Cumulative Volume Delta)     — net BTC buying pressure
  2. Order Book Imbalance              — bid vs ask depth ratio
  3. Buy/Sell Volume Ratio             — USD buying vs selling
  4. Whale Activity                    — large orders (≥ threshold) in window
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from data.orderflow import OrderFlowAnalyzer, OrderFlowSnapshot, WINDOW_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    timeframe: str          # "5m" | "15m"
    direction: str          # "UP" | "DOWN"
    confidence: float       # 0.0 – 1.0  (fired_signals / total_possible)
    signals_fired: List[str]
    price: float
    timestamp: float

    @property
    def confidence_label(self) -> str:
        if self.confidence >= 0.75:
            return "HIGH"
        if self.confidence >= 0.50:
            return "MEDIUM"
        return "LOW"


class SignalEngine:
    _TOTAL_CONDITIONS = 4  # denominator for confidence score

    def __init__(
        self,
        analyzer: OrderFlowAnalyzer,
        min_confluence: int = 2,
        cvd_threshold: float = 5.0,
        ob_imbalance_threshold: float = 0.60,
        volume_spike_multiplier: float = 2.0,
        cooldown_seconds: int = 60,
    ):
        self.analyzer = analyzer
        self.min_confluence = min_confluence
        self.cvd_threshold = cvd_threshold
        self.ob_threshold = ob_imbalance_threshold
        self.vol_spike_mult = volume_spike_multiplier
        self.cooldown = cooldown_seconds

        self._last_signal: Dict[str, float] = {tf: 0.0 for tf in WINDOW_SECONDS}
        self._vol_history: Dict[str, List[float]] = {tf: [] for tf in WINDOW_SECONDS}
        self._signal_cb: Optional[Callable] = None

    def on_signal(self, callback: Callable) -> "SignalEngine":
        self._signal_cb = callback
        return self

    # ── Volume spike baseline ──────────────────────────────────────────────────

    def _record_volume(self, tf: str, vol: float):
        history = self._vol_history[tf]
        history.append(vol)
        if len(history) > 120:  # keep ~60 min of 30-s samples
            history.pop(0)

    def _is_volume_spike(self, tf: str, current_vol: float) -> bool:
        history = self._vol_history[tf]
        if len(history) < 6:
            return False
        # Compare against all but the latest sample
        baseline = history[:-1]
        avg = sum(baseline) / len(baseline)
        return avg > 0 and current_vol >= avg * self.vol_spike_mult

    # ── Core evaluation ────────────────────────────────────────────────────────

    async def evaluate(self, timeframe: str):
        """
        Evaluate signals for one timeframe.
        Should be called every ~30 s by the main loop.
        """
        now = time.time()

        # Enforce per-timeframe cooldown
        if now - self._last_signal[timeframe] < self.cooldown:
            return

        snap: OrderFlowSnapshot = self.analyzer.snapshot(timeframe)

        # Need real price data before firing anything
        if snap.price == 0.0 or snap.total_volume_usd == 0.0:
            return

        self._record_volume(timeframe, snap.total_volume_usd)

        bull: List[str] = []
        bear: List[str] = []

        # ── Condition 1: CVD ──────────────────────────────────────────────────
        if snap.cvd > self.cvd_threshold:
            bull.append(f"CVD +{snap.cvd:.2f} BTC")
        elif snap.cvd < -self.cvd_threshold:
            bear.append(f"CVD {snap.cvd:.2f} BTC")

        # ── Condition 2: Order Book Imbalance ─────────────────────────────────
        if snap.ob_imbalance > self.ob_threshold:
            bull.append(f"OB Imbalance {snap.ob_imbalance:.0%} bids")
        elif snap.ob_imbalance < (1.0 - self.ob_threshold):
            bear.append(f"OB Imbalance {snap.ob_imbalance:.0%} bids")

        # ── Condition 3: Buy/Sell Volume Ratio ───────────────────────────────
        if snap.buy_sell_ratio > 0.60:
            bull.append(f"Buy Ratio {snap.buy_sell_ratio:.0%}")
        elif snap.buy_sell_ratio < 0.40:
            bear.append(f"Sell Ratio {1 - snap.buy_sell_ratio:.0%}")

        # ── Condition 4: Whale Pressure ───────────────────────────────────────
        if snap.whale_count >= 2:
            if snap.whale_direction == "BUY":
                bull.append(f"Whale Buys x{snap.whale_count}")
            elif snap.whale_direction == "SELL":
                bear.append(f"Whale Sells x{snap.whale_count}")

        # ── Confluence gate ───────────────────────────────────────────────────
        bull_n, bear_n = len(bull), len(bear)

        if bull_n >= self.min_confluence and bull_n > bear_n:
            direction, fired = "UP", bull
        elif bear_n >= self.min_confluence and bear_n > bull_n:
            direction, fired = "DOWN", bear
        else:
            return  # No strong consensus

        confidence = len(fired) / self._TOTAL_CONDITIONS

        signal = TradeSignal(
            timeframe=timeframe,
            direction=direction,
            confidence=confidence,
            signals_fired=fired,
            price=snap.price,
            timestamp=now,
        )

        self._last_signal[timeframe] = now
        logger.info(
            f"Signal → {direction} {timeframe} | conf={confidence:.0%} | {fired}"
        )

        if self._signal_cb:
            await self._signal_cb(signal)
