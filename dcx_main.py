"""
CoinDCX Futures Bot — Main Entry Point
=======================================
Runs the EMA Trend Strategy on 4H candles for ETH, SOL, HYPE, AAVE, ZEC, UNI.

Loops:
  signal_loop()    — polls CoinDCX candles every POLL_INTERVAL seconds,
                     evaluates strategy, opens/closes positions
  state_loop()     — writes live state JSON for dashboard every 30 s
  summary_loop()   — sends hourly P&L summary to Telegram
  telegram.start_polling() — listens for phone commands

Usage:
  python dcx_main.py

Environment (.env):
  PAPER_TRADING=true          # set false for live orders
  COINDCX_API_KEY=xxx
  COINDCX_API_SECRET=xxx
  TELEGRAM_TOKEN=xxx
  TELEGRAM_CHAT_ID=xxx
  CAPITAL_USDT=1000
  LEVERAGE=3
  RISK_PCT=1.0
  MAX_POSITIONS=4
  MAX_DAILY_LOSS_PCT=5.0
"""

import asyncio
import json
import logging
import signal as _signal
import sys
import time
from pathlib import Path

from coindcx_config import dcx_config as cfg
from coindcx.feed import CoinDCXFeed
from coindcx.strategy import EMATrendStrategy, StrategyParams
from coindcx.trader import PaperTrader, LiveTrader
from coindcx.formatters import (
    fmt_startup, fmt_open, fmt_close, fmt_status, fmt_positions, fmt_daily_summary,
)
from dcx_telegram import DCXTelegramBot

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("dcx_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── Build components ──────────────────────────────────────────────────────────

feed     = CoinDCXFeed(cfg.symbols, interval=cfg.primary_tf)
strategy = EMATrendStrategy(StrategyParams(
    ema_period  = cfg.ema_period,
    atr_period  = cfg.atr_period,
    atr_sl_mult = cfg.atr_sl_mult,
    atr_tp_mult = cfg.atr_tp_mult,
))
telegram = DCXTelegramBot(cfg.telegram_token, cfg.telegram_chat_id)

_trader_kwargs = dict(
    capital_usdt      = cfg.capital_usdt,
    risk_pct          = cfg.risk_per_trade_pct,
    max_positions     = cfg.max_open_positions,
    leverage          = cfg.leverage,
    max_daily_loss_pct= cfg.max_daily_loss_pct,
    trades_file       = cfg.trades_file,
    state_file        = cfg.state_file,
)

if cfg.paper_trading:
    trader = PaperTrader(**_trader_kwargs)
else:
    from coindcx.client import CoinDCXClient
    client = CoinDCXClient(cfg.api_key, cfg.api_secret)
    trader = LiveTrader(client=client, **_trader_kwargs)

# ── Trader callbacks ──────────────────────────────────────────────────────────

async def _on_open(pos, sig):
    await telegram.send(fmt_open(pos, sig))

async def _on_close(pos):
    await telegram.send(fmt_close(pos))

trader.on_open(_on_open).on_close(_on_close)

# ── Telegram providers ────────────────────────────────────────────────────────

telegram.set_status_provider(lambda: fmt_status(trader, telegram.paused))
telegram.set_positions_provider(lambda: fmt_positions(trader))

# ── Main signal loop ──────────────────────────────────────────────────────────

_last_candle_ts: dict = {s: 0 for s in cfg.symbols}


async def signal_loop():
    """
    Poll candles every POLL_INTERVAL seconds.
    On each new completed 4H bar, evaluate strategy signals and check exits.
    """
    logger.info("Signal loop started")
    while True:
        try:
            for sym in cfg.symbols:
                candles = await feed.fetch_candles(sym, limit=cfg.candles_limit)
                if len(candles) < 60:
                    logger.warning(f"{sym}: insufficient candles ({len(candles)})")
                    continue

                # Check exits for all open positions in this symbol
                closed = trader.check_exits(sym, candles)
                for pos in closed:
                    logger.info(f"Position closed: {sym} {pos.exit_reason} pnl={pos.pnl:+.4f}")

                # Only evaluate signal on new bar
                if not feed.has_new_bar(sym, candles):
                    continue

                # Skip if paused
                if telegram.paused:
                    logger.debug(f"Signal skipped ({sym}): bot is paused")
                    continue

                # Get current open side for this symbol
                current_side = None
                for p in trader.positions.values():
                    if p.symbol == sym:
                        current_side = p.direction
                        break

                sig = strategy.generate_signal(sym, candles, current_side)
                if sig:
                    logger.info(f"Signal: {sig.direction} {sym} entry={sig.entry:.4f}")
                    if cfg.paper_trading:
                        trader.open_position(sig)
                    else:
                        await trader.open_position(sig)

                await asyncio.sleep(0.2)   # small delay between symbols

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Signal loop error: {e}", exc_info=True)

        await asyncio.sleep(cfg.poll_interval_seconds)


# ── State file loop (for dashboard) ──────────────────────────────────────────

async def state_loop():
    """Write live state to JSON every 30 s so the dashboard can read it."""
    while True:
        try:
            stats = trader.stats
            indicators = {
                sym: strategy.compute_indicators(feed.cached(sym))
                for sym in cfg.symbols
            }
            state = {
                "timestamp":    time.time(),
                "paper":        cfg.paper_trading,
                "paused":       telegram.paused,
                "capital":      stats["capital"],
                "initial_capital": stats["initial_capital"],
                "total_pnl":    stats["total_pnl"],
                "total_return_pct": stats["total_return_pct"],
                "total_trades": stats["total_trades"],
                "wins":         stats["wins"],
                "losses":       stats["losses"],
                "win_rate":     stats["win_rate"],
                "open_count":   trader.open_count,
                "open_positions": [p.to_dict() for p in trader.positions.values()],
                "indicators":   indicators,
            }
            Path(cfg.state_file).write_text(json.dumps(state, indent=2))
        except Exception as e:
            logger.debug(f"State write error: {e}")
        await asyncio.sleep(30)


# ── Hourly summary loop ────────────────────────────────────────────────────────

async def summary_loop():
    """Send daily P&L summary to Telegram every 4 hours."""
    while True:
        await asyncio.sleep(4 * 3600)
        try:
            await telegram.send(fmt_daily_summary(trader))
        except Exception as e:
            logger.error(f"Summary loop error: {e}")


# ── Graceful shutdown ─────────────────────────────────────────────────────────

async def shutdown(loop: asyncio.AbstractEventLoop):
    logger.info("Shutting down…")
    trader._save()
    await telegram.stop()
    tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()
    logger.info("Bot stopped.")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(loop)))

    await telegram.send(fmt_startup(
        paper      = cfg.paper_trading,
        symbols    = cfg.symbols,
        capital    = cfg.capital_usdt,
        leverage   = cfg.leverage,
        risk_pct   = cfg.risk_per_trade_pct,
    ))

    logger.info(
        f"Bot started | paper={cfg.paper_trading} | "
        f"capital=${cfg.capital_usdt} | leverage={cfg.leverage}x | "
        f"symbols={cfg.symbols}"
    )

    await asyncio.gather(
        signal_loop(),
        state_loop(),
        summary_loop(),
        telegram.start_polling(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
