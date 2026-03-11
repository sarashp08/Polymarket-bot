"""
dcx_sync.py — push dcx_state.json + dcx_trades.json to a GitHub Gist every 30s.

Run alongside dcx_main.py:
    python dcx_sync.py

Required .env vars:
    GITHUB_GIST_TOKEN  — personal access token with 'gist' scope
    GITHUB_GIST_ID     — ID from gist.github.com/{user}/{GIST_ID}
"""
import asyncio
import logging
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

GIST_TOKEN = os.getenv("GITHUB_GIST_TOKEN", "")
GIST_ID    = os.getenv("GITHUB_GIST_ID", "")
FILES      = ["dcx_state.json", "dcx_trades.json"]
INTERVAL   = 30  # seconds

logger = logging.getLogger(__name__)


async def sync_once(session: aiohttp.ClientSession) -> None:
    files_payload: dict = {}
    for fname in FILES:
        p = Path(fname)
        if p.exists():
            files_payload[fname] = {"content": p.read_text()}

    if not files_payload:
        logger.debug("No data files found yet — skipping sync")
        return

    async with session.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        json={"files": files_payload},
        headers={
            "Authorization": f"token {GIST_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
    ) as resp:
        if resp.status == 200:
            logger.info("Gist updated ✓")
        else:
            body = await resp.text()
            logger.warning("Gist sync failed %s: %s", resp.status, body[:120])


async def main() -> None:
    if not GIST_TOKEN or not GIST_ID:
        raise SystemExit(
            "Missing env vars — add GITHUB_GIST_TOKEN and GITHUB_GIST_ID to .env"
        )

    logger.info("dcx_sync started — pushing to gist %s every %ss", GIST_ID[:8] + "…", INTERVAL)

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await sync_once(session)
            except Exception as exc:
                logger.warning("Sync error: %s", exc)
            await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(main())
