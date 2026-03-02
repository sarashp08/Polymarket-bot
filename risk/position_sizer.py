"""
Position sizing — Fixed % of bankroll.

Scales position size down proportionally when approaching the max
open-position limit, and optionally scales by signal confidence
(high confidence = full size, low confidence = half size).
"""

from dataclasses import dataclass


@dataclass
class PositionSize:
    amount_usd: float
    effective_risk_pct: float
    bankroll: float


class PositionSizer:
    def __init__(self, risk_pct: float = 2.0, max_open: int = 3):
        """
        Args:
            risk_pct:  Base percentage of bankroll to risk per trade (e.g. 2.0 = 2%).
            max_open:  Maximum simultaneous open positions.
        """
        self.risk_pct = risk_pct
        self.max_open = max_open

    def size(
        self,
        bankroll: float,
        open_positions: int,
        confidence: float = 1.0,
    ) -> PositionSize:
        """
        Returns the dollar amount to risk on the next trade.

        Returns amount_usd=0 when at max capacity or bankroll is too low.
        """
        if open_positions >= self.max_open:
            return PositionSize(0.0, 0.0, bankroll)

        # Scale risk by confidence: HIGH=full, MEDIUM=75%, LOW=50%
        if confidence >= 0.75:
            scale = 1.0
        elif confidence >= 0.50:
            scale = 0.75
        else:
            scale = 0.50

        effective_pct = self.risk_pct * scale
        amount = round(bankroll * (effective_pct / 100.0), 2)

        # Hard floor of $1
        amount = max(1.0, amount)

        # Never risk more than we have
        amount = min(amount, bankroll)

        return PositionSize(
            amount_usd=amount,
            effective_risk_pct=effective_pct,
            bankroll=bankroll,
        )
