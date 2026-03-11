"""
CoinDCX OHLCV Data Feed
========================
Fetches candles from CoinDCX public market data API.

Endpoint:  https://public.coindcx.com/market_data/candles/
No API key required for market data.

Params:
  pair      - e.g. "B-ETH_USDT"
  interval  - "1m" | "5m" | "15m" | "30m" | "1h" | "2h" | "4h" | "6h" | "1d"
  startTime - Unix milliseconds (optional)
  limit     - max 1000 candles (default 500)

Candle response fields (CoinDCX returns a list of arrays or dicts):
  [timestamp_ms, open, high, low, close, volume]
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

CANDLES_URL = "https://public.coindcx.com/market_data/candles/"

# Binance proxy mapping for symbols not yet on CoinDCX
# Used as fallback for backtesting historical depth
BINANCE_PROXY: Dict[str, str] = {
    "B-ETH_USDT":  "ETHUSDT",
    "B-SOL_USDT":  "SOLUSDT",
    "B-HYPE_USDT": "HYPEUSDT",
    "B-AAVE_USDT": "AAVEUSDT",
    "B-ZEC_USDT":  "ZECUSDT",
    "B-UNI_USDT":  "UNIUSDT",
}
BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_INTERVAL_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "1d": "1d",
}


def _parse_coindcx_candles(raw: list) -> List[Dict]:
    """Parse CoinDCX candle response into normalised dicts."""
    candles = []
    for c in raw:
        if isinstance(c, (list, tuple)) and len(c) >= 6:
            candles.append({
                "ts":     int(c[0]),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        elif isinstance(c, dict):
            candles.append({
                "ts":     int(c.get("time", c.get("t", 0))),
                "open":   float(c.get("open",   c.get("o", 0))),
                "high":   float(c.get("high",   c.get("h", 0))),
                "low":    float(c.get("low",    c.get("l", 0))),
                "close":  float(c.get("close",  c.get("c", 0))),
                "volume": float(c.get("volume", c.get("v", 0))),
            })
    candles.sort(key=lambda x: x["ts"])
    return candles


def _parse_binance_candles(raw: list) -> List[Dict]:
    """Parse Binance kline response (fallback)."""
    candles = []
    for c in raw:
        candles.append({
            "ts":     int(c[0]),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        })
    candles.sort(key=lambda x: x["ts"])
    return candles


class CoinDCXFeed:
    """
    Fetches OHLCV candles for multiple CoinDCX futures symbols.
    Falls back to Binance Futures API if CoinDCX returns no data.
    """

    def __init__(self, symbols: List[str], interval: str = "4h"):
        self.symbols = symbols
        self.interval = interval
        self._cache: Dict[str, List[Dict]] = {s: [] for s in symbols}
        self._last_bar_ts: Dict[str, int] = {s: 0 for s in symbols}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def fetch_candles(
        self,
        symbol: str,
        limit: int = 300,
        start_time: Optional[int] = None,
    ) -> List[Dict]:
        """
        Fetch candles for one symbol.  Returns sorted list oldest→newest.
        Tries CoinDCX first; falls back to Binance Futures on failure.
        """
        candles = await self._fetch_coindcx(symbol, limit, start_time)
        if not candles:
            logger.warning(
                f"{symbol}: CoinDCX returned no data — trying Binance fallback"
            )
            candles = await self._fetch_binance(symbol, limit, start_time)

        if candles:
            self._cache[symbol] = candles
            self._last_bar_ts[symbol] = candles[-1]["ts"]

        return candles

    async def fetch_all(self, limit: int = 300) -> Dict[str, List[Dict]]:
        """Fetch candles for every configured symbol concurrently."""
        tasks = {s: self.fetch_candles(s, limit=limit) for s in self.symbols}
        results: Dict[str, List[Dict]] = {}
        for sym, coro in tasks.items():
            results[sym] = await coro
            await asyncio.sleep(0.15)   # gentle rate limiting
        return results

    def cached(self, symbol: str) -> List[Dict]:
        return self._cache.get(symbol, [])

    def latest_price(self, symbol: str) -> float:
        candles = self._cache.get(symbol, [])
        return candles[-1]["close"] if candles else 0.0

    def has_new_bar(self, symbol: str, candles: List[Dict]) -> bool:
        """Returns True if there is a new completed bar since last check."""
        if len(candles) < 2:
            return False
        # Use second-to-last (last *completed* bar)
        bar_ts = candles[-2]["ts"]
        if bar_ts > self._last_bar_ts.get(symbol, 0):
            self._last_bar_ts[symbol] = bar_ts
            return True
        return False

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _fetch_coindcx(
        self, symbol: str, limit: int, start_time: Optional[int]
    ) -> List[Dict]:
        params: Dict = {"pair": symbol, "interval": self.interval, "limit": min(limit, 1000)}
        if start_time:
            params["startTime"] = start_time
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    CANDLES_URL, params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"CoinDCX candles HTTP {resp.status} for {symbol}")
                        return []
                    raw = await resp.json(content_type=None)
                    return _parse_coindcx_candles(raw)
        except Exception as e:
            logger.error(f"CoinDCX candles error ({symbol}): {e}")
            return []

    async def _fetch_binance(
        self, symbol: str, limit: int, start_time: Optional[int]
    ) -> List[Dict]:
        binance_sym = BINANCE_PROXY.get(symbol)
        if not binance_sym:
            return []
        params: Dict = {
            "symbol":   binance_sym,
            "interval": BINANCE_INTERVAL_MAP.get(self.interval, "4h"),
            "limit":    min(limit, 1500),
        }
        if start_time:
            params["startTime"] = start_time
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    BINANCE_FUTURES_URL, params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return []
                    raw = await resp.json(content_type=None)
                    return _parse_binance_candles(raw)
        except Exception as e:
            logger.error(f"Binance fallback error ({symbol}): {e}")
            return []
