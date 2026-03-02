"""
Signal Engine.

Evaluates five order-flow conditions every 30 seconds per timeframe.
When at least `min_confluence` conditions agree on the same direction,
a TradeSignal is emitted.

Conditions checked:
  1. CVD (Cumulative Volume Delta)     — net BTC buying pressure
  2. Order Book Imbalance              — bid vs ask depth ratio
  3. Buy/Sell Volume Ratio             — USD buying vs selling
  4. Whale Activity                    — large orders (≥ threshold) in window
  5. Volume Spike                      — current volume vs rolling baseline
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from data.orderflow import OrderFlowAnalyzer, OrderFlowSnapshot, WINDOW_SECONDS

logger = logging.getLogger(__name__)

# Seconds of live data required before any signal can fire per timeframe
WARMUP_SECONDS: Dict[str, int] = {
    "5m": 60,    # 1 min of data before 5m signals
    "15m": 120,  # 2 min of data before 15m signals
}


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
    _TOTAL_CONDITIONS = 5  # denominator for confidence score

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

        # Tracks when the first real price tick arrived (for warmup gate)
        self._first_data_time: Optional[float] = None

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

        # Record time of first real data (for warmup gate)
        if self._first_data_time is None:
            self._first_data_time = now
            logger.info("Signal engine: first data received — warmup started")

        # Warmup gate: don't fire until we have enough data in the window
        elapsed = now - self._first_data_time
        warmup = WARMUP_SECONDS.get(timeframe, 60)
        if elapsed < warmup:
            remaining = int(warmup - elapsed)
            logger.debug(f"Warmup [{timeframe}]: {remaining}s remaining")
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

        # ── Condition 3: Buy/Sell Volume Ratio ────────────────────────────────
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

        # ── Condition 5: Volume Spike ─────────────────────────────────────────
        # Volume spike is direction-neutral: it confirms whichever side is dominant
        if self._is_volume_spike(timeframe, snap.total_volume_usd):
            vol_m = snap.total_volume_usd / 1_000_000
            spike_label = f"Vol Spike {vol_m:.1f}M USD"
            # Assign to whichever side currently has more evidence
            if len(bull) >= len(bear):
                bull.append(spike_label)
            else:
                bear.append(spike_label)

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
