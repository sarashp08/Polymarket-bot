"""
Telegram message formatters.

All messages use Markdown (Telegram MarkdownV1 style — no escaping needed
for the characters we use here).
"""

import time

from polymarket.client import PaperPosition
from signals.signal_engine import TradeSignal
from tracking.trade_tracker import Stats, TradeTracker


def _ts(ts: float) -> str:
    return time.strftime("%H:%M:%S UTC", time.gmtime(ts))


def _pnl_str(pnl: float) -> str:
    return f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"


# ── Individual message formatters ───────────────────────────────────────────────

def fmt_signal(sig: TradeSignal) -> str:
    arrow = "📈 LONG (UP)" if sig.direction == "UP" else "📉 SHORT (DOWN)"
    bullets = "\n".join(f"  • {s}" for s in sig.signals_fired)
    return (
        f"{'─' * 28}\n"
        f"⚡ *TRADE SIGNAL*\n"
        f"{'─' * 28}\n"
        f"{arrow} BTC `{sig.timeframe}`\n"
        f"💰 BTC Price: `${sig.price:,.2f}`\n"
        f"🎯 Confidence: *{sig.confidence_label}* ({sig.confidence:.0%})\n"
        f"📊 Signals:\n{bullets}\n"
        f"🕐 `{_ts(sig.timestamp)}`"
    )


def fmt_entry(pos: PaperPosition, sig: "TradeSignal | None" = None) -> str:
    side = "🟢 BUY YES (UP)" if pos.direction == "UP" else "🔴 BUY NO (DOWN)"
    resolve_clock = time.strftime("%H:%M UTC", time.gmtime(pos.resolve_time))
    lines = [
        f"{'─' * 28}",
        f"✅ *TRADE ENTERED*",
        f"{'─' * 28}",
        f"{side}  —  BTC `{pos.timeframe}` market",
        f"₿  BTC @ `${pos.entry_btc_price:,.2f}`",
        f"💵 Stake: `${pos.cost_usd:.2f}`  →  {pos.shares:.2f} shares @ $0.50",
        f"⏰ Resolves at: `{resolve_clock}`",
    ]
    if sig is not None:
        bullets = "  •  ".join(sig.signals_fired)
        lines.append(f"🎯 Confidence: *{sig.confidence_label}* ({sig.confidence:.0%})")
        lines.append(f"📊 `{bullets}`")
    lines.append(f"🆔 `{pos.id}`")
    return "\n".join(lines)


def fmt_result(pos: PaperPosition, tracker: "TradeTracker | None" = None) -> str:
    icon = "🏆 *WON*" if pos.status == "WON" else "💀 *LOST*"
    direction_emoji = "📈" if pos.direction == "UP" else "📉"
    price_move = pos.exit_btc_price - pos.entry_btc_price
    move_str = f"+${price_move:,.0f}" if price_move >= 0 else f"-${abs(price_move):,.0f}"
    pnl_pct = (pos.pnl / pos.cost_usd) * 100

    lines = [
        f"{'─' * 28}",
        f"{icon}  {direction_emoji} {pos.direction}  `{pos.timeframe}`",
        f"{'─' * 28}",
        f"₿  BTC: `${pos.entry_btc_price:,.0f}` → `${pos.exit_btc_price:,.0f}`  ({move_str})",
        f"💰 P&L: `{_pnl_str(pos.pnl)}`  (`{pnl_pct:+.0f}%` on stake)",
    ]
    if tracker is not None:
        s = tracker.overall
        lines.append(
            f"📊 Session: `{s.wins}W / {s.losses}L`  |  "
            f"Total P&L `{_pnl_str(s.pnl)}`  |  WR `{s.win_rate:.0%}`"
        )
    lines.append(f"🆔 `{pos.id}`")
    return "\n".join(lines)


def fmt_dashboard(
    tracker: TradeTracker,
    bankroll: float,
    initial_bankroll: float,
) -> str:
    s = tracker.overall
    s5 = tracker.for_timeframe("5m")
    s15 = tracker.for_timeframe("15m")

    total_pnl = bankroll - initial_bankroll
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"

    def tf_block(label: str, stat: Stats) -> str:
        if stat.total == 0:
            return f"*{label}:* No trades yet"
        return (
            f"*{label}:* {stat.wins}W / {stat.losses}L  "
            f"({stat.win_rate:.0%} WR)  |  P&L `{_pnl_str(stat.pnl)}`  |  ROI `{stat.roi:+.1%}`"
        )

    return (
        f"{'═' * 28}\n"
        f"📊 *HOURLY DASHBOARD*\n"
        f"{'═' * 28}\n"
        f"💼 Bankroll: `${bankroll:.2f}`  {pnl_emoji} `{_pnl_str(total_pnl)}`\n"
        f"{'─' * 28}\n"
        f"{tf_block('5m Trades', s5)}\n"
        f"{tf_block('15m Trades', s15)}\n"
        f"{'─' * 28}\n"
        f"📋 *Overall:* {s.total} trades  |  WR `{s.win_rate:.0%}`  |  "
        f"Avg P&L `{_pnl_str(s.avg_pnl)}`\n"
        f"🏆 Best win: `{_pnl_str(s.best_win)}`  |  "
        f"💀 Worst loss: `{_pnl_str(s.worst_loss)}`\n"
        f"⏱ Runtime: `{s.runtime_hours:.1f}h`"
    )


def fmt_status(trader, tracker: TradeTracker, paused: bool) -> str:
    """Live status for the /status command."""
    s = tracker.overall
    session_pnl = trader.bankroll - trader.session_start_bankroll
    pnl_emoji = "📈" if session_pnl >= 0 else "📉"
    state = "⏸ PAUSED" if paused else "▶️ ACTIVE"
    win_line = (
        f"`{s.wins}W / {s.losses}L`  ({s.win_rate:.0%} WR)"
        if s.total > 0
        else "No trades yet"
    )
    return (
        f"📊 *Bot Status* — {state}\n"
        f"{'─' * 28}\n"
        f"💼 Bankroll: `${trader.bankroll:.2f}`\n"
        f"📈 Session P&L: `{_pnl_str(session_pnl)}`  {pnl_emoji}\n"
        f"📂 Open positions: `{trader.open_count}`\n"
        f"🏆 Session trades: {win_line}\n"
        f"⏱ Runtime: `{s.runtime_hours:.1f}h`"
    )


def fmt_positions(trader) -> str:
    """Open position list for the /positions command."""
    if not trader.positions:
        return "📂 No open positions right now."
    now = time.time()
    lines = []
    for pos in trader.positions.values():
        remaining = max(0, int(pos.resolve_time - now))
        emoji = "📈" if pos.direction == "UP" else "📉"
        lines.append(
            f"{emoji} `{pos.id}`  {pos.direction} {pos.timeframe}\n"
            f"   💵 `${pos.cost_usd:.2f}`  ₿ entry `${pos.entry_btc_price:,.0f}`  ⏳ `{remaining}s`"
        )
    header = f"📂 *Open Positions* ({len(trader.positions)})\n{'─' * 28}\n"
    return header + "\n".join(lines)


def fmt_startup(
    bankroll: float,
    risk_pct: float,
    timeframes: list,
    paper: bool,
) -> str:
    mode = "📝 PAPER TRADING" if paper else "🔴 LIVE TRADING"
    tfs = " / ".join(timeframes)
    return (
        f"🤖 *Polymarket BTC Bot Started*\n"
        f"{'─' * 28}\n"
        f"Mode: {mode}\n"
        f"💰 Bankroll: `${bankroll:.2f}`\n"
        f"⚠️  Risk/trade: `{risk_pct}%`\n"
        f"📊 Timeframes: `{tfs}` BTC UP/DOWN\n"
        f"📡 Feed: Binance WebSocket\n"
        f"{'─' * 28}\n"
        f"Listening for signals…"
    )
