#!/usr/bin/env python3
# ==============================================================================
# micro_profit.py — Pyrmethus AIChat Micro-Profit Estimator v4.5.2-ASCENDED
# argc/aichat compatible · Analytical Closed-Form Exit Solver · Orderbook Analytics
#
# @describe Estimate micro-profit opportunities from order-book data with
#           precision analytical exit solving and position sizing.
#
# @meta require-tools python3
#
# @option --symbol! <STRING>          Required trading pair (e.g., BTCUSDT).
# @option --side! <STRING>            Required side: Buy or Sell.
# @option --qty=0                     Quantity in base asset (0 = auto-size from balance).
# @option --target=5.0                Optional target profit in USDT (default 5.0).
# @option --target_profit <NUMBER>    Alias for --target (net profit in USDT).
# @option --leverage=1                Optional leverage multiplier (default 1).
# @option --maker_fee=0.0002          Maker fee rate (default 0.0002).
# @option --taker_fee=0.00055         Taker fee rate (default 0.00055).
# @option --funding_rate=0.0001       Funding rate per interval (default 0.0001).
# @option --slippage=0.0001           Estimated slippage rate (default 0.0001).
# @option --risk_reward=2.0           Risk/reward ratio (default 2.0).
# @option --max_risk=0.0              Optional max risk in USDT (overrides risk_reward).
# @option --kelly_win=0.55            Estimated win rate for Kelly (default 0.55).
# @option --depth=40                  Order book depth to analyze (default 40).
# @option --account_balance=0.0       Account balance for sizing (default 0.0).
# @option --balance=0.0               Alias for --account_balance.
# @option --risk_percent=0.0          Risk percent per trade (0 = use Kelly).
# @option --bids_json=[]              JSON encoded bids array.
# @option --asks_json=[]              JSON encoded asks array.
# @flag   --use_vwap_entry            Use VWAP entry price instead of best bid/ask.
# @flag   --execute_order             Submit/execute order after calculating metrics.
# @flag   --dry_run                   Simulate order placement without sending.
# @flag   --limit_entry               Use limit order for entry price.
# @flag   --post_only                 Use passive PostOnly maker entry (zero slippage).
# @flag   --include_funding           Include 8h funding rate in entry fee cost.
# @flag   --no_color                  Disable ANSI color output.
# @flag   --verbose                   Enable verbose debug logging.
#
# @env LLM_OUTPUT=/dev/stdout         Output path for LLM integration
# ==============================================================================

from __future__ import annotations

import argparse
import enum
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.exceptions import RequestException

# Set 24-digit precision for crypto financial arithmetic
getcontext().prec = 24

# Module Imports
try:
    import proxy_utils

    proxy_utils.set_proxy_environment()
except ImportError:
    proxy_utils = None

try:
    import scientific_calculator
except ImportError:
    scientific_calculator = None

try:
    import bybit_smart_order
except ImportError:
    bybit_smart_order = None

try:
    import bybit_core
except ImportError:
    bybit_core = None

__version__ = "4.5.2-ASCENDED"
__all__ = [
    "TradeMetrics",
    "__version__",
    "calculate_micro_profit",
    "run",
]

log = logging.getLogger(__name__)

# ==============================================================================
# SECTION 1: Exit Codes & JSON Serializer
# ==============================================================================

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_INVALID_INPUT = 2


class ToolJSONEncoder(json.JSONEncoder):
    """Safe JSON encoder for Decimal, Path, Enum, datetime, timedelta, set."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, enum.Enum):
            return obj.value
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, timedelta):
            return obj.total_seconds()
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        return super().default(obj)


# ==============================================================================
# SECTION 2: Terminal Color Palette & UI Helpers
# ==============================================================================

NEON_CYAN = "\033[38;5;51m"
NEON_GREEN = "\033[38;5;46m"
NEON_RED = "\033[38;5;196m"
NEON_YELLOW = "\033[38;5;226m"
NEON_PURPLE = "\033[38;5;129m"
NEON_PINK = "\033[38;5;198m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

_ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-9;]*[ -/]*[@-~])"
    r"|\033\[[0-9;?]*[a-zA-Z]"
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_tty() -> bool:
    """
    FIX: Unset TERM defaults to "" which previously disabled color on valid
    TTYs. Default to "xterm" so only explicitly "dumb" terminals are blocked.
    """
    if not sys.stderr.isatty():
        return False
    term = os.environ.get("TERM", "xterm").lower()
    return term != "dumb"


def _cprint(
    text: str,
    file: Any = None,
    no_color: bool = False,
    end: str = "\n",
) -> None:
    target = file or sys.stderr
    if no_color or not _is_tty():
        text = _strip_ansi(text)
    print(text, file=target, flush=True, end=end)


def print_human_readable_ui(
    data: Dict[str, Any],
    no_color: bool = False,
) -> None:
    """Render a human-friendly box UI to stderr for interactive users."""
    # FIX: Handle calculation errors gracefully instead of printing an empty UI
    if not data.get("success", True):
        err_msg = data.get("error", "Unknown calculation error.")
        _cprint(f"{NEON_RED}✖ Error: {err_msg}{RESET}", no_color=no_color)
        return

    if not _is_tty() or no_color:
        return

    symbol = data.get("symbol", "N/A")
    side = data.get("side", "N/A").upper()
    side_col = NEON_GREEN if side == "BUY" else NEON_RED

    box_w = 68
    border = "─" * box_w

    _cprint(f"{NEON_PURPLE}╭{border}╮{RESET}")
    _cprint(
        f"{NEON_PURPLE}│{RESET} "
        f"{NEON_PINK}⚡ [MICRO-PROFIT ESTIMATOR v{__version__}]{RESET} "
        f"{side_col}{BOLD}{symbol} ({side}){RESET}"
    )
    _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
    _cprint(
        f"{NEON_PURPLE}│{RESET} "
        f"{NEON_CYAN}Entry Price:{RESET}     "
        f"${data.get('entry_price', 0.0):.4f}"
        f"  |  {NEON_CYAN}Qty:{RESET} {data.get('requested_qty', 0.0)}"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} "
        f"{NEON_GREEN}Target Exit:{RESET}     "
        f"${data.get('target_exit_price', 0.0):.4f}"
        f"  (Net Profit: ${data.get('net_profit_usdt', 0.0):.2f})"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} "
        f"{NEON_RED}Stop Loss:{RESET}       "
        f"${data.get('stop_loss_price', 0.0):.4f}"
        f"  (Max Risk: ${data.get('risk_amount_usdt', 0.0):.2f})"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} "
        f"{NEON_CYAN}Liquidation:{RESET}     "
        f"${data.get('liquidation_price', 0.0):.4f}"
        f"  |  {NEON_CYAN}Leverage:{RESET} {data.get('leverage', 1)}x"
    )
    _cprint(
        f"{NEON_PURPLE}│{RESET} "
        f"{NEON_CYAN}Spread:{RESET}          "
        f"{data.get('spread_bps', 0.0):.2f} bps"
        f"  |  {NEON_CYAN}Book Imbalance:{RESET} "
        f"{data.get('book_imbalance_ratio', 0.5):.2f}"
    )

    warnings = data.get("warnings", [])
    if warnings:
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(
            f"{NEON_PURPLE}│{RESET} "
            f"{NEON_YELLOW}{BOLD}Risk Warnings ({len(warnings)}):{RESET}"
        )
        for w in warnings:
            _cprint(f"{NEON_PURPLE}│{RESET}   {NEON_YELLOW}⚑{RESET} {w}")

    if "order_result" in data:
        res = data["order_result"]
        ok = res.get("success", False)
        _cprint(f"{NEON_PURPLE}├{border}┤{RESET}")
        _cprint(
            f"{NEON_PURPLE}│{RESET} "
            f"{NEON_CYAN}Order Execution:{RESET} "
            f"{NEON_GREEN if ok else NEON_RED}"
            f"{'SUCCESS' if ok else 'FAILED'}{RESET}"
        )

    _cprint(f"{NEON_PURPLE}╰{border}╯{RESET}")


# ==============================================================================
# SECTION 3: DECIMALS & DATA MODELS
# ==============================================================================


def _d(value: Any) -> Decimal:
    """Sanitize and convert inputs safely to Decimal. Returns Decimal(0) on failure."""
    if value is None:
        return Decimal(0)
    s = str(value).replace("\x00", "").strip()
    if not s:
        return Decimal(0)
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal(0)


def _round_f(value: Decimal, places: int = 8) -> float:
    """Round a Decimal to `places` decimal places and return as float."""
    try:
        return float(value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return float(value)


@dataclass
class TradeMetrics:
    symbol: str
    side: str
    requested_qty: float
    recommended_qty: float
    best_bid: float
    best_ask: float
    spread_usdt: float
    spread_bps: float
    entry_price: float
    target_exit_price: float
    stop_loss_price: float
    liquidation_price: float
    gross_profit_usdt: float
    estimated_fees_usdt: float
    funding_cost_usdt: float
    net_profit_usdt: float
    margin_required: float
    risk_amount_usdt: float
    kelly_fraction: float
    half_kelly_fraction: float
    book_imbalance_ratio: float
    book_depth_bid_usdt: float
    book_depth_ask_usdt: float
    confidence_score: float
    signal: str
    leverage: int
    fee_scenario: str
    slippage_cost_usdt: float
    risk_percent_used: float
    warnings: List[str] = field(default_factory=list)


def _get_book_execution_price(
    levels: List[Any],
    qty: Decimal,
) -> Tuple[Decimal, bool]:
    """
    Calculate VWAP walking orderbook levels for qty.

    Returns:
        (vwap_price, fully_filled)
    """
    accumulated_qty = Decimal(0)
    accumulated_value = Decimal(0)

    for level in levels:
        if len(level) < 2:
            continue
        p = _d(level[0])
        s = _d(level[1])
        if p <= 0 or s <= 0:
            continue
        if accumulated_qty + s >= qty:
            remaining = qty - accumulated_qty
            accumulated_value += remaining * p
            accumulated_qty = qty
            break
        accumulated_value += s * p
        accumulated_qty += s

    fully_filled = accumulated_qty >= qty
    if accumulated_qty > 0:
        return accumulated_value / accumulated_qty, fully_filled
    return Decimal(0), False


# ==============================================================================
# SECTION 4: CORE MICRO-PROFIT CALCULATION ENGINE
# ==============================================================================


_INSTRUMENT_SPEC_CACHE: Dict[str, Dict[str, Decimal]] = {}

_DEFAULT_INSTRUMENT_SPECS: Dict[str, Dict[str, Decimal]] = {
    "BTCUSDT": {
        "max_leverage": Decimal("100"),
        "qty_step": Decimal("0.001"),
        "min_qty": Decimal("0.001"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.1"),
    },
    "ETHUSDT": {
        "max_leverage": Decimal("100"),
        "qty_step": Decimal("0.01"),
        "min_qty": Decimal("0.01"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.01"),
    },
    "SOLUSDT": {
        "max_leverage": Decimal("100"),
        "qty_step": Decimal("0.1"),
        "min_qty": Decimal("0.1"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.01"),
    },
    "XRPUSDT": {
        "max_leverage": Decimal("75"),
        "qty_step": Decimal("1"),
        "min_qty": Decimal("1"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.0001"),
    },
    "LINKUSDT": {
        "max_leverage": Decimal("75"),
        "qty_step": Decimal("0.1"),
        "min_qty": Decimal("0.1"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.001"),
    },
    "DOGEUSDT": {
        "max_leverage": Decimal("75"),
        "qty_step": Decimal("1"),
        "min_qty": Decimal("1"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.00001"),
    },
    "ADAUSDT": {
        "max_leverage": Decimal("75"),
        "qty_step": Decimal("1"),
        "min_qty": Decimal("1"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.0001"),
    },
    "SUIUSDT": {
        "max_leverage": Decimal("75"),
        "qty_step": Decimal("1"),
        "min_qty": Decimal("1"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.0001"),
    },
    "AVAXUSDT": {
        "max_leverage": Decimal("75"),
        "qty_step": Decimal("0.1"),
        "min_qty": Decimal("0.1"),
        "min_notional": Decimal("5.0"),
        "tick_size": Decimal("0.01"),
    },
}


def _fetch_instrument_spec(
    symbol: str,
    proxies: Optional[Dict[str, str]] = None,
    allow_network: bool = True,
) -> Optional[Dict[str, Decimal]]:
    """Fetch Bybit V5 linear instrument specification (maxLeverage, qtyStep, minOrderQty, minNotionalValue, tickSize)."""
    if not symbol:
        return None
    if symbol in _INSTRUMENT_SPEC_CACHE:
        return _INSTRUMENT_SPEC_CACHE[symbol]
    if not allow_network:
        return _DEFAULT_INSTRUMENT_SPECS.get(symbol)

    try:
        item = None
        if bybit_core and hasattr(bybit_core, "get_instruments_info"):
            try:
                resp = bybit_core.get_instruments_info(symbol=symbol, category="linear")
                if isinstance(resp, dict) and resp.get("retCode") == 0:
                    items = resp.get("result", {}).get("list", [])
                    if items:
                        item = items[0]
            except Exception:
                pass
        if item is None:
            url = f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={symbol}"
            r = requests.get(url, proxies=proxies, timeout=5)
            r.raise_for_status()
            body = r.json()
            if body.get("retCode") == 0:
                items = body.get("result", {}).get("list", [])
                if items:
                    item = items[0]
        if item:
            lot = item.get("lotSizeFilter", {})
            lev_flt = item.get("leverageFilter", {})
            prc_flt = item.get("priceFilter", {})
            spec = {
                "max_leverage": max(Decimal(1), _d(lev_flt.get("maxLeverage", "100"))),
                "qty_step": _d(lot.get("qtyStep", "0.0001")),
                "min_qty": _d(lot.get("minOrderQty", "0.0001")),
                "min_notional": max(Decimal("5.0"), _d(lot.get("minNotionalValue", "5.0"))),
                "tick_size": _d(prc_flt.get("tickSize", "0.0001")),
            }
            _INSTRUMENT_SPEC_CACHE[symbol] = spec
            return spec
    except Exception as exc:
        log.debug("Instrument spec fetch failed for %s: %s", symbol, exc)
    return _DEFAULT_INSTRUMENT_SPECS.get(symbol)


def _snap_down(val: Decimal, step: Decimal) -> Decimal:
    if step <= 0 or val <= 0:
        return val
    return (val // step) * step


def _snap_up(val: Decimal, step: Decimal) -> Decimal:
    if step <= 0 or val <= 0:
        return val
    q = val // step
    if q * step < val:
        q += Decimal(1)
    return q * step


def calculate_micro_profit(**kwargs: Any) -> Dict[str, Any]:
    """
    Calculate micro-profit exit/stop bounds, position sizing, and fee impact.
    Supports both explicit qty and auto-sizing from balance/account_balance,
    target/target_profit, max_risk/risk_reward, and PostOnly maker execution.
    """
    verbose: bool = bool(kwargs.get("verbose", False))

    # ------------------------------------------------------------------
    # 1. Inputs & Sanitization (supporting aliases used by LLM agents)
    # ------------------------------------------------------------------
    s = str(kwargs.get("symbol", "") or "").upper().strip()
    side = str(kwargs.get("side", "") or "").lower().strip()
    if side == "long":
        side = "buy"
    elif side == "short":
        side = "sell"
    if side not in ("buy", "sell"):
        return {
            "success": False,
            "error": f"Invalid side '{side}'. Must be 'buy' or 'sell'.",
        }

    q = _d(kwargs.get("qty", 0))
    req_lev = max(Decimal(1), _d(kwargs.get("leverage", 1)))
    lev = req_lev

    raw_acc_bal = kwargs.get("account_balance")
    if raw_acc_bal is None or _d(raw_acc_bal) == 0:
        raw_acc_bal = kwargs.get("balance", 0)
    acc_bal = _d(raw_acc_bal)

    raw_target = kwargs.get("target")
    if raw_target is None or (kwargs.get("target_profit") is not None and _d(raw_target) == Decimal("5.0")):
        if kwargs.get("target_profit") is not None:
            raw_target = kwargs.get("target_profit")
    if raw_target is None:
        raw_target = Decimal("0.02") if (Decimal(0) < acc_bal < Decimal("2.0")) else Decimal("5.0")
    target = _d(raw_target)
    if target <= 0:
        return {
            "success": False,
            "error": "--target / --target_profit must be > 0.",
        }

    mk = _d(kwargs.get("maker_fee", 0.0002))
    tk = _d(kwargs.get("taker_fee", 0.00055))
    include_funding = bool(kwargs.get("include_funding", True if "funding_rate" in kwargs else False))
    raw_fr = _d(kwargs.get("funding_rate", 0.0001))
    fr = raw_fr if include_funding else Decimal(0)
    post_only = bool(kwargs.get("post_only", False))
    limit_entry = bool(kwargs.get("limit_entry", False)) or post_only
    raw_slippage = _d(kwargs.get("slippage", 0.0 if post_only else 0.0001))
    slippage = Decimal(0) if post_only else raw_slippage
    risk_pct = _d(kwargs.get("risk_percent", 0.0))
    max_risk_input = _d(kwargs.get("max_risk", 0.0))

    log.debug(
        "Inputs: symbol=%s side=%s qty=%s lev=%s target=%s "
        "mk=%s tk=%s fr=%s slippage=%s acc_bal=%s max_risk=%s",
        s,
        side,
        q,
        lev,
        target,
        mk,
        tk,
        fr,
        slippage,
        acc_bal,
        max_risk_input,
    )

    # ------------------------------------------------------------------
    # 2. Parse or Fetch Orderbook Depth & Instrument Spec
    # ------------------------------------------------------------------
    bids_in = kwargs.get("bids")
    asks_in = kwargs.get("asks")
    bids_raw = str(kwargs.get("bids_json", "[]") or "[]").strip()
    asks_raw = str(kwargs.get("asks_json", "[]") or "[]").strip()

    try:
        bids: List[Any] = (
            list(bids_in)
            if isinstance(bids_in, (list, tuple))
            else (json.loads(str(bids_in)) if isinstance(bids_in, str) and bids_in.strip() else (json.loads(bids_raw) if bids_raw else []))
        )
    except Exception:
        bids = []

    try:
        asks: List[Any] = (
            list(asks_in)
            if isinstance(asks_in, (list, tuple))
            else (json.loads(str(asks_in)) if isinstance(asks_in, str) and asks_in.strip() else (json.loads(asks_raw) if asks_raw else []))
        )
    except Exception:
        asks = []

    inst_spec: Optional[Dict[str, Decimal]] = _fetch_instrument_spec(
        s, allow_network=False
    )
    if (not bids or not asks) and s:
        proxies = proxy_utils.get_proxies() if proxy_utils else None
        depth = int(kwargs.get("depth", 40))
        inst_spec = _fetch_instrument_spec(s, proxies=proxies, allow_network=True) or inst_spec

        # Bybit primary endpoint
        try:
            ob = None
            if bybit_core:
                try:
                    resp = bybit_core.get_orderbook(symbol=s, limit=depth)
                    if resp.get("retCode") == 0:
                        ob = resp.get("result", {})
                        log.debug("Fetched orderbook via bybit_core for %s", s)
                except Exception as e_core:
                    log.debug(
                        "bybit_core.get_orderbook failed, falling back: %s", e_core
                    )

            if ob is None:
                url = (
                    f"https://api.bybit.com/v5/market/orderbook"
                    f"?category=linear&symbol={s}&limit={depth}"
                )
                try:
                    r = requests.get(url, proxies=proxies, timeout=5)
                    r.raise_for_status()
                    if r.json().get("retCode") == 0:
                        ob = r.json().get("result", {})
                except RequestException as e_req:
                    log.debug("Bybit API request failed: %s", e_req)

            if ob:
                bids = ob.get("b", [])
                asks = ob.get("a", [])
                log.debug("Fetched %d bids / %d asks from Bybit.", len(bids), len(asks))
        except Exception as exc:
            log.debug("Bybit orderbook fetch failed: %s", exc)

        # Gate.io fallback
        if not bids or not asks:
            try:
                if s.endswith("USDT"):
                    currency_pair = f"{s[:-4]}_USDT"
                else:
                    currency_pair = s
                url = (
                    f"https://api.gateio.ws/api/v4/spot/order_book"
                    f"?currency_pair={currency_pair}"
                )
                r = requests.get(url, proxies=proxies, timeout=5)
                r.raise_for_status()
                data = r.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                log.debug(
                    "Fetched %d bids / %d asks from Gate.io.", len(bids), len(asks)
                )
            except RequestException as exc:
                log.debug("Gate.io orderbook fetch failed: %s", exc)

        # Binance tertiary fallback
        if not bids or not asks:
            try:
                url = f"https://api.binance.com/api/v3/depth?symbol={s}&limit={depth}"
                r = requests.get(url, proxies=proxies, timeout=5)
                r.raise_for_status()
                data = r.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                log.debug(
                    "Fetched %d bids / %d asks from Binance.", len(bids), len(asks)
                )
            except RequestException as exc:
                log.debug("Binance orderbook fetch failed: %s", exc)

    bid_depth_val = sum(_d(p[0]) * _d(p[1]) for p in bids[:20] if len(p) >= 2)
    ask_depth_val = sum(_d(p[0]) * _d(p[1]) for p in asks[:20] if len(p) >= 2)
    total_depth = bid_depth_val + ask_depth_val
    imbalance = bid_depth_val / total_depth if total_depth > 0 else Decimal("0.5")

    best_bid = _d(bids[0][0]) if bids else Decimal(0)
    best_ask = _d(asks[0][0]) if asks else Decimal(0)

    # ------------------------------------------------------------------
    # Entry Price Solver & Instrument Constraints
    # ------------------------------------------------------------------
    warnings: List[str] = []

    if inst_spec and req_lev > inst_spec["max_leverage"]:
        lev = inst_spec["max_leverage"]
        warnings.append(
            f"Requested leverage {int(req_lev)}x exceeds {s} max leverage "
            f"({int(lev)}x); clamped to {int(lev)}x."
        )

    tick_size = inst_spec["tick_size"] if inst_spec else Decimal(0)
    qty_step = inst_spec["qty_step"] if inst_spec else Decimal(0)
    min_notional = inst_spec["min_notional"] if inst_spec else Decimal("5.0")

    if kwargs.get("use_vwap_entry") and q > 0:
        book_levels = asks if side == "buy" else bids
        vwap, fully_filled = _get_book_execution_price(book_levels, q)
        if not fully_filled:
            warnings.append(
                f"Order qty {float(q):.4f} exceeds available book depth; "
                "VWAP is partial — actual fill price will be worse."
            )
        entry = vwap if vwap > 0 else (best_ask if side == "buy" else best_bid)
    elif post_only and best_bid > 0 and best_ask > 0:
        # Passive PostOnly maker price rests on the book instead of crossing the spread
        entry = best_bid if side == "buy" else best_ask
    else:
        entry = best_ask if side == "buy" else best_bid

    if entry <= 0:
        entry = Decimal(1.0)  # Safe fallback when no market data available
        warnings.append("No market data available; using fallback entry price of $1.0.")

    # ------------------------------------------------------------------
    # 3. Fee & Slippage Configuration
    # ------------------------------------------------------------------
    e_fee_base = mk if limit_entry else tk

    e_fee = min(e_fee_base + fr + slippage, Decimal("0.99"))
    x_fee_tp = min(mk + slippage, Decimal("0.99"))
    x_fee_sl = min(tk + slippage, Decimal("0.99"))

    # ------------------------------------------------------------------
    # 4. Kelly & Bybit Margin-Safe Position Sizing
    # ------------------------------------------------------------------
    risk_rr = max(Decimal("0.1"), _d(kwargs.get("risk_reward", 2.0)))
    kelly_w = max(Decimal(0), min(Decimal(1), _d(kwargs.get("kelly_win", 0.55))))
    kelly = kelly_w - ((Decimal(1) - kelly_w) / risk_rr)
    kelly_safe = max(Decimal(0), kelly)
    half_kelly = kelly_safe / Decimal(2)

    margin_per_unit = (entry / lev) + (entry * (mk + tk + fr))
    # Bybit UTA reserves initial margin + 2x taker fee + market order slippage buffer
    market_buffer = Decimal("0.0003") if limit_entry else Decimal("0.0015")
    bybit_order_cost_per_unit = entry * (
        (Decimal(1) / lev) + (Decimal(2) * tk) + market_buffer
    )
    max_affordable_qty = Decimal(0)
    min_notional_qty = _snap_up(min_notional / entry, qty_step) if entry > 0 else Decimal(0)

    if acc_bal > 0 and bybit_order_cost_per_unit > 0:
        raw_max_aff = (acc_bal * Decimal("0.95")) / bybit_order_cost_per_unit
        max_affordable_qty = (
            _snap_down(raw_max_aff, qty_step) if qty_step > 0 else raw_max_aff
        )

    rec_qty = q

    if acc_bal > 0 and margin_per_unit > 0:
        if risk_pct > 0:
            risk_budget = acc_bal * (risk_pct / Decimal(100))
            rec_qty = risk_budget / margin_per_unit
            log.debug(
                "Position sized by risk_percent=%.4f → rec_qty=%.6f",
                float(risk_pct),
                float(rec_qty),
            )
        else:
            rec_qty = (acc_bal * half_kelly) / margin_per_unit
            log.debug(
                "Position sized by half-Kelly=%.4f → rec_qty=%.6f",
                float(half_kelly),
                float(rec_qty),
            )
        # For micro-balances where half-Kelly is below Bybit's $5 minNotional,
        # scale up to a margin-safe quantity that satisfies minNotional without triggering 110007
        if (
            q <= 0
            and max_affordable_qty > 0
            and (rec_qty * entry) < min_notional
        ):
            rec_qty = max_affordable_qty
        elif max_affordable_qty > 0 and rec_qty > max_affordable_qty:
            rec_qty = max_affordable_qty
        if qty_step > 0 and rec_qty > 0:
            rec_qty = max(qty_step, _snap_down(rec_qty, qty_step))

    if q > 0 and max_affordable_qty > 0 and q > max_affordable_qty:
        warnings.append(
            f"Requested qty {float(q)} exceeds Bybit margin-safe max qty "
            f"{float(max_affordable_qty)} for balance ${float(acc_bal):.4f} at {int(lev)}x "
            "(risk of retCode=110007 'ab not enough for new order')."
        )

    trading_qty = q if q > 0 else rec_qty
    if trading_qty <= 0:
        return {
            "success": False,
            "error": "trading_qty resolved to zero; provide --qty > 0 or --balance > 0.",
        }

    if (trading_qty * entry) < min_notional and inst_spec is not None:
        warnings.append(
            f"Order notional (${float(trading_qty * entry):.2f}) is below Bybit "
            f"minimum notional (${float(min_notional):.2f}); min qty is {float(min_notional_qty)}."
        )

    # ------------------------------------------------------------------
    # 5. Analytical Exit Price Solver (Closed-Form, slippage-adjusted)
    # ------------------------------------------------------------------
    if side == "buy":
        denom_tp = Decimal(1) - x_fee_tp
        if denom_tp <= 0:
            return {
                "success": False,
                "error": "Exit fees and slippage are too high (>= 100%) to calculate a valid exit price.",
            }
        exit_p = ((target / trading_qty) + entry * (Decimal(1) + e_fee)) / denom_tp
    else:
        exit_p = (entry * (Decimal(1) - e_fee) - (target / trading_qty)) / (
            Decimal(1) + x_fee_tp
        )

    log.debug("Solved exit_p=%.6f for target=%.4f", float(exit_p), float(target))

    # ------------------------------------------------------------------
    # 6. Liquidation Price (computed before SL so SL can be bounded safely)
    # ------------------------------------------------------------------
    mmr = Decimal("0.005")  # 0.5% maintenance margin rate
    if side == "buy":
        liq = entry * (Decimal(1) - Decimal(1) / lev + mmr)
    else:
        liq = entry * (Decimal(1) + Decimal(1) / lev - mmr)

    # ------------------------------------------------------------------
    # 7. Stop-Loss Price Solver (slippage-adjusted, strictly directional)
    # ------------------------------------------------------------------
    risk_amt = max_risk_input if max_risk_input > 0 else (target / risk_rr)
    if side == "buy":
        denom_sl = Decimal(1) - x_fee_sl
        if denom_sl <= 0:
            return {
                "success": False,
                "error": "Stop-loss fees and slippage are too high (>= 100%) to calculate a valid stop price.",
            }
        sl_p = (entry * (Decimal(1) + e_fee) - (risk_amt / trading_qty)) / denom_sl
    else:
        sl_p = ((risk_amt / trading_qty) + entry * (Decimal(1) - e_fee)) / (
            Decimal(1) + x_fee_sl
        )

    min_sl_step = tick_size if tick_size > 0 else (entry * Decimal("0.001"))
    if side == "buy" and sl_p >= entry:
        warnings.append(
            f"Fees+slippage exceed risk limit (${float(risk_amt):.4f}). "
            "Adjusted stop-loss below entry using price-move risk so order is valid."
        )
        price_sl_dist = max(min_sl_step * Decimal(2), risk_amt / trading_qty)
        sl_p = entry - price_sl_dist
        if liq > 0 and sl_p <= liq:
            sl_p = liq + min_sl_step
    elif side == "sell" and sl_p <= entry:
        warnings.append(
            f"Fees+slippage exceed risk limit (${float(risk_amt):.4f}). "
            "Adjusted stop-loss above entry using price-move risk so order is valid."
        )
        price_sl_dist = max(min_sl_step * Decimal(2), risk_amt / trading_qty)
        sl_p = entry + price_sl_dist
        if liq > 0 and sl_p >= liq:
            sl_p = liq - min_sl_step

    if tick_size > 0:
        if side == "buy":
            exit_p = _snap_up(exit_p, tick_size)
            sl_p = _snap_down(sl_p, tick_size)
            if sl_p >= entry:
                sl_p = _snap_down(entry, tick_size) - tick_size
        else:
            exit_p = _snap_down(exit_p, tick_size)
            sl_p = _snap_up(sl_p, tick_size)
            if sl_p <= entry:
                sl_p = _snap_up(entry, tick_size) + tick_size

    # ------------------------------------------------------------------
    # 8. Risk Warnings & Wall Detection
    # ------------------------------------------------------------------
    if not bids or not asks:
        warnings.append("Order book is empty; depth metrics may be inaccurate.")

    # FIX: Prevent negative spread if orderbook is crossed
    spread_bps_val = (
        ((best_ask - best_bid) / best_bid) * Decimal(10000)
        if best_bid > 0 and best_ask >= best_bid
        else Decimal(0)
    )
    if spread_bps_val > Decimal(15):
        warnings.append("High bid-ask spread; execution slippage risk is elevated.")

    if side == "buy" and imbalance < Decimal("0.45"):
        warnings.append("Bearish book imbalance; downward price pressure expected.")
    elif side == "sell" and imbalance > Decimal("0.55"):
        warnings.append("Bullish book imbalance; upward price pressure expected.")

    if trading_qty > 0 and target > (entry * trading_qty * Decimal("0.05")):
        warnings.append(
            "Unrealistically high target profit relative to total order value."
        )

    if exit_p <= 0:
        warnings.append(
            "Computed exit price is non-positive; target may be unreachable "
            "given current fees and entry price."
        )

    # Orderbook wall detection via optional scientific_calculator module
    if scientific_calculator:
        for label, book in (("buy", bids), ("sell", asks)):
            if not book:
                continue
            try:
                sizes = [float(p[1]) for p in book[:20] if len(p) >= 2]
                if not sizes:
                    continue
                stats = scientific_calculator.execute_tool(
                    mode="stats", data=sizes
                ).get("result", {})
                b_mean = stats.get("mean", 0)
                b_stdev = stats.get("stdev", 0)
                b_max = stats.get("max", 0)
                if b_max > b_mean + (2 * b_stdev) and b_max > 0:
                    wall_side = "buy" if label == "buy" else "sell"
                    warnings.append(
                        f"Significant {wall_side} wall detected "
                        f"(Max: {b_max:.2f} vs Mean: {b_mean:.2f})"
                    )
            except Exception as exc:
                log.debug("Wall detection failed for %s side: %s", label, exc)

    # ------------------------------------------------------------------
    # 9. Confidence Score
    # ------------------------------------------------------------------
    conf = Decimal(75)
    if side == "buy":
        conf += (imbalance - Decimal("0.5")) * Decimal(40)
    else:
        conf += (Decimal("0.5") - imbalance) * Decimal(40)
    if spread_bps_val > Decimal(10):
        conf -= (spread_bps_val - Decimal(10)) * Decimal(2)
    conf = max(Decimal(10), min(Decimal(99), conf))

    # ------------------------------------------------------------------
    # 10. P&L Components & Passive Maker Optimization Plan
    # ------------------------------------------------------------------
    gross_profit = (
        (exit_p - entry) * trading_qty
        if side == "buy"
        else (entry - exit_p) * trading_qty
    )

    slippage_cost = entry * trading_qty * slippage * Decimal(2)  # entry + exit

    estimated_fees = (entry * trading_qty * e_fee_base) + (exit_p * trading_qty * mk)

    funding_cost = entry * trading_qty * fr

    # Compute passive PostOnly Maker entry + Limit Maker exit plan (0.02% + 0.02% = 0.04% RT fee)
    maker_entry = (
        (best_bid if side == "buy" else best_ask)
        if (best_bid > 0 and best_ask > 0)
        else entry
    )
    if side == "buy":
        maker_tp = ((target / trading_qty) + maker_entry * (Decimal(1) + mk)) / (
            Decimal(1) - mk
        )
        maker_be = maker_entry * (Decimal(1) + mk) / (Decimal(1) - mk)
        maker_sl = maker_entry - max(min_sl_step * Decimal(2), risk_amt / trading_qty)
        if tick_size > 0:
            maker_tp = _snap_up(maker_tp, tick_size)
            maker_sl = _snap_down(maker_sl, tick_size)
    else:
        maker_tp = (maker_entry * (Decimal(1) - mk) - (target / trading_qty)) / (
            Decimal(1) + mk
        )
        maker_be = maker_entry * (Decimal(1) - mk) / (Decimal(1) + mk)
        maker_sl = maker_entry + max(min_sl_step * Decimal(2), risk_amt / trading_qty)
        if tick_size > 0:
            maker_tp = _snap_down(maker_tp, tick_size)
            maker_sl = _snap_up(maker_sl, tick_size)
    maker_rt_fees = (maker_entry + maker_tp) * trading_qty * mk
    taker_rt_fees = (entry + exit_p) * trading_qty * tk

    # ------------------------------------------------------------------
    # 11. Optional Order Execution via bybit_smart_order
    # ------------------------------------------------------------------
    order_result: Optional[Dict[str, Any]] = None
    if kwargs.get("execute_order") and bybit_smart_order:
        try:
            order_result = bybit_smart_order.run(
                symbol=s,
                side=side.capitalize(),
                order_type="Limit" if limit_entry else "Market",
                entry_price=float(entry) if limit_entry else None,
                qty=float(trading_qty),
                leverage=int(lev),
                sl_price=float(sl_p),
                tp_price=float(exit_p),
                dry_run=bool(kwargs.get("dry_run", False)),
            )
            log.debug("Order result: %s", order_result)
        except Exception as err:
            log.exception("bybit_smart_order.run raised: %s", err)
            order_result = {"success": False, "error": str(err)}

    # ------------------------------------------------------------------
    # 12. Assemble Result
    # ------------------------------------------------------------------
    metrics = TradeMetrics(
        symbol=s,
        side=side,
        requested_qty=float(q if q > 0 else trading_qty),
        recommended_qty=_round_f(max(Decimal("0.0001"), rec_qty), 4),
        best_bid=float(best_bid),
        best_ask=float(best_ask),
        spread_usdt=float(best_ask - best_bid),
        spread_bps=float(spread_bps_val),
        entry_price=float(entry),
        target_exit_price=float(exit_p),
        stop_loss_price=float(sl_p),
        liquidation_price=float(liq),
        gross_profit_usdt=float(gross_profit),
        estimated_fees_usdt=float(estimated_fees),
        funding_cost_usdt=float(funding_cost),
        net_profit_usdt=float(target),
        margin_required=float(entry * trading_qty / lev),
        risk_amount_usdt=float(risk_amt),
        kelly_fraction=float(kelly_safe),
        half_kelly_fraction=float(half_kelly),
        book_imbalance_ratio=float(imbalance),
        book_depth_bid_usdt=float(bid_depth_val),
        book_depth_ask_usdt=float(ask_depth_val),
        confidence_score=float(conf),
        signal="BUY" if side == "buy" else "SELL",
        leverage=int(lev),
        fee_scenario=(
            "post_only_maker_entry_maker_exit"
            if post_only
            else (
                "limit_entry_maker_exit"
                if limit_entry
                else "taker_entry_maker_exit"
            )
        ),
        slippage_cost_usdt=float(slippage_cost),
        risk_percent_used=float(risk_pct),
        warnings=warnings,
    )

    res_dict: Dict[str, Any] = asdict(metrics)
    res_dict["success"] = True  # FIX: Explicit success flag for caller
    res_dict["status"] = "success"
    res_dict["leverage_used"] = int(lev)
    res_dict["margin_required_usdt"] = res_dict["margin_required"]
    res_dict["trading_qty"] = float(trading_qty)
    res_dict["notional_usdt"] = round(float(entry * trading_qty), 4)
    if max_affordable_qty > 0:
        res_dict["max_affordable_qty"] = float(max_affordable_qty)
    if min_notional_qty > 0:
        res_dict["min_notional_qty"] = float(min_notional_qty)
    res_dict["maker_plan"] = {
        "order_type": "Limit",
        "time_in_force": "PostOnly",
        "entry_price": _round_f(maker_entry, 6),
        "target_exit_price": _round_f(maker_tp, 6),
        "stop_loss_price": _round_f(maker_sl, 6),
        "breakeven_price": _round_f(maker_be, 6),
        "round_trip_fees_usdt": _round_f(maker_rt_fees, 6),
        "fee_savings_vs_taker_usdt": _round_f(max(Decimal(0), taker_rt_fees - maker_rt_fees), 6),
        "qty": float(trading_qty),
    }
    if order_result is not None:
        res_dict["order_result"] = order_result
    return res_dict


# ==============================================================================
# SECTION 5: OUTPUT ROUTING (LLM_OUTPUT)
# ==============================================================================


def write_llm_output(data: Dict[str, Any]) -> None:
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    json_payload = (
        json.dumps(data, indent=2, ensure_ascii=False, cls=ToolJSONEncoder) + "\n"
    )

    if out_path in ("/dev/stdout", "/dev/fd/1", "-"):
        sys.stdout.write(json_payload)
        sys.stdout.flush()
    else:
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "a", encoding="utf-8") as fp:
                fp.write(json_payload)
        except OSError as err:
            sys.stderr.write(f"Failed writing to LLM_OUTPUT '{out_path}': {err}\n")
            sys.stdout.write(json_payload)
            sys.stdout.flush()


# ==============================================================================
# SECTION 6: PROGRAMMATIC ENTRY POINT FOR AICHAT
# ==============================================================================


def run(**kwargs: Any) -> Dict[str, Any]:
    """Execute micro-profit calculation and route results."""
    verbose = bool(kwargs.get("verbose", False))
    cli_mode = bool(kwargs.pop("_cli_mode", False))
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stderr,
        )
        log.debug("Verbose logging enabled. kwargs=%s", list(kwargs.keys()))

    result = calculate_micro_profit(**kwargs)
    print_human_readable_ui(result, no_color=bool(kwargs.get("no_color", False)))
    if cli_mode:
        write_llm_output(result)
    return result


# ==============================================================================
# SECTION 7: CLI ARGUMENT PARSER & ENTRYPOINT
# ==============================================================================


def _coerce(val: str) -> Any:
    """
    Coerce env-var string to most specific Python type.
    """
    if val == "":
        return None
    low = val.lower()
    if low in ("true", "yes", "1"):
        return True
    if low in ("false", "no", "0"):
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="micro_profit.py",
        description=f"Micro-Profit Estimator Engine v{__version__}",
    )
    parser.add_argument(
        "--symbol",
        required=True,
        help="Trading pair symbol (e.g. BTCUSDT)",
    )
    parser.add_argument(
        "--side",
        required=True,
        choices=["Buy", "Sell", "buy", "sell", "Long", "Short", "long", "short"],
        help="Order side",
    )
    parser.add_argument(
        "--qty",
        type=float,
        default=0.0,
        help="Order quantity in base asset (0 = auto-size from balance)",
    )
    parser.add_argument(
        "--target",
        type=float,
        default=5.0,
        help="Target net profit in USDT",
    )
    parser.add_argument(
        "--target_profit",
        type=float,
        default=None,
        help="Alias for --target (net profit in USDT)",
    )
    parser.add_argument(
        "--leverage",
        type=int,
        default=1,
        help="Leverage multiplier",
    )
    parser.add_argument(
        "--maker_fee",
        type=float,
        default=0.0002,
        help="Maker fee rate",
    )
    parser.add_argument(
        "--taker_fee",
        type=float,
        default=0.00055,
        help="Taker fee rate",
    )
    parser.add_argument(
        "--funding_rate",
        type=float,
        default=0.0001,
        help="Funding rate per interval",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.0001,
        help="Estimated slippage rate",
    )
    parser.add_argument(
        "--risk_reward",
        type=float,
        default=2.0,
        help="Target risk to reward ratio",
    )
    parser.add_argument(
        "--max_risk",
        type=float,
        default=0.0,
        help="Maximum risk in USDT (overrides risk_reward when > 0)",
    )
    parser.add_argument(
        "--kelly_win",
        type=float,
        default=0.55,
        help="Estimated win rate for Kelly criterion",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=40,
        help="Order book depth level",
    )
    parser.add_argument(
        "--account_balance",
        type=float,
        default=0.0,
        help="Account balance for sizing",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=0.0,
        help="Alias for --account_balance",
    )
    parser.add_argument(
        "--risk_percent",
        type=float,
        default=0.0,
        help="Risk percent per trade (0 = use Kelly)",
    )
    parser.add_argument(
        "--bids_json",
        default="[]",
        help="JSON encoded bids array",
    )
    parser.add_argument(
        "--asks_json",
        default="[]",
        help="JSON encoded asks array",
    )
    parser.add_argument(
        "--use_vwap_entry",
        action="store_true",
        help="Use VWAP entry price from depth",
    )
    parser.add_argument(
        "--execute_order",
        action="store_true",
        help="Submit order after calculations",
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="Simulate order placement",
    )
    parser.add_argument(
        "--limit_entry",
        action="store_true",
        help="Use limit order for entry",
    )
    parser.add_argument(
        "--post_only",
        action="store_true",
        help="Use passive PostOnly maker entry (zero slippage)",
    )
    parser.add_argument(
        "--include_funding",
        action="store_true",
        help="Include 8h funding rate in entry fee cost",
    )
    parser.add_argument(
        "--no_color",
        action="store_true",
        help="Disable ANSI color UI",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )
    return parser


if __name__ == "__main__":
    # argc/aichat environment variable interface
    if any(k.startswith("argc_") for k in os.environ):
        kwargs: Dict[str, Any] = {"_cli_mode": True}
        for k, v in os.environ.items():
            if k.startswith("argc_"):
                coerced = _coerce(v)
                if coerced is not None:
                    kwargs[k[5:].lower()] = coerced
        res = run(**kwargs)
        sys.exit(EXIT_SUCCESS if res.get("success", False) else EXIT_ERROR)

    # Standard CLI interface
    args = _build_parser().parse_args()
    cli_kwargs = vars(args)
    cli_kwargs["_cli_mode"] = True
    res = run(**cli_kwargs)
    sys.exit(EXIT_SUCCESS if res.get("success", False) else EXIT_ERROR)
