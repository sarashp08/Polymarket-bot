"""
EMA Trend Strategy — 4H Candles
=================================
Entry logic:
  LONG  → close crosses above 50 EMA, AND current ATR(20) > median ATR(20)
  SHORT → close crosses below 50 EMA, AND current ATR(20) > median ATR(20)

Exits:
  SL_HIT       → price touches stop_loss level
  TP_HIT       → price touches take_profit level
  SIGNAL_FLIP  → EMA side flips (overridden by SL/TP check first)

Position sizing (returned with signal, caller applies risk %):
  atr_distance = atr_sl_mult * ATR
  risk_per_unit → let caller compute: risk_amount / atr_distance = units
"""

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol: str
    direction: str        # "LONG" | "SHORT"
    entry: float          # expected fill price (last close)
    stop_loss: float
    take_profit: float
    atr: float
    ema: float
    bar_ts: int           # ms timestamp of the triggering completed bar


@dataclass
class StrategyParams:
    ema_period: int   = 50
    atr_period: int   = 20
    atr_sl_mult: float = 1.5
    atr_tp_mult: float = 3.0


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _ema(values: List[float], period: int) -> List[float]:
    """Exponential moving average. Warmup positions hold 0.0."""
    n = len(values)
    if n < period:
        return [0.0] * n
    k = 2.0 / (period + 1)
    result = [0.0] * n
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def _atr(candles: List[Dict], period: int) -> List[float]:
    """Average True Range (Wilder smoothing). Warmup positions hold 0.0."""
    n = len(candles)
    if n < 2:
        return [0.0] * n
    trs = [0.0]
    for i in range(1, n):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_vals = [0.0] * n
    if n >= period:
        atr_vals[period - 1] = sum(trs[:period]) / period
        for i in range(period, n):
            atr_vals[i] = (atr_vals[i - 1] * (period - 1) + trs[i]) / period
    return atr_vals


# ── Strategy class ─────────────────────────────────────────────────────────────

class EMATrendStrategy:
    def __init__(self, params: Optional[StrategyParams] = None):
        self.p = params or StrategyParams()
        # Track the last bar timestamp that produced a signal per symbol
        self._last_signal_bar: Dict[str, int] = {}

    def generate_signal(
        self,
        symbol: str,
        candles: List[Dict],
        current_side: Optional[str] = None,   # "LONG" | "SHORT" | None
    ) -> Optional[Signal]:
        """
        Evaluate completed candles and return a Signal if conditions met.

        Uses the second-to-last candle as the "completed bar" signal source
        so we never trade on an incomplete (still-forming) candle.

        Returns None if no signal.
        """
        warmup = max(self.p.ema_period, self.p.atr_period) + 5
        if len(candles) < warmup + 1:
            return None

        closes   = [c["close"] for c in candles]
        ema_vals = _ema(closes, self.p.ema_period)
        atr_vals = _atr(candles, self.p.atr_period)

        # i = last COMPLETED bar index
        i = len(candles) - 2
        if ema_vals[i] == 0.0 or atr_vals[i] == 0.0:
            return None

        bar_ts = candles[i]["ts"]

        # Don't re-fire on the same completed bar
        if self._last_signal_bar.get(symbol) == bar_ts:
            return None

        close_i  = closes[i]
        ema_i    = ema_vals[i]
        atr_i    = atr_vals[i]
        close_p  = closes[i - 1]
        ema_p    = ema_vals[i - 1]

        # ATR filter: current ATR must be above median of recent ATRs
        recent = [v for v in atr_vals[max(0, i - self.p.atr_period): i] if v > 0]
        if len(recent) < 5:
            return None
        if atr_i <= statistics.median(recent):
            return None      # choppy, skip

        sl_dist = self.p.atr_sl_mult * atr_i
        tp_dist = self.p.atr_tp_mult * atr_i

        sig: Optional[Signal] = None

        # LONG: cross above EMA
        if close_i > ema_i and close_p <= ema_p and current_side != "LONG":
            sig = Signal(
                symbol=symbol,
                direction="LONG",
                entry=close_i,
                stop_loss=close_i - sl_dist,
                take_profit=close_i + tp_dist,
                atr=atr_i,
                ema=ema_i,
                bar_ts=bar_ts,
            )

        # SHORT: cross below EMA
        elif close_i < ema_i and close_p >= ema_p and current_side != "SHORT":
            sig = Signal(
                symbol=symbol,
                direction="SHORT",
                entry=close_i,
                stop_loss=close_i + sl_dist,
                take_profit=close_i - tp_dist,
                atr=atr_i,
                ema=ema_i,
                bar_ts=bar_ts,
            )

        if sig:
            self._last_signal_bar[symbol] = bar_ts

        return sig

    def check_exit(
        self,
        position: Dict,
        candles: List[Dict],
    ) -> Optional[str]:
        """
        Check if an open position should be closed on the latest candle.
        Returns exit reason string or None.

        position dict must have: direction, stop_loss, take_profit
        """
        if not candles:
            return None
        latest = candles[-1]
        high, low = latest["high"], latest["low"]

        if position["direction"] == "LONG":
            if low  <= position["stop_loss"]:   return "SL_HIT"
            if high >= position["take_profit"]:  return "TP_HIT"
        else:  # SHORT
            if high >= position["stop_loss"]:   return "SL_HIT"
            if low  <= position["take_profit"]:  return "TP_HIT"

        return None

    def compute_indicators(self, candles: List[Dict]) -> Dict:
        """Return latest indicator values for dashboard display."""
        if len(candles) < self.p.ema_period:
            return {"ema": 0.0, "atr": 0.0}
        closes   = [c["close"] for c in candles]
        ema_vals = _ema(closes, self.p.ema_period)
        atr_vals = _atr(candles, self.p.atr_period)
        return {
            "ema": ema_vals[-1],
            "atr": atr_vals[-1],
            "close": closes[-1],
            "side": "ABOVE" if closes[-1] > ema_vals[-1] else "BELOW",
        }
