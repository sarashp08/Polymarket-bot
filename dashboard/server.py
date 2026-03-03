"""
Dashboard server for the Polymarket BTC trading bot.

Runs on port 8080 as a separate process:
  uvicorn dashboard.server:app --host 0.0.0.0 --port 8080

Reads shared JSON files written by main.py:
  live_state.json   — live bot metrics (price, CVD, bankroll, etc.)
  signals_log.json  — last 50 trade signals
  trades.json       — open + closed positions

Writes commands back to main.py via:
  dashboard_cmds.json
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent.parent  # /home/user/Polymarket-bot
STATIC = Path(__file__).parent       # /home/user/Polymarket-bot/dashboard

app = FastAPI(title="Polymarket Bot Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_json(name: str, default):
    p = BASE / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _merged_snapshot() -> dict:
    state   = _read_json("live_state.json", {})
    signals = _read_json("signals_log.json", [])
    trades  = _read_json("trades.json", {})

    open_positions  = trades.get("open_positions", [])
    closed_positions = trades.get("closed_positions", [])

    return {
        "state": state,
        "signals": signals[-20:],          # last 20 for the feed
        "open_positions": open_positions,
        "closed_count": len(closed_positions),
    }


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/snapshot")
async def snapshot():
    return JSONResponse(_merged_snapshot())


class Command(BaseModel):
    paused: bool | None = None
    risk_pct: float | None = None
    close_position: str | None = None
    paper_trading: bool | None = None


_cmd_seq = 0


@app.post("/api/command")
async def command(cmd: Command):
    global _cmd_seq
    _cmd_seq += 1

    # Read existing file to preserve fields not being changed
    existing = _read_json("dashboard_cmds.json", {})

    payload = {
        "paused":          cmd.paused          if cmd.paused          is not None else existing.get("paused", False),
        "risk_pct":        cmd.risk_pct        if cmd.risk_pct        is not None else existing.get("risk_pct", 2.0),
        "close_position":  cmd.close_position,
        "paper_trading":   cmd.paper_trading   if cmd.paper_trading   is not None else existing.get("paper_trading", True),
        "_seq": _cmd_seq,
    }

    (BASE / "dashboard_cmds.json").write_text(json.dumps(payload))
    return {"ok": True, "seq": _cmd_seq}


# ── WebSocket broadcast ────────────────────────────────────────────────────────

_clients: Set[WebSocket] = set()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            # Keep connection alive — actual pushes come from the broadcast task
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


async def _broadcast_loop():
    """Push a merged snapshot to all connected dashboard clients every 1.5 s."""
    while True:
        if _clients:
            data = json.dumps(_merged_snapshot())
            dead = set()
            for ws in list(_clients):
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.add(ws)
            _clients.difference_update(dead)
        await asyncio.sleep(1.5)


@app.on_event("startup")
async def startup():
    asyncio.create_task(_broadcast_loop())
