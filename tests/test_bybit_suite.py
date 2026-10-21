#!/usr/bin/env python3
"""Offline test suite for the unified Bybit trading package.

Run:  .venv/bin/python -m unittest tests.test_bybit_suite -v
      (or `python -m unittest discover tests`)

All tests run without network access. BYBIT_SYNC_TIME=false is set so the
client constructor never attempts a live time sync.
"""

import io
import json
import os
import random
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BYBIT_SYNC_TIME", "false")
os.environ.setdefault("PROXY_ENABLED", "false")

from tools.bybit.base import BybitBaseClient, TradingConfig  # noqa: E402
from tools.bybit.market import MarketDataMixin  # noqa: E402
from tools.bybit.price_action import (  # noqa: E402
    PriceActionEngine,
    Signal,
    analyze_price_action,
)
from tools.bybit.backtest import run_backtest  # noqa: E402
from utils.bybit_base import calculate_exit_price, calculate_pnl  # noqa: E402


# ── synthetic candle helpers ────────────────────────────────────────────────


def make_klines(
    drift_fn=lambda i: 0.0,
    start: float = 30000.0,
    n: int = 400,
    seed: int = 7,
    noise_pct: float = 0.004,
) -> list:
    """Generate a random-walk-with-drift OHLCV series (newest-first, Bybit fmt)."""
    rng = random.Random(seed)
    price = start
    klines = []
    ts = 1700000000000
    for i in range(n):
        drift = drift_fn(i)
        noise = price * noise_pct
        o = price + rng.uniform(-noise, noise)
        c = price + drift + rng.uniform(-noise, noise)
        h = max(o, c) + rng.uniform(0, noise * 0.6)
        l = min(o, c) - rng.uniform(0, noise * 0.6)
        v = 100 + rng.uniform(0, 50)
        klines.append([ts, str(o), str(h), str(l), str(c), str(v), "0"])
        ts += 3600 * 1000
        price = c
    return list(reversed(klines))  # newest first


def uptrend(n=400):
    return make_klines(lambda i: 45 if i < n // 2 else 120, start=30000, n=n)


def downtrend(n=400):
    return make_klines(lambda i: -60, start=60000, n=n)


def flat(n=400):
    return make_klines(lambda i: 0, start=50000, n=n)


# ── test cases ──────────────────────────────────────────────────────────────


class TestPnLMath(unittest.TestCase):
    def test_long_pnl(self):
        res = calculate_pnl(100.0, 110.0, 1.0, "Buy", fees=0.0, leverage=1.0)
        self.assertAlmostEqual(res["net_pnl"], 10.0)
        self.assertAlmostEqual(res["roe_pct"], 10.0)

    def test_short_pnl(self):
        res = calculate_pnl(100.0, 90.0, 1.0, "Sell", fees=0.0, leverage=5.0)
        self.assertAlmostEqual(res["net_pnl"], 10.0)
        # margin = 100*1/5 = 20 -> ROE 50%
        self.assertAlmostEqual(res["roe_pct"], 50.0)

    def test_exit_price_for_target(self):
        # long: need exit s.t. (exit-100)*1 - 0 = 5 -> 105
        self.assertAlmostEqual(calculate_exit_price(100, 5, 1, "Buy", 0), 105.0)
        # short: (exit-100)*-1 = 5 -> exit = 95
        self.assertAlmostEqual(calculate_exit_price(100, 5, 1, "Sell", 0), 95.0)


class TestSigningSecurity(unittest.TestCase):
    def test_sign_does_not_leak_key(self):
        cfg = TradingConfig()
        cfg.api_key = "SECRETKEY123"
        cfg.api_secret = "SECRETSECRET456"
        client = BybitBaseClient(cfg)
        buf = io.StringIO()
        with redirect_stderr(buf):
            sig = client._sign("{}", "1700000000000")
        self.assertTrue(sig)
        self.assertNotIn("SECRETKEY123", buf.getvalue())
        self.assertNotIn("SECRETSECRET456", buf.getvalue())

    def test_signed_request_without_credentials_is_gated(self):
        cfg = TradingConfig()
        cfg.api_key = ""
        cfg.api_secret = ""
        client = BybitBaseClient(cfg)
        res = client._request("GET", "/v5/account/info", {}, signed=True)
        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("code"), "NO_CREDENTIALS")


class TestIndicators(unittest.TestCase):
    def setUp(self):
        self.mix = MarketDataMixin()

    def test_ema_on_known_series(self):
        # EMA(2) of [1, 2, 3, 4] with k=2/3: values 1, 1.6667, 2.5556, 3.5185
        klines = [
            [0, "4", "4", "4", "4", "1", "0"],
            [0, "3", "3", "3", "3", "1", "0"],
            [0, "2", "2", "2", "2", "1", "0"],
            [0, "1", "1", "1", "1", "1", "0"],
        ]
        self.mix.get_klines = lambda *a, **k: {"list": klines}
        res = self.mix.calculate_ema("T", "60", 2)
        self.assertAlmostEqual(res["ema"], 3.5185, places=3)

    def test_rsi_extremes(self):
        up = [[0, str(p), str(p), str(p), str(p), "1", "0"] for p in range(30, 0, -1)]
        self.mix.get_klines = lambda *a, **k: {"list": up}
        self.assertAlmostEqual(self.mix.calculate_rsi("T", "60")["rsi"], 100.0, places=1)

    def test_supertrend_direction(self):
        self.mix.get_klines = lambda *a, **k: {"list": uptrend(120)}
        up = self.mix.calculate_supertrend("T", "60")
        self.assertEqual(up["trend"], "Up")
        self.mix.get_klines = lambda *a, **k: {"list": downtrend(120)}
        dn = self.mix.calculate_supertrend("T", "60")
        self.assertEqual(dn["trend"], "Down")

    def test_orderbook_normalization(self):
        # unwrapped shape (what the base client returns)
        unwrapped = {"b": [["100", "2"], ["99", "3"]], "a": [["101", "1"], ["102", "4"]]}
        bids, asks = self.mix._orderbook_levels(unwrapped)
        self.assertEqual(bids, [(100.0, 2.0), (99.0, 3.0)])
        self.assertEqual(asks, [(101.0, 1.0), (102.0, 4.0)])
        # nested shape must also work
        nested = {"result": unwrapped}
        bids2, asks2 = self.mix._orderbook_levels(nested)
        self.assertEqual(bids2, bids)
        self.assertEqual(asks2, asks)
        # garbage must not raise
        self.assertEqual(self.mix._orderbook_levels({}), ([], []))


class TestPriceActionEngine(unittest.TestCase):
    def setUp(self):
        self.eng = PriceActionEngine()

    def test_structure_classification(self):
        up = self.eng.market_structure(uptrend())
        self.assertEqual(up["state"], "UPTREND")
        dn = self.eng.market_structure(downtrend())
        self.assertEqual(dn["state"], "DOWNTREND")

    def test_signal_in_trend(self):
        s_up = self.eng.price_action_signal(uptrend(), min_confidence=50)
        self.assertEqual(s_up.action, "LONG")
        self.assertGreater(s_up.stop_loss, 0)
        self.assertLess(s_up.stop_loss, s_up.entry)  # SL below entry for longs
        self.assertEqual(len(s_up.take_profits), 3)

        s_dn = self.eng.price_action_signal(downtrend(), min_confidence=50)
        self.assertEqual(s_dn.action, "SHORT")
        self.assertGreater(s_dn.stop_loss, s_dn.entry)

    def test_flat_market_signal_gated(self):
        s = self.eng.price_action_signal(flat(), min_confidence=50)
        self.assertEqual(s.action, "WAIT")

    def test_analysis_wrapper(self):
        res = analyze_price_action(uptrend(300), min_confidence=50)
        self.assertEqual(res["status"], "ok")
        self.assertIn("structure", res)
        self.assertIn("zones", res)

    def test_trade_plan_sizing(self):
        signal = Signal("LONG", 72, 30000, 29700, [30300, 30600, 30900], 2.0,
                        "TREND_CONTINUATION")
        plan = self.eng.trade_plan(signal, balance=1000.0, risk_pct=1.0, leverage=1.0)
        self.assertEqual(plan["status"], "ok")
        # risk $10 over $300 stop distance -> qty 0.0333
        self.assertAlmostEqual(plan["qty"], 10 / 300, places=6)
        self.assertAlmostEqual(plan["risk_amount"], 10.0, places=2)

    def test_trade_plan_wait_is_no_trade(self):
        signal = Signal("WAIT", 0, 30000, 30000, [], 0, "NONE")
        plan = self.eng.trade_plan(signal, balance=1000.0)
        self.assertEqual(plan["status"], "no_trade")


class TestBacktest(unittest.TestCase):
    def test_trending_market_is_profitable(self):
        res = run_backtest(uptrend(), risk_pct=1.0, rr=2.0, sl_atr=1.5)
        s = res["summary"]
        self.assertGreater(s["total_trades"], 0)
        self.assertGreater(s["net_pnl"], 0)
        self.assertGreater(s["profit_factor"], 1.0)
        self.assertLess(s["max_drawdown_pct"], 10.0)  # 1% risk per trade

    def test_downtrend_market(self):
        res = run_backtest(downtrend(), risk_pct=1.0, rr=2.0, sl_atr=1.5)
        s = res["summary"]
        self.assertGreater(s["net_pnl"], 0)
        self.assertLess(s["max_drawdown_pct"], 10.0)

    def test_flat_market_losses_bounded(self):
        res = run_backtest(flat(), risk_pct=1.0, rr=2.0, sl_atr=1.5)
        s = res["summary"]
        # chop may lose a little, but never an uncontrolled drawdown
        self.assertLess(s["max_drawdown_pct"], 10.0)
        self.assertGreater(s["final_balance"], 800)


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        # Fixture _request: serve klines for market endpoints only
        fixture_klines = uptrend(300)
        self.orig_request = BybitBaseClient._request

        def fake_request(self, method, endpoint, params=None, json_data=None,
                         signed=True, category="default"):
            if "kline" in endpoint:
                return {"list": fixture_klines}
            return {"status": "error", "msg": "unmocked endpoint: " + endpoint}

        BybitBaseClient._request = fake_request

    def tearDown(self):
        BybitBaseClient._request = self.orig_request

    def test_run_price_action_analysis(self):
        from tools.bybit.terminal import run as realm_run

        res = realm_run(action="price_action_analysis", symbol="BTCUSDT",
                        interval="60", limit=300, min_confidence=50)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["symbol"], "BTCUSDT")
        self.assertEqual(res["signal"]["action"], "LONG")

    def test_run_backtest_pa(self):
        from tools.bybit.terminal import run as realm_run

        res = realm_run(action="backtest_pa", symbol="BTCUSDT", interval="60",
                        limit=300, min_confidence=50)
        self.assertEqual(res["status"], "ok")
        self.assertGreater(res["summary"]["net_pnl"], 0)

    def test_unknown_action(self):
        from tools.bybit.terminal import run as realm_run

        res = realm_run(action="definitely_not_an_action")
        self.assertEqual(res["status"], "error")


class TestMicroProfitAndBybitLiveHardening(unittest.TestCase):
    def test_micro_profit_xrp_and_link_directional_sl_and_affordable_sizing(self):
        from tools import micro_profit

        # XRPUSDT with 0.1106 USDT balance and 100x requested leverage (clamped to 75x)
        xrp = micro_profit.calculate_micro_profit(
            symbol="XRPUSDT",
            side="buy",
            balance=0.1106,
            leverage=100,
            target_profit=0.05,
            max_risk=0.025,
            bids=[[1.3320, 5000], [1.3319, 8000]],
            asks=[[1.3321, 4500], [1.3322, 6000]],
        )
        self.assertEqual(xrp["status"], "success")
        self.assertEqual(xrp["leverage_used"], 75.0)
        self.assertEqual(xrp["trading_qty"], 4.0)
        self.assertLessEqual(xrp["margin_required_usdt"], 0.1106)
        self.assertLess(xrp["stop_loss_price"], xrp["entry_price"])
        self.assertGreater(xrp["stop_loss_price"], xrp["liquidation_price"])
        self.assertGreater(xrp["target_exit_price"], xrp["entry_price"])
        self.assertIn("maker_plan", xrp)
        self.assertGreater(xrp["maker_plan"]["fee_savings_vs_taker_usdt"], 0)

        # LINKUSDT with 0.1106 USDT balance at 75x
        link = micro_profit.calculate_micro_profit(
            symbol="LINKUSDT",
            side="buy",
            balance=0.1106,
            leverage=75,
            target_profit=0.05,
            max_risk=0.025,
            bids=[[12.331, 400], [12.330, 800]],
            asks=[[12.332, 350], [12.333, 600]],
        )
        self.assertEqual(link["status"], "success")
        self.assertEqual(link["trading_qty"], 0.5)
        self.assertLess(link["stop_loss_price"], link["entry_price"])
        self.assertGreater(link["target_exit_price"], link["entry_price"])

        # Short side directional SL guarantee
        short_plan = micro_profit.calculate_micro_profit(
            symbol="XRPUSDT",
            side="sell",
            qty=8.0,
            balance=0.25,
            leverage=75,
            target_profit=0.05,
            max_risk=0.01,
            bids=[[1.3320, 5000]],
            asks=[[1.3321, 4500]],
        )
        self.assertGreater(short_plan["stop_loss_price"], short_plan["entry_price"])
        self.assertLess(short_plan["target_exit_price"], short_plan["entry_price"])

    def test_bybit_live_api_error_fast_fail_and_auto_resize_on_110007(self):
        from unittest.mock import MagicMock
        from tools import bybit_live

        cfg = bybit_live.TradingConfig(
            api_key="test_key",
            api_secret="test_secret",
            use_tor=True,
        )
        bot = bybit_live.BybitToolDispatcher(cfg)
        bot._time_synced = True

        # 1. Verify TorManager does NOT fall back to _tier_direct on BybitAPIError
        bot.tor._socks_alive = True
        bot.tor._socks_probe_ts = 9e12
        direct_called = []
        bot.tor._tier_socks = MagicMock(
            side_effect=bybit_live.BybitAPIError(
                110007, "ab not enough for new order", endpoint="/v5/order/create"
            )
        )
        bot.tor._tier_direct = MagicMock(
            side_effect=lambda *a, **kw: direct_called.append(True)
        )

        with self.assertRaises(bybit_live.BybitAPIError) as ctx:
            bot.api_request("POST", "/v5/order/create", json_data={"symbol": "XRPUSDT"})
        self.assertEqual(ctx.exception.ret_code, 110007)
        self.assertEqual(len(direct_called), 0)
        # Circuit breaker must remain CLOSED on business API errors
        self.assertEqual(bot.circuit.state, bybit_live.CircuitState.CLOSED)

        # 2. Verify place_order translates LimitMaker -> Limit + PostOnly and auto-resizes on 110007
        bot._instr_cache["XRPUSDT"] = bybit_live.InstrumentInfo(
            symbol="XRPUSDT",
            category=bybit_live.Category.LINEAR,
            lot_size=bybit_live.LotSizeFilter(1.0, 1.0, 100000.0, 5.0),
            price_flt=bybit_live.PriceFilter(0.0001, 0.0001, 1000.0),
            max_leverage=75.0,
        )
        bot.get_wallet_balance = MagicMock(
            return_value={"list": [{"totalAvailableBalance": "0.1106", "coin": []}]}
        )
        bot.get_ticker = MagicMock(
            return_value={"bid1Price": "1.3320", "ask1Price": "1.3321", "lastPrice": "1.3321"}
        )
        bot.get_positions = MagicMock(return_value=[{"leverage": "75"}])

        calls = []

        def fake_api_request(method, endpoint, params=None, json_data=None, signed=True):
            calls.append((method, endpoint, dict(json_data or {})))
            if endpoint == "/v5/order/create" and json_data.get("qty") == "8.0":
                raise bybit_live.BybitAPIError(110007, "ab not enough for new order")
            return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "ord-123"}}

        bot.api_request = fake_api_request
        res = bot.place_order(
            symbol="XRPUSDT",
            side=bybit_live.OrderSide.BUY,
            qty=8.0,
            order_type=bybit_live.OrderType.LIMIT_MAKER,
        )
        self.assertEqual(res["retCode"], 0)
        self.assertEqual(res["auto_resized_from"], 8.0)
        self.assertLess(res["executed_qty"], 8.0)
        self.assertGreaterEqual(res["executed_qty"] * 1.3320, 5.0)
        # First call must have translated LimitMaker -> Limit + PostOnly with bid1Price
        first_payload = calls[0][2]
        self.assertEqual(first_payload["orderType"], bybit_live.OrderType.LIMIT)
        self.assertEqual(first_payload["timeInForce"], bybit_live.TimeInForce.POST_ONLY)

    def test_bybit_live_dust_sweep_and_set_leverage_clamping(self):
        from tools import bybit_live

        cfg = bybit_live.TradingConfig(
            api_key="test_key",
            api_secret="test_secret",
            use_tor=False,
        )
        bot = bybit_live.BybitToolDispatcher(cfg)
        bot._time_synced = True
        bot._instr_cache["XRPUSDT"] = bybit_live.InstrumentInfo(
            symbol="XRPUSDT",
            category=bybit_live.Category.LINEAR,
            lot_size=bybit_live.LotSizeFilter(1.0, 1.0, 100000.0, 5.0),
            price_flt=bybit_live.PriceFilter(0.0001, 0.0001, 1000.0),
            max_leverage=75.0,
        )

        recorded = []

        def fake_api(method, endpoint, params=None, json_data=None, signed=True):
            recorded.append((method, endpoint, params, json_data))
            if endpoint == "/v5/position/set-leverage":
                return {"retCode": 0, "retMsg": "OK", "result": json_data}
            if endpoint == "/v5/asset/transfer/query-account-coins-balance":
                return {"retCode": 0, "result": {"balance": [{"coin": "USDT", "transferBalance": "0.02"}]}}
            if endpoint == "/v5/asset/transfer/inter-transfer":
                return {"retCode": 0, "result": {"transferId": "tr-1"}}
            if endpoint == "/v5/asset/covert/small-balance-list":
                return {
                    "retCode": 0,
                    "result": {
                        "smallAssetCoins": [
                            {"fromCoin": "SOL", "supportConvert": 1, "availableBalance": "0.001"},
                            {"fromCoin": "USDT", "supportConvert": 1, "availableBalance": "0.11"},
                        ]
                    },
                }
            if endpoint == "/v5/asset/covert/get-quote":
                return {"retCode": 0, "result": {"quoteId": "q-999"}}
            if endpoint == "/v5/asset/covert/small-balance-execute":
                return {"retCode": 0, "result": {"status": "SUCCESS"}}
            return {"retCode": 0, "result": {}}

        bot.api_request = fake_api
        lev_res = bot.set_leverage("XRPUSDT", leverage=100)
        self.assertEqual(lev_res["result"]["buyLeverage"], "75")
        self.assertEqual(lev_res["result"]["sellLeverage"], "75")

        sweep = bot.sweep_dust(to_coin="USDT")
        self.assertEqual(sweep["status"], "ok")
        self.assertEqual(sweep["converted_coins"], ["SOL"])
        self.assertEqual(sweep["quote_id"], "q-999")
        self.assertEqual(len(sweep["transferred_from_fund"]), 1)

    def test_smart_order_directional_sl(self):
        from tools import bybit_smart_order

        sl_buy = bybit_smart_order.calculate_exit_price_for_net_loss(
            1.3321, 8.0, "Buy", 0.005, 0.00055, 0.00055
        )
        self.assertLess(sl_buy, 1.3321)
        sl_sell = bybit_smart_order.calculate_exit_price_for_net_loss(
            1.3321, 8.0, "Sell", 0.005, 0.00055, 0.00055
        )
        self.assertGreater(sl_sell, 1.3321)



if __name__ == "__main__":
    unittest.main()
