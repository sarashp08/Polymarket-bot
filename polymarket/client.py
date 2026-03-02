"""
Polymarket trading client.

Paper trading mode (default):
  - Simulates opening YES/NO positions on BTC direction markets.
  - Resolves positions after the target timeframe by comparing BTC price
    at entry vs BTC price at resolution time.
  - Persists state to trades.json between restarts.

Live trading mode:
  - Uses py-clob-client to place real orders on Polymarket CLOB.
  - Requires POLY_PRIVATE_KEY + API credentials in .env.
"""

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"5m": 300, "15m": 900}


# ── Data models ─────────────────────────────────────────────────────────────────

@dataclass
class PaperPosition:
    id: str
    direction: str          # "UP" | "DOWN"
    timeframe: str          # "5m" | "15m"
    shares: float           # number of YES (or NO) shares held
    entry_price: float      # per-share cost at open (e.g. 0.50)
    cost_usd: float         # total USDC spent
    entry_btc_price: float  # BTC spot price when position was opened
    entry_time: float       # unix timestamp
    resolve_time: float     # unix timestamp when market resolves
    question: str
    status: str = "OPEN"    # OPEN | WON | LOST
    pnl: float = 0.0
    exit_btc_price: float = 0.0


# ── Paper trader ─────────────────────────────────────────────────────────────────

class PaperTrader:
    def __init__(self, initial_bankroll: float = 1_000.0, log_file: str = "trades.json"):
        self.initial_bankroll = initial_bankroll
        self.log_file = log_file
        self.bankroll: float = initial_bankroll
        self.positions: Dict[str, PaperPosition] = {}
        self.closed_positions: List[PaperPosition] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self):
        path = Path(self.log_file)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            self.bankroll = data.get("bankroll", self.initial_bankroll)
            for p in data.get("open_positions", []):
                pos = PaperPosition(**p)
                self.positions[pos.id] = pos
            for p in data.get("closed_positions", []):
                self.closed_positions.append(PaperPosition(**p))
            logger.info(
                f"Loaded state: bankroll=${self.bankroll:.2f}, "
                f"{len(self.positions)} open, {len(self.closed_positions)} closed"
            )
        except Exception as exc:
            logger.warning(f"Could not load {self.log_file}: {exc}")

    def _save(self):
        data = {
            "bankroll": self.bankroll,
            "initial_bankroll": self.initial_bankroll,
            "open_positions": [asdict(p) for p in self.positions.values()],
            # Keep last 500 closed trades
            "closed_positions": [asdict(p) for p in self.closed_positions[-500:]],
        }
        Path(self.log_file).write_text(json.dumps(data, indent=2))

    # ── Trading ───────────────────────────────────────────────────────────────

    def open_position(
        self,
        direction: str,
        cost_usd: float,
        entry_price: float,
        entry_btc_price: float,
        question: str,
        timeframe: str,
    ) -> Optional[PaperPosition]:
        if cost_usd > self.bankroll:
            logger.warning(
                f"Insufficient bankroll (${self.bankroll:.2f}) for ${cost_usd:.2f} trade"
            )
            return None

        if cost_usd < 1.0:
            logger.warning("Trade size below $1 minimum — skipping")
            return None

        shares = cost_usd / entry_price
        resolve_seconds = TIMEFRAME_SECONDS[timeframe]

        pos = PaperPosition(
            id=str(uuid.uuid4())[:8],
            direction=direction,
            timeframe=timeframe,
            shares=shares,
            entry_price=entry_price,
            cost_usd=cost_usd,
            entry_btc_price=entry_btc_price,
            entry_time=time.time(),
            resolve_time=time.time() + resolve_seconds,
            question=question,
        )

        self.bankroll -= cost_usd
        self.positions[pos.id] = pos
        self._save()

        logger.info(
            f"[PAPER] Opened {direction} {timeframe} | "
            f"${cost_usd:.2f} → {shares:.4f} shares @ ${entry_price:.3f} | "
            f"BTC @ ${entry_btc_price:,.2f}"
        )
        return pos

    def resolve_position(
        self, pos_id: str, current_btc_price: float
    ) -> Optional[PaperPosition]:
        pos = self.positions.get(pos_id)
        if not pos:
            return None

        price_went_up = current_btc_price > pos.entry_btc_price
        won = (pos.direction == "UP" and price_went_up) or (
            pos.direction == "DOWN" and not price_went_up
        )

        if won:
            # Each share pays out $1 at resolution
            payout = pos.shares * 1.0
            pos.pnl = payout - pos.cost_usd
            pos.status = "WON"
            self.bankroll += payout
        else:
            pos.pnl = -pos.cost_usd
            pos.status = "LOST"
            # bankroll was already debited at open; no payout on loss

        pos.exit_btc_price = current_btc_price
        del self.positions[pos_id]
        self.closed_positions.append(pos)
        self._save()

        logger.info(
            f"[PAPER] Resolved {pos.id} → {pos.status} | "
            f"P&L ${pos.pnl:+.2f} | "
            f"BTC {pos.entry_btc_price:,.0f} → {current_btc_price:,.0f}"
        )
        return pos

    async def check_resolutions(self, current_btc_price: float) -> List[PaperPosition]:
        """Resolve any positions whose time has expired. Call periodically."""
        resolved = []
        now = time.time()
        for pos_id, pos in list(self.positions.items()):
            if now >= pos.resolve_time:
                closed = self.resolve_position(pos_id, current_btc_price)
                if closed:
                    resolved.append(closed)
        return resolved

    # ── Stats helpers ─────────────────────────────────────────────────────────

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def total_pnl(self) -> float:
        realized = sum(p.pnl for p in self.closed_positions)
        unrealized = 0.0  # paper positions have no mark-to-market
        return realized + unrealized

    @property
    def win_rate(self) -> float:
        if not self.closed_positions:
            return 0.0
        wins = sum(1 for p in self.closed_positions if p.status == "WON")
        return wins / len(self.closed_positions)

    @property
    def total_closed(self) -> int:
        return len(self.closed_positions)
