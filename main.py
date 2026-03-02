"""
Polymarket BTC Direction Trading Bot
======================================
Entry point — wires all components together and runs the async event loop.

Components:
  BinanceFeed      → streams trades + order book from Binance WebSocket
  OrderFlowAnalyzer→ maintains rolling CVD, volume, OB imbalance per timeframe
  SignalEngine     → fires TradeSignal when 2+ order-flow conditions align
  PaperTrader      → simulates Polymarket YES/NO positions (paper mode)
  PositionSizer    → computes trade size as fixed % of bankroll
  TradeTracker     → accumulates session stats for the dashboard
  TelegramNotifier → sends signal / entry / result / dashboard messages

Loops running concurrently:
  feed.start()       → Binance WebSocket (trade + orderbook)
  signal_loop()      → evaluates signals every 30 s per timeframe
  resolution_loop()  → checks for expired paper positions every 10 s
  dashboard_loop()   → sends hourly P&L dashboard to Telegram
"""

import asyncio
import logging
import sys

from config import config
from bot.formatters import fmt_dashboard, fmt_entry, fmt_result, fmt_signal, fmt_startup
from bot.telegram_bot import TelegramNotifier
from data.binance_feed import BinanceFeed
from data.orderflow import OrderFlowAnalyzer
from polymarket.client import PaperTrader
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

trader = PaperTrader(
    initial_bankroll=config.bankroll,
    log_file=config.log_file,
)

sizer = PositionSizer(
    risk_pct=config.risk_per_trade_pct,
    max_open=config.max_open_trades,
)

tracker = TradeTracker()
telegram = TelegramNotifier(
    token=config.telegram_token,
    chat_id=config.telegram_chat_id,
)

# ── Signal handler ─────────────────────────────────────────────────────────────

async def on_signal(signal: TradeSignal):
    # 1. Notify Telegram about the raw signal
    await telegram.send(fmt_signal(signal))

    # 2. Compute position size
    size = sizer.size(
        bankroll=trader.bankroll,
        open_positions=trader.open_count,
        confidence=signal.confidence,
    )

    if size.amount_usd <= 0:
        logger.info("Trade skipped: max open positions reached or bankroll too low")
        return

    # 3. Paper trade entry price
    # On Polymarket, BTC 5m/15m binary markets typically open close to 0.50.
    # We simulate buying at 0.50 (fair odds). Our edge comes from being right
    # more than 50% of the time, not from price arbitrage.
    entry_price = 0.50

    # 4. Build question label
    question = (
        f"Will BTC be HIGHER in {signal.timeframe}?"
        if signal.direction == "UP"
        else f"Will BTC be LOWER in {signal.timeframe}?"
    )

    # 5. Open paper position
    pos = trader.open_position(
        direction=signal.direction,
        cost_usd=size.amount_usd,
        entry_price=entry_price,
        entry_btc_price=signal.price,
        question=question,
        timeframe=signal.timeframe,
    )

    if pos:
        await telegram.send(fmt_entry(pos))


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
    """Check for expired paper positions every 10 seconds."""
    while True:
        try:
            current_price = analyzer.latest_price()
            if current_price > 0:
                resolved = await trader.check_resolutions(current_price)
                for pos in resolved:
                    tracker.record(pos)
                    await telegram.send(fmt_result(pos))
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


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    # Register callbacks
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
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
