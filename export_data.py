#!/usr/bin/env python3
"""Export bot state to docs/data.json for the GitHub Pages dashboard.

Usage:
    python3 export_data.py

Run this script (manually or via cron) from the bot's working directory.
It reads live_state.json, signals_log.json, and trades.json, then writes
docs/data.json so the GitHub Pages dashboard can fetch live data.

Cron example (every 5 minutes):
    */5 * * * * cd /home/user/Polymarket-bot && python3 export_data.py
"""

import json
import time
from pathlib import Path

BASE = Path(__file__).parent
DOCS = BASE / "docs"


def read_json(name, default):
    p = BASE / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def main():
    state   = read_json("live_state.json", {})
    signals = read_json("signals_log.json", [])
    trades  = read_json("trades.json", {})

    open_positions   = trades.get("open_positions", [])
    closed_positions = trades.get("closed_positions", [])

    data = {
        "exported_at":    time.time(),
        "state":          state,
        "signals":        signals[-20:],
        "open_positions": open_positions,
        "closed_count":   len(closed_positions),
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(data))
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Exported "
        f"{len(open_positions)} open, {len(closed_positions)} closed, "
        f"{len(signals)} signals → docs/data.json"
    )


if __name__ == "__main__":
    main()
