"""Performance analytics, validation and sizing for the Bybit trading suite.

Everything here is pure math over a trade list / equity curve, so it needs no
network and is fully unit-testable. The backtester (`tools.bybit.backtest`)
consumes `summarize()` so that every entry point reports the same numbers.

Why this module exists
----------------------
A backtest that only reports "net PnL" is close to useless for deciding
whether an edge is real. The metrics that actually predict live survival are:

  * **expectancy in R** — average result per unit risked
  * **profit factor** — gross wins / gross losses (>1.3 to be interesting)
  * **max drawdown** — the number that decides whether you can stay solvent
  * **Sharpe / Sortino** — return per unit of (downside) volatility
  * **out-of-sample degradation** — hurt by overfitting, see walk_forward()
  * **risk of ruin** — probability of losing the account at a given size

`optimize_strategy()` deliberately selects parameters by a *robustness* score
(expectancy x sample size, penalised for drawdown and trade scarcity) rather
than by peak return, and `walk_forward()` re-tests on unseen data. Chasing the
best in-sample number is the single most common way a profitable-looking
strategy loses money live.
"""

from __future__ import annotations

import math
import random
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ── equity-curve statistics ────────────────────────────────────────────────


def _returns(equity: Sequence[float]) -> List[float]:
    out = []
    for prev, cur in zip(equity, equity[1:]):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def max_drawdown(equity: Sequence[float]) -> Tuple[float, int]:
    """Return (max drawdown fraction, longest drawdown length in bars)."""
    if not equity:
        return 0.0, 0
    peak = equity[0]
    worst = 0.0
    longest = streak = 0
    for eq in equity:
        if eq >= peak:
            peak = eq
            streak = 0
        else:
            streak += 1
            longest = max(longest, streak)
            if peak > 0:
                worst = max(worst, (peak - eq) / peak)
    return worst, longest


def equity_metrics(
    equity: Sequence[float],
    periods_per_year: float = 8760.0,
    risk_free: float = 0.0,
) -> dict:
    """Sharpe, Sortino, Calmar, drawdown and volatility from an equity curve."""
    rets = _returns(equity)
    n = len(rets)
    if n == 0:
        return {
            "sharpe": 0.0, "sortino": 0.0, "calmar": 0.0,
            "max_drawdown_pct": 0.0, "max_drawdown_bars": 0,
            "volatility_pct": 0.0, "ulcer_index": 0.0,
            "total_return_pct": 0.0, "cagr_pct": 0.0,
        }
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    sd = math.sqrt(var)
    downside = [r for r in rets if r < 0]
    dsd = math.sqrt(sum(r * r for r in downside) / n) if downside else 0.0

    ann = math.sqrt(periods_per_year)
    sharpe = (mean - risk_free / periods_per_year) / sd * ann if sd > 0 else 0.0
    sortino = (mean - risk_free / periods_per_year) / dsd * ann if dsd > 0 else 0.0

    dd, dd_bars = max_drawdown(equity)
    total = equity[-1] / equity[0] - 1.0 if equity[0] > 0 else 0.0
    years = n / periods_per_year if periods_per_year else 0.0
    cagr = ((1 + total) ** (1 / years) - 1) if years > 0 and total > -1 else 0.0
    calmar = cagr / dd if dd > 0 else 0.0

    # Ulcer index: RMS of percentage drawdowns (penalises long, deep drawdowns)
    peak = equity[0]
    sq = []
    for eq in equity:
        peak = max(peak, eq)
        if peak > 0:
            sq.append(((peak - eq) / peak) ** 2)
    ulcer = math.sqrt(sum(sq) / len(sq)) * 100 if sq else 0.0

    return {
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "max_drawdown_bars": dd_bars,
        "volatility_pct": round(sd * math.sqrt(periods_per_year) * 100, 2),
        "ulcer_index": round(ulcer, 2),
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
    }


# ── trade statistics ───────────────────────────────────────────────────────


def _streaks(pnls: Sequence[float]) -> Tuple[int, int]:
    best_w = best_l = cw = cl = 0
    for p in pnls:
        if p > 0:
            cw += 1
            cl = 0
            best_w = max(best_w, cw)
        elif p < 0:
            cl += 1
            cw = 0
            best_l = max(best_l, cl)
        else:
            cw = cl = 0
    return best_w, best_l


def trade_metrics(trades: Sequence[dict]) -> dict:
    """Win rate, profit factor, expectancy, streaks and payoff from a trade log."""
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "win_rate_pct": 0.0, "profit_factor": 0.0, "expectancy_r": 0.0,
            "expectancy_usdt": 0.0, "avg_win_usdt": 0.0, "avg_loss_usdt": 0.0,
            "avg_win_r": 0.0, "avg_loss_r": 0.0, "payoff_ratio": 0.0,
            "largest_win_usdt": 0.0, "largest_loss_usdt": 0.0,
            "max_consecutive_wins": 0, "max_consecutive_losses": 0,
            "avg_bars_held": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "total_fees": 0.0, "net_pnl": 0.0,
        }
    pnls = [float(t.get("net_pnl", t.get("pnl_usdt", 0.0))) for t in trades]
    rs = [float(t.get("r_multiple", 0.0)) for t in trades]
    wins = [i for i, p in enumerate(pnls) if p > 0]
    losses = [i for i, p in enumerate(pnls) if p < 0]
    flat = len(pnls) - len(wins) - len(losses)
    gross_profit = sum(pnls[i] for i in wins)
    gross_loss = abs(sum(pnls[i] for i in losses))
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    payoff = avg_win / avg_loss if avg_loss > 0 else (avg_win if avg_win > 0 else 0.0)
    decided = len(wins) + len(losses)
    win_rate = len(wins) / decided if decided else 0.0
    best_w, best_l = _streaks(pnls)

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": flat,
        "win_rate_pct": round(win_rate * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0
        else (round(gross_profit, 2) if gross_profit > 0 else 0.0),
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
        "expectancy_usdt": round(sum(pnls) / len(pnls), 4),
        "avg_win_usdt": round(avg_win, 4),
        "avg_loss_usdt": round(avg_loss, 4),
        "avg_win_r": round(sum(rs[i] for i in wins) / len(wins), 3) if wins else 0.0,
        "avg_loss_r": round(sum(rs[i] for i in losses) / len(losses), 3) if losses else 0.0,
        "payoff_ratio": round(payoff, 2),
        "largest_win_usdt": round(max(pnls), 4) if pnls else 0.0,
        "largest_loss_usdt": round(min(pnls), 4) if pnls else 0.0,
        "max_consecutive_wins": best_w,
        "max_consecutive_losses": best_l,
        "avg_bars_held": round(
            sum(float(t.get("bars_held", 0)) for t in trades) / len(trades), 1
        ),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "total_fees": round(sum(float(t.get("fees", 0.0)) for t in trades), 4),
        "net_pnl": round(sum(pnls), 4),
    }


# ── position sizing / survival ─────────────────────────────────────────────


def kelly_fraction(win_rate: float, payoff_ratio: float, cap: float = 0.5) -> float:
    """Kelly-optimal fraction of capital to risk, given win rate and payoff.

    ``f* = W - (1-W)/R``. Raw Kelly is famously too aggressive in practice
    (it assumes you know W and R exactly), so callers should scale it — see
    `kelly_sizing()` which defaults to a quarter-Kelly.
    """
    w = max(0.0, min(1.0, float(win_rate)))
    r = float(payoff_ratio)
    if r <= 0:
        return 0.0
    f = w - (1 - w) / r
    return max(0.0, min(cap, f))


def kelly_sizing(
    balance: float,
    win_rate: float,
    payoff_ratio: float,
    fraction: float = 0.25,
    hard_cap_pct: float = 2.0,
    min_pct: float = 0.1,
) -> dict:
    """Fractional-Kelly risk per trade, clamped to a sane band.

    Returns the recommended ``risk_pct`` for `trade_plan`/`execute_signal`.
    Below `min_pct` the edge is not worth trading at all (fees eat it).
    """
    full = kelly_fraction(win_rate, payoff_ratio)
    raw_pct = full * fraction * 100
    pct = min(hard_cap_pct, max(0.0, raw_pct))
    return {
        "status": "ok",
        "kelly_full_pct": round(full * 100, 3),
        "kelly_fraction": fraction,
        "recommended_risk_pct": round(pct, 3),
        "risk_amount": round(balance * pct / 100, 2),
        "tradeable": pct >= min_pct,
        "note": (
            "Edge too thin: fractional Kelly lands below the minimum, skip trading."
            if pct < min_pct
            else f"Risk {pct:.2f}% of {balance:.2f} USDT per trade."
        ),
    }


def risk_of_ruin(
    win_rate: float,
    payoff_ratio: float,
    risk_pct: float,
    ruin_threshold: float = 0.5,
    simulations: int = 2000,
    max_bets: int = 400,
    seed: int = 7,
) -> dict:
    """Monte-Carlo probability of losing `ruin_threshold` of the account.

    Simulates fixed-fractional betting: each trade risks `risk_pct` of the
    *current* balance, wins `payoff_ratio * risk` with probability `win_rate`
    and loses `risk` otherwise. Ruin is absorbing. This is deliberately
    simulated rather than solved in closed form: the gambler's-ruin formula
    only holds for unit payoffs, and real strategies have R != 1.
    """
    p = max(0.0, min(1.0, float(win_rate)))
    r = float(payoff_ratio)
    if r <= 0 or p <= 0:
        return {"status": "ok", "ruin_probability": 1.0, "median_final_pct": -100.0,
                "note": "no edge — ruin is certain"}
    if p >= 1.0:
        return {"status": "ok", "ruin_probability": 0.0, "median_final_pct": None,
                "note": "no losing trades modelled"}

    rng = random.Random(seed)
    frac = max(1e-6, float(risk_pct) / 100.0)
    ruin_equity = 1.0 - max(0.01, min(0.99, ruin_threshold))
    ruined = 0
    finals: List[float] = []
    for _ in range(max(1, simulations)):
        eq = 1.0
        for _ in range(max_bets):
            if rng.random() < p:
                eq *= 1 + frac * r
            else:
                eq *= 1 - frac
            if eq <= ruin_equity:
                eq = ruin_equity
                break
        if eq <= ruin_equity:
            ruined += 1
        finals.append(eq)
    finals.sort()
    median = finals[len(finals) // 2]
    return {
        "status": "ok",
        "ruin_probability": round(ruined / simulations, 4),
        "ruin_threshold_pct": round(ruin_threshold * 100, 2),
        "median_final_pct": round((median - 1) * 100, 2),
        "p05_final_pct": round((finals[int(0.05 * len(finals))] - 1) * 100, 2),
        "p95_final_pct": round((finals[int(0.95 * len(finals))] - 1) * 100, 2),
        "simulations": simulations,
    }


def monte_carlo_drawdowns(
    trades: Sequence[dict],
    simulations: int = 1000,
    start_balance: float = 1000.0,
    seed: int = 42,
) -> dict:
    """Bootstrap the trade sequence to estimate the drawdown distribution.

    Resampling the *actual* trades with replacement shows how much worse the
    equity curve could have looked purely from ordering, which in-sample
    drawdown always understates.
    """
    pnls = [float(t.get("net_pnl", t.get("pnl_usdt", 0.0))) for t in trades]
    if len(pnls) < 5:
        return {"status": "error", "msg": "need at least 5 trades"}
    rng = random.Random(seed)
    dds, finals = [], []
    for _ in range(max(1, simulations)):
        eq = start_balance
        peak = eq
        worst = 0.0
        for _ in range(len(pnls)):
            eq += rng.choice(pnls)
            peak = max(peak, eq)
            if peak > 0:
                worst = max(worst, (peak - eq) / peak)
        dds.append(worst * 100)
        finals.append(eq)
    dds.sort()
    finals.sort()

    def pct(vals, q):
        return vals[min(len(vals) - 1, int(q * len(vals)))]

    return {
        "status": "ok",
        "simulations": simulations,
        "drawdown_pct": {
            "median": round(pct(dds, 0.50), 2),
            "p75": round(pct(dds, 0.75), 2),
            "p90": round(pct(dds, 0.90), 2),
            "p95": round(pct(dds, 0.95), 2),
            "worst": round(dds[-1], 2),
        },
        "final_balance": {
            "p05": round(pct(finals, 0.05), 2),
            "median": round(pct(finals, 0.50), 2),
            "p95": round(pct(finals, 0.95), 2),
        },
        "prob_profitable": round(
            sum(1 for f in finals if f > start_balance) / len(finals), 3
        ),
    }


# ── summary assembly ───────────────────────────────────────────────────────


def summarize(
    trades: Sequence[dict],
    equity_curve: Sequence[float],
    start_balance: float,
    periods_per_year: float = 8760.0,
) -> dict:
    """Full performance report: trade stats + equity-curve stats + sanity flag."""
    t = trade_metrics(trades)
    e = equity_metrics(equity_curve, periods_per_year)
    # The equity curve (i.e. real cash) is the source of truth. The trade log
    # must explain it; if it does not, the simulator has an accounting bug and
    # `reconciled` says so rather than silently reporting a flattering number.
    final = equity_curve[-1] if equity_curve else float(start_balance)
    net = final - start_balance
    log_net = t["net_pnl"]
    reconciled = abs(log_net - net) < max(0.01, abs(net) * 1e-6)
    t["trade_log_net_pnl"] = round(log_net, 4)
    out = {
        "start_balance": round(start_balance, 2),
        "final_balance": round(final, 2),
        "net_pnl": round(net, 2),
        "return_pct": round(net / start_balance * 100, 2) if start_balance else 0.0,
        "reconciled": reconciled,
    }
    out.update({k: v for k, v in t.items() if k not in ("net_pnl",)})
    out.update(e)
    return out


def robustness_score(summary: dict, min_trades: int = 20) -> float:
    """Score a backtest for *robustness*, not peak return.

    Rewards expectancy and sample size, punishes drawdown and thin samples.
    A strategy with 8 lucky trades should never outrank one with 80 steady
    ones, which is exactly what a raw-return ranking would do.
    """
    n = summary.get("total_trades", 0)
    if n <= 0:
        return -1e9
    exp_r = summary.get("expectancy_r", 0.0)
    dd = summary.get("max_drawdown_pct", 100.0)
    pf = summary.get("profit_factor", 0.0)
    if exp_r <= 0:
        return exp_r * 10  # losing strategies rank below everything
    sample_conf = min(1.0, n / max(1, min_trades))
    dd_penalty = 1.0 / (1.0 + dd / 10.0)
    pf_bonus = min(2.0, math.log1p(max(0.0, pf - 1.0)))
    return round(exp_r * sample_conf * math.sqrt(n) * dd_penalty * (1 + pf_bonus), 4)


# ── validation: walk-forward + optimisation ────────────────────────────────

DEFAULT_GRID = {
    "min_confidence": [50.0, 60.0, 70.0],
    "rr": [1.5, 2.0, 3.0],
    "sl_atr": [1.2, 1.5, 2.0],
}


def _slice_klines(klines: Sequence, start: int, end: int) -> list:
    """Slice a newest-first kline list by chronological position."""
    n = len(klines)
    # Input is newest-first (index 0 = newest). A chronological window
    # [start, end) maps to the newest-first slice [n-end, n-start).
    lo = max(0, n - end)
    hi = max(0, n - start)
    return list(klines)[lo:hi]


def optimize_strategy(
    klines: Sequence,
    grid: Optional[Dict[str, Iterable]] = None,
    risk_pct: float = 1.0,
    start_balance: float = 1000.0,
    fee_rate: float = 0.00055,
    slippage_bps: float = 2.0,
    engine=None,
    top_n: int = 5,
    **fixed,
) -> dict:
    """Grid-search parameters, ranked by `robustness_score`.

    This is an *in-sample* search. Treat the winner as a hypothesis and
    confirm it with `walk_forward()` before risking money.
    """
    from .backtest import run_backtest

    grid = grid or DEFAULT_GRID
    keys = list(grid.keys())
    combos = [{}]
    for k in keys:
        combos = [dict(c, **{k: v}) for c in combos for v in grid[k]]

    results = []
    for params in combos:
        kwargs = {**fixed, **params}
        try:
            res = run_backtest(
                klines,
                risk_pct=risk_pct,
                start_balance=start_balance,
                fee_rate=fee_rate,
                slippage_bps=slippage_bps,
                engine=engine,
                **kwargs,
            )
        except Exception as e:  # a bad combo must not kill the sweep
            results.append({"params": params, "error": str(e), "score": -1e9})
            continue
        s = res["summary"]
        results.append({
            "params": params,
            "score": robustness_score(s),
            "net_pnl": s["net_pnl"],
            "expectancy_r": s["expectancy_r"],
            "total_trades": s["total_trades"],
            "win_rate_pct": s["win_rate_pct"],
            "profit_factor": s["profit_factor"],
            "max_drawdown_pct": s["max_drawdown_pct"],
            "sharpe": s["sharpe"],
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return {
        "status": "ok",
        "combinations": len(combos),
        "ranking": results[:top_n],
        "best_params": results[0]["params"] if results else {},
        "warning": "In-sample optimisation. Confirm with walk_forward() before trading.",
    }


def walk_forward(
    klines: Sequence,
    folds: int = 4,
    train_frac: float = 0.7,
    grid: Optional[Dict[str, Iterable]] = None,
    risk_pct: float = 1.0,
    start_balance: float = 1000.0,
    fee_rate: float = 0.00055,
    slippage_bps: float = 2.0,
    engine=None,
    min_train_bars: int = 300,
    min_test_bars: int = 120,
    **fixed,
) -> dict:
    """Anchored walk-forward validation.

    Splits the series into `folds` sequential blocks. For each block the
    parameters are optimised on the first `train_frac` of the block and scored
    on the untouched remainder. The out-of-sample aggregate is the only number
    worth trusting; the in/out degradation ratio exposes overfitting.

    If the requested fold count leaves blocks too small to warm an indicator
    stack, the fold count is reduced automatically and reported in `notes`
    rather than silently returning zero usable folds.
    """
    from .backtest import run_backtest

    n = len(klines)
    if n < min_train_bars + min_test_bars:
        return {
            "status": "error",
            "msg": (
                f"need at least {min_train_bars + min_test_bars} candles for a "
                f"single fold, got {n}"
            ),
        }
    notes: List[str] = []
    folds = max(2, int(folds))
    while folds > 2:
        block = n // folds
        if block * train_frac >= min_train_bars and block * (1 - train_frac) >= min_test_bars:
            break
        folds -= 1
        notes.append(
            f"reduced to {folds} folds: {n} candles cannot warm {min_train_bars} "
            "train bars per fold at the requested fold count"
        )

    block = n // folds
    oos_rows: List[dict] = []
    is_exp: List[float] = []
    oos_exp: List[float] = []

    for f in range(folds):
        b_start = f * block
        b_end = n if f == folds - 1 else (f + 1) * block
        split = b_start + int((b_end - b_start) * train_frac)
        train = _slice_klines(klines, b_start, split)
        test = _slice_klines(klines, split, b_end)
        if len(train) < min_train_bars or len(test) < min_test_bars:
            continue

        warmup = min(220, max(60, len(train) // 3))
        opt = optimize_strategy(
            train, grid=grid, risk_pct=risk_pct, start_balance=start_balance,
            fee_rate=fee_rate, slippage_bps=slippage_bps, engine=engine,
            top_n=1, warmup=warmup, **fixed,
        )
        params = opt.get("best_params") or {}
        best_is = opt["ranking"][0] if opt.get("ranking") else {}
        try:
            oos = run_backtest(
                test, risk_pct=risk_pct, start_balance=start_balance,
                fee_rate=fee_rate, slippage_bps=slippage_bps, engine=engine,
                warmup=min(220, max(60, len(test) // 2)),
                **{**fixed, **params},
            )["summary"]
        except Exception as e:
            oos_rows.append({"fold": f, "error": str(e)})
            continue

        oos_rows.append({
            "fold": f,
            "params": params,
            "train_expectancy_r": best_is.get("expectancy_r"),
            "oos_expectancy_r": oos["expectancy_r"],
            "oos_net_pnl": oos["net_pnl"],
            "oos_trades": oos["total_trades"],
            "oos_win_rate_pct": oos["win_rate_pct"],
            "oos_max_drawdown_pct": oos["max_drawdown_pct"],
            "oos_sharpe": oos["sharpe"],
        })
        if best_is.get("expectancy_r") is not None:
            is_exp.append(best_is["expectancy_r"])
        oos_exp.append(oos["expectancy_r"])

    if not oos_rows:
        return {
            "status": "error",
            "msg": "no fold produced a result — supply more history",
            "candles": n,
            "folds": folds,
        }
    avg_is = sum(is_exp) / len(is_exp) if is_exp else 0.0
    avg_oos = sum(oos_exp) / len(oos_exp) if oos_exp else 0.0
    positive = sum(1 for r in oos_rows if (r.get("oos_expectancy_r") or 0) > 0)
    robust = avg_oos > 0 and positive >= max(2, math.ceil(len(oos_rows) * 0.6))
    return {
        "status": "ok",
        "candles": n,
        "folds": len(oos_rows),
        "avg_in_sample_expectancy_r": round(avg_is, 3),
        "avg_out_of_sample_expectancy_r": round(avg_oos, 3),
        "degradation_pct": round((1 - avg_oos / avg_is) * 100, 1) if avg_is > 0 else None,
        "positive_oos_folds": positive,
        "consistency_pct": round(positive / len(oos_rows) * 100, 1),
        "verdict": (
            "robust — out-of-sample expectancy holds up"
            if robust
            else "not validated — do not trade this live"
        ),
        "notes": notes,
        "rows": oos_rows,
    }


def portfolio_scan(
    klines_by_symbol: Dict[str, Sequence],
    risk_pct: float = 1.0,
    start_balance: float = 1000.0,
    fee_rate: float = 0.00055,
    slippage_bps: float = 2.0,
    engine=None,
    **params,
) -> dict:
    """Backtest several symbols and rank them by risk-adjusted quality."""
    from .backtest import run_backtest

    rows = []
    for sym, kl in klines_by_symbol.items():
        if not kl or len(kl) < 300:
            rows.append({"symbol": sym, "error": "not enough klines"})
            continue
        try:
            s = run_backtest(
                kl, risk_pct=risk_pct, start_balance=start_balance,
                fee_rate=fee_rate, slippage_bps=slippage_bps, engine=engine, **params,
            )["summary"]
        except Exception as e:
            rows.append({"symbol": sym, "error": str(e)})
            continue
        rows.append({
            "symbol": sym,
            "score": robustness_score(s),
            "net_pnl": s["net_pnl"],
            "expectancy_r": s["expectancy_r"],
            "total_trades": s["total_trades"],
            "win_rate_pct": s["win_rate_pct"],
            "profit_factor": s["profit_factor"],
            "sharpe": s["sharpe"],
            "max_drawdown_pct": s["max_drawdown_pct"],
        })
    rows.sort(key=lambda r: r.get("score", -1e9), reverse=True)
    return {
        "status": "ok",
        "ranked": rows,
        "best": rows[0]["symbol"] if rows and "score" in rows[0] else None,
        "note": "Ranked by robustness score (expectancy, sample size, drawdown).",
    }
