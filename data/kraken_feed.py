"""
Kraken WebSocket feed — drop-in replacement for BinanceFeed.

Streams two channels concurrently via Kraken WS v1 API:
  - trade   → individual executed trades (price, qty, aggressor side)
  - book-25 → top-25 order book, snapshot + incremental updates

Both channels auto-reconnect with a 5 s back-off on failure.
Exposes the same Trade / OrderBook dataclasses and callback interface
as the original BinanceFeed so main.py needs only one import change.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import websockets

logger = logging.getLogger(__name__)

KRAKEN_WS   = "wss://ws.kraken.com"
KRAKEN_PAIR = "XBT/USD"


# ── Data models (identical interface to binance_feed) ───────────────────────

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


# ── Feed ────────────────────────────────────────────────────────────────────

class KrakenFeed:
    def __init__(self, symbol: str = "btcusdt"):
        # `symbol` param kept for interface compatibility — Kraken always uses XBT/USD
        self._trade_cb: Optional[Callable] = None
        self._ob_cb: Optional[Callable] = None
        self._running = False
        # Local order-book state for incremental updates
        self._bids: Dict[str, str] = {}
        self._asks: Dict[str, str] = {}

    def on_trade(self, callback: Callable) -> "KrakenFeed":
        self._trade_cb = callback
        return self

    def on_orderbook(self, callback: Callable) -> "KrakenFeed":
        self._ob_cb = callback
        return self

    # ── Trade stream ──────────────────────────────────────────────────────────

    async def _stream_trades(self):
        url = KRAKEN_WS
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair": [KRAKEN_PAIR],
                        "subscription": {"name": "trade"},
                    }))
                    logger.info("Connected to Kraken trade stream")
                    async for raw in ws:
                        if not self._running:
                            break
                        msg = json.loads(raw)
                        # Kraken trade: [chanID, [[price,vol,time,side,...], ...], "trade", "XBT/USD"]
                        if not isinstance(msg, list) or len(msg) < 4 or msg[2] != "trade":
                            continue
                        for entry in msg[1]:
                            price    = float(entry[0])
                            qty      = float(entry[1])
                            ts_ms    = int(float(entry[2]) * 1000)
                            side     = entry[3]  # "b" = buyer aggressor, "s" = seller aggressor
                            # "b" → aggressive BUY  → is_buyer_maker = False
                            # "s" → aggressive SELL → is_buyer_maker = True
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
            except Exception as exc:
                if self._running:
                    logger.warning(f"Trade stream error: {exc} — reconnecting in 5 s")
                    await asyncio.sleep(5)

    # ── Order book stream ──────────────────────────────────────────────────────

    def _apply_levels(self, side_dict: Dict[str, str], updates: list):
        """Apply incremental order-book update; qty '0' removes the level."""
        for level in updates:
            price_str = level[0]
            qty_str   = level[1]
            if float(qty_str) == 0.0:
                side_dict.pop(price_str, None)
            else:
                side_dict[price_str] = qty_str

    def _emit_book(self) -> Optional[OrderBook]:
        if not self._bids or not self._asks:
            return None
        sorted_bids = sorted(self._bids.items(), key=lambda x: float(x[0]), reverse=True)[:20]
        sorted_asks = sorted(self._asks.items(), key=lambda x: float(x[0]))[:20]
        return OrderBook(
            bids=[[p, q] for p, q in sorted_bids],
            asks=[[p, q] for p, q in sorted_asks],
            timestamp_ms=int(time.time() * 1000),
        )

    async def _stream_orderbook(self):
        url = KRAKEN_WS
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({
                        "event": "subscribe",
                        "pair": [KRAKEN_PAIR],
                        "subscription": {"name": "book", "depth": 25},
                    }))
                    logger.info("Connected to Kraken order book stream")
                    self._bids.clear()
                    self._asks.clear()
                    async for raw in ws:
                        if not self._running:
                            break
                        msg = json.loads(raw)
                        if not isinstance(msg, list) or len(msg) < 3:
                            continue
                        data = msg[1]
                        if not isinstance(data, dict):
                            continue
                        # Snapshot keys: "bs" (bid snapshot), "as" (ask snapshot)
                        if "bs" in data:
                            for lvl in data["bs"]:
                                self._bids[lvl[0]] = lvl[1]
                        if "as" in data:
                            for lvl in data["as"]:
                                self._asks[lvl[0]] = lvl[1]
                        # Incremental keys: "b" (bid update), "a" (ask update)
                        if "b" in data:
                            self._apply_levels(self._bids, data["b"])
                        if "a" in data:
                            self._apply_levels(self._asks, data["a"])
                        if self._ob_cb:
                            ob = self._emit_book()
                            if ob:
                                await self._ob_cb(ob)
            except Exception as exc:
                if self._running:
                    logger.warning(f"Orderbook stream error: {exc} — reconnecting in 5 s")
                    await asyncio.sleep(5)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        await asyncio.gather(
            self._stream_trades(),
            self._stream_orderbook(),
        )

    def stop(self):
        self._running = False
