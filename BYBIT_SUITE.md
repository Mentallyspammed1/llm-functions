# Bybit Trading Suite — Unified Architecture

> **Status:** all Bybit tools reviewed, tested, fixed and wired into one
> dispatcher. 84 offline tests cover the strategy maths, the backtester's
> accounting and the execution safety gates.
>
> **Entry point:** `tools/bybit_suite.py` (or `bin/bybit_suite`) — every Bybit
> capability is reachable through a single `--action` interface.
>
> **Safety model:** every action is read-only or a *dry-run plan* by default.
> The ONLY action that can place a live order is `execute_signal`, and only
> when called with `execute=true` after passing all safety gates.

---

## 1. How the tools tie together

```
                     ┌──────────────────────────────────────────┐
   CLI / LLM  ──────►│  tools/bybit_suite.py  (run(**kwargs))    │
   run-tool.py       └───────────────┬──────────────────────────┘
                                     │ dispatcher: terminal.run()
                                     ▼
                     ┌──────────────────────────────────────────┐
                     │        tools/bybit/  (canonical package)  │
                     ├──────────────────────────────────────────┤
                     │ base.py        V5 client, HMAC, rate       │
                     │                limiter, retry, Tor tiers   │
                     │ market.py      data + 35+ indicators,      │
                     │                paged klines (<1000 cap)    │
                     │ execution.py   orders, batches, stops      │
                     │ account.py     balance/positions/PnL       │
                     │ smart.py       risk-sized smart orders     │
                     │ price_action.py  STRUCTURE + TREND ENGINE  │
                     │ backtest.py    HONEST simulator            │
                     │ analytics.py   metrics, walk-forward,      │
                     │                Kelly, ruin, portfolio      │
                     │ unified.py     composite actions           │
                     │ terminal.py    BybitRealm + dispatcher     │
                     └──────┬───────────────────────┬────────────┘
                            │                       │
                 ┌──────────▼─────────┐   ┌─────────▼──────────────┐
                 │ tools/bybit_wbta.py│   │ utils/bybit_base.py     │
                 │ L2/flow/funding/OI │   │ utils/trading_engines.py│
                 │ observatory (WBTA) │   │ PnL + exit strategies   │
                 └────────────────────┘   └────────────────────────┘
```

**One decision function.** `PriceActionEngine.decide_signal()` holds *every*
entry rule. Live signals and the backtester both call it (the backtester
precomputes indicator series and calls the same function bar by bar). Before
this change the backtester re-implemented the rules, had already drifted, and
was silently testing a different strategy from the one that would trade.

`bybit_market.py`, `bybit_analysis.py`, `bybit_account.py`, `bybit_trade.py`
(repo root and `tools/`) are thin ports over the same package — no `pybit`
dependency, one client implementation for everything.

## 2. All actions (one dispatcher)

```sh
# Discovery
bin/bybit_suite --action list_actions
bin/bybit_suite --action check_config      # pre-flight, never prints secrets

# Market data
bin/bybit_suite --action get_ticker --symbol BTCUSDT
bin/bybit_suite --action get_klines --symbol BTCUSDT --interval 60 --limit 5000
bin/bybit_suite --action get_orderbook --symbol BTCUSDT
bin/bybit_suite --action market_snapshot --symbol BTCUSDT

# Indicators
bin/bybit_suite --action calculate_rsi --symbol BTCUSDT --interval 60
bin/bybit_suite --action calculate_supertrend --symbol BTCUSDT --interval 60
# ... calculate_ema|sma|macd|atr|adx|stochastic|bollinger_bands|vwap|cci|
#     ichimoku|mfi|williams_r|tema|hma|klinger|cmf|fisher_transform|...

# Trend analysis  (the core loop)
bin/bybit_suite --action price_action_analysis --symbol BTCUSDT --interval 60
bin/bybit_suite --action trend_scan --symbols BTCUSDT,ETHUSDT,SOLUSDT
bin/bybit_suite --action full_signal --symbol BTCUSDT --interval 60

# Planning / validation (always dry)
bin/bybit_suite --action build_trade_plan --symbol BTCUSDT --risk_pct 1
bin/bybit_suite --action backtest_pa --symbol BTCUSDT --interval 60 --limit 1000
bin/bybit_suite --action optimize_strategy --symbol BTCUSDT --interval 60
bin/bybit_suite --action walk_forward --symbol BTCUSDT --interval 60 --folds 4
bin/bybit_suite --action portfolio_scan --symbols BTCUSDT,ETHUSDT,SOLUSDT
bin/bybit_suite --action risk_report --symbol BTCUSDT --risk_pct 1
bin/bybit_suite --action position_sizing --symbol BTCUSDT --balance 1000
bin/bybit_suite --action execute_signal --symbol BTCUSDT --execute false

# Account / execution (authenticated)
bin/bybit_suite --action get_wallet_balance
bin/bybit_suite --action get_positions
bin/bybit_suite --action get_open_orders
bin/bybit_suite --action place_smart_order --symbol BTCUSDT --side Buy --risk_pct 1
bin/bybit_suite --action set_trading_stop --symbol BTCUSDT --stop_loss 45000
bin/bybit_suite --action journal_stats
```

### New in this pass

| Action | What it answers |
|---|---|
| `check_config` | Are my keys/network/proxy sane? Does anything leak secrets? |
| `risk_report` | Full survivability report: expectancy, Drawdown, Monte-Carlo drawdown, Kelly size, risk of ruin, verdict |
| `position_sizing` | Fractional-Kelly risk % derived from the strategy's own stats |
| `optimize_strategy` | Grid search ranked by **robustness**, not peak return |
| `walk_forward` | Does the edge survive on unseen data? |
| `portfolio_scan` | Which symbols actually carry the edge? |

## 3. Price action engine (trend analysis)

`tools/bybit/price_action.py` produces the tradeable signal:

1. **Market structure** — swing highs/lows (fractal confirmation) → HH/HL =
   `UPTREND`, LH/LL = `DOWNTREND`, otherwise `RANGE`; plus a slope/EMA
   fallback so strong trends are never missed when swings are sparse.
   Break-of-structure is flagged (`BULLISH_BOS` / `BEARISH_BOS`).
2. **EMA stack** — 20/50/200 alignment + price vs fast EMA.
3. **Trend-quality gates** (see §4 — this is where the edge is):
   * Kaufman **efficiency ratio** — net move ÷ path length. ADX measures the
     *size* of moves, not whether a move is a tradeable trend: a random walk
     routinely prints ADX > 35. Measured across synthetic regimes, ADX barely
     separates trend (37.9) from chop (36.0), while efficiency separates them
     cleanly.
   * **Slow-EMA slope / ATR** — a flat 200-EMA means mean-reverting tape, where
     every trend entry is a fade. Measured: trend ≈ 3.9, choppy no-drift ≈ 0.8,
     mean-reverting ≈ 0.3.
   * **Anti-chase**, two-tier — a soft penalty past 3 ATR from the fast EMA, a
     hard block past 6 ATR (blow-off extension).
   * **Volatility-spike guard** — stand down when ATR is in its top 2% locally.
4. **Momentum filters** — Wilder RSI and ADX. RSI only vetoes in weak trends
   (ADX < 25); overbought readings are *expected* in healthy trends.
5. **Output** — LONG / SHORT / WAIT with entry, structure-based stop, TP
   ladder, confidence score and the full reason/filter list.

`full_signal` additionally requires agreement between the price-action engine
and the WBTA observatory (L2 imbalance, taker flow, funding, OI, L/S ratio).
Conflicts are reported as WAIT.

## 4. What actually moved the needle

Profitability cannot be guaranteed — anyone promising it is lying. What this
suite guarantees is a **risk-first framework** plus a strategy whose edge was
measured instead of assumed.

Two changes produced the improvement. Both were found by measurement and are
structural, not curve-fits:

**1. Stop amputating the right tail.** The old exit model took 50% off at 1R
and moved the stop to breakeven. A trend system's entire edge lives in its
right tail; banking half at 1R and scratching the rest removes it. Running the
full position to target roughly doubled expectancy per trade.

**2. Refuse to trade mean-reverting tape.** The old gate was `ADX >= 20`,
which mean-reverting markets pass easily. Adding the slow-EMA-slope gate cut
the mean-reversion drawdown from **15.7% → 1.5%** at essentially no cost to
trend capture.

Measured on synthetic markets (8 seeds per regime, 1% risk/trade, taker fees +
slippage), *same honest backtester*, old rules → new rules:

| Regime | Net PnL (old → new) | Expectancy R (old → new) | Max DD% (old → new) |
|---|---|---|---|
| Strong trend | 456 → **685** | 1.28 → **2.66** | 1.65 → 1.80 |
| Weak trend | 97 → **135** | 0.45 → **1.26** | 3.43 → 3.29 |
| Regime-switching | 126 → **185** | 0.40 → **1.11** | 4.23 → 4.63 |
| Mean-reverting | −149 → **−14** | −0.63 → −0.80 | 15.71 → **1.52** |
| Choppy no-drift | −0.5 → −14 | 0.04 → −0.25 | 4.90 → 6.15 |

Read the last two rows before the first three: the strategy is a *trend*
system, it loses a little in chop, and the gate exists to stop it losing a lot
in mean-reverting markets. **These are synthetic markets, not live results.**
Re-run `walk_forward` on your own symbol before risking anything — it is
designed to tell you "not validated" when the edge is illusory, and on
synthetic regime-switching data it usually does.

### Risk-first sizing

* position size is derived from **risk per trade (% of balance ÷ SL distance)**,
  never from a fixed lot;
* default 1% risk per trade, hard cap 2% in `execute_signal`;
* stop is always structure + ATR based and is attached to the order;
* `execute_signal` gates: confidence ≥ 60, side must match the signal, risk
  under cap, and `execute=true` must be passed explicitly;
* every fill is journaled to SQLite (`bybit_trades.db`) and surfaced via
  `journal_stats` (win rate, profit factor, expectancy).

### Honest backtesting

`backtest.py` is deliberately pessimistic, because an optimistic backtest is
worse than none:

* entries fill at the **next bar's open** plus adverse slippage — never at the
  signal bar's close;
* a bar that **gaps through the stop fills at the open** (`STOP_GAP`), not at
  the stop price;
* when a bar touches both stop and target, the **stop** is counted;
* taker fees are charged on entry, every scale-out and the final exit, and
  each trade's `net_pnl` includes its own entry fee;
* `summary.reconciled` proves the trade log sums to the change in balance —
  if the simulator ever drifts, it says so instead of reporting a flattering
  number.

Metrics reported: expectancy (R and USDT), profit factor, payoff ratio, win
rate, average win/loss, largest win/loss, max consecutive wins/losses, Sharpe,
Sortino, Calmar, max drawdown + duration, ulcer index, exposure, annualised
volatility, total fees paid.

## 5. Environment

```env
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
BYBIT_USE_TESTNET=true       # true → api-testnet.bybit.com
PROXY_ENABLED=false          # route through Tor on device
PROXY_TYPE=socks5h
PROXY_HOST=127.0.0.1
PROXY_PORT=9050
BYBIT_SYNC_TIME=true         # false disables the server-time sync on init
```

Copy `.env.example` to `.env`. `.env` is gitignored and `check_config` will
warn if it is world-readable or ever gets tracked by git.

## 6. Testing

```sh
# create a venv with the two runtime deps
python3 -m venv .venv && .venv/bin/pip install requests python-dotenv

# offline suite (no network, no orders) — 84 tests
.venv/bin/python -m unittest discover -s tests -p "test_bybit*" -v

# read-only live smoke test on the device (never places orders)
.venv/bin/python scripts/bybit_live_smoke.py [--with-auth] [--tor]
```

The regression tests are mutation-checked: re-introducing the inert-`rr` bug,
the entry-fee accounting bug, stop-price gap fills, or the unreachable
BREAKEVEN branch each makes the suite fail.

## 7. What was fixed in this pass

**Correctness**

* **Backtester entry fees** were charged to the balance but not to the trade,
  so `net_pnl` never equalled the change in balance and profit factor was
  computed from incomplete costs.
* **`rr` was inert** — 1R through 4R produced byte-identical results, so any
  "optimisation" returned a meaningless ranking.
* **The BREAKEVEN exit branch was unreachable** — it sat behind a condition
  already consumed by the STOP_LOSS branch, so breakeven exits were mislabelled
  as full losses.
* **Stops always filled at the stop price**, even when a bar gapped through it,
  which flattered every trend backtest.
* **Entry filled at the signal bar's close** — a price that cannot be traded
  once the candle has closed.
* **`get_klines` silently truncated at 1000 candles** (Bybit's per-response
  cap), quietly starving long backtests. Now pages backwards with `end`.
* **The backtester and the live engine had diverged**; both now call
  `decide_signal()`.
* **CLI string arguments were never coerced** to their annotated types
  (`from __future__ import annotations` makes annotations strings, so
  `p.annotation == float` is always false); `limit="300"` broke slicing and
  `min_confidence="60"` raised in comparisons.
* **`--execute false` parsed as True** (`bool("false") is True`) — a live-order
  flag that could be enabled by writing "false". Now only explicit
  `true/1/yes/on` enables execution, with a test pinning it.
* **`get_performance_summary` reported "no trades" when the API call failed**,
  masking an expired key as an empty account.
* **`bybit_analysis.py --help` crashed** on a `%` in an argparse help string.
* `get_klines`/`_atr_series` alignment: ATR series is `n-1` long and was being
  indexed as if it were `n` long (a one-bar lag in every backtest signal).

**Security**

* Credential files (`.env`, `tools/.env`, `tools/tools/.env`) were **tracked in
  git**. They are now untracked, gitignored, `chmod 600`, with `.env.example`
  as the template. **Rotate any key that was ever committed** — removing the
  file does not remove it from history.
* `check_config` flags world-readable credential files, tracked credential
  files, a key equal to its secret, and a testnet/live key mismatch — and never
  echoes a secret or key.

**Strategy** — see §4: shared decision function, efficiency-ratio filter,
slow-EMA-slope regime gate, two-tier anti-chase, volatility guard, and an exit
model validated against trending, regime-switching, mean-reverting and choppy
markets.
