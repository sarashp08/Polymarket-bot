"""
Kraken REST polling feed — drop-in replacement for the WebSocket feed.

Uses Kraken's free public REST API (no auth required), accessible from this
server unlike WebSocket connections which are firewall-blocked.

Two async loops run concurrently:
  - Trade poller    → GET /0/public/Trades every 1 s  (incremental via `since`)
  - Orderbook poll  → GET /0/public/Depth  every 2 s  (full top-25 snapshot)

Pyth Hermes (Polymarket's own on-chain oracle) is also available for price
confirmation: https://hermes.pyth.network — but Kraken alone provides the
full trade + order-book stream the signal engine needs.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

KRAKEN_BASE       = "https://api.kraken.com/0/public"
KRAKEN_PAIR       = "XBTUSD"       # Kraken symbol for BTC/USD
KRAKEN_RESULT_KEY = "XXBTZUSD"     # key inside the result dict


# ── Data models ─────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    price: float
    quantity: float        # BTC
    usd_value: float       # price × quantity
    is_buyer_maker: bool   # True → aggressive SELL, False → aggressive BUY
    timestamp_ms: int

    @property
    def direction(self) -> str:
        return "SELL" if self.is_buyer_maker else "BUY"

    @property
    def signed_quantity(self) -> float:
        """Positive for buys, negative for sells — used for CVD."""
        return self.quantity if not self.is_buyer_maker else -self.quantity


@dataclass
class OrderBook:
    bids: list   # [[price_str, qty_str], …] sorted descending
    asks: list   # [[price_str, qty_str], …] sorted ascending
    timestamp_ms: int

    @property
    def best_bid(self) -> float:
        return float(self.bids[0][0]) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return float(self.asks[0][0]) if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0


# ── Feed ────────────────────────────────────────────────────────────────────────

class KrakenFeed:
    def __init__(self, symbol: str = "btcusdt"):
        # symbol param kept for interface compatibility — ignored internally
        self._trade_cb: Optional[Callable] = None
        self._ob_cb: Optional[Callable] = None
        self._running = False
        self._since: Optional[str] = None  # nanosecond cursor string

    def on_trade(self, callback: Callable) -> "KrakenFeed":
        self._trade_cb = callback
        return self

    def on_orderbook(self, callback: Callable) -> "KrakenFeed":
        self._ob_cb = callback
        return self

    # ── Trade polling ──────────────────────────────────────────────────────────

    async def _poll_trades(self):
        """Fetch new trades every second using the `since` cursor."""
        # Seed cursor to current tip so we only process trades going forward
        try:
            r = requests.get(
                f"{KRAKEN_BASE}/Trades",
                params={"pair": KRAKEN_PAIR},
                timeout=8,
            )
            self._since = r.json()["result"]["last"]
            logger.info("Kraken trade feed seeded — polling every 1 s")
        except Exception as exc:
            logger.warning(f"Trade feed seed failed: {exc}")

        while self._running:
            try:
                params = {"pair": KRAKEN_PAIR}
                if self._since:
                    params["since"] = self._since

                r = requests.get(f"{KRAKEN_BASE}/Trades", params=params, timeout=8)
                data = r.json()

                trades    = data["result"].get(KRAKEN_RESULT_KEY, [])
                new_since = data["result"].get("last")

                for entry in trades:
                    # entry: [price, volume, time, side, orderType, misc, tradeId]
                    price = float(entry[0])
                    qty   = float(entry[1])
                    ts_ms = int(float(entry[2]) * 1000)
                    side  = entry[3]  # "b" = buyer aggressor, "s" = seller aggressor

                    # "s" → seller hit the bid → is_buyer_maker = True
                    is_buyer_maker = (side == "s")

                    if self._trade_cb:
                        await self._trade_cb(Trade(
                            symbol="BTCUSD",
                            price=price,
                            quantity=qty,
                            usd_value=price * qty,
                            is_buyer_maker=is_buyer_maker,
                            timestamp_ms=ts_ms,
                        ))

                if new_since:
                    self._since = new_since

            except Exception as exc:
                logger.warning(f"Trade poll error: {exc}")

            await asyncio.sleep(1)

    # ── Order book polling ─────────────────────────────────────────────────────

    async def _poll_orderbook(self):
        """Fetch top-25 order book snapshot every 2 seconds."""
        logger.info("Kraken orderbook feed started — polling every 2 s")
        while self._running:
            try:
                r = requests.get(
                    f"{KRAKEN_BASE}/Depth",
                    params={"pair": KRAKEN_PAIR, "count": 25},
                    timeout=8,
                )
                data = r.json()
                book = data["result"].get(KRAKEN_RESULT_KEY, {})

                bids = [[b[0], b[1]] for b in book.get("bids", [])]
                asks = [[a[0], a[1]] for a in book.get("asks", [])]

                if self._ob_cb and bids and asks:
                    await self._ob_cb(OrderBook(
                        bids=bids,
                        asks=asks,
                        timestamp_ms=int(time.time() * 1000),
                    ))

            except Exception as exc:
                logger.warning(f"Orderbook poll error: {exc}")

            await asyncio.sleep(2)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        await asyncio.gather(
            self._poll_trades(),
            self._poll_orderbook(),
        )

    def stop(self):
        self._running = False
