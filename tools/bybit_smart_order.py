#!/usr/bin/env python3
# @describe Place a smart order with automatic position sizing and risk management.
# @option --symbol BTCUSDT Trading pair (e.g. BTCUSDT).
# @option --side Buy Buy or Sell.
# @option --order-type Market Market or Limit (default: Market).
# @option --entry-price <NUM> Entry price (required if order-type is Limit).
# @option --leverage <NUM> Set account leverage before placing order.
# @option --risk-pct <NUM> % of balance to risk (default: 1.0).
# @option --sl-dist <NUM> Stop loss distance in price.
# @option --sl-price <NUM> Absolute Stop Loss price.
# @option --tp-price <NUM> Absolute Take Profit price.
# @option --rr-ratio <NUM> Risk to Reward ratio for Take Profit calculation (default: 2.0).
# @option --tp-usdt <NUM> Target Take Profit in absolute USDT value (calculates exact exit price).
# @option --sl-usdt <NUM> Maximum Risk in absolute USDT value (overrides --risk-pct).
# @option --trailing-stop <NUM> Trailing Stop activation distance in price.
# @option --trailing-activation <NUM> Trailing Stop activation price (requires --trailing-stop).
# @option --reduce-only <BOOL> Submit order as reduce-only (close position only).
# @option --margin-mode <STR> Margin mode: isolated or cross (sets before leverage if provided).
# @option --position-idx <NUM> Position index: 0=One-Way, 1=Buy hedge, 2=Sell hedge (default: 0).
# @option --time-in-force <STR> Time in force: GTC, IOC, FOK, PostOnly (default: GTC).
# @option --sl-trigger-by <STR> SL trigger price type: Mark, Index, Last (default: Mark).
# @option --tp-trigger-by <STR> TP trigger price type: Mark, Index, Last (default: Mark).
# @option --order-link-id <STR> Custom order link ID (max 36 chars) for idempotency.
# @option --dry-run <BOOL> Calculate everything and return the order payload without submitting.
# @option --max-position-usdt <NUM> Safety cap: refuse if notional > this USDT value.

"""Place a smart order with automatic position sizing and risk management."""
import os
import sys
import json
import logging
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from typing import Any, Dict, Optional, Tuple

# Add current directory to path to import modular bybit package
sys.path.append(str(Path(__file__).parent))

try:
    import bybit_core
except ImportError:
    # If standard import fails, try relative import for sub-agent environment
    from . import bybit_core

# Module-level logger; falls back to a basic config if no handlers exist.
log = logging.getLogger("bybit.smart_order")
if not log.handlers and os.environ.get("BYBIT_SMART_ORDER_LOG"):
    logging.basicConfig(level=os.environ.get("BYBIT_SMART_ORDER_LOG", "INFO"),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def format_precision(value, step, rounding_mode=ROUND_HALF_UP) -> str:
    """Round a numeric value to a given step and return a non-scientific string.

    Uses Decimal throughout to avoid binary float drift on small steps.
    """
    if step is None or step <= 0:
        return format(Decimal(str(value)), "f")

    try:
        val_d = Decimal(str(value))
        step_d = Decimal(str(step))
        quantized = val_d.quantize(step_d, rounding=rounding_mode)
        return format(quantized, "f")
    except (InvalidOperation, ValueError):
        return str(value)


def calculate_exit_price_by_pnl(entry_price: float, qty: float, side: str, pnl_usdt: float) -> float:
    """Inverse of PnL: returns the exit price that produces `pnl_usdt` for a linear contract."""
    if qty <= 0:
        return entry_price
    if side.lower() == "buy":
        return entry_price + (pnl_usdt / qty)
    return entry_price - (pnl_usdt / qty)


def calculate_pnl_by_exit_price(entry_price: float, exit_price: float, qty: float, side: str) -> float:
    """Linear (USDT-margined) contract PnL in USDT for the given exit price."""
    if side.lower() == "buy":
        return qty * (exit_price - entry_price)
    return qty * (entry_price - exit_price)


def _atr_wilder(highs, lows, closes, period: int = 14) -> Optional[float]:
    """Wilder's smoothing ATR (more accurate than SMA on the same window)."""
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    # Seed with SMA of first `period` TRs, then Wilder's smoothing.
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _atr_sma(highs, lows, closes, period: int = 14) -> Optional[float]:
    """Plain SMA ATR (kept for backward compatibility / fallback)."""
    if len(highs) < period + 1:
        return None
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]))
        for i in range(1, len(highs))
    ]
    return sum(trs[-period:]) / period


# ---------------------------------------------------------------------------
# API helper wrappers (each returns parsed dict, never raises)
# ---------------------------------------------------------------------------

def _safe_api(method: str, path: str, params: Optional[Dict] = None, signed: bool = False,
              default: Any = None) -> Dict:
    try:
        return bybit_core.api_request(method, path, params=params, signed=signed)
    except Exception as e:  # pragma: no cover - defensive
        log.exception("API call %s %s failed", method, path)
        return {"retCode": -1, "retMsg": f"Exception: {e}",
                "result": default if default is not None else {}}


def get_instrument_info(symbol: str) -> Tuple[Dict, float, float, float, float, float]:
    """Fetch instrument info; returns (info_dict, qty_step, tick_size, min_qty, min_price, min_notional)."""
    qty_step = 0.001
    tick_size = 0.01
    min_qty = 0.0
    min_price = 0.0
    min_notional = 0.0
    info: Dict = {}

    data = _safe_api("GET", "/v5/market/instruments-info",
                     params={"category": "linear", "symbol": symbol}, signed=False)
    if data.get("retCode") == 0:
        lst = data.get("result", {}).get("list", [])
        if lst:
            info = lst[0]
            lot = info.get("lotSizeFilter", {}) or {}
            price = info.get("priceFilter", {}) or {}
            qty_step = float(lot.get("qtyStep", qty_step) or qty_step)
            tick_size = float(price.get("tickSize", tick_size) or tick_size)
            min_qty = float(lot.get("minOrderQty", 0) or 0)
            # Bybit publishes minPrice on some instruments
            min_price = float(price.get("minPrice", 0) or 0)
            # Some payloads include minNotionalValue / minOrderAmt on the leverage filter
            lev_filter = info.get("leverageFilter", {}) or {}
            for key in ("minNotionalValue", "minOrderAmt", "notionalValue"):
                if lev_filter.get(key):
                    try:
                        min_notional = float(lev_filter[key])
                        break
                    except (TypeError, ValueError):
                        pass
    return info, qty_step, tick_size, min_qty, min_price, min_notional


def get_ticker(symbol: str) -> Optional[Dict]:
    data = _safe_api("GET", "/v5/market/tickers",
                     params={"category": "linear", "symbol": symbol}, signed=False)
    if data.get("retCode") != 0:
        return None
    lst = data.get("result", {}).get("list", [])
    return lst[0] if lst else None


def get_wallet_balance() -> Tuple[float, Optional[Dict]]:
    """Return (usdt_equity, raw_response). Tries UNIFIED then CONTRACT."""
    for acct in ("UNIFIED", "CONTRACT"):
        data = _safe_api("GET", "/v5/account/wallet-balance",
                         params={"accountType": acct}, signed=True)
        if data.get("retCode") == 0:
            coins = data.get("result", {}).get("list", [{}])[0].get("coin", [])
            usdt = next((c for c in coins if c.get("coin") == "USDT"), {})
            equity = float(usdt.get("equity", usdt.get("walletBalance", 0)) or 0)
            return equity, data
    return 0.0, None


def get_klines(symbol: str, interval: str = "60", limit: int = 100):
    data = _safe_api("GET", "/v5/market/klines",
                     params={"category": "linear", "symbol": symbol,
                             "interval": interval, "limit": str(limit)},
                     signed=False)
    if data.get("retCode") != 0:
        return []
    klines = data.get("result", {}).get("list", [])
    klines.reverse()  # chronological order
    return klines


def get_position(symbol: str) -> Optional[Dict]:
    """Returns the linear position record (or None) for the symbol."""
    data = _safe_api("GET", "/v5/position/list",
                     params={"category": "linear", "symbol": symbol}, signed=True)
    if data.get("retCode") != 0:
        return None
    for p in data.get("result", {}).get("list", []):
        if p.get("symbol") == symbol:
            return p
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    order_type: str = "Market",
    entry_price: Optional[float] = None,
    leverage: Optional[int] = None,
    risk_pct: float = 1.0,
    sl_dist: Optional[float] = None,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
    rr_ratio: float = 2.0,
    tp_usdt: Optional[float] = None,
    sl_usdt: Optional[float] = None,
    trailing_stop: Optional[float] = None,
    # --- New optional flags (all default to legacy behaviour) ---
    trailing_activation: Optional[float] = None,
    reduce_only: bool = False,
    margin_mode: Optional[str] = None,        # "isolated" or "cross"
    position_idx: int = 0,                    # 0=one-way, 1=buy hedge, 2=sell hedge
    time_in_force: str = "GTC",
    sl_trigger_by: str = "Mark",
    tp_trigger_by: str = "Mark",
    order_link_id: Optional[str] = None,
    dry_run: bool = False,
    max_position_usdt: Optional[float] = None,
):
    """Place a smart order with automatic position sizing and risk management."""

    # ---------------------------------------------------------------------
    # 0. Input validation
    # ---------------------------------------------------------------------
    symbol = (symbol or "BTCUSDT").upper().strip()
    side_norm = side.capitalize() if side.lower() in ("buy", "sell") else None
    if side_norm not in ("Buy", "Sell"):
        return {"success": False, "error": f"Invalid --side '{side}'. Use 'Buy' or 'Sell'."}
    side = side_norm

    order_type_norm = order_type.capitalize() if order_type.lower() in ("market", "limit") else None
    if order_type_norm not in ("Market", "Limit"):
        return {"success": False, "error": f"Invalid --order-type '{order_type}'."}
    order_type = order_type_norm

    if risk_pct is not None and not (0 < risk_pct <= 100):
        return {"success": False, "error": f"--risk-pct must be in (0, 100], got {risk_pct}."}
    if rr_ratio is not None and rr_ratio <= 0:
        return {"success": False, "error": f"--rr-ratio must be > 0, got {rr_ratio}."}
    if sl_usdt is not None and sl_usdt <= 0:
        return {"success": False, "error": f"--sl-usdt must be > 0, got {sl_usdt}."}
    if tp_usdt is not None and tp_usdt <= 0:
        return {"success": False, "error": f"--tp-usdt must be > 0, got {tp_usdt}."}
    if order_link_id is not None and len(order_link_id) > 36:
        return {"success": False, "error": "--order-link-id must be <= 36 characters."}
    if trailing_stop is not None and trailing_stop <= 0:
        return {"success": False, "error": "--trailing-stop must be > 0."}
    if trailing_activation is not None and trailing_stop is None:
        return {"success": False, "error": "--trailing-activation requires --trailing-stop."}
    if margin_mode is not None and margin_mode.lower() not in ("isolated", "cross"):
        return {"success": False, "error": "--margin-mode must be 'isolated' or 'cross'."}
    if position_idx not in (0, 1, 2):
        return {"success": False, "error": "--position-idx must be 0, 1 or 2."}
    if time_in_force not in ("GTC", "IOC", "FOK", "PostOnly"):
        return {"success": False, "error": f"--time-in-force invalid: {time_in_force}"}
    if sl_trigger_by not in ("Mark", "Index", "Last"):
        return {"success": False, "error": f"--sl-trigger-by invalid: {sl_trigger_by}"}
    if tp_trigger_by not in ("Mark", "Index", "Last"):
        return {"success": False, "error": f"--tp-trigger-by invalid: {tp_trigger_by}"}

    # ---------------------------------------------------------------------
    # 1. Instrument precision
    # ---------------------------------------------------------------------
    inst_info, qty_step, tick_size, min_qty, min_price, min_notional = get_instrument_info(symbol)
    log.info("Instrument %s: qtyStep=%s tickSize=%s minQty=%s minNotional=%s",
             symbol, qty_step, tick_size, min_qty, min_notional)

    # ---------------------------------------------------------------------
    # 2. Margin mode (optional, must be set BEFORE leverage in hedge mode)
    # ---------------------------------------------------------------------
    if margin_mode is not None:
        mm_data = _safe_api(
            "POST", "/v5/position/set-margin-mode",
            params={"category": "linear", "symbol": symbol,
                    "tradeMode": 0 if margin_mode.lower() == "isolated" else 1},
            signed=True,
        )
        if mm_data.get("retCode") not in (0, 110043):
            return {"success": False,
                    "error": f"Failed to set margin mode: {mm_data.get('retMsg')}",
                    "retCode": mm_data.get("retCode")}

    # ---------------------------------------------------------------------
    # 3. Leverage
    # ---------------------------------------------------------------------
    if leverage is not None:
        lev_params: Dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        if position_idx in (1, 2):
            lev_params["positionIdx"] = position_idx
        lev_data = _safe_api("POST", "/v5/position/set-leverage",
                             params=lev_params, signed=True)
        if lev_data.get("retCode") not in (0, 110043):
            return {"success": False,
                    "error": f"Failed to set leverage: {lev_data.get('retMsg')}",
                    "retCode": lev_data.get("retCode")}

    # ---------------------------------------------------------------------
    # 4. Current market price
    # ---------------------------------------------------------------------
    ticker = get_ticker(symbol)
    if not ticker:
        return {"success": False, "error": f"Failed to get ticker for {symbol}."}
    try:
        current_price = float(ticker.get("lastPrice", 0))
    except (TypeError, ValueError):
        return {"success": False, "error": "Ticker has no valid lastPrice."}
    if current_price <= 0:
        return {"success": False, "error": "Invalid lastPrice from ticker."}

    # ---------------------------------------------------------------------
    # 5. Determine calc_price (Market vs Limit)
    # ---------------------------------------------------------------------
    is_limit = order_type == "Limit"
    if is_limit and entry_price is not None:
        try:
            calc_price = float(entry_price)
        except (TypeError, ValueError):
            return {"success": False, "error": f"Invalid --entry-price {entry_price}"}
        if calc_price <= 0:
            return {"success": False, "error": "--entry-price must be > 0"}
    else:
        calc_price = current_price
        is_limit = False

    # ---------------------------------------------------------------------
    # 6. Balance & risk budget
    # ---------------------------------------------------------------------
    balance, _bal_raw = get_wallet_balance()
    if balance <= 0 and sl_usdt is None:
        return {"success": False,
                "error": "Insufficient USDT balance to calculate risk."}

    risk_amount = float(sl_usdt) if sl_usdt is not None else balance * (risk_pct / 100.0)
    if risk_amount <= 0:
        return {"success": False, "error": "Computed risk amount is <= 0."}

    # ---------------------------------------------------------------------
    # 7. ATR for default SL distance (Wilder's smoothing, SMA fallback)
    # ---------------------------------------------------------------------
    klines = get_klines(symbol, interval="60", limit=100)
    atr: Optional[float] = None
    if klines:
        try:
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            atr = _atr_wilder(highs, lows, closes, 14) or _atr_sma(highs, lows, closes, 14)
        except (ValueError, IndexError):
            atr = None
    if not atr or atr <= 0:
        atr = calc_price * 0.01  # safe default 1% of price

    # ---------------------------------------------------------------------
    # 8. Stop-loss price
    # ---------------------------------------------------------------------
    if sl_price is not None:
        stop_loss = float(sl_price)
    elif sl_dist is not None:
        stop_loss = calc_price - sl_dist if side == "Buy" else calc_price + sl_dist
    else:
        stop_loss = calc_price - (atr * 2) if side == "Buy" else calc_price + (atr * 2)

    if stop_loss <= 0:
        return {"success": False, "error": f"Stop loss computed as non-positive: {stop_loss}"}

    # Sanity: stop loss must be on the correct side of entry.
    if side == "Buy" and stop_loss >= calc_price:
        return {"success": False,
                "error": (f"Invalid stop loss ({stop_loss}) for Buy: must be < entry ({calc_price}). "
                          "Provide --sl-dist, --sl-price or widen ATR.")}
    if side == "Sell" and stop_loss <= calc_price:
        return {"success": False,
                "error": (f"Invalid stop loss ({stop_loss}) for Sell: must be > entry ({calc_price}). "
                          "Provide --sl-dist, --sl-price or widen ATR.")}

    # ---------------------------------------------------------------------
    # 9. Position sizing
    # ---------------------------------------------------------------------
    price_diff = abs(calc_price - stop_loss)
    if price_diff > 0:
        qty = risk_amount / price_diff
    else:
        # Shouldn't happen after the sanity check above, but keep a fallback.
        qty = risk_amount / calc_price

    qty_str = format_precision(qty, qty_step, ROUND_DOWN)
    actual_qty = float(qty_str)

    # Min qty check
    if min_qty and actual_qty < min_qty:
        return {"success": False,
                "error": (f"Calculated qty {actual_qty} < exchange minOrderQty {min_qty}. "
                          "Increase --risk-pct, --sl-usdt or use a wider stop.")}

    if actual_qty <= 0:
        return {"success": False,
                "error": f"Calculated quantity is too small (step={qty_step}, risk={risk_amount})."}

    # Notional safety cap
    notional = actual_qty * calc_price
    if max_position_usdt is not None and notional > max_position_usdt:
        cap_qty = max_position_usdt / calc_price
        cap_qty_str = format_precision(cap_qty, qty_step, ROUND_DOWN)
        log.warning("Notional %.2f > cap %.2f; capping qty %s -> %s",
                    notional, max_position_usdt, qty_str, cap_qty_str)
        qty_str = cap_qty_str
        actual_qty = float(qty_str)
        notional = actual_qty * calc_price
        if actual_qty <= 0 or (min_qty and actual_qty < min_qty):
            return {"success": False,
                    "error": "After applying --max-position-usdt cap, qty is below minOrderQty."}

    # Min notional check (only when exchange advertises a value)
    if min_notional and notional < min_notional and not reduce_only:
        return {"success": False,
                "error": (f"Order notional {notional:.4f} USDT < exchange min "
                          f"{min_notional:.4f} USDT. Increase risk or use a smaller-precision pair.")}

    # Stop loss strictly on tick grid
    stop_loss_str = format_precision(stop_loss, tick_size, ROUND_HALF_UP)

    # ---------------------------------------------------------------------
    # 10. Take-profit price
    # ---------------------------------------------------------------------
    if tp_price is not None:
        take_profit = float(tp_price)
        if side == "Buy" and take_profit <= calc_price:
            return {"success": False, "error": "TP must be > entry for Buy orders."}
        if side == "Sell" and take_profit >= calc_price:
            return {"success": False, "error": "TP must be < entry for Sell orders."}
    elif tp_usdt is not None:
        take_profit = calculate_exit_price_by_pnl(calc_price, actual_qty, side, float(tp_usdt))
    else:
        tp_distance = price_diff * rr_ratio
        take_profit = calc_price + tp_distance if side == "Buy" else calc_price - tp_distance

    take_profit_str = format_precision(take_profit, tick_size, ROUND_HALF_UP)

    expected_profit = calculate_pnl_by_exit_price(calc_price, float(take_profit_str), actual_qty, side)
    expected_loss = calculate_pnl_by_exit_price(calc_price, float(stop_loss_str), actual_qty, side)

    # ---------------------------------------------------------------------
    # 11. Build /v5/order/create payload
    # ---------------------------------------------------------------------
    params: Dict[str, Any] = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Limit" if is_limit else "Market",
        "qty": qty_str,
        "timeInForce": time_in_force,
        "positionIdx": position_idx,
        "slTriggerBy": sl_trigger_by,
        "tpTriggerBy": tp_trigger_by,
        "stopLoss": stop_loss_str,
        "takeProfit": take_profit_str,
    }
    if is_limit:
        params["price"] = format_precision(calc_price, tick_size, ROUND_HALF_UP)
    if trailing_stop is not None:
        params["trailingStop"] = format_precision(float(trailing_stop), tick_size, ROUND_HALF_UP)
    if trailing_activation is not None:
        params["activePrice"] = format_precision(float(trailing_activation), tick_size, ROUND_HALF_UP)
    if reduce_only:
        params["reduceOnly"] = True
    if order_link_id:
        params["orderLinkId"] = order_link_id

    # Bybit requires stopLoss whenever trailingStop is set. The builder above
    # always sets stopLoss, so this is satisfied by construction.

    payload = {
        "success": True,
        "dry_run": dry_run,
        "symbol": symbol,
        "side": side,
        "order_type": "Limit" if is_limit else "Market",
        "qty": actual_qty,
        "entry_price": calc_price,
        "stop_loss": float(stop_loss_str),
        "take_profit": float(take_profit_str),
        "trailing_stop_dist": float(trailing_stop) if trailing_stop is not None else None,
        "trailing_activation": float(trailing_activation) if trailing_activation is not None else None,
        "expected_profit_usdt": round(expected_profit, 2),
        "expected_loss_usdt": round(abs(expected_loss), 2),
        "notional_usdt": round(notional, 4),
        "risk_pct": risk_pct if sl_usdt is None else None,
        "risk_amount": round(risk_amount, 2),
        "balance": round(balance, 2),
        "order_params": params,
    }

    if dry_run:
        payload["note"] = "Dry run: order was NOT submitted."
        return payload

    # ---------------------------------------------------------------------
    # 12. Submit
    # ---------------------------------------------------------------------
    data = _safe_api("POST", "/v5/order/create", params=params, signed=True)
    if data.get("retCode") == 0:
        payload.update({
            "order_id": data.get("result", {}).get("orderId"),
            "order_link_id": data.get("result", {}).get("orderLinkId"),
            "data": data.get("result"),
        })
        return payload

    return {"success": False,
            "error": data.get("retMsg"),
            "retCode": data.get("retCode"),
            "order_params": params}


# ---------------------------------------------------------------------------
# CLI / argc entry
# ---------------------------------------------------------------------------

def _coerce(value: str) -> Any:
    """Best-effort coercion of an env-var string into bool/int/float/str."""
    if value == "":
        return None
    low = value.lower()
    if low in ("true", "yes", "1"):
        return True
    if low in ("false", "no", "0"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


if __name__ == "__main__":
    from argparse import ArgumentParser

    # ---- argc env-var mode (sub-agent compatibility) --------------------
    if any(k.startswith("argc_") for k in os.environ):
        kwargs = {}
        for k, v in os.environ.items():
            if not k.startswith("argc_"):
                continue
            kwargs[k[5:].replace("-", "_")] = _coerce(v)
        result = run(**kwargs)
        print(json.dumps(result, indent=2))
        sys.exit(0)

    # ---- Standard CLI mode ----------------------------------------------
    parser = ArgumentParser(description="Place a smart order on Bybit")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--side", default="Buy")
    parser.add_argument("--order-type", default="Market", choices=["Market", "Limit"])
    parser.add_argument("--entry-price", type=float, default=None)
    parser.add_argument("--leverage", type=int, default=None)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--sl-dist", type=float, default=None)
    parser.add_argument("--sl-price", type=float, default=None)
    parser.add_argument("--tp-price", type=float, default=None)
    parser.add_argument("--rr-ratio", type=float, default=2.0)
    parser.add_argument("--tp-usdt", type=float, default=None)
    parser.add_argument("--sl-usdt", type=float, default=None)
    parser.add_argument("--trailing-stop", type=float, default=None)
    parser.add_argument("--trailing-activation", type=float, default=None)
    parser.add_argument("--reduce-only", action="store_true")
    parser.add_argument("--margin-mode", default=None, choices=["isolated", "cross"])
    parser.add_argument("--position-idx", type=int, default=0, choices=[0, 1, 2])
    parser.add_argument("--time-in-force", default="GTC", choices=["GTC", "IOC", "FOK", "PostOnly"])
    parser.add_argument("--sl-trigger-by", default="Mark", choices=["Mark", "Index", "Last"])
    parser.add_argument("--tp-trigger-by", default="Mark", choices=["Mark", "Index", "Last"])
    parser.add_argument("--order-link-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-position-usdt", type=float, default=None)
    args = parser.parse_args()

    result = run(
        symbol=args.symbol,
        side=args.side,
        order_type=args.order_type,
        entry_price=args.entry_price,
        leverage=args.leverage,
        risk_pct=args.risk_pct,
        sl_dist=args.sl_dist,
        sl_price=args.sl_price,
        tp_price=args.tp_price,
        rr_ratio=args.rr_ratio,
        tp_usdt=args.tp_usdt,
        sl_usdt=args.sl_usdt,
        trailing_stop=args.trailing_stop,
        trailing_activation=args.trailing_activation,
        reduce_only=args.reduce_only,
        margin_mode=args.margin_mode,
        position_idx=args.position_idx,
        time_in_force=args.time_in_force,
        sl_trigger_by=args.sl_trigger_by,
        tp_trigger_by=args.tp_trigger_by,
        order_link_id=args.order_link_id,
        dry_run=args.dry_run,
        max_position_usdt=args.max_position_usdt,
    )
    print(json.dumps(result, indent=2))