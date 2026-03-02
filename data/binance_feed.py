"""
Binance WebSocket feed.

Streams two channels concurrently:
  - @trade      → individual executed trades (price, qty, aggressor side)
  - @depth20    → top-20 order book snapshot every 100 ms

Both channels auto-reconnect with a 5 s back-off on failure.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

import websockets

logger = logging.getLogger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443/ws"


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    price: float
    quantity: float        # BTC
    usd_value: float       # price * quantity
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


# ── Feed ───────────────────────────────────────────────────────────────────────

class BinanceFeed:
    def __init__(self, symbol: str = "btcusdt"):
        self.symbol = symbol.lower()
        self._trade_cb: Optional[Callable] = None
        self._ob_cb: Optional[Callable] = None
        self._running = False

    def on_trade(self, callback: Callable) -> "BinanceFeed":
        self._trade_cb = callback
        return self

    def on_orderbook(self, callback: Callable) -> "BinanceFeed":
        self._ob_cb = callback
        return self

    async def _stream_trades(self):
        url = f"{BINANCE_WS}/{self.symbol}@trade"
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("Connected to Binance trade stream")
                    async for raw in ws:
                        if not self._running:
                            break
                        d = json.loads(raw)
                        if self._trade_cb:
                            trade = Trade(
                                symbol=d["s"],
                                price=float(d["p"]),
                                quantity=float(d["q"]),
                                usd_value=float(d["p"]) * float(d["q"]),
                                is_buyer_maker=bool(d["m"]),
                                timestamp_ms=int(d["T"]),
                            )
                            await self._trade_cb(trade)
            except Exception as exc:
                if self._running:
                    logger.warning(f"Trade stream error: {exc} — reconnecting in 5 s")
                    await asyncio.sleep(5)

    async def _stream_orderbook(self):
        url = f"{BINANCE_WS}/{self.symbol}@depth20@100ms"
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("Connected to Binance orderbook stream")
                    async for raw in ws:
                        if not self._running:
                            break
                        d = json.loads(raw)
                        if self._ob_cb:
                            ob = OrderBook(
                                bids=d["bids"],
                                asks=d["asks"],
                                timestamp_ms=int(asyncio.get_event_loop().time() * 1000),
                            )
                            await self._ob_cb(ob)
            except Exception as exc:
                if self._running:
                    logger.warning(f"Orderbook stream error: {exc} — reconnecting in 5 s")
                    await asyncio.sleep(5)

    async def start(self):
        self._running = True
        await asyncio.gather(
            self._stream_trades(),
            self._stream_orderbook(),
        )

    def stop(self):
        self._running = False
