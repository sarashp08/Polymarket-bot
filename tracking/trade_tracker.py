"""
Trade tracker.

Accumulates performance statistics (overall and per-timeframe)
across the current session for display in the hourly dashboard.
"""

import time
from dataclasses import dataclass, field
from typing import Dict

from polymarket.client import PaperPosition


@dataclass
class Stats:
    total: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    wagered: float = 0.0
    best_win: float = 0.0
    worst_loss: float = 0.0
    start_time: float = field(default_factory=time.time)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total else 0.0

    @property
    def roi(self) -> float:
        return self.pnl / self.wagered if self.wagered else 0.0

    @property
    def runtime_hours(self) -> float:
        return (time.time() - self.start_time) / 3600.0

    @property
    def avg_pnl(self) -> float:
        return self.pnl / self.total if self.total else 0.0


class TradeTracker:
    def __init__(self):
        self.overall = Stats()
        self._by_tf: Dict[str, Stats] = {
            "5m": Stats(),
            "15m": Stats(),
        }

    def record(self, pos: PaperPosition):
        self._update(self.overall, pos)
        tf_stat = self._by_tf.get(pos.timeframe)
        if tf_stat:
            self._update(tf_stat, pos)

    @staticmethod
    def _update(s: Stats, pos: PaperPosition):
        s.total += 1
        s.wagered += pos.cost_usd
        s.pnl += pos.pnl

        if pos.status == "WON":
            s.wins += 1
            s.best_win = max(s.best_win, pos.pnl)
        else:
            s.losses += 1
            s.worst_loss = min(s.worst_loss, pos.pnl)

    def for_timeframe(self, tf: str) -> Stats:
        return self._by_tf.get(tf, self.overall)
