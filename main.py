"""
Polymarket BTC Direction Trading Bot
======================================
Entry point — wires all components together and runs the async event loop.

Components:
  BinanceFeed      → streams trades + order book from Binance WebSocket
  OrderFlowAnalyzer→ maintains rolling CVD, volume, OB imbalance per timeframe
  SignalEngine     → fires TradeSignal when 2+ order-flow conditions align
  PaperTrader      → simulates Polymarket YES/NO positions (paper mode)
  LiveTrader       → places real orders on Polymarket CLOB (live mode)
  PositionSizer    → computes trade size as fixed % of bankroll
  TradeTracker     → accumulates session stats for the dashboard
  TelegramBot      → sends alerts + receives /status /positions /pause /resume

Loops running concurrently:
  feed.start()              → Binance WebSocket (trade + orderbook)
  signal_loop()             → evaluates signals every 30 s per timeframe
  resolution_loop()         → checks for expired positions every 10 s
  dashboard_loop()          → sends hourly P&L dashboard to Telegram
  telegram.start_polling()  → listens for phone commands
"""

import asyncio
import logging
import signal
import sys

from config import config
from bot.formatters import (
    fmt_dashboard, fmt_entry, fmt_result, fmt_startup,
    fmt_status, fmt_positions,
)
from bot.telegram_bot import TelegramBot
from data.binance_feed import BinanceFeed
from data.orderflow import OrderFlowAnalyzer
from polymarket.client import PaperTrader, LiveTrader, find_active_market
from risk.position_sizer import PositionSizer
from signals.signal_engine import SignalEngine, TradeSignal
from tracking.trade_tracker import TradeTracker

# ── Logging setup ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# ── Component wiring ───────────────────────────────────────────────────────────

feed = BinanceFeed(symbol=config.binance_symbol_lower)

analyzer = OrderFlowAnalyzer(whale_threshold=config.whale_usd_threshold)

engine = SignalEngine(
    analyzer=analyzer,
    min_confluence=config.min_confluence,
    cvd_threshold=config.cvd_threshold,
    ob_imbalance_threshold=config.ob_imbalance_threshold,
    volume_spike_multiplier=config.volume_spike_multiplier,
    cooldown_seconds=config.signal_cooldown_seconds,
)

# Choose trader based on mode
if config.paper_trading:
    trader = PaperTrader(
        initial_bankroll=config.bankroll,
        log_file=config.log_file,
    )
else:
    trader = LiveTrader(
        initial_bankroll=config.bankroll,
        log_file=config.log_file,
        host=config.polymarket_host,
        chain_id=config.chain_id,
        private_key=config.polymarket_private_key,
        api_key=config.polymarket_api_key,
        api_secret=config.polymarket_api_secret,
        api_passphrase=config.polymarket_api_passphrase,
    )

sizer = PositionSizer(
    risk_pct=config.risk_per_trade_pct,
    max_open=config.max_open_trades,
)

tracker = TradeTracker()
telegram = TelegramBot(
    token=config.telegram_token,
    chat_id=config.telegram_chat_id,
)

# ── Signal handler ─────────────────────────────────────────────────────────────

async def on_signal(sig: TradeSignal):
    # 0. Pause guard — user sent /pause from phone
    if telegram.paused:
        logger.info("Trade skipped: bot is paused via Telegram /pause")
        return

    # 1. Session loss guard — stop trading if down too much from session start
    session_loss_pct = (
        (trader.bankroll - trader.session_start_bankroll)
        / trader.session_start_bankroll
        * 100
    )
    if session_loss_pct <= -config.max_daily_loss_pct:
        logger.warning(
            f"Session loss limit hit ({session_loss_pct:.1f}%) — "
            f"trading paused for this session"
        )
        return

    # 2. Compute position size
    size = sizer.size(
        bankroll=trader.bankroll,
        open_positions=trader.open_count,
        confidence=sig.confidence,
    )

    if size.amount_usd <= 0:
        logger.info("Trade skipped: max open positions reached or bankroll too low")
        return

    # 3. Entry price (binary markets trade near 0.50)
    entry_price = 0.50

    # 4. Build question label
    question = (
        f"Will BTC be HIGHER in {sig.timeframe}?"
        if sig.direction == "UP"
        else f"Will BTC be LOWER in {sig.timeframe}?"
    )

    # 5. For live trading, find the active Polymarket market
    market = None
    if not config.paper_trading:
        market = await find_active_market(sig.timeframe)
        if market is None:
            await telegram.send(
                f"⚠️ No active BTC {sig.timeframe} market found — trade skipped"
            )
            return

    # 6. Open position (paper or live)
    pos = trader.open_position(
        direction=sig.direction,
        cost_usd=size.amount_usd,
        entry_price=entry_price,
        entry_btc_price=sig.price,
        question=question,
        timeframe=sig.timeframe,
        **({"market": market} if market else {}),
    )

    if pos:
        await telegram.send(fmt_entry(pos, sig))


# ── Background loops ───────────────────────────────────────────────────────────

async def signal_loop():
    """Evaluate order-flow signals every 30 seconds."""
    while True:
        try:
            for tf in config.timeframes:
                await engine.evaluate(tf)
        except Exception as exc:
            logger.error(f"Signal loop error: {exc}", exc_info=True)
        await asyncio.sleep(30)


async def resolution_loop():
    """Check for expired positions every 10 seconds."""
    while True:
        try:
            current_price = analyzer.latest_price()
            if current_price > 0:
                resolved = await trader.check_resolutions(current_price)
                for pos in resolved:
                    tracker.record(pos)
                    await telegram.send(fmt_result(pos, tracker))
        except Exception as exc:
            logger.error(f"Resolution loop error: {exc}", exc_info=True)
        await asyncio.sleep(10)


async def dashboard_loop():
    """Send a performance dashboard to Telegram every hour."""
    interval = config.dashboard_interval_minutes * 60
    while True:
        await asyncio.sleep(interval)
        try:
            msg = fmt_dashboard(
                tracker=tracker,
                bankroll=trader.bankroll,
                initial_bankroll=trader.initial_bankroll,
            )
            await telegram.send(msg)
        except Exception as exc:
            logger.error(f"Dashboard loop error: {exc}", exc_info=True)


# ── Graceful shutdown ──────────────────────────────────────────────────────────

async def shutdown(loop: asyncio.AbstractEventLoop):
    """Cancel all running tasks and save state cleanly."""
    logger.info("Shutting down — saving state…")
    feed.stop()
    trader._save()
    await telegram.stop()

    tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()
    logger.info("Bot stopped cleanly.")


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    loop = asyncio.get_running_loop()

    # Register SIGTERM / SIGINT for graceful shutdown
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(shutdown(loop)),
        )

    # Wire Telegram /status and /positions providers
    telegram.set_status_provider(
        lambda: fmt_status(trader=trader, tracker=tracker, paused=telegram.paused)
    )
    telegram.set_positions_provider(
        lambda: fmt_positions(trader=trader)
    )

    # Register feed and signal callbacks
    engine.on_signal(on_signal)
    feed.on_trade(analyzer.on_trade).on_orderbook(analyzer.on_orderbook)

    # Send startup message
    await telegram.send(
        fmt_startup(
            bankroll=trader.bankroll,
            risk_pct=config.risk_per_trade_pct,
            timeframes=config.timeframes,
            paper=config.paper_trading,
        )
    )

    logger.info(
        f"Bot started | paper={config.paper_trading} | "
        f"bankroll=${trader.bankroll:.2f} | risk={config.risk_per_trade_pct}%"
    )

    # Run all loops concurrently
    await asyncio.gather(
        feed.start(),
        signal_loop(),
        resolution_loop(),
        dashboard_loop(),
        telegram.start_polling(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
