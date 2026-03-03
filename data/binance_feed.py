"""
Bybit WebSocket feed (drop-in replacement for Binance feed).

Streams both channels over a single Bybit V5 public spot WebSocket:
  - publicTrade.BTCUSDT   → individual executed trades
  - orderbook.50.BTCUSDT  → top-50 order book (snapshot then deltas)

Auto-reconnects with 5 s back-off on failure.
Exposes the same Trade / OrderBook dataclasses and BinanceFeed class
so no other file needs to change.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import websockets

logger = logging.getLogger(__name__)

BYBIT_WS = "wss://stream.bybit.com/v5/public/spot"


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
    """
    Bybit-backed feed with the same interface as the original BinanceFeed.
    Class name kept for compatibility with main.py and config.
    """

    def __init__(self, symbol: str = "btcusdt"):
        self.symbol = symbol.upper()  # Bybit expects uppercase: BTCUSDT
        self._trade_cb: Optional[Callable] = None
        self._ob_cb: Optional[Callable] = None
        self._running = False

        # Local order book state for applying deltas
        self._bids: Dict[str, str] = {}  # price_str -> qty_str
        self._asks: Dict[str, str] = {}

    def on_trade(self, callback: Callable) -> "BinanceFeed":
        self._trade_cb = callback
        return self

    def on_orderbook(self, callback: Callable) -> "BinanceFeed":
        self._ob_cb = callback
        return self

    # ── Order book state management ───────────────────────────────────────────

    def _apply_ob_update(self, bids: list, asks: list):
        for price, qty in bids:
            if float(qty) == 0:
                self._bids.pop(price, None)
            else:
                self._bids[price] = qty
        for price, qty in asks:
            if float(qty) == 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = qty

    def _build_orderbook(self, timestamp_ms: int) -> OrderBook:
        sorted_bids = sorted(self._bids.items(), key=lambda x: float(x[0]), reverse=True)
        sorted_asks = sorted(self._asks.items(), key=lambda x: float(x[0]))
        return OrderBook(
            bids=[[p, q] for p, q in sorted_bids],
            asks=[[p, q] for p, q in sorted_asks],
            timestamp_ms=timestamp_ms,
        )

    # ── Message handlers ──────────────────────────────────────────────────────

    async def _handle_trades(self, msg: dict):
        if not self._trade_cb:
            return
        for t in msg.get("data", []):
            try:
                price = float(t["p"])
                qty = float(t["v"])
                # Bybit S="Sell" → taker was seller → aggressive sell → is_buyer_maker=True
                is_buyer_maker = t["S"] == "Sell"
                trade = Trade(
                    symbol=t["s"],
                    price=price,
                    quantity=qty,
                    usd_value=price * qty,
                    is_buyer_maker=is_buyer_maker,
                    timestamp_ms=int(t["T"]),
                )
                await self._trade_cb(trade)
            except Exception as exc:
                logger.debug(f"Trade parse error: {exc}")

    async def _handle_orderbook(self, msg: dict):
        if not self._ob_cb:
            return
        data = msg.get("data", {})
        ts = int(msg.get("ts", 0))
        bids = data.get("b", [])
        asks = data.get("a", [])

        if msg.get("type") == "snapshot":
            self._bids = {p: q for p, q in bids}
            self._asks = {p: q for p, q in asks}
        else:
            self._apply_ob_update(bids, asks)

        await self._ob_cb(self._build_orderbook(ts))

    # ── Main stream loop ──────────────────────────────────────────────────────

    async def _stream(self):
        trade_topic = f"publicTrade.{self.symbol}"
        ob_topic = f"orderbook.50.{self.symbol}"

        while self._running:
            try:
                async with websockets.connect(BYBIT_WS, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": [trade_topic, ob_topic],
                    }))
                    logger.info(f"Connected to Bybit: {trade_topic}, {ob_topic}")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        topic = msg.get("topic", "")
                        if topic == trade_topic:
                            await self._handle_trades(msg)
                        elif topic == ob_topic:
                            await self._handle_orderbook(msg)

            except Exception as exc:
                if self._running:
                    logger.warning(f"Bybit stream error: {exc} — reconnecting in 5 s")
                    await asyncio.sleep(5)

    async def start(self):
        self._running = True
        await self._stream()

    def stop(self):
        self._running = False
