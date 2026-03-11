"""
CoinDCX Bot — Telegram message formatters.
Uses Telegram MarkdownV1 style (backticks, asterisks, no escaping).
"""

import time
from typing import Dict, Optional

from coindcx.trader import Position
from coindcx.strategy import Signal


def _ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _pnl(v: float) -> str:
    return f"+{v:.4f}" if v >= 0 else f"{v:.4f}"


def fmt_startup(paper: bool, symbols, capital: float, leverage: int, risk_pct: float) -> str:
    mode = "PAPER TRADING" if paper else "LIVE TRADING"
    sym_list = "\n".join(f"  • `{s}`" for s in symbols)
    return (
        f"🤖 *CoinDCX Futures Bot Started*\n"
        f"{'─' * 30}\n"
        f"Mode: `{mode}`\n"
        f"💰 Capital: `${capital:.2f} USDT`\n"
        f"⚡ Leverage: `{leverage}x`\n"
        f"⚠️  Risk/trade: `{risk_pct}%`\n"
        f"📊 Strategy: `50 EMA Trend | 4H | ATR filter`\n"
        f"{'─' * 30}\n"
        f"*Symbols:*\n{sym_list}\n"
        f"{'─' * 30}\n"
        f"Watching for signals…"
    )


def fmt_open(pos: Position, sig: Signal) -> str:
    arrow = "🟢 LONG" if pos.direction == "LONG" else "🔴 SHORT"
    return (
        f"{'─' * 30}\n"
        f"✅ *TRADE OPENED*\n"
        f"{'─' * 30}\n"
        f"{arrow}  `{pos.symbol}`\n"
        f"💵 Entry:  `{pos.entry_price:.4f}`\n"
        f"🛑 SL:     `{pos.stop_loss:.4f}`\n"
        f"🎯 TP:     `{pos.take_profit:.4f}`\n"
        f"📦 Qty:    `{pos.quantity}`\n"
        f"💼 Margin: `${pos.margin:.2f}`  ({pos.leverage}x)\n"
        f"📈 EMA50:  `{sig.ema:.4f}`\n"
        f"🌡 ATR:    `{sig.atr:.4f}`\n"
        f"🕐 `{_ts(pos.opened_at)}`\n"
        f"🆔 `{pos.id}`"
    )


def fmt_close(pos: Position) -> str:
    icon  = "🏆" if (pos.pnl or 0) > 0 else "💀"
    arrow = "📈" if pos.direction == "LONG" else "📉"
    reason_emoji = {
        "TP_HIT": "🎯 Take Profit",
        "SL_HIT": "🛑 Stop Loss",
        "MANUAL": "🖐 Manual Close",
        "SIGNAL_FLIP": "🔄 Signal Flip",
    }.get(pos.exit_reason or "", pos.exit_reason or "?")
    return (
        f"{'─' * 30}\n"
        f"{icon} *TRADE CLOSED*  {arrow}\n"
        f"{'─' * 30}\n"
        f"`{pos.symbol}`  {pos.direction}\n"
        f"📥 Entry: `{pos.entry_price:.4f}` → 📤 Exit: `{pos.exit_price:.4f}`\n"
        f"💰 P&L:   `{_pnl(pos.pnl or 0)} USDT`\n"
        f"🏷 Reason: {reason_emoji}\n"
        f"🕐 `{_ts(pos.closed_at or time.time())}`\n"
        f"🆔 `{pos.id}`"
    )


def fmt_status(trader, paused: bool) -> str:
    stats = trader.stats
    state = "⏸ PAUSED" if paused else "▶️ ACTIVE"
    return (
        f"📊 *Bot Status* — {state}\n"
        f"{'─' * 30}\n"
        f"💼 Capital:     `${stats['capital']:.2f}`\n"
        f"📈 Total P&L:   `{_pnl(stats['total_pnl'])} USDT`\n"
        f"📊 Return:      `{stats['total_return_pct']:+.2f}%`\n"
        f"📂 Open:        `{trader.open_count}` position(s)\n"
        f"🏆 Trades:      `{stats['total_trades']}` ({stats['wins']}W / {stats['losses']}L)\n"
        f"🎯 Win rate:    `{stats['win_rate']:.0%}`"
    )


def fmt_positions(trader) -> str:
    if not trader.positions:
        return "📂 No open positions."
    lines = [f"📂 *Open Positions* ({trader.open_count})\n{'─' * 30}"]
    for pos in trader.positions.values():
        arrow = "🟢" if pos.direction == "LONG" else "🔴"
        lines.append(
            f"{arrow} `{pos.symbol}` {pos.direction}\n"
            f"   Entry `{pos.entry_price:.4f}` | SL `{pos.stop_loss:.4f}` | TP `{pos.take_profit:.4f}`\n"
            f"   Qty `{pos.quantity}` | Margin `${pos.margin:.2f}` | {pos.leverage}x\n"
            f"   Since `{_ts(pos.opened_at)}` | ID `{pos.id}`"
        )
    return "\n".join(lines)


def fmt_daily_summary(trader) -> str:
    stats = trader.stats
    return (
        f"📅 *Daily Summary*\n"
        f"{'─' * 30}\n"
        f"💼 Capital:   `${stats['capital']:.2f}`\n"
        f"💰 Session P&L: `{_pnl(trader.session_pnl)} USDT`\n"
        f"📊 Total trades: `{stats['total_trades']}` | WR `{stats['win_rate']:.0%}`\n"
        f"{'─' * 30}\n"
        f"Open: `{trader.open_count}` | Closed today: checked"
    )
