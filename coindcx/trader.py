"""
CoinDCX Futures Trader
=======================
Handles paper trading (simulation) and live trading (via CoinDCX API).

Paper mode: no real orders are placed. P&L is simulated using candle
high/low to determine SL/TP hits. Positions are managed in-memory and
persisted to a JSON file.

Live mode: real orders are placed via CoinDCX REST API with exchange-side
SL and TP orders. Positions are tracked locally and reconciled with the
exchange on startup.

Position sizing:
  risk_amount   = capital * risk_pct / 100
  atr_distance  = atr_sl_mult * ATR          (from signal)
  quantity      = risk_amount / atr_distance  (base asset units)
  notional      = quantity * entry_price
  margin_used   = notional / leverage
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from coindcx.client import CoinDCXClient
from coindcx.strategy import Signal

logger = logging.getLogger(__name__)


# ── Position data class ────────────────────────────────────────────────────────

@dataclass
class Position:
    id: str
    symbol: str
    direction: str        # "LONG" | "SHORT"
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float       # base asset units
    notional: float       # entry_price * quantity
    margin: float         # notional / leverage
    leverage: int
    opened_at: float      # Unix timestamp
    closed_at: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None   # "SL_HIT" | "TP_HIT" | "MANUAL" | "SIGNAL_FLIP"
    pnl: Optional[float] = None         # realised P&L in USDT
    order_id: Optional[str] = None      # exchange order ID (live mode)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_dict(self) -> Dict:
        return asdict(self)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _compute_pnl(pos: Position, exit_price: float) -> float:
    if pos.direction == "LONG":
        return (exit_price - pos.entry_price) * pos.quantity
    else:
        return (pos.entry_price - exit_price) * pos.quantity


# ── Paper Trader ───────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Simulates futures positions without real orders.
    SL/TP are checked using candle high/low on each poll cycle.
    """

    def __init__(
        self,
        capital_usdt: float,
        risk_pct: float,
        max_positions: int,
        leverage: int,
        max_daily_loss_pct: float,
        trades_file: str = "dcx_trades.json",
        state_file: str  = "dcx_state.json",
    ):
        self.capital         = capital_usdt
        self.initial_capital = capital_usdt
        self.risk_pct        = risk_pct
        self.max_positions   = max_positions
        self.leverage        = leverage
        self.max_daily_loss_pct = max_daily_loss_pct
        self._trades_path    = Path(trades_file)
        self._state_path     = Path(state_file)

        self.positions: Dict[str, Position] = {}    # open positions keyed by ID
        self.closed_trades: List[Position]  = []
        self._session_start_capital = capital_usdt
        self._session_date: str = ""

        # Callbacks
        self._on_open_cb: Optional[Callable]  = None
        self._on_close_cb: Optional[Callable] = None

        self._load()

    # ── Callbacks ──────────────────────────────────────────────────────────────

    def on_open(self, fn: Callable) -> "PaperTrader":
        self._on_open_cb = fn
        return self

    def on_close(self, fn: Callable) -> "PaperTrader":
        self._on_close_cb = fn
        return self

    # ── Risk checks ────────────────────────────────────────────────────────────

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def session_pnl(self) -> float:
        return self.capital - self._session_start_capital

    def _reset_daily_session(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._session_date:
            self._session_date           = today
            self._session_start_capital  = self.capital

    def can_open(self) -> tuple[bool, str]:
        """Returns (allowed, reason)."""
        self._reset_daily_session()
        if self.open_count >= self.max_positions:
            return False, f"max {self.max_positions} positions open"
        daily_loss_pct = (self.session_pnl / self._session_start_capital) * 100
        if daily_loss_pct <= -self.max_daily_loss_pct:
            return False, f"daily loss limit {self.max_daily_loss_pct}% hit"
        return True, ""

    # ── Position sizing ────────────────────────────────────────────────────────

    def _compute_quantity(self, signal: Signal) -> float:
        """
        How many base-asset units to buy/sell.
        risk_amount / (ATR * atr_sl_mult) = units at risk per unit of price.
        But SL distance in price = |entry - stop_loss|.
        """
        risk_amount  = self.capital * self.risk_pct / 100
        sl_distance  = abs(signal.entry - signal.stop_loss)
        if sl_distance == 0:
            return 0.0
        quantity = risk_amount / sl_distance
        # Cap: notional must not exceed capital * leverage
        max_notional = self.capital * self.leverage
        max_quantity = max_notional / signal.entry
        return min(quantity, max_quantity)

    # ── Open / close ───────────────────────────────────────────────────────────

    def open_position(self, signal: Signal) -> Optional[Position]:
        allowed, reason = self.can_open()
        if not allowed:
            logger.info(f"Trade skipped ({signal.symbol}): {reason}")
            return None

        # Don't open 2 positions on same symbol
        for p in self.positions.values():
            if p.symbol == signal.symbol:
                logger.info(f"Trade skipped: already open position on {signal.symbol}")
                return None

        qty      = self._compute_quantity(signal)
        notional = qty * signal.entry
        margin   = notional / self.leverage

        if margin > self.capital * 0.5:   # never risk more than 50% of capital as margin
            logger.warning(f"Position too large for {signal.symbol}, skipping")
            return None

        pos = Position(
            id          = str(uuid.uuid4())[:8],
            symbol      = signal.symbol,
            direction   = signal.direction,
            entry_price = signal.entry,
            stop_loss   = signal.stop_loss,
            take_profit = signal.take_profit,
            quantity    = round(qty, 6),
            notional    = round(notional, 4),
            margin      = round(margin, 4),
            leverage    = self.leverage,
            opened_at   = time.time(),
        )

        self.positions[pos.id] = pos
        self._save()
        logger.info(
            f"[PAPER] OPEN {pos.direction} {pos.symbol} "
            f"qty={pos.quantity} entry={pos.entry_price:.4f} "
            f"sl={pos.stop_loss:.4f} tp={pos.take_profit:.4f}"
        )
        if self._on_open_cb:
            asyncio.ensure_future(self._on_open_cb(pos, signal))
        return pos

    def close_position(
        self,
        pos_id: str,
        exit_price: float,
        reason: str = "MANUAL",
    ) -> Optional[Position]:
        pos = self.positions.pop(pos_id, None)
        if pos is None:
            return None

        pos.exit_price  = exit_price
        pos.exit_reason = reason
        pos.closed_at   = time.time()
        pos.pnl         = _compute_pnl(pos, exit_price)
        self.capital   += pos.pnl
        self.closed_trades.append(pos)
        self._save()

        logger.info(
            f"[PAPER] CLOSE {pos.direction} {pos.symbol} "
            f"exit={exit_price:.4f} reason={reason} pnl={pos.pnl:+.4f}"
        )
        if self._on_close_cb:
            asyncio.ensure_future(self._on_close_cb(pos))
        return pos

    def check_exits(self, symbol: str, candles: List[Dict]) -> List[Position]:
        """
        Check SL/TP for all open positions in `symbol` using latest candle.
        Returns list of positions that were closed.
        """
        if not candles:
            return []
        latest = candles[-1]
        high, low = latest["high"], latest["low"]
        closed = []

        for pid, pos in list(self.positions.items()):
            if pos.symbol != symbol:
                continue
            if pos.direction == "LONG":
                if low <= pos.stop_loss:
                    closed.append(self.close_position(pid, pos.stop_loss, "SL_HIT"))
                elif high >= pos.take_profit:
                    closed.append(self.close_position(pid, pos.take_profit, "TP_HIT"))
            else:  # SHORT
                if high >= pos.stop_loss:
                    closed.append(self.close_position(pid, pos.stop_loss, "SL_HIT"))
                elif low <= pos.take_profit:
                    closed.append(self.close_position(pid, pos.take_profit, "TP_HIT"))

        return [p for p in closed if p is not None]

    # ── Stats ──────────────────────────────────────────────────────────────────

    @property
    def stats(self) -> Dict:
        trades = self.closed_trades
        n      = len(trades)
        wins   = [t for t in trades if (t.pnl or 0) > 0]
        losses = [t for t in trades if (t.pnl or 0) <= 0]
        total_pnl = sum(t.pnl or 0 for t in trades)
        return {
            "total_trades":   n,
            "wins":           len(wins),
            "losses":         len(losses),
            "win_rate":       len(wins) / n if n else 0.0,
            "total_pnl":      round(total_pnl, 4),
            "avg_pnl":        round(total_pnl / n, 4) if n else 0.0,
            "capital":        round(self.capital, 4),
            "initial_capital":self.initial_capital,
            "total_return_pct": round((self.capital - self.initial_capital) / self.initial_capital * 100, 2),
        }

    # ── Persistence ────────────────────────────────────────────────────────────

    def _save(self):
        try:
            data = {
                "capital":       self.capital,
                "open":          {pid: p.to_dict() for pid, p in self.positions.items()},
                "closed":        [p.to_dict() for p in self.closed_trades],
                "session_start": self._session_start_capital,
                "session_date":  self._session_date,
            }
            self._trades_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save trades: {e}")

    def _load(self):
        if not self._trades_path.exists():
            return
        try:
            data = json.loads(self._trades_path.read_text())
            self.capital                = data.get("capital", self.capital)
            self._session_start_capital = data.get("session_start", self.capital)
            self._session_date          = data.get("session_date", "")
            for p_dict in data.get("closed", []):
                self.closed_trades.append(Position(**p_dict))
            # Re-open positions (skip if they were from a prior run with no exit)
            for pid, p_dict in data.get("open", {}).items():
                pos = Position(**p_dict)
                if pos.is_open:
                    self.positions[pid] = pos
            logger.info(
                f"Loaded state: capital=${self.capital:.2f} "
                f"open={len(self.positions)} closed={len(self.closed_trades)}"
            )
        except Exception as e:
            logger.error(f"Failed to load trades: {e}")


# ── Live Trader ────────────────────────────────────────────────────────────────

class LiveTrader(PaperTrader):
    """
    Extends PaperTrader with real order placement via CoinDCX API.
    Local position tracking mirrors exchange state.
    """

    def __init__(self, client: CoinDCXClient, **kwargs):
        super().__init__(**kwargs)
        self._client = client

    async def open_position(self, signal: Signal) -> Optional[Position]:   # type: ignore[override]
        allowed, reason = self.can_open()
        if not allowed:
            logger.info(f"Trade skipped ({signal.symbol}): {reason}")
            return None

        for p in self.positions.values():
            if p.symbol == signal.symbol:
                return None

        qty      = self._compute_quantity(signal)
        side     = "buy" if signal.direction == "LONG" else "sell"

        result = await self._client.place_order(
            pair       = signal.symbol,
            side       = side,
            order_type = "market_order",
            quantity   = round(qty, 6),
            leverage   = self.leverage,
            sl         = round(signal.stop_loss, 8),
            tp         = round(signal.take_profit, 8),
        )

        if result is None:
            logger.error(f"Order placement failed for {signal.symbol}")
            return None

        order_id   = result.get("id") or result.get("order_id", "")
        fill_price = float(result.get("avg_fill_price", signal.entry) or signal.entry)
        notional   = qty * fill_price
        margin     = notional / self.leverage

        pos = Position(
            id          = str(uuid.uuid4())[:8],
            symbol      = signal.symbol,
            direction   = signal.direction,
            entry_price = fill_price,
            stop_loss   = signal.stop_loss,
            take_profit = signal.take_profit,
            quantity    = round(qty, 6),
            notional    = round(notional, 4),
            margin      = round(margin, 4),
            leverage    = self.leverage,
            opened_at   = time.time(),
            order_id    = order_id,
        )

        self.positions[pos.id] = pos
        self._save()
        logger.info(f"[LIVE] OPEN {pos.direction} {pos.symbol} order_id={order_id}")
        if self._on_open_cb:
            await self._on_open_cb(pos, signal)
        return pos

    async def close_position(   # type: ignore[override]
        self,
        pos_id: str,
        exit_price: float,
        reason: str = "MANUAL",
    ) -> Optional[Position]:
        pos = self.positions.get(pos_id)
        if pos is None:
            return None
        close_side = "sell" if pos.direction == "LONG" else "buy"
        await self._client.close_position(
            pair=pos.symbol, quantity=pos.quantity, side=close_side
        )
        # Delegate to parent for local tracking
        return super().close_position(pos_id, exit_price, reason)
