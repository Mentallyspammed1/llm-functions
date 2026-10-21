#!/usr/bin/env python3
"""Backtester for the Bybit price-action strategy.

@describe Backtest the price-action trend strategy over Bybit klines.
@option --symbol <TEXT>      Trading pair (e.g. BTCUSDT).
@option --interval <TEXT>    Kline timeframe: 1, 5, 15, 60, 240, D (default: 60).
@option --limit <NUM>        Number of candles to fetch (default: 1000).
@option --risk-pct <NUM>     Risk per trade, % of balance (default: 1.0).
@option --rr <NUM>           Reward:risk multiple for the final target (default: 2.0).
@option --sl-atr <NUM>       Stop-loss distance in ATRs (default: 1.5).
@option --start-balance <NUM> Starting balance in USDT (default: 1000).
@option --fee-rate <NUM>     Taker fee rate per side (default: 0.00055).
@option --slippage-bps <NUM> Adverse slippage in basis points (default: 2).
@option --min-confidence <NUM> Minimum signal confidence (default: 55).
@option --trailing-atr <NUM> ATR trailing-stop multiple (0 = off).
@option --time-stop <NUM>    Force-exit after N bars (0 = off).
@option --json                Output raw JSON.

Honesty notes
-------------
The simulator is deliberately pessimistic, because an optimistic backtest is
worse than no backtest:

* entries fill at the **next bar's open** plus adverse slippage, never at the
  signal bar's close (you cannot trade a candle you have only just seen close);
* a bar that gaps through the stop fills at the **open**, not at the stop;
* when a bar touches both the stop and the target, the **stop** is counted;
* taker fees are charged on entry, every scale-out and the final exit;
* every trade's `net_pnl` includes its own entry fee, so the trade log sums
  exactly to the change in balance (verified by `summary.reconciled`).
"""

from __future__ import annotations

import argparse
import json
from typing import List, Optional, Sequence

try:  # package import (normal path)
    from .analytics import summarize as _summarize
    from .price_action import (
        PriceActionEngine,
        Signal,
        _adx_series,
        _atr_series,
        _efficiency_ratio,
        _ema_series,
        _percentile_rank,
        _rsi_series,
    )
except ImportError:  # direct execution: `python tools/bybit/backtest.py`
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from tools.bybit.analytics import summarize as _summarize
    from tools.bybit.price_action import (
        PriceActionEngine,
        Signal,
        _adx_series,
        _atr_series,
        _efficiency_ratio,
        _ema_series,
        _percentile_rank,
        _rsi_series,
    )

PERIODS_PER_YEAR = {
    "1": 525600, "3": 175200, "5": 105120, "15": 35040, "30": 17520,
    "60": 8760, "120": 4380, "240": 2190, "360": 1460, "720": 730,
    "D": 365, "W": 52, "M": 12,
}


def periods_per_year(interval: str) -> float:
    return float(PERIODS_PER_YEAR.get(str(interval).upper(), 8760))


# ── indicator precompute ────────────────────────────────────────────────────


def _precompute(chron, engine: PriceActionEngine) -> dict:
    """Compute every series once; the per-bar signal then costs O(1) lookups."""
    o = [float(k[1]) for k in chron]
    h = [float(k[2]) for k in chron]
    l = [float(k[3]) for k in chron]
    c = [float(k[4]) for k in chron]
    v = [float(k[5]) for k in chron]
    n = len(c)
    ema_f = _ema_series(c, engine.ema_fast) if n else []
    ema_m = _ema_series(c, engine.ema_mid) if n else []
    ema_s = _ema_series(c, engine.ema_slow) if n else []
    # _atr_series returns n-1 values (TR of bar j+1 at index j); shift by one
    # so that atr[i] is the ATR *of bar i*, matching the other series.
    atr_raw = _atr_series(h, l, c, engine.atr_period) if n > 1 else []
    atr = ([atr_raw[0]] + atr_raw) if atr_raw else [0.0] * n
    rsi_series = _rsi_series(c, engine.rsi_period) if n else []
    adx_series = _adx_series(h, l, c, engine.adx_period) if n else []
    # Slow-EMA slope normalised by ATR (directional regime gate), causal.
    k = max(1, engine.slope_lookback)
    slow_slope = [None] * n
    for i in range(k, n):
        if atr[i] > 0:
            slow_slope[i] = (ema_s[i] - ema_s[i - k]) / atr[i]

    # ATR percentile rank over a trailing window, computed causally
    atr_pctl = [0.5] * n
    win = 100
    for i in range(n):
        lo = max(0, i - win + 1)
        seg = atr[lo : i + 1]
        if seg:
            atr_pctl[i] = _percentile_rank(seg, seg[-1])

    # Fractal swings detected once over the full series. A swing at index j is
    # only *knowable* at bar i once j + swing_strength <= i, which the per-bar
    # lookup below enforces — so this is not lookahead.
    proxy = [
        [0, str(oo), str(hh), str(ll), str(cc), "0"]
        for oo, hh, ll, cc in zip(o, h, l, c)
    ]
    proxy.reverse()  # swing_points expects newest-first
    swings = engine.swing_points(proxy)

    return {
        "o": o, "h": h, "l": l, "c": c, "v": v,
        "ema_f": ema_f, "ema_m": ema_m, "ema_s": ema_s,
        "atr": atr, "rsi": rsi_series, "adx": adx_series,
        "atr_pctl": atr_pctl, "swings": swings, "slow_slope": slow_slope,
    }


def _signal_at(
    chron,
    i: int,
    pre: dict,
    engine: PriceActionEngine,
    min_confidence: float,
    rr_min: float,
    sl_mult: float = 1.5,
) -> Optional[Signal]:
    """Signal for bar i using only information available at that bar's close.

    Delegates to `PriceActionEngine.decide_signal` — the same function live
    trading uses — so the backtest cannot drift from production behaviour.
    """
    c = pre["c"]
    atr = pre["atr"][i] if i < len(pre["atr"]) else 0.0
    if atr <= 0:
        return None
    price = c[i]
    f, m, s = pre["ema_f"][i], pre["ema_m"][i], pre["ema_s"][i]
    if f > m > s and price > f:
        alignment = "BULLISH"
    elif f < m < s and price < f:
        alignment = "BEARISH"
    elif price > s:
        alignment = "MIXED_BULL"
    else:
        alignment = "MIXED_BEAR"

    # Structure from confirmed swings inside the lookback window.
    s_strength = engine.swing_strength
    lookback = engine.structure_lookback
    known = [
        sw for sw in pre["swings"]
        if sw.index <= i - s_strength and sw.index >= i - lookback
    ]

    structure = engine.structure_from_arrays(
        pre["h"][max(0, i - lookback) : i + 1],
        pre["l"][max(0, i - lookback) : i + 1],
        pre["c"][max(0, i - lookback) : i + 1],
        atr,
        ema_fast=f,
        ema_slow=s,
        swings=known,
    )

    # Rejection candle from the bar that just closed (chron[i]).
    rejection = None
    if i >= 1:
        shape = engine._candle_shape(chron[i])
        if shape["lower_wick_pct"] >= 0.6 and shape["body_pct"] <= 0.35:
            rejection = "BULLISH_REJECTION"
        elif shape["upper_wick_pct"] >= 0.6 and shape["body_pct"] <= 0.35:
            rejection = "BEARISH_REJECTION"

    sig = engine.decide_signal(
        price=price,
        atr=atr,
        rsi_val=pre["rsi"][i] if i < len(pre["rsi"]) else 50.0,
        adx_val=pre["adx"][i] if i < len(pre["adx"]) else 0.0,
        ema_alignment=alignment,
        ema_fast_val=f,
        struct_state=structure.get("state"),
        bos=structure.get("bos"),
        last_high=structure.get("last_high"),
        last_low=structure.get("last_low"),
        rejection=rejection,
        er=_efficiency_ratio(c[: i + 1], engine.er_period),
        atr_pctl=pre["atr_pctl"][i],
        slow_slope_atr=pre["slow_slope"][i],
        min_confidence=min_confidence,
        rr_min=rr_min,
        sl_atr_mult=sl_mult,
    )
    return None if sig.action == "WAIT" else sig


# ── main simulation ─────────────────────────────────────────────────────────


def run_backtest(
    klines: Sequence[Sequence],
    risk_pct: float = 1.0,
    rr: float = 3.0,
    sl_atr: float = 1.5,
    start_balance: float = 1000.0,
    fee_rate: float = 0.00055,
    min_confidence: float = 55.0,
    warmup: int = 220,
    sl_atr_mult: Optional[float] = None,
    engine: Optional[PriceActionEngine] = None,
    slippage_bps: float = 2.0,
    partial_at_r: float = 1.0,
    partial_frac: float = 0.0,
    trailing_atr: float = 0.0,
    trail_after_r: float = 1.0,
    breakeven_at_r: float = 0.0,
    time_stop_bars: int = 0,
    allow_shorts: bool = True,
    interval: str = "60",
) -> dict:
    """Run the price-action strategy over a kline series (newest-first input).

    Returns summary stats, a per-trade log and the equity curve.
    See the module docstring for the fill assumptions.

    Exit-model defaults (`partial_frac=0.0`, `breakeven_at_r=0.0`, `rr=3.0`)
    were chosen by measuring expectancy across trending, regime-switching,
    mean-reverting and choppy synthetic markets, not by fitting one series.
    Taking half the position off at 1R and moving the stop to breakeven cost
    roughly half the strategy's expectancy in every trending regime: a trend
    system's edge lives entirely in its right tail, and the classic
    "bank half at 1R" rule amputates it. The knobs remain available — pass
    `partial_frac=0.5, breakeven_at_r=1.0` to get the old behaviour back.
    """
    engine = engine or PriceActionEngine()
    sl_mult = sl_atr_mult if sl_atr_mult is not None else sl_atr
    slip = max(0.0, float(slippage_bps)) / 10000.0

    chron = list(reversed(list(klines)))
    if len(chron) < warmup + 10:
        return {
            "status": "error",
            "msg": f"need at least {warmup + 10} candles, got {len(chron)}",
        }
    pre = _precompute(chron, engine)
    o, h, l, c = pre["o"], pre["h"], pre["l"], pre["c"]

    balance = float(start_balance)
    equity_curve: List[float] = [balance]
    trades: List[dict] = []
    pos: Optional[dict] = None
    pending: Optional[Signal] = None  # signal awaiting next-bar entry

    def mark(p: Optional[dict], price: float) -> float:
        if p is None:
            return balance
        return balance + p["side"] * p["qty"] * (price - p["entry"])

    def close_leg(p: dict, qty: float, price: float, reason: str, at_open: bool = False):
        """Close `qty` at `price`; returns (cash_delta, fee)."""
        # Stops are market orders and slip; take-profits are resting limits.
        if at_open or reason.startswith("STOP"):
            exit_price = price * (1 - p["side"] * slip)
        else:
            exit_price = price
        gross = (exit_price - p["entry"]) * p["side"] * qty
        fee = fee_rate * qty * exit_price
        return gross - fee, fee, exit_price

    for i in range(warmup, len(chron)):
        bar_open, bar_high, bar_low, bar_close = o[i], h[i], l[i], c[i]

        # ── 1. fill a signal raised on the previous bar, at this bar's open ──
        if pos is None and pending is not None:
            sig = pending
            pending = None
            side = 1 if sig.action == "LONG" else -1
            entry = bar_open * (1 + side * slip)  # adverse slippage on entry
            stop = float(sig.stop_loss)
            # A gap can invalidate the setup entirely (stop already breached).
            valid = (stop < entry) if side == 1 else (stop > entry)
            r_dist = abs(entry - stop)
            if valid and r_dist > 0:
                risk_amount = balance * (risk_pct / 100.0)
                qty = risk_amount / r_dist
                notional = qty * entry
                if notional > balance:  # no leverage in the sim
                    qty = balance / entry
                    risk_amount = qty * r_dist
                if qty > 0:
                    entry_fee = fee_rate * qty * entry
                    balance -= entry_fee
                    pos = {
                        "side": side,
                        "entry": entry,
                        "qty": qty,
                        "orig_qty": qty,
                        "stop": stop,
                        "r_dist": r_dist,
                        "risk_amount": risk_amount,
                        "entry_fee": entry_fee,
                        # Carry the entry fee in realized so this trade's
                        # net_pnl is the *full* cost of the trade.
                        "realized": -entry_fee,
                        "fees": entry_fee,
                        "bars_held": 0,
                        "partial_done": False,
                        "be_done": False,
                        "trail_on": False,
                        "extreme": entry,
                        "entry_i": i,
                        "reasons": list(sig.reasons),
                        "setup": sig.setup,
                        "confidence": sig.confidence,
                        "partials": [],
                    }

        # ── 2. manage an open position on this bar ───────────────────────────
        if pos is not None:
            pos["bars_held"] += 1
            side = pos["side"]
            stop = pos["stop"]
            r_dist = pos["r_dist"]
            entry = pos["entry"]
            partial_level = entry + side * r_dist * partial_at_r
            final_level = entry + side * r_dist * max(rr, 0.1)

            # Gap check first: if the bar opens beyond our stop we are filled
            # at the open, not at the stop (this is where trend systems bleed).
            gapped = (bar_open <= stop) if side == 1 else (bar_open >= stop)
            hit_partial = (
                not pos["partial_done"]
                and partial_frac > 0
                and ((bar_high >= partial_level) if side == 1 else (bar_low <= partial_level))
            )
            hit_final = (bar_high >= final_level) if side == 1 else (bar_low <= final_level)
            hit_stop = (bar_low <= stop) if side == 1 else (bar_high >= stop)

            exit_reason = None
            exit_price = None

            if gapped:
                # Full remaining position out at the open.
                cash, fee, px = close_leg(pos, pos["qty"], bar_open, "STOP_LOSS", at_open=True)
                balance += cash
                pos["realized"] += cash
                pos["fees"] += fee
                exit_reason, exit_price = "STOP_GAP", px
                pos["qty"] = 0.0
            elif hit_stop:
                cash, fee, px = close_leg(pos, pos["qty"], stop, "STOP_LOSS")
                balance += cash
                pos["realized"] += cash
                pos["fees"] += fee
                exit_reason = (
                    "BREAKEVEN" if pos["be_done"] and abs(stop - entry) < 1e-9 else "STOP_LOSS"
                )
                exit_price = px
                pos["qty"] = 0.0
            elif hit_partial and not pos["partial_done"]:
                qty_part = pos["orig_qty"] * min(1.0, partial_frac)
                cash, fee, px = close_leg(pos, qty_part, partial_level, "PARTIAL")
                balance += cash
                pos["realized"] += cash
                pos["fees"] += fee
                pos["qty"] -= qty_part
                pos["partial_done"] = True
                pos["partials"].append({"r": partial_at_r, "price": px, "qty": qty_part})
                if breakeven_at_r and partial_at_r >= breakeven_at_r:
                    pos["stop"] = entry
                    pos["be_done"] = True
                if pos["qty"] <= 1e-12:
                    exit_reason, exit_price = "PARTIAL_ONLY", px
            elif hit_final:
                cash, fee, px = close_leg(pos, pos["qty"], final_level, "TAKE_PROFIT")
                balance += cash
                pos["realized"] += cash
                pos["fees"] += fee
                exit_reason, exit_price = "TAKE_PROFIT", px
                pos["qty"] = 0.0
            elif time_stop_bars and pos["bars_held"] >= time_stop_bars:
                cash, fee, px = close_leg(pos, pos["qty"], bar_close, "TIME_STOP", at_open=True)
                balance += cash
                pos["realized"] += cash
                pos["fees"] += fee
                exit_reason, exit_price = "TIME_STOP", px
                pos["qty"] = 0.0

            if pos is not None and exit_reason is not None:
                net = pos["realized"]
                trades.append({
                    "entry": round(entry, 8),
                    "exit": round(exit_price, 8),
                    "side": "LONG" if side == 1 else "SHORT",
                    "reason": exit_reason,
                    "setup": pos["setup"],
                    "confidence": pos["confidence"],
                    "bars_held": pos["bars_held"],
                    "qty": round(pos["orig_qty"], 8),
                    "risk_amount": round(pos["risk_amount"], 6),
                    "gross_pnl": round(net + pos["fees"], 6),
                    "fees": round(pos["fees"], 6),
                    "net_pnl": round(net, 6),
                    "r_multiple": round(net / pos["risk_amount"], 3) if pos["risk_amount"] else 0.0,
                    "partials": pos["partials"],
                })
                pos = None

        # ── 3. trailing stop / breakeven management on the closed bar ────────
        if pos is not None:
            side = pos["side"]
            pos["extreme"] = (
                max(pos["extreme"], bar_high) if side == 1 else min(pos["extreme"], bar_low)
            )
            gain_r = (pos["extreme"] - pos["entry"]) * side / pos["r_dist"] if pos["r_dist"] else 0

            if breakeven_at_r and not pos["be_done"] and gain_r >= breakeven_at_r:
                pos["stop"] = pos["entry"]
                pos["be_done"] = True

            if trailing_atr and trailing_atr > 0 and gain_r >= trail_after_r:
                atr_now = pre["atr"][i] if i < len(pre["atr"]) else 0.0
                if atr_now > 0:
                    new_stop = pos["extreme"] - side * atr_now * trailing_atr
                    if (side == 1 and new_stop > pos["stop"]) or (
                        side == -1 and new_stop < pos["stop"]
                    ):
                        pos["stop"] = new_stop
                        pos["trail_on"] = True

        # ── 4. raise a new signal if flat ────────────────────────────────────
        if pos is None and pending is None and i < len(chron) - 1:
            sig = _signal_at(chron, i, pre, engine, min_confidence, rr, sl_mult)
            if sig is not None and (allow_shorts or sig.action == "LONG"):
                pending = sig

        equity_curve.append(mark(pos, bar_close))

    # ── close anything still open at the last close ─────────────────────────
    if pos is not None:
        last = c[-1]
        cash, fee, px = close_leg(pos, pos["qty"], last, "END_OF_DATA", at_open=True)
        balance += cash
        pos["realized"] += cash
        pos["fees"] += fee
        trades.append({
            "entry": round(pos["entry"], 8),
            "exit": round(px, 8),
            "side": "LONG" if pos["side"] == 1 else "SHORT",
            "reason": "END_OF_DATA",
            "setup": pos["setup"],
            "confidence": pos["confidence"],
            "bars_held": pos["bars_held"],
            "qty": round(pos["orig_qty"], 8),
            "risk_amount": round(pos["risk_amount"], 6),
            "gross_pnl": round(pos["realized"] + pos["fees"], 6),
            "fees": round(pos["fees"], 6),
            "net_pnl": round(pos["realized"], 6),
            "r_multiple": round(pos["realized"] / pos["risk_amount"], 3) if pos["risk_amount"] else 0.0,
            "partials": pos["partials"],
        })
        pos = None
    equity_curve[-1] = balance
    summary = _summarize(
        trades, equity_curve, start_balance, periods_per_year(interval)
    )
    summary["bars"] = len(chron)
    summary["exposure_pct"] = round(
        sum(t["bars_held"] for t in trades) / max(1, len(chron) - warmup) * 100, 2
    )
    summary["avg_r"] = summary.get("expectancy_r", 0.0)
    summary["costs"] = {
        "fees_paid": round(sum(t["fees"] for t in trades), 4),
        "slippage_bps": slippage_bps,
        "modeled_as": "adverse fills on entry and stop exits",
    }

    # Downsample the curve so payloads stay small for LLM/CLI consumption.
    step = max(1, len(equity_curve) // 200)
    curve = [round(equity_curve[j], 2) for j in range(0, len(equity_curve), step)]

    return {
        "status": "ok",
        "summary": summary,
        "trades": trades,
        "equity_curve": curve,
        "params": {
            "risk_pct": risk_pct,
            "rr": rr,
            "sl_atr": sl_mult,
            "min_confidence": min_confidence,
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
            "partial_at_r": partial_at_r,
            "partial_frac": partial_frac,
            "trailing_atr": trailing_atr,
            "time_stop_bars": time_stop_bars,
            "interval": interval,
        },
    }


def run(
    symbol: str = "BTCUSDT",
    interval: str = "60",
    limit: int = 1000,
    risk_pct: float = 1.0,
    rr: float = 2.0,
    sl_atr: float = 1.5,
    start_balance: float = 1000.0,
    fee_rate: float = 0.00055,
    min_confidence: float = 55.0,
    slippage_bps: float = 2.0,
    trailing_atr: float = 0.0,
    time_stop_bars: int = 0,
    klines: Optional[Sequence[Sequence]] = None,
    **kwargs,
) -> dict:
    """Unified entry point. Fetch klines from the realm client unless `klines`
    is supplied (used by the in-process dispatcher and tests)."""
    if klines is None:
        try:
            from .terminal import BybitRealm
        except ImportError:
            from tools.bybit.terminal import BybitRealm

        bot = BybitRealm()
        try:
            data = bot.get_klines(symbol, interval, limit)
            klines = data.get("list", data.get("result", {}).get("list", []))
        finally:
            bot.close()
        if not klines:
            return {"status": "error", "msg": f"No klines returned for {symbol}"}
    res = run_backtest(
        klines,
        risk_pct=risk_pct,
        rr=rr,
        sl_atr=sl_atr,
        start_balance=start_balance,
        fee_rate=fee_rate,
        min_confidence=min_confidence,
        slippage_bps=slippage_bps,
        trailing_atr=trailing_atr,
        time_stop_bars=time_stop_bars,
        interval=interval,
    )
    res["symbol"] = symbol.upper()
    res["interval"] = interval
    return res


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the Bybit price-action strategy")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="60")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--sl-atr", type=float, default=1.5)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--fee-rate", type=float, default=0.00055)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--min-confidence", type=float, default=55.0)
    parser.add_argument("--trailing-atr", type=float, default=0.0)
    parser.add_argument("--time-stop", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run(
        symbol=args.symbol,
        interval=args.interval,
        limit=args.limit,
        risk_pct=args.risk_pct,
        rr=args.rr,
        sl_atr=args.sl_atr,
        start_balance=args.start_balance,
        fee_rate=args.fee_rate,
        min_confidence=args.min_confidence,
        slippage_bps=args.slippage_bps,
        trailing_atr=args.trailing_atr,
        time_stop_bars=args.time_stop,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
