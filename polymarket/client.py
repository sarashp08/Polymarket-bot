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
  - Finds the active BTC 15-minute direction market via Gamma API.
  - Places FOK market orders for YES (UP signal) or NO (DOWN signal).
  - Resolution is tracked via BTC price comparison (same as paper mode);
    actual USDC payouts are handled on-chain by Polymarket automatically.
"""

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TIMEFRAME_SECONDS = {"5m": 300, "15m": 900}

GAMMA_API = "https://gamma-api.polymarket.com"


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
        # Snapshot of bankroll at the start of THIS session (after loading saved state)
        self.session_start_bankroll: float = self.bankroll

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


# ── Market discovery ─────────────────────────────────────────────────────────────

# These BTC up/down series markets use a slug with the window start timestamp.
# The slug pattern is: btc-updown-{tf}-{unix_ts}
# Window timestamps are aligned to timeframe-second boundaries.
_TF_SLUG_PREFIX = {
    "5m":  "btc-updown-5m",
    "15m": "btc-updown-15m",
}


async def find_active_market(timeframe: str) -> Optional[dict]:
    """
    Find the active BTC up/down market for the given timeframe.

    Uses slug-based discovery: constructs the expected slug from the current
    UTC time (e.g. btc-updown-15m-1772559900) and fetches it directly.
    Tries current window and the next window in case of boundary conditions.
    """
    prefix = _TF_SLUG_PREFIX.get(timeframe)
    if not prefix:
        logger.error(f"No slug prefix for timeframe {timeframe}")
        return None

    window_secs = TIMEFRAME_SECONDS.get(timeframe, 900)
    now = int(time.time())

    # Try current window, then next (handles boundary / pre-open next window)
    for offset in (0, window_secs):
        window_ts = ((now + offset) // window_secs) * window_secs
        slug = f"{prefix}-{window_ts}"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{GAMMA_API}/markets", params={"slug": slug}
                )
                if resp.status_code != 200:
                    logger.warning(f"Gamma API {resp.status_code} for slug {slug}")
                    continue
                markets = resp.json()

            if not markets:
                logger.debug(f"No market found for slug {slug}")
                continue

            market = markets[0]

            if market.get("closed"):
                logger.debug(f"Market {slug} is closed, trying next window")
                continue

            if not market.get("active"):
                logger.debug(f"Market {slug} not active, trying next window")
                continue

            logger.info(
                f"Found market: {market.get('question', slug)[:80]}"
            )
            return market

        except Exception as exc:
            logger.error(f"Market lookup error for slug {slug}: {exc}")

    logger.warning(f"No active BTC {timeframe} market found on Polymarket")
    return None


def _parse_json_field(value) -> list:
    """Parse a field that may be a JSON-encoded string or already a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


def _extract_token_ids(market: dict) -> Optional[tuple]:
    """
    Extract (up_token_id, down_token_id) from a Gamma API market dict.

    These markets use "Up"/"Down" outcomes (not "YES"/"NO").
    clobTokenIds order matches outcomes order: [up_id, down_id].
    Returns None if tokens can't be parsed.
    """
    tokens = market.get("tokens")
    if tokens and isinstance(tokens, list) and len(tokens) >= 2:
        up_id = None
        down_id = None
        for t in tokens:
            outcome = (t.get("outcome") or "").upper()
            tid = t.get("token_id")
            if outcome in ("YES", "UP"):
                up_id = tid
            elif outcome in ("NO", "DOWN"):
                down_id = tid
        if up_id and down_id:
            return up_id, down_id

    # Gamma API returns clobTokenIds as a JSON-encoded string: ["id1", "id2"]
    # Order is [up_token_id, down_token_id] matching the outcomes array.
    clob_ids = _parse_json_field(market.get("clobTokenIds"))
    if len(clob_ids) >= 2:
        return clob_ids[0], clob_ids[1]

    return None


def get_market_entry_price(market: dict, direction: str) -> float:
    """
    Return the current market price for the given direction token.
    Falls back to 0.50 if the price can't be read.
    """
    # CLOB-style tokens list (when market dict comes from CLOB API)
    tokens = market.get("tokens")
    if tokens and isinstance(tokens, list):
        for t in tokens:
            outcome = (t.get("outcome") or "").upper()
            price = t.get("price")
            if price is not None:
                if direction == "UP" and outcome in ("YES", "UP"):
                    return float(price)
                if direction == "DOWN" and outcome in ("NO", "DOWN"):
                    return float(price)

    # Gamma API outcomePrices: JSON string "[up_price, down_price]"
    prices = _parse_json_field(market.get("outcomePrices"))
    if len(prices) >= 2:
        idx = 0 if direction == "UP" else 1
        try:
            return float(prices[idx])
        except (TypeError, ValueError):
            pass

    return 0.50


# ── Live trader ──────────────────────────────────────────────────────────────────

class LiveTrader:
    """
    Real Polymarket trader using py-clob-client.

    Same interface as PaperTrader so main.py can swap them transparently.
    """

    def __init__(
        self,
        initial_bankroll: float,
        log_file: str,
        host: str,
        chain_id: int,
        private_key: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
    ):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        self.initial_bankroll = initial_bankroll
        self.log_file = log_file
        self.bankroll: float = initial_bankroll
        self.positions: Dict[str, PaperPosition] = {}
        self.closed_positions: List[PaperPosition] = []

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        self.client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=private_key,
            creds=creds,
        )

        self._load()
        self.session_start_bankroll: float = self.bankroll

        logger.info(
            f"[LIVE] Trader ready | wallet={self.client.get_address()} | "
            f"bankroll=${self.bankroll:.2f}"
        )

    # ── Persistence (same as PaperTrader) ─────────────────────────────────────

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
            "closed_positions": [asdict(p) for p in self.closed_positions[-500:]],
        }
        Path(self.log_file).write_text(json.dumps(data, indent=2))

    # ── Trading ────────────────────────────────────────────────────────────────

    def open_position(
        self,
        direction: str,
        cost_usd: float,
        entry_price: float,
        entry_btc_price: float,
        question: str,
        timeframe: str,
        market: Optional[dict] = None,
    ) -> Optional[PaperPosition]:
        """
        Place a real order on Polymarket.

        If `market` is provided (from find_active_market), it must contain
        token IDs. Otherwise the position is skipped.
        """
        if cost_usd > self.bankroll:
            logger.warning(
                f"Insufficient bankroll (${self.bankroll:.2f}) for ${cost_usd:.2f} trade"
            )
            return None

        if cost_usd < 1.0:
            logger.warning(
                f"Trade size ${cost_usd:.2f} below $1 minimum — skipping"
            )
            return None

        if market is None:
            logger.error("No market provided — cannot place live order")
            return None

        token_pair = _extract_token_ids(market)
        if token_pair is None:
            logger.error("Could not extract token IDs from market")
            return None

        yes_token, no_token = token_pair

        # UP signal → buy YES (betting BTC goes up)
        # DOWN signal → buy NO (betting BTC goes down)
        token_id = yes_token if direction == "UP" else no_token
        side_label = "YES" if direction == "UP" else "NO"

        # Place FOK market order; fall back to GTC limit if no liquidity yet
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs
        from py_clob_client.order_builder.constants import BUY

        resp = None
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=cost_usd,
            side=BUY,
        )

        try:
            signed = self.client.create_market_order(order_args)
            resp = self.client.post_order(signed, order_type="FOK")
        except Exception as exc:
            if "no match" in str(exc).lower():
                # Fresh market with no liquidity yet — try a GTC limit order
                # at entry_price so market makers can fill us.
                logger.warning(
                    f"[LIVE] Market order no match (illiquid) — "
                    f"falling back to GTC limit @ {entry_price:.3f}"
                )
                try:
                    size = round(cost_usd / entry_price, 2)
                    limit_args = OrderArgs(
                        token_id=token_id,
                        price=entry_price,
                        size=size,
                        side=BUY,
                    )
                    signed = self.client.create_order(limit_args)
                    resp = self.client.post_order(signed, order_type="GTC")
                except Exception as exc2:
                    logger.error(f"[LIVE] GTC limit fallback failed: {exc2}")
                    return None
            else:
                logger.error(f"[LIVE] Order placement failed: {exc}")
                return None

        # Check if order was accepted
        if not resp or not resp.get("success"):
            error_msg = resp.get("errorMsg", "unknown error") if resp else "no response"
            logger.error(f"[LIVE] Order rejected: {error_msg}")
            return None

        order_id = resp.get("orderID", str(uuid.uuid4())[:8])
        shares = cost_usd / entry_price
        resolve_seconds = TIMEFRAME_SECONDS[timeframe]

        pos = PaperPosition(
            id=order_id[:8],
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
            f"[LIVE] Opened {direction} {timeframe} | BUY {side_label} | "
            f"${cost_usd:.2f} → {shares:.4f} shares | "
            f"BTC @ ${entry_btc_price:,.2f} | order={order_id[:12]}"
        )
        return pos

    def resolve_position(
        self, pos_id: str, current_btc_price: float
    ) -> Optional[PaperPosition]:
        """
        Resolve a position by comparing BTC prices.

        On Polymarket, the actual USDC payout is handled on-chain.
        We track P&L locally for notifications/stats.
        """
        pos = self.positions.get(pos_id)
        if not pos:
            return None

        price_went_up = current_btc_price > pos.entry_btc_price
        won = (pos.direction == "UP" and price_went_up) or (
            pos.direction == "DOWN" and not price_went_up
        )

        if won:
            payout = pos.shares * 1.0
            pos.pnl = payout - pos.cost_usd
            pos.status = "WON"
            self.bankroll += payout
        else:
            pos.pnl = -pos.cost_usd
            pos.status = "LOST"

        pos.exit_btc_price = current_btc_price
        del self.positions[pos_id]
        self.closed_positions.append(pos)
        self._save()

        logger.info(
            f"[LIVE] Resolved {pos.id} → {pos.status} | "
            f"P&L ${pos.pnl:+.2f} | "
            f"BTC {pos.entry_btc_price:,.0f} → {current_btc_price:,.0f}"
        )
        return pos

    async def check_resolutions(self, current_btc_price: float) -> List[PaperPosition]:
        resolved = []
        now = time.time()
        for pos_id, pos in list(self.positions.items()):
            if now >= pos.resolve_time:
                closed = self.resolve_position(pos_id, current_btc_price)
                if closed:
                    resolved.append(closed)
        return resolved

    # ── Stats helpers (same as PaperTrader) ───────────────────────────────────

    @property
    def open_count(self) -> int:
        return len(self.positions)

    @property
    def total_pnl(self) -> float:
        return sum(p.pnl for p in self.closed_positions)

    @property
    def win_rate(self) -> float:
        if not self.closed_positions:
            return 0.0
        wins = sum(1 for p in self.closed_positions if p.status == "WON")
        return wins / len(self.closed_positions)

    @property
    def total_closed(self) -> int:
        return len(self.closed_positions)
