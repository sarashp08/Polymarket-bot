"""
CoinDCX Futures REST API Client
================================
Handles authentication and order management for CoinDCX futures.

Auth: HMAC-SHA256 signature over JSON request body.
  Headers: X-AUTH-APIKEY, X-AUTH-SIGNATURE

Docs: https://docs.coindcx.com/
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coindcx.com"


class CoinDCXClient:
    """Authenticated CoinDCX REST client for futures trading."""

    def __init__(self, api_key: str, api_secret: str):
        self._key    = api_key
        self._secret = api_secret.encode()

    def _sign(self, body: Dict) -> tuple[str, str]:
        """Return (json_body, signature)."""
        body_str  = json.dumps(body, separators=(",", ":"))
        signature = hmac.new(self._secret, body_str.encode(), hashlib.sha256).hexdigest()
        return body_str, signature

    def _headers(self, signature: str) -> Dict[str, str]:
        return {
            "Content-Type":      "application/json",
            "X-AUTH-APIKEY":     self._key,
            "X-AUTH-SIGNATURE":  signature,
        }

    async def _post(self, path: str, payload: Dict) -> Optional[Dict]:
        payload["timestamp"] = int(time.time() * 1000)
        body_str, sig = self._sign(payload)
        url = BASE_URL + path
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=body_str,
                    headers=self._headers(sig),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status not in (200, 201):
                        logger.error(f"CoinDCX POST {path} → {resp.status}: {data}")
                        return None
                    return data
        except Exception as e:
            logger.error(f"CoinDCX POST {path} error: {e}")
            return None

    async def _get_auth(self, path: str, payload: Dict) -> Optional[Any]:
        payload["timestamp"] = int(time.time() * 1000)
        body_str, sig = self._sign(payload)
        url = BASE_URL + path
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    data=body_str,
                    headers=self._headers(sig),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200:
                        logger.error(f"CoinDCX GET {path} → {resp.status}: {data}")
                        return None
                    return data
        except Exception as e:
            logger.error(f"CoinDCX GET {path} error: {e}")
            return None

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balances(self) -> Optional[List[Dict]]:
        """Return list of balances across all assets."""
        return await self._get_auth(
            "/exchange/v1/users/balances", {}
        )

    async def get_positions(self) -> Optional[List[Dict]]:
        """Return open futures positions."""
        return await self._post(
            "/exchange/v1/derivatives/futures/positions", {}
        )

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        pair: str,
        side: str,           # "buy" | "sell"
        order_type: str,     # "market_order" | "limit_order"
        quantity: float,
        price: Optional[float] = None,
        leverage: int = 3,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> Optional[Dict]:
        """
        Place a futures order.

        pair      - e.g. "B-ETH_USDT"
        side      - "buy" (open long / close short) | "sell" (open short / close long)
        order_type - "market_order" | "limit_order"
        quantity  - contract quantity in base asset units
        price     - required for limit orders
        leverage  - integer (e.g. 3)
        sl / tp   - optional stop-loss and take-profit prices
        """
        payload: Dict[str, Any] = {
            "pair":        pair,
            "side":        side,
            "order_type":  order_type,
            "quantity":    quantity,
            "leverage":    leverage,
        }
        if price is not None:
            payload["price"] = price
        if sl is not None:
            payload["stop_price"] = sl
        if tp is not None:
            payload["take_profit_price"] = tp

        logger.info(f"Placing order: {payload}")
        return await self._post(
            "/exchange/v1/derivatives/futures/orders", payload
        )

    async def cancel_order(self, order_id: str) -> Optional[Dict]:
        return await self._post(
            "/exchange/v1/derivatives/futures/orders/cancel",
            {"id": order_id},
        )

    async def close_position(
        self,
        pair: str,
        quantity: float,
        side: str,           # "buy" to close short, "sell" to close long
    ) -> Optional[Dict]:
        """Close an open futures position at market price."""
        return await self.place_order(
            pair=pair,
            side=side,
            order_type="market_order",
            quantity=quantity,
        )

    async def get_order_status(self, order_id: str) -> Optional[Dict]:
        return await self._post(
            "/exchange/v1/derivatives/futures/orders/status",
            {"id": order_id},
        )
