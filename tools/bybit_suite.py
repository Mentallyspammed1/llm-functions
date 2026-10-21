#!/usr/bin/env python3
# @describe Unified Bybit Trading Suite — market data, indicators, price-action
#           trend analysis, WBTA observatory, trade plans, backtests and gated
#           order execution through one dispatcher.
# @option --action! <TEXT>  Action: health_check | market_snapshot | get_ticker |
#       get_orderbook | get_klines | get_funding_rate | get_open_interest |
#       get_wallet_balance | get_account_info | get_positions | get_position_risk |
#       get_open_orders | get_order_history | get_pnl_history | set_leverage |
#       set_trading_stop | cancel_order | cancel_all_orders | amend_order |
#       batch_place_orders | panic_close | place_smart_order |
#       price_action_analysis | build_trade_plan | trend_scan | wbta_signal |
#       full_signal | market_snapshot | backtest_pa | execute_signal |
#       optimize_strategy | walk_forward | portfolio_scan | risk_report |
#       position_sizing | check_config | journal_stats | list_actions |
#       calculate_<indicator>
# @option --symbol <TEXT>    Trading pair (e.g. BTCUSDT; default BTCUSDT).
# @option --symbols <TEXT>   Comma-separated symbols for trend_scan.
# @option --side <TEXT>      Buy | Sell | auto.
# @option --qty <NUM>        Order quantity.
# @option --price <NUM>      Limit price.
# @option --stop_loss <NUM>  Stop-loss price.
# @option --take_profit <NUM> Take-profit price.
# @option --risk_pct <NUM>   Risk per trade (% of balance; default 1.0).
# @option --leverage <NUM>   Leverage (default 1).
# @option --rr <NUM>         Reward:risk multiple (default 2.0).
# @option --interval <TEXT>  Kline timeframe: 1,5,15,60,240,D (default 60).
# @option --limit <NUM>      Number of candles (default 300).
# @option --min_confidence <NUM> Min signal confidence (default 55).
# @option --start_balance <NUM> Backtest starting balance (default 1000).
# @option --fee_rate <NUM>   Taker fee rate per side (default 0.00055).
# @option --slippage_bps <NUM> Adverse slippage for backtests, in bps (default 2).
# @option --trailing_atr <NUM> ATR trailing-stop multiple, 0 = off (default 0).
# @option --time_stop_bars <NUM> Force-exit after N bars, 0 = off (default 0).
# @option --partial_frac <NUM> Fraction to scale out at --partial_at_r (default 0 = run the full position).
# @option --breakeven_at_r <NUM> Move stop to entry at R, 0 = off (default 0).
# @option --folds <NUM>      Walk-forward fold count (default 4).
# @option --grid <TEXT>      JSON parameter grid for optimize_strategy.
# @option --balance <NUM>    Balance for position_sizing (0 = read the account).
# @option --execute <BOOL>   Explicitly allow live order placement (default false).
# @option --category <TEXT>  linear | spot | inverse (default linear).
# @flag --json              Raw JSON output.
"""
Unified Bybit Trading Suite — single entry point for the whole Bybit toolchain.

Everything is dry by default: analysis and plans never place orders. Only
`execute_signal --execute true` can place a live order, and only after the
safety gates pass (confidence, risk cap, side agreement).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Bootstrap the repo root onto sys.path BEFORE the first import. When this
# file is executed directly (script-dir on sys.path), a stray `tools/tools/`
# directory can shadow the real `tools` package.
_here = Path(__file__).resolve()
_repo_root = str(_here.parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
if "tools" in sys.modules and not getattr(sys.modules["tools"], "__file__", None):
    del sys.modules["tools"]  # drop a cached namespace-package shadow

from tools.bybit.terminal import run as realm_run  # noqa: E402


def _str2bool(value) -> bool:
    """Parse a boolean CLI argument WITHOUT bool()'s trap.

    ``bool("false")`` is ``True``, so using ``type=bool`` for ``--execute``
    would turn ``--execute false`` into a live order. Only an explicit truthy
    token enables execution.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def run(**kwargs) -> dict:
    """Unified dispatcher — mirrors `tools.bybit.terminal.run`."""
    return realm_run(**kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified Bybit Trading Suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--action", required=True, help="Action to execute (see docs)")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--side", default="auto")
    parser.add_argument("--qty", type=float, default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--stop_loss", type=float, default=None)
    parser.add_argument("--take_profit", type=float, default=None)
    parser.add_argument("--risk_pct", type=float, default=1.0)
    parser.add_argument("--leverage", type=float, default=1.0)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--interval", default="60")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--min_confidence", type=float, default=55.0)
    parser.add_argument("--start_balance", type=float, default=1000.0)
    parser.add_argument("--fee_rate", type=float, default=0.00055)
    parser.add_argument("--slippage_bps", type=float, default=2.0)
    parser.add_argument("--trailing_atr", type=float, default=0.0)
    parser.add_argument("--time_stop_bars", type=int, default=0)
    parser.add_argument("--partial_at_r", type=float, default=1.0)
    parser.add_argument("--partial_frac", type=float, default=0.0)
    parser.add_argument("--breakeven_at_r", type=float, default=0.0)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--grid", default=None)
    parser.add_argument("--balance", type=float, default=0.0)
    parser.add_argument(
        "--execute", type=_str2bool, nargs="?", const=True, default=False,
        help="Place a LIVE order. Any value other than true/1/yes/on is False.",
    )
    parser.add_argument("--category", default="linear")
    args = parser.parse_args()

    kwargs = {k: v for k, v in vars(args).items() if v is not None}
    result = realm_run(**kwargs)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
