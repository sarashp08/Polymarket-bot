"""
Polymarket market discovery.

Queries the Polymarket Gamma API to find open BTC direction markets
(e.g. "Will BTC be higher in the next 5 minutes?").

Used in live trading mode to get real condition_ids / token_ids.
In paper trading mode this module is informational only.
"""

import logging
from typing import List

import aiohttp

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

BTC_KEYWORDS = [
    "bitcoin", "btc", "bitcoin price",
]
DIRECTION_KEYWORDS = [
    "higher", "lower", "above", "below", "up", "down",
]


async def find_btc_direction_markets() -> List[dict]:
    """
    Return a list of open Polymarket markets related to BTC direction.
    Each dict contains keys from the Gamma API response.
    """
    params = {
        "active": "true",
        "closed": "false",
        "limit": 100,
    }

    results = []
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{GAMMA_API}/markets", params=params) as resp:
                if resp.status != 200:
                    logger.error(f"Gamma API returned {resp.status}")
                    return []
                markets = await resp.json()
        except Exception as exc:
            logger.error(f"Failed to query Gamma API: {exc}")
            return []

    for m in markets:
        question = (m.get("question") or "").lower()
        has_btc = any(kw in question for kw in BTC_KEYWORDS)
        has_direction = any(kw in question for kw in DIRECTION_KEYWORDS)
        if has_btc and has_direction:
            results.append(m)

    logger.info(f"Found {len(results)} BTC direction markets on Polymarket")
    return results
