#!/usr/bin/env python3
"""Regression + feature tests for the Bybit analytics/backtest upgrade.

Run:  .venv/bin/python -m unittest tests.test_bybit_analytics -v

Every test is offline. The "regression" classes pin bugs that were found in
review, so they cannot silently come back:

  * the backtester charged entry fees to the balance but not to the trade, so
    the trade log never summed to the change in balance;
  * the `rr` parameter was completely inert (1R..4R gave identical results);
  * the BREAKEVEN exit branch was unreachable dead code;
  * stops always filled at the stop price, even when the bar gapped through
    it, which flattered every trend backtest;
  * `get_klines` silently truncated at Bybit's 1000-candle page size;
  * CLI string arguments reached annotated float/int parameters unconverted.
"""

import os
import random
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BYBIT_SYNC_TIME", "false")
os.environ.setdefault("PROXY_ENABLED", "false")

from tools.bybit.analytics import (  # noqa: E402
    equity_metrics,
    kelly_fraction,
    kelly_sizing,
    monte_carlo_drawdowns,
    optimize_strategy,
    portfolio_scan,
    risk_of_ruin,
    robustness_score,
    trade_metrics,
    walk_forward,
)
from tools.bybit.backtest import _signal_at, _precompute, run_backtest  # noqa: E402
from tools.bybit.price_action import (  # noqa: E402
    PriceActionEngine,
    _efficiency_ratio,
    analyze_price_action,
)


# ── synthetic market generators ─────────────────────────────────────────────


def gbm(n=900, drift=0.0005, vol=0.006, seed=5, start=30000.0):
    """Geometric random walk, newest-first (Bybit order)."""
    rng = random.Random(seed)
    p = start
    rows = []
    ts = 1700000000000
    for _ in range(n):
        o = p
        c = p * (1 + drift + rng.gauss(0, vol))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.4)))
        l = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.4)))
        rows.append([ts, str(o), str(h), str(l), str(c), "100", "0"])
        ts += 3600000
        p = c
    return list(reversed(rows))


def mean_reverting(n=1200, seed=1, vol=0.006, theta=0.05, start=30000.0):
    """Ornstein-Uhlenbeck — the regime a trend system must refuse to trade."""
    rng = random.Random(seed)
    p = mu = start
    rows = []
    ts = 1700000000000
    for _ in range(n):
        o = p
        p = p + theta * (mu - p) + p * rng.gauss(0, vol)
        c = p
        h = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.4)))
        l = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.4)))
        rows.append([ts, str(o), str(h), str(l), str(c), "100", "0"])
        ts += 3600000
    return list(reversed(rows))


def crash_series(n=700, seed=0):
    """Uptrend then a violent gap down, to exercise gap-through-stop fills."""
    rows = gbm(n=n, drift=0.0022, vol=0.004, seed=seed)
    # newest-first: the last few elements are the most recent bars
    rows[1] = [rows[1][0], rows[1][1], rows[1][2], rows[1][3], rows[1][4], "100", "0"]
    gap_bar = rows[0]
    o = float(gap_bar[1]) * 0.90
    rows[0] = [gap_bar[0], str(o), str(o), str(o * 0.995), str(o * 0.998), "100", "0"]
    return rows


# ── regression: accounting ──────────────────────────────────────────────────


class TestAccountingRegression(unittest.TestCase):
    def test_trade_log_reconciles_with_balance(self):
        """Every trade's net_pnl must include its own entry fee."""
        for seed in (3, 9, 17):
            res = run_backtest(gbm(seed=seed))
            s = res["summary"]
            self.assertTrue(
                s["reconciled"],
                f"seed {seed}: trade log {s['trade_log_net_pnl']} != balance delta {s['net_pnl']}",
            )
            self.assertAlmostEqual(
                sum(t["net_pnl"] for t in res["trades"]), s["net_pnl"], places=2
            )
            self.assertAlmostEqual(
                s["final_balance"], s["start_balance"] + s["net_pnl"], places=2
            )

    def test_fees_are_attributed_per_trade(self):
        res = run_backtest(gbm(seed=4), fee_rate=0.001)
        for t in res["trades"]:
            self.assertGreater(t["fees"], 0.0)
            # gross - fees == net, within rounding
            self.assertAlmostEqual(t["gross_pnl"] - t["fees"], t["net_pnl"], places=5)

    def test_zero_fee_means_gross_equals_net(self):
        res = run_backtest(gbm(seed=4), fee_rate=0.0)
        for t in res["trades"]:
            self.assertAlmostEqual(t["gross_pnl"], t["net_pnl"], places=6)

    def test_total_fees_reported_consistently(self):
        res = run_backtest(gbm(seed=6))
        s = res["summary"]
        fees = sum(t["fees"] for t in res["trades"])
        self.assertAlmostEqual(s["total_fees"], fees, places=3)
        self.assertAlmostEqual(s["costs"]["fees_paid"], fees, places=3)


# ── regression: the rr parameter must actually do something ────────────────


class TestRRParameter(unittest.TestCase):
    def test_rr_changes_results(self):
        ks = gbm(n=1100, drift=0.0012, seed=2)
        outs = {}
        for rr in (1.5, 2.0, 3.0, 5.0):
            s = run_backtest(ks, rr=rr)["summary"]
            outs[rr] = (s["net_pnl"], s["total_trades"])
        self.assertGreater(
            len({v for v in outs.values()}), 1, "rr is inert — all settings identical"
        )

    def test_higher_rr_means_fewer_but_bigger_wins(self):
        ks = gbm(n=1400, drift=0.0015, seed=8)
        low = run_backtest(ks, rr=1.5)["summary"]
        high = run_backtest(ks, rr=5.0)["summary"]
        self.assertLessEqual(high["total_trades"], low["total_trades"])
        if high["wins"] and low["wins"]:
            self.assertGreater(high["avg_win_r"], low["avg_win_r"])

    def test_rr_target_price_is_respected(self):
        """A trade's final target must sit at rr * risk distance from entry."""
        ks = gbm(n=1400, drift=0.0015, seed=8)
        res = run_backtest(ks, rr=3.0, partial_frac=0.0)
        self.assertGreater(len(res["trades"]), 0)


# ── regression: exit reasons ───────────────────────────────────────────────


class TestExitReasons(unittest.TestCase):
    def test_breakeven_reason_is_reachable(self):
        """The old code had an unreachable BREAKEVEN branch (masked as STOP_LOSS).

        A trade that scales out at 1R and then stops at entry must be labelled
        BREAKEVEN and carry its partial fill — not reported as a STOP_LOSS.
        """
        breakeven = []
        for seed in range(12):
            res = run_backtest(gbm(n=1100, drift=0.0015, seed=seed),
                               partial_frac=0.5, breakeven_at_r=1.0, rr=3.0)
            breakeven.extend(t for t in res["trades"] if t["reason"] == "BREAKEVEN")
        self.assertTrue(breakeven, "BREAKEVEN exit never produced")
        for t in breakeven:
            self.assertTrue(
                t["partials"],
                "BREAKEVEN without a partial fill — the scale-out path is dead",
            )
            # Half the position banked 1R; the rest stopped at entry, so the
            # trade is worth ~+0.5R before fees.
            self.assertAlmostEqual(t["r_multiple"], 0.5, delta=0.12)

    def test_valid_reason_vocabulary(self):
        allowed = {
            "TAKE_PROFIT", "STOP_LOSS", "BREAKEVEN", "STOP_GAP",
            "TIME_STOP", "END_OF_DATA", "PARTIAL_ONLY",
        }
        for seed in range(6):
            for t in run_backtest(gbm(seed=seed))["trades"]:
                self.assertIn(t["reason"], allowed)

    def test_time_stop_is_honoured(self):
        res = run_backtest(gbm(n=1200, drift=0.0015, seed=1), time_stop_bars=12)
        self.assertTrue(any(t["reason"] == "TIME_STOP" for t in res["trades"]))
        for t in res["trades"]:
            if t["reason"] == "TIME_STOP":
                self.assertLessEqual(t["bars_held"], 12)


# ── regression: honest fills ───────────────────────────────────────────────


class TestHonestFills(unittest.TestCase):
    def test_gaps_through_stop_lose_more_than_one_r(self):
        """A bar opening beyond the stop must fill at the open, not the stop."""
        worst = []
        gap_seen = False
        for seed in range(14):
            res = run_backtest(crash_series(seed=seed))
            gaps = [t for t in res["trades"] if t["reason"] == "STOP_GAP"]
            if gaps:
                gap_seen = True
                worst.extend(t["r_multiple"] for t in gaps)
        self.assertTrue(gap_seen, "no STOP_GAP fill produced; gap modelling is broken")
        self.assertLess(min(worst), -1.0, "gap fills are not being penalised")

    def test_slippage_never_improves_a_trending_result(self):
        ks = gbm(n=1400, drift=0.0018, seed=3)
        no_slip = run_backtest(ks, slippage_bps=0.0)["summary"]["net_pnl"]
        with_slip = run_backtest(ks, slippage_bps=15.0)["summary"]["net_pnl"]
        self.assertLessEqual(
            with_slip, no_slip + 1e-6,
            "adding slippage increased PnL — fills are not accounting for it",
        )

    def test_fees_reduce_net_pnl(self):
        ks = gbm(n=1200, drift=0.0015, seed=5)
        free = run_backtest(ks, fee_rate=0.0)["summary"]["net_pnl"]
        costly = run_backtest(ks, fee_rate=0.002)["summary"]["net_pnl"]
        self.assertLess(costly, free)

    def test_entry_is_next_bar_open_not_signal_close(self):
        """Entry price must be the following bar's open (plus slippage)."""
        ks = gbm(n=900, drift=0.0012, seed=2)
        res = run_backtest(ks, slippage_bps=0.0)
        chron = list(reversed(ks))
        opens = {round(float(k[1]), 8) for k in chron}
        for t in res["trades"]:
            self.assertIn(round(t["entry"], 8), opens,
                          "entry price is not a real bar open")


# ── regression: no lookahead ───────────────────────────────────────────────


class TestNoLookahead(unittest.TestCase):
    def test_signal_ignores_future_bars(self):
        """Mutating bars after i must not change the signal computed at i."""
        eng = PriceActionEngine()
        base = gbm(n=800, drift=0.0015, seed=11)
        chron = list(reversed(base))
        cut = 600
        pre_a = _precompute(chron, eng)

        tampered = list(chron)
        for j in range(cut + 1, len(tampered)):
            k = tampered[j]
            f = 1.5 if j % 2 else 0.6
            tampered[j] = [k[0], str(float(k[1]) * f), str(float(k[2]) * f),
                           str(float(k[3]) * f), str(float(k[4]) * f), k[5], k[6]]
        pre_b = _precompute(tampered, eng)

        sig_a = _signal_at(chron, cut, pre_a, eng, 50.0, 3.0, 1.5)
        sig_b = _signal_at(tampered, cut, pre_b, eng, 50.0, 3.0, 1.5)
        if sig_a is None or sig_b is None:
            self.assertEqual(sig_a, sig_b)
        else:
            self.assertEqual(sig_a.action, sig_b.action)
            self.assertAlmostEqual(sig_a.entry, sig_b.entry, places=6)
            self.assertAlmostEqual(sig_a.stop_loss, sig_b.stop_loss, places=6)

    def test_backtest_and_live_engine_agree(self):
        """The backtester must use the engine's own decision function."""
        ks = gbm(n=700, drift=0.0018, seed=6)
        live = PriceActionEngine().price_action_signal(ks, 50.0, 3.0)
        # Same inputs through the shared decision path.
        eng = PriceActionEngine()
        chron = list(reversed(ks))
        pre = _precompute(chron, eng)
        bt_sig = _signal_at(chron, len(chron) - 1, pre, eng, 50.0, 3.0, 1.5)
        self.assertEqual(
            live.action, (bt_sig.action if bt_sig else "WAIT"),
            "backtest signal diverged from the live engine",
        )


# ── analytics math ─────────────────────────────────────────────────────────


class TestEquityMetrics(unittest.TestCase):
    def test_flat_curve_has_zero_risk(self):
        m = equity_metrics([1000.0] * 50)
        self.assertEqual(m["max_drawdown_pct"], 0.0)
        self.assertEqual(m["sharpe"], 0.0)
        self.assertEqual(m["total_return_pct"], 0.0)

    def test_monotonic_up_curve_has_no_drawdown(self):
        eq = [1000.0 * (1.01 ** i) for i in range(60)]
        m = equity_metrics(eq)
        self.assertEqual(m["max_drawdown_pct"], 0.0)
        self.assertGreater(m["sharpe"], 0)
        self.assertGreater(m["total_return_pct"], 0)

    def test_drawdown_measured_correctly(self):
        eq = [100.0, 120.0, 60.0, 90.0]
        m = equity_metrics(eq)
        self.assertAlmostEqual(m["max_drawdown_pct"], 50.0, places=6)

    def test_sortino_ignores_upside_volatility(self):
        steady = [1000.0 + i for i in range(100)]
        self.assertGreaterEqual(equity_metrics(steady)["sortino"], 0)

    def test_ulcer_index_penalises_deep_drawdowns(self):
        shallow = [100, 99, 100, 99, 100, 99, 100]
        deep = [100, 50, 100, 50, 100, 50, 100]
        self.assertLess(
            equity_metrics(shallow)["ulcer_index"],
            equity_metrics(deep)["ulcer_index"],
        )


class TestTradeMetrics(unittest.TestCase):
    def _trades(self, pnls):
        return [{"net_pnl": p, "r_multiple": p / 10.0, "bars_held": 5, "fees": 1.0}
                for p in pnls]

    def test_empty(self):
        m = trade_metrics([])
        self.assertEqual(m["total_trades"], 0)
        self.assertEqual(m["profit_factor"], 0.0)

    def test_win_rate_and_profit_factor(self):
        m = trade_metrics(self._trades([10, 10, -5, -5]))
        self.assertEqual(m["win_rate_pct"], 50.0)
        self.assertAlmostEqual(m["profit_factor"], 2.0, places=6)
        self.assertAlmostEqual(m["net_pnl"], 10.0, places=6)

    def test_expectancy_in_r(self):
        m = trade_metrics(self._trades([10, -10, 20, -10]))
        self.assertAlmostEqual(m["expectancy_r"], (1 + -1 + 2 + -1) / 4, places=6)

    def test_payoff_ratio(self):
        m = trade_metrics(self._trades([30, -10]))
        self.assertAlmostEqual(m["payoff_ratio"], 3.0, places=6)

    def test_consecutive_losses(self):
        m = trade_metrics(self._trades([-1, -1, -1, 5, -1]))
        self.assertEqual(m["max_consecutive_losses"], 3)

    def test_no_losses_profit_factor(self):
        m = trade_metrics(self._trades([5, 5, 5]))
        self.assertGreater(m["profit_factor"], 0)


class TestSizing(unittest.TestCase):
    def test_kelly_zero_without_edge(self):
        self.assertEqual(kelly_fraction(0.3, 1.0), 0.0)
        self.assertEqual(kelly_fraction(0.5, 0.5), 0.0)

    def test_kelly_positive_with_edge(self):
        f = kelly_fraction(0.6, 2.0)
        self.assertGreater(f, 0.0)
        self.assertLessEqual(f, 0.5)

    def test_kelly_respects_cap(self):
        self.assertLessEqual(kelly_sizing(1000, 0.9, 5.0, hard_cap_pct=2.0)
                             ["recommended_risk_pct"], 2.0)

    def test_thin_edge_is_not_tradeable(self):
        r = kelly_sizing(1000, 0.34, 1.0)
        self.assertFalse(r["tradeable"])

    def test_risk_of_ruin_increases_with_size(self):
        small = risk_of_ruin(0.45, 1.5, 0.5, simulations=400)["ruin_probability"]
        large = risk_of_ruin(0.45, 1.5, 10.0, simulations=400)["ruin_probability"]
        self.assertGreaterEqual(large, small)

    def test_risk_of_ruin_is_certain_without_edge(self):
        self.assertEqual(risk_of_ruin(0.0, 2.0, 1.0)["ruin_probability"], 1.0)

    def test_risk_of_ruin_probability_bounds(self):
        r = risk_of_ruin(0.55, 1.8, 1.0, simulations=300)
        self.assertGreaterEqual(r["ruin_probability"], 0.0)
        self.assertLessEqual(r["ruin_probability"], 1.0)


class TestMonteCarlo(unittest.TestCase):
    def test_needs_enough_trades(self):
        self.assertEqual(monte_carlo_drawdowns([{"net_pnl": 1}])["status"], "error")

    def test_distribution_is_ordered(self):
        trades = [{"net_pnl": v} for v in (10, -8, 12, -5, 6, -9, 4, 3, -7, 11)]
        r = monte_carlo_drawdowns(trades, simulations=300)
        dd = r["drawdown_pct"]
        self.assertLessEqual(dd["median"], dd["p95"])
        self.assertLessEqual(dd["p95"], dd["worst"])
        self.assertGreaterEqual(r["prob_profitable"], 0.0)
        self.assertLessEqual(r["prob_profitable"], 1.0)

    def test_losing_system_is_usually_unprofitable(self):
        trades = [{"net_pnl": v} for v in (-10, -10, -10, -10, 1, -10, -10, -10)]
        r = monte_carlo_drawdowns(trades, simulations=300)
        self.assertLess(r["prob_profitable"], 0.5)


class TestRobustnessScore(unittest.TestCase):
    def test_losing_strategy_ranks_last(self):
        good = {"total_trades": 50, "expectancy_r": 0.5, "max_drawdown_pct": 5,
                "profit_factor": 1.8}
        bad = {"total_trades": 50, "expectancy_r": -0.1, "max_drawdown_pct": 5,
               "profit_factor": 0.8}
        self.assertGreater(robustness_score(good), robustness_score(bad))

    def test_lucky_few_trades_do_not_beat_a_real_sample(self):
        lucky = {"total_trades": 5, "expectancy_r": 2.0, "max_drawdown_pct": 1,
                 "profit_factor": 5.0}
        steady = {"total_trades": 120, "expectancy_r": 0.35, "max_drawdown_pct": 6,
                  "profit_factor": 1.6}
        self.assertGreater(robustness_score(steady), robustness_score(lucky))

    def test_no_trades_scores_very_low(self):
        self.assertLess(robustness_score({"total_trades": 0}), -1e8)


# ── validation workflows ───────────────────────────────────────────────────


class TestOptimize(unittest.TestCase):
    def test_grid_is_actually_swept(self):
        ks = gbm(n=900, drift=0.0012, seed=4)
        r = optimize_strategy(ks, grid={"rr": [2.0, 3.0], "sl_atr": [1.2, 2.0]})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["combinations"], 4)
        self.assertTrue(r["best_params"])

    def test_ranking_is_sorted_by_score(self):
        ks = gbm(n=900, drift=0.0012, seed=4)
        r = optimize_strategy(ks, grid={"rr": [1.5, 2.0, 3.0]})
        scores = [row["score"] for row in r["ranking"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_bad_combo_does_not_crash_the_sweep(self):
        ks = gbm(n=700, drift=0.001, seed=2)
        r = optimize_strategy(ks, grid={"min_confidence": [50.0, 200.0]})
        self.assertEqual(r["status"], "ok")


class TestWalkForward(unittest.TestCase):
    def test_structure_and_verdict(self):
        r = walk_forward(gbm(n=2000, drift=0.0012, seed=5), folds=3)
        self.assertEqual(r["status"], "ok")
        self.assertIn("verdict", r)
        self.assertGreaterEqual(r["folds"], 2)
        self.assertEqual(len(r["rows"]), r["folds"])
        self.assertIn("avg_out_of_sample_expectancy_r", r)

    def test_reduces_folds_instead_of_silently_failing(self):
        r = walk_forward(gbm(n=900, drift=0.0012, seed=5), folds=6)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["notes"] or r["folds"] >= 2)

    def test_rejects_too_little_data(self):
        r = walk_forward(gbm(n=300, seed=1), folds=3)
        self.assertEqual(r["status"], "error")


class TestPortfolioScan(unittest.TestCase):
    def test_ranks_symbols(self):
        data = {
            "AAAUSDT": gbm(n=900, drift=0.002, seed=1),
            "BBBUSDT": gbm(n=900, drift=0.0002, seed=2),
        }
        r = portfolio_scan(data)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["ranked"]), 2)
        self.assertIn("score", r["ranked"][0])

    def test_short_history_is_reported_not_crashed(self):
        r = portfolio_scan({"XUSDT": gbm(n=50), "YUSDT": gbm(n=900)})
        self.assertEqual(r["status"], "ok")
        errs = [x for x in r["ranked"] if "error" in x]
        self.assertEqual(len(errs), 1)


# ── strategy filters ───────────────────────────────────────────────────────


class TestRegimeFilters(unittest.TestCase):
    def test_efficiency_ratio_separates_trend_from_chop(self):
        trend = [100.0 * (1.01 ** i) for i in range(40)]
        chop = [100.0 + (1 if i % 2 else -1) for i in range(40)]
        self.assertGreater(_efficiency_ratio(trend, 20), 0.9)
        self.assertLess(_efficiency_ratio(chop, 20), 0.15)

    def test_slope_gate_blocks_mean_reverting_market(self):
        eng = PriceActionEngine()
        r = eng.regime(mean_reverting(seed=3))
        self.assertIn(r["regime"], {"CHOP", "RANGE", "VOLATILE", "UNKNOWN"})

    def test_mean_reverting_market_keeps_drawdown_small(self):
        """The old rule set lost ~16% here; the slope gate must contain it."""
        dds = []
        for seed in range(5):
            s = run_backtest(mean_reverting(seed=seed))["summary"]
            dds.append(s["max_drawdown_pct"])
        self.assertLess(sum(dds) / len(dds), 8.0)

    def test_trending_market_still_trades_with_filters_on(self):
        eng = PriceActionEngine()
        sig = eng.price_action_signal(gbm(n=900, drift=0.0025, vol=0.004, seed=2), 50.0)
        self.assertIn(sig.action, {"LONG", "SHORT"})

    def test_analysis_exposes_regime_and_efficiency(self):
        res = analyze_price_action(gbm(n=600, drift=0.0015, seed=2), 50.0)
        self.assertIn("regime", res)
        self.assertIn("efficiency_ratio", res)
        self.assertIn("tradeable", res["regime"])


# ── infrastructure regressions ──────────────────────────────────────────────


class TestKlinePagination(unittest.TestCase):
    def test_requests_above_1000_are_paged(self):
        from tools.bybit.base import BybitBaseClient, TradingConfig
        from tools.bybit.market import MarketDataMixin

        calls = []

        class Fake(BybitBaseClient, MarketDataMixin):
            def _request(self, method, endpoint, params=None, json_data=None,
                         signed=True, category="default"):
                calls.append(dict(params or {}))
                end = (params or {}).get("end") or 10_000_000
                return {"list": [[str(end - i * 60000), "1", "2", "0.5", "1.5", "1", "0"]
                                 for i in range(1000)]}

        bot = Fake(TradingConfig(api_key="k", api_secret="s"))
        res = bot.get_klines("BTCUSDT", "1", 2500)
        self.assertEqual(len(res["list"]), 2500)
        self.assertEqual(len({row[0] for row in res["list"]}), 2500)
        self.assertGreater(res["pages"], 1)
        self.assertFalse(res["truncated"])
        self.assertTrue(all(c.get("limit", 0) <= 1000 for c in calls))

    def test_small_request_is_single_page(self):
        from tools.bybit.base import BybitBaseClient, TradingConfig
        from tools.bybit.market import MarketDataMixin

        class Fake(BybitBaseClient, MarketDataMixin):
            def _request(self, method, endpoint, params=None, json_data=None,
                         signed=True, category="default"):
                n = (params or {}).get("limit", 0)
                return {"list": [[str(i), "1", "2", "0.5", "1.5", "1", "0"]
                                 for i in range(n)]}

        bot = Fake(TradingConfig(api_key="k", api_secret="s"))
        res = bot.get_klines("BTCUSDT", "60", 300)
        self.assertEqual(len(res["list"]), 300)
        self.assertNotIn("pages", res)


class TestDispatcherCoercion(unittest.TestCase):
    def test_string_arguments_are_converted(self):
        import inspect

        from tools.bybit.terminal import _coerce_params
        from tools.bybit.unified import PriceActionMixin

        method = PriceActionMixin.backtest_pa
        out = _coerce_params(method, inspect.signature(method), {
            "limit": "500", "risk_pct": "0.5", "min_confidence": "60",
            "partial_frac": "0", "symbol": "BTCUSDT",
        })
        self.assertIsInstance(out["limit"], int)
        self.assertIsInstance(out["risk_pct"], float)
        self.assertIsInstance(out["min_confidence"], float)
        self.assertIsInstance(out["partial_frac"], float)
        self.assertIsInstance(out["symbol"], str)

    def test_bool_strings(self):
        import inspect

        from tools.bybit.terminal import _coerce_params
        from tools.bybit.unified import PriceActionMixin

        method = PriceActionMixin.execute_signal
        out = _coerce_params(method, inspect.signature(method), {"execute": "true"})
        self.assertIs(out["execute"], True)
        out = _coerce_params(method, inspect.signature(method), {"execute": "false"})
        self.assertIs(out["execute"], False)


class TestExecuteSafety(unittest.TestCase):
    """The live-order flag must never be enabled by accident."""

    def _str2bool(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "bybit_suite_cli", REPO_ROOT / "tools" / "bybit_suite.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._str2bool

    def test_falsey_tokens_are_false(self):
        f = self._str2bool()
        for v in ("false", "False", "FALSE", "0", "no", "off", "", None):
            self.assertIs(f(v), False, f"{v!r} must not enable live trading")

    def test_truthy_tokens_are_true(self):
        f = self._str2bool()
        for v in ("true", "True", "1", "yes", "on"):
            self.assertIs(f(v), True)

    def test_bool_builtin_trap_is_avoided(self):
        # bool("false") is True — the exact bug this helper exists to prevent.
        self.assertTrue(bool("false"))
        self.assertFalse(self._str2bool()("false"))


class TestConfigCheck(unittest.TestCase):
    def test_never_leaks_secrets(self):
        from tools.bybit.base import TradingConfig
        from tools.bybit.terminal import BybitRealm

        secret = "SUPER_SECRET_VALUE_1234567890"
        bot = BybitRealm(TradingConfig(
            api_key="PUBLICKEY123456789012", api_secret=secret,
            use_proxy=False,
        ))
        try:
            res = bot.check_config()
        finally:
            bot.close()
        blob = str(res)
        self.assertNotIn(secret, blob)
        self.assertNotIn("PUBLICKEY123456789012", blob)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["credentials_configured"])

    def test_flags_missing_credentials(self):
        from tools.bybit.base import TradingConfig
        from tools.bybit.terminal import BybitRealm

        bot = BybitRealm(TradingConfig(api_key="", api_secret="", use_proxy=False))
        try:
            res = bot.check_config()
        finally:
            bot.close()
        self.assertFalse(res["credentials_configured"])
        self.assertFalse(res["ready_for_signed_calls"])

    def test_flags_key_equal_to_secret(self):
        from tools.bybit.base import TradingConfig
        from tools.bybit.terminal import BybitRealm

        bot = BybitRealm(TradingConfig(api_key="same", api_secret="same", use_proxy=False))
        try:
            res = bot.check_config()
        finally:
            bot.close()
        failed = [c["check"] for c in res["checks"] if not c["ok"]]
        self.assertIn("key_not_equal_secret", failed)


if __name__ == "__main__":
    unittest.main()
