"""
CoinDCX Futures Backtest Engine
================================
Runs the EMA Trend Strategy over historical OHLCV data fetched from
CoinDCX (or Binance fallback).

Usage:
  python -m coindcx.backtest

Results are written to dcx_backtest.json for the dashboard to consume.

Backtest assumptions:
  - Fill at the open of the candle AFTER the signal bar (next-bar entry)
  - SL / TP checked on candle high/low (conservative: SL fills at SL price,
    TP fills at TP price; if both hit on same bar, SL takes priority)
  - No funding fees in paper backtest (can add later)
  - Fees: 0.04% round-trip (CoinDCX futures taker)
  - No slippage model (conservative for liquid pairs)
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from coindcx.feed import CoinDCXFeed
from coindcx.strategy import EMATrendStrategy, Signal, StrategyParams

logger = logging.getLogger(__name__)

FEE_RATE = 0.0004   # 0.04% taker per side (CoinDCX futures base)


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_bar_ts: int
    entry_price: float
    stop_loss: float
    take_profit: float
    exit_bar_ts: int
    exit_price: float
    exit_reason: str       # "SL_HIT" | "TP_HIT"
    quantity: float
    gross_pnl: float
    fee: float
    net_pnl: float
    atr: float
    ema: float


@dataclass
class BacktestResult:
    symbol: str
    trades: List[BacktestTrade]
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_net_pnl: float
    max_drawdown: float
    total_return_pct: float
    equity_curve: List[float]


def _run_symbol(
    symbol: str,
    candles: List[Dict],
    params: StrategyParams,
    capital: float,
    risk_pct: float,
    leverage: int,
) -> BacktestResult:
    """Run backtest for a single symbol."""
    strategy = EMATrendStrategy(params)
    trades: List[BacktestTrade] = []
    equity = capital
    equity_curve = [capital]
    peak = capital

    n = len(candles)
    warmup = max(params.ema_period, params.atr_period) + 5

    current_side: Optional[str] = None  # "LONG" | "SHORT" | None
    open_trade: Optional[Dict] = None   # holds active position data

    for i in range(warmup, n - 1):
        bar_slice  = candles[: i + 1]
        next_bar   = candles[i + 1]
        next_open  = next_bar["open"]
        next_high  = next_bar["high"]
        next_low   = next_bar["low"]

        # ── Check exit first (before new signal) ─────────────────────────────
        if open_trade is not None:
            direction   = open_trade["direction"]
            sl          = open_trade["stop_loss"]
            tp          = open_trade["take_profit"]
            qty         = open_trade["quantity"]
            entry_price = open_trade["entry_price"]
            exit_reason = None
            exit_price  = None

            if direction == "LONG":
                if next_low <= sl:
                    exit_reason, exit_price = "SL_HIT", sl
                elif next_high >= tp:
                    exit_reason, exit_price = "TP_HIT", tp
            else:
                if next_high >= sl:
                    exit_reason, exit_price = "SL_HIT", sl
                elif next_low <= tp:
                    exit_reason, exit_price = "TP_HIT", tp

            if exit_reason:
                if direction == "LONG":
                    gross_pnl = (exit_price - entry_price) * qty
                else:
                    gross_pnl = (entry_price - exit_price) * qty
                fee = (entry_price + exit_price) * qty * FEE_RATE
                net_pnl = gross_pnl - fee
                equity += net_pnl
                equity_curve.append(equity)
                peak = max(peak, equity)

                trades.append(BacktestTrade(
                    symbol       = symbol,
                    direction    = direction,
                    entry_bar_ts = open_trade["entry_bar_ts"],
                    entry_price  = entry_price,
                    stop_loss    = sl,
                    take_profit  = tp,
                    exit_bar_ts  = next_bar["ts"],
                    exit_price   = exit_price,
                    exit_reason  = exit_reason,
                    quantity     = qty,
                    gross_pnl    = round(gross_pnl, 6),
                    fee          = round(fee, 6),
                    net_pnl      = round(net_pnl, 6),
                    atr          = open_trade["atr"],
                    ema          = open_trade["ema"],
                ))
                open_trade   = None
                current_side = None

        # ── Check for new signal (only if flat) ───────────────────────────────
        if open_trade is None:
            sig: Optional[Signal] = strategy.generate_signal(symbol, bar_slice, current_side)
            if sig:
                # Entry on next bar open
                entry_price = next_open
                sl_dist     = abs(sig.entry - sig.stop_loss)
                tp_dist     = abs(sig.take_profit - sig.entry)
                # Re-anchor SL/TP to actual fill price
                if sig.direction == "LONG":
                    sl = entry_price - sl_dist
                    tp = entry_price + tp_dist
                else:
                    sl = entry_price + sl_dist
                    tp = entry_price - tp_dist

                risk_amount = equity * risk_pct / 100
                qty = risk_amount / sl_dist if sl_dist > 0 else 0.0
                # Cap to leverage
                max_notional = equity * leverage
                qty = min(qty, max_notional / entry_price)

                open_trade = {
                    "direction":    sig.direction,
                    "entry_price":  entry_price,
                    "stop_loss":    sl,
                    "take_profit":  tp,
                    "quantity":     qty,
                    "entry_bar_ts": next_bar["ts"],
                    "atr":          sig.atr,
                    "ema":          sig.ema,
                }
                current_side = sig.direction

    # Compute summary
    n_trades = len(trades)
    wins     = [t for t in trades if t.net_pnl > 0]
    losses   = [t for t in trades if t.net_pnl <= 0]
    total_pnl = sum(t.net_pnl for t in trades)

    # Max drawdown
    peak_eq, max_dd = capital, 0.0
    for eq in equity_curve:
        peak_eq = max(peak_eq, eq)
        dd = (peak_eq - eq) / peak_eq * 100
        max_dd = max(max_dd, dd)

    return BacktestResult(
        symbol          = symbol,
        trades          = trades,
        total_trades    = n_trades,
        wins            = len(wins),
        losses          = len(losses),
        win_rate        = len(wins) / n_trades if n_trades else 0.0,
        total_net_pnl   = round(total_pnl, 4),
        max_drawdown    = round(max_dd, 2),
        total_return_pct= round((equity - capital) / capital * 100, 2),
        equity_curve    = [round(e, 4) for e in equity_curve],
    )


async def run_backtest(
    symbols: List[str],
    interval: str = "4h",
    candle_limit: int = 500,
    params: Optional[StrategyParams] = None,
    capital: float = 1000.0,
    risk_pct: float = 1.0,
    leverage: int = 3,
    output_file: str = "dcx_backtest.json",
) -> Dict[str, BacktestResult]:
    """
    Fetch real historical data and run backtest on all symbols.
    Saves results to output_file.
    Returns dict of symbol → BacktestResult.
    """
    params = params or StrategyParams()
    feed   = CoinDCXFeed(symbols, interval=interval)

    logger.info(f"Fetching {candle_limit} x {interval} candles for {len(symbols)} symbols…")
    all_candles = await feed.fetch_all(limit=candle_limit)

    results: Dict[str, BacktestResult] = {}
    for sym, candles in all_candles.items():
        if len(candles) < 60:
            logger.warning(f"{sym}: only {len(candles)} candles — skipping backtest")
            continue
        logger.info(f"Running backtest for {sym} on {len(candles)} candles…")
        result = _run_symbol(sym, candles, params, capital, risk_pct, leverage)
        results[sym] = result
        logger.info(
            f"  {sym}: {result.total_trades} trades | "
            f"WR={result.win_rate:.0%} | "
            f"PnL={result.total_net_pnl:+.2f} | "
            f"MaxDD={result.max_drawdown:.1f}%"
        )

    # Persist
    out = {
        sym: {
            "summary": {
                "symbol":            r.symbol,
                "total_trades":      r.total_trades,
                "wins":              r.wins,
                "losses":            r.losses,
                "win_rate":          r.win_rate,
                "total_net_pnl":     r.total_net_pnl,
                "max_drawdown":      r.max_drawdown,
                "total_return_pct":  r.total_return_pct,
            },
            "equity_curve": r.equity_curve,
            "trades":       [asdict(t) for t in r.trades],
        }
        for sym, r in results.items()
    }
    Path(output_file).write_text(json.dumps(out, indent=2))
    logger.info(f"Backtest saved to {output_file}")
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    symbols = [
        "B-ETH_USDT", "B-SOL_USDT", "B-HYPE_USDT",
        "B-AAVE_USDT", "B-ZEC_USDT", "B-UNI_USDT",
    ]
    asyncio.run(run_backtest(symbols=symbols, candle_limit=500))
