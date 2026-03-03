#!/usr/bin/env python3
"""
Backtest: 5m vs 15m strategy comparison
- 90 days of BTCUSDT klines from Binance (taker buy volume available in klines)
- Optimises CVD threshold + min_confluence per timeframe
- Simulates Polymarket binary trades: entry at 0.50 + 1% slippage, $14 bankroll
- Outputs backtest_results.xlsx
"""

import time
import requests
import pandas as pd
import numpy as np
from itertools import product
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── Constants ──────────────────────────────────────────────────────────────────
BANKROLL        = 14.0
RISK_PCT        = 10.0        # % of bankroll per trade (from .env)
SLIPPAGE        = 0.01        # 1% slippage
ENTRY_ODDS      = 0.50        # binary market entry price per share
DAYS            = 90
SYMBOL          = "BTCUSDT"

# Cooldown in bars per timeframe (900s cooldown / bar size)
COOLDOWN_BARS   = {"5m": 3, "15m": 1}

# Bars to build volume baseline (rolling mean)
VOL_HISTORY     = 12          # 12 x 5m = 60 min baseline; 12 x 15m = 3 hr baseline

# Buy/sell ratio threshold (fixed — mirrors live config)
BS_THRESHOLD    = 0.60

# Optimisation grids
CVD_GRID        = [0.3, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0]
VOL_SPIKE_GRID  = [1.5, 2.0, 3.0]
CONFLUENCE_GRID = [2, 3]


# ── Data Fetching ──────────────────────────────────────────────────────────────

def fetch_klines(interval: str, days: int) -> pd.DataFrame:
    """
    Fetch OHLCV klines from Kraken public API.
    interval: '5m' or '15m'  →  Kraken uses minutes as integer (5 or 15).
    Kraken returns up to 720 rows per call; we paginate via 'since'.
    Buy volume estimated via Kaufman's (close-low)/(high-low) * volume.
    """
    kraken_interval = int(interval.replace("m", ""))   # "5m" -> 5
    url      = "https://api.kraken.com/0/public/OHLC"
    end_ts   = int(time.time())
    since_ts = end_ts - days * 24 * 3600
    rows     = []

    print(f"  Fetching {interval} klines ({days} days) from Kraken...", end="", flush=True)
    cur_since = since_ts
    while True:
        resp = requests.get(url, params={
            "pair":     "XBTUSD",
            "interval": kraken_interval,
            "since":    cur_since,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken error: {data['error']}")

        result = data.get("result", {})
        # Kraken key varies: "XXBTZUSD" or "XBTUSD"
        pair_key = [k for k in result if k != "last"][0]
        batch    = result[pair_key]
        last_ts  = result.get("last", 0)

        if not batch:
            break
        rows.extend(batch)

        if len(batch) < 720 or last_ts <= cur_since:
            break
        cur_since = last_ts
        time.sleep(0.5)  # Kraken rate limit

    if not rows:
        raise RuntimeError(f"No kline data returned for {interval}")

    # Kraken OHLC columns: time, open, high, low, close, vwap, volume, count
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","vwap","volume","count"])
    df = df.astype({c: float for c in ["open","high","low","close","volume"]})
    df["ts"] = pd.to_datetime(df["open_time"].astype("int64"), unit="s")
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    # Keep only data within range
    df = df[df.index >= pd.Timestamp.fromtimestamp(since_ts)]

    # Estimate taker buy volume using Kaufman's OHLCV formula:
    hl = df["high"] - df["low"]
    buy_frac = np.where(hl > 0, (df["close"] - df["low"]) / hl, 0.5)
    df["taker_buy_base"] = df["volume"] * buy_frac

    print(f" {len(df):,} bars")
    return df


# ── Feature Engineering ────────────────────────────────────────────────────────

def add_features(df: pd.DataFrame, vol_spike_mult: float) -> pd.DataFrame:
    df = df.copy()
    df["buy_vol"]       = df["taker_buy_base"]
    df["sell_vol"]      = df["volume"] - df["taker_buy_base"]
    df["cvd"]           = df["buy_vol"] - df["sell_vol"]          # BTC net delta
    df["bs_ratio"]      = (df["buy_vol"] / df["volume"].replace(0, np.nan)).fillna(0.5)
    df["vol_baseline"]  = df["volume"].shift(1).rolling(VOL_HISTORY).mean()
    df["vol_spike"]     = df["volume"] > (df["vol_baseline"] * vol_spike_mult)
    return df


# ── Single Backtest Run ────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame, timeframe: str,
                 cvd_thresh: float, min_conf: int) -> tuple:
    bankroll      = BANKROLL
    cooldown_bars = COOLDOWN_BARS[timeframe]
    bars_since    = cooldown_bars   # allow first signal immediately
    trades        = []

    for i in range(VOL_HISTORY + 1, len(df) - 1):
        row = df.iloc[i]
        bars_since += 1

        if bars_since < cooldown_bars:
            continue
        if pd.isna(row["vol_baseline"]):
            continue

        bull, bear = [], []

        # ── Condition 1: CVD ────────────────────────────────────────────────
        if row["cvd"] > cvd_thresh:
            bull.append("CVD")
        elif row["cvd"] < -cvd_thresh:
            bear.append("CVD")

        # ── Condition 2: Buy/Sell Ratio ─────────────────────────────────────
        if row["bs_ratio"] > BS_THRESHOLD:
            bull.append("BS_RATIO")
        elif row["bs_ratio"] < (1 - BS_THRESHOLD):
            bear.append("BS_RATIO")

        # ── Condition 3: Volume Spike (confirms dominant side) ──────────────
        if bool(row["vol_spike"]):
            if len(bull) >= len(bear):
                bull.append("VOL_SPIKE")
            else:
                bear.append("VOL_SPIKE")

        bn, brn = len(bull), len(bear)
        if bn >= min_conf and bn > brn:
            direction, n_fired = "UP", bn
        elif brn >= min_conf and brn > bn:
            direction, n_fired = "DOWN", brn
        else:
            continue

        confidence = n_fired / 3.0   # 3 conditions max
        scale      = 1.0 if confidence >= 0.75 else (0.75 if confidence >= 0.5 else 0.5)
        stake      = max(1.0, min(round(bankroll * (RISK_PCT / 100.0) * scale, 2), bankroll))

        entry_p    = ENTRY_ODDS * (1 + SLIPPAGE)   # 0.505
        shares     = stake / entry_p

        entry_c    = row["close"]
        exit_c     = df.iloc[i + 1]["close"]
        win        = (exit_c > entry_c) if direction == "UP" else (exit_c < entry_c)
        pnl        = (shares * 1.0 - stake) if win else -stake

        bankroll  = max(0.0, bankroll + pnl)
        bars_since = 0

        trades.append({
            "timestamp":    df.index[i],
            "direction":    direction,
            "confidence":   round(confidence, 2),
            "stake":        stake,
            "entry_price":  round(entry_p, 4),
            "shares":       round(shares, 4),
            "btc_entry":    round(entry_c, 2),
            "btc_exit":     round(exit_c, 2),
            "win":          win,
            "pnl":          round(pnl, 4),
            "bankroll":     round(bankroll, 4),
        })

        if bankroll <= 0:
            break

    if not trades:
        return None, pd.DataFrame()

    tdf = pd.DataFrame(trades)

    # ── Metrics ────────────────────────────────────────────────────────────────
    n          = len(tdf)
    win_rate   = tdf["win"].mean() * 100
    total_pnl  = tdf["pnl"].sum()

    cum        = tdf["pnl"].cumsum() + BANKROLL
    peak       = cum.cummax()
    dd_vals    = cum - peak
    worst_idx  = dd_vals.idxmin()
    max_dd_pct = (dd_vals[worst_idx] / peak[worst_idx]) * 100 if n > 0 else 0.0

    tdf["date"]   = tdf["timestamp"].dt.date
    daily         = tdf.groupby("date")["pnl"].sum()
    sharpe        = ((daily.mean() / daily.std()) * np.sqrt(252)
                     if len(daily) > 1 and daily.std() > 0 else 0.0)

    metrics = {
        "timeframe":        timeframe,
        "cvd_threshold":    cvd_thresh,
        "min_confluence":   min_conf,
        "n_trades":         n,
        "win_rate_%":       round(win_rate, 1),
        "total_pnl_$":      round(total_pnl, 4),
        "final_bankroll_$": round(bankroll, 4),
        "max_drawdown_%":   round(abs(max_dd_pct), 2),
        "sharpe_ratio":     round(sharpe, 3),
        "avg_pnl_$":        round(total_pnl / n, 4) if n > 0 else 0.0,
    }
    return metrics, tdf


# ── Excel Helpers ──────────────────────────────────────────────────────────────

DARK_BLUE   = "1F3864"
MID_BLUE    = "2E75B6"
LIGHT_BLUE  = "BDD7EE"
GREEN_BG    = "E2EFDA"
RED_BG      = "FFE0E0"
WHITE       = "FFFFFF"
HEADER_FONT = Font(name="Calibri", bold=True, color=WHITE, size=11)
BODY_FONT   = Font(name="Calibri", size=10)
CENTER      = Alignment(horizontal="center", vertical="center", wrap_text=False)
LEFT        = Alignment(horizontal="left",   vertical="center")
THIN        = Border(
    left=Side(style="thin", color="AAAAAA"), right=Side(style="thin", color="AAAAAA"),
    top=Side(style="thin", color="AAAAAA"),  bottom=Side(style="thin", color="AAAAAA"),
)


def hfill(col: str) -> PatternFill:
    return PatternFill("solid", fgColor=col)


def style_header_row(ws, row: int, n_cols: int, bg: str = MID_BLUE):
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill   = hfill(bg)
        cell.font   = HEADER_FONT
        cell.alignment = CENTER
        cell.border = THIN


def style_data_row(ws, row: int, n_cols: int, alt: bool):
    bg = "F2F7FD" if alt else WHITE
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill      = hfill(bg)
        cell.font      = BODY_FONT
        cell.alignment = CENTER
        cell.border    = THIN


def auto_col_width(ws):
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=0)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 4, 30)


# ── Write Summary Sheet ────────────────────────────────────────────────────────

def write_summary(ws, best_5m: dict, best_15m: dict):
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    # Title
    ws.merge_cells("A1:J1")
    t = ws["A1"]
    t.value     = "POLYMARKET BOT — 5m vs 15m Strategy Comparison  (90-day Backtest)"
    t.fill      = hfill(DARK_BLUE)
    t.font      = Font(name="Calibri", bold=True, color=WHITE, size=13)
    t.alignment = CENTER

    headers = ["Timeframe","CVD Threshold","Min Confluence","# Trades",
               "Win Rate %","Total PnL ($)","Final Bankroll ($)",
               "Max Drawdown %","Sharpe Ratio","Avg PnL/Trade ($)"]

    for c, h in enumerate(headers, 1):
        ws.cell(row=2, column=c).value = h
    style_header_row(ws, 2, len(headers), MID_BLUE)

    for row_i, m in enumerate([best_5m, best_15m], 3):
        if m is None:
            continue
        vals = [m["timeframe"], m["cvd_threshold"], m["min_confluence"],
                m["n_trades"], m["win_rate_%"], m["total_pnl_$"],
                m["final_bankroll_$"], m["max_drawdown_%"],
                m["sharpe_ratio"], m["avg_pnl_$"]]
        for c, v in enumerate(vals, 1):
            ws.cell(row=row_i, column=c).value = v
        style_data_row(ws, row_i, len(headers), row_i % 2 == 0)
        # colour PnL cell
        pnl_cell = ws.cell(row=row_i, column=6)
        pnl_cell.fill = hfill(GREEN_BG if (m["total_pnl_$"] or 0) >= 0 else RED_BG)
        pnl_cell.font = Font(name="Calibri", bold=True, size=10,
                             color="375623" if (m["total_pnl_$"] or 0) >= 0 else "9C0006")

    auto_col_width(ws)


# ── Write Grid Sheet ───────────────────────────────────────────────────────────

def write_grid(ws, results: list, tf: str):
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A1:J1")
    t = ws["A1"]
    t.value     = f"Parameter Optimisation Grid — {tf} Timeframe"
    t.fill      = hfill(DARK_BLUE)
    t.font      = Font(name="Calibri", bold=True, color=WHITE, size=12)
    t.alignment = CENTER

    headers = ["CVD Threshold","Min Confluence","# Trades","Win Rate %",
               "Total PnL ($)","Final Bankroll ($)","Max Drawdown %",
               "Sharpe Ratio","Avg PnL/Trade ($)","Score"]

    for c, h in enumerate(headers, 1):
        ws.cell(row=2, column=c).value = h
    style_header_row(ws, 2, len(headers))

    # Score = sharpe + win_rate/100 + total_pnl/bankroll (composite rank)
    def score(m):
        return (m["sharpe_ratio"] * 0.4 +
                (m["win_rate_%"] / 100) * 0.4 +
                (m["total_pnl_$"] / BANKROLL) * 0.2)

    results_s = sorted([r for r in results if r], key=score, reverse=True)

    for row_i, m in enumerate(results_s, 3):
        s = round(score(m), 3)
        vals = [m["cvd_threshold"], m["min_confluence"], m["n_trades"],
                m["win_rate_%"], m["total_pnl_$"], m["final_bankroll_$"],
                m["max_drawdown_%"], m["sharpe_ratio"], m["avg_pnl_$"], s]
        for c, v in enumerate(vals, 1):
            ws.cell(row=row_i, column=c).value = v
        style_data_row(ws, row_i, len(headers), row_i % 2 == 0)

        # Highlight best row
        if row_i == 3:
            for c in range(1, len(headers) + 1):
                ws.cell(row=row_i, column=c).fill = hfill("C6EFCE")
                ws.cell(row=row_i, column=c).font = Font(name="Calibri", bold=True, size=10, color="375623")

        pnl_cell = ws.cell(row=row_i, column=5)
        pnl_cell.fill = hfill(GREEN_BG if (m["total_pnl_$"] or 0) >= 0 else RED_BG)

    auto_col_width(ws)


# ── Write Trade Log Sheet ──────────────────────────────────────────────────────

def write_trade_log(ws, tdf: pd.DataFrame, tf: str, params: dict):
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A1:K1")
    t = ws["A1"]
    t.value     = (f"Trade Log — {tf}  |  CVD={params['cvd_threshold']}  "
                   f"Confluence={params['min_confluence']}  "
                   f"({len(tdf)} trades)")
    t.fill      = hfill(DARK_BLUE)
    t.font      = Font(name="Calibri", bold=True, color=WHITE, size=12)
    t.alignment = CENTER

    headers = ["#","Timestamp","Direction","Confidence","Stake ($)",
               "Entry Price","Shares","BTC Entry","BTC Exit","Win","PnL ($)","Bankroll ($)"]
    for c, h in enumerate(headers, 1):
        ws.cell(row=2, column=c).value = h
    style_header_row(ws, 2, len(headers))

    for row_i, (_, r) in enumerate(tdf.iterrows(), 3):
        vals = [row_i - 2, str(r["timestamp"])[:19], r["direction"],
                f"{r['confidence']:.0%}", r["stake"], r["entry_price"],
                r["shares"], r["btc_entry"], r["btc_exit"],
                "WIN" if r["win"] else "LOSS", r["pnl"], r["bankroll"]]
        for c, v in enumerate(vals, 1):
            ws.cell(row=row_i, column=c).value = v
        style_data_row(ws, row_i, len(headers), row_i % 2 == 0)

        win_cell = ws.cell(row=row_i, column=10)
        pnl_cell = ws.cell(row=row_i, column=11)
        is_win   = bool(r["win"])
        win_cell.fill = hfill(GREEN_BG if is_win else RED_BG)
        win_cell.font = Font(name="Calibri", bold=True, size=10,
                             color="375623" if is_win else "9C0006")
        pnl_cell.fill = hfill(GREEN_BG if is_win else RED_BG)

    auto_col_width(ws)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Polymarket Bot Backtest: 5m vs 15m ===\n")

    # 1. Fetch data
    print("Step 1 — Fetching kline data:")
    df5  = fetch_klines("5m",  DAYS)
    df15 = fetch_klines("15m", DAYS)

    # 2. Optimise per timeframe
    print("\nStep 2 — Running optimisation grid...")
    results = {"5m": [], "15m": []}

    for tf, df_raw in [("5m", df5), ("15m", df15)]:
        combos = list(product(CVD_GRID, VOL_SPIKE_GRID, CONFLUENCE_GRID))
        print(f"  {tf}: {len(combos)} combinations", end="", flush=True)
        for cvd, vsm, mc in combos:
            df_f = add_features(df_raw, vsm)
            m, _ = run_backtest(df_f, tf, cvd, mc)
            if m:
                m["vol_spike_mult"] = vsm
                results[tf].append(m)
        print(f"  → {len(results[tf])} valid runs")

    # 3. Pick best per timeframe (composite score)
    def score(m):
        return (m["sharpe_ratio"] * 0.4 +
                (m["win_rate_%"] / 100) * 0.4 +
                (m["total_pnl_$"] / BANKROLL) * 0.2)

    best = {}
    for tf in ("5m", "15m"):
        valid = [r for r in results[tf] if r]
        best[tf] = max(valid, key=score) if valid else None

    print("\nStep 3 — Best parameters found:")
    for tf in ("5m", "15m"):
        b = best[tf]
        if b:
            print(f"  {tf}: CVD={b['cvd_threshold']}  Confluence={b['min_confluence']}  "
                  f"WinRate={b['win_rate_%']}%  PnL=${b['total_pnl_$']}  "
                  f"Sharpe={b['sharpe_ratio']}")

    # 4. Re-run best params to get full trade logs
    print("\nStep 4 — Generating trade logs for best params...")
    trade_logs = {}
    for tf, df_raw in [("5m", df5), ("15m", df15)]:
        b = best[tf]
        if b:
            df_f = add_features(df_raw, b.get("vol_spike_mult", 2.0))
            _, tdf = run_backtest(df_f, tf, b["cvd_threshold"], b["min_confluence"])
            trade_logs[tf] = tdf

    # 5. Build Excel workbook
    print("\nStep 5 — Building Excel file...")
    wb = Workbook()
    wb.remove(wb.active)

    # Summary sheet
    ws_sum = wb.create_sheet("Summary")
    write_summary(ws_sum, best.get("5m"), best.get("15m"))

    # Grid sheets
    for tf in ("5m", "15m"):
        ws_grid = wb.create_sheet(f"{tf} Optimisation Grid")
        write_grid(ws_grid, results[tf], tf)

    # Trade log sheets
    for tf in ("5m", "15m"):
        tdf = trade_logs.get(tf)
        if tdf is not None and not tdf.empty:
            ws_log = wb.create_sheet(f"{tf} Trade Log")
            write_trade_log(ws_log, tdf, tf, best[tf])

    out = "backtest_results.xlsx"
    wb.save(out)
    print(f"\nDone! Saved → {out}")
    print(f"Sheets: {[s.title for s in wb.worksheets]}\n")


if __name__ == "__main__":
    main()
