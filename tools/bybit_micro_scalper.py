#!/usr/bin/env python3
"""
Bybit Micro-Scalper Tool (V5 Unified Edition)
=============================================
@describe High-frequency micro-scalper for Bybit V5 linear perpetuals using
          orderbook imbalance, RSI/EMA micro-momentum, and fee-aware maker/taker
          profit solving.
@option --symbol <STR>       Trading pair symbol (default: BTCUSDT)
@option --risk-pct <FLOAT>   Risk percentage per trade (default: 1.0)
@option --leverage <INT>     Leverage multiplier (default: 20)
@option --tp-pct <FLOAT>     Take-profit percentage (default: 0.35)
@option --sl-pct <FLOAT>     Stop-loss percentage (default: 0.25)
@flag   --post-only          Use PostOnly Maker entry (0.02% fee)
@flag   --execute            Submit live order instead of dry-run analysis
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

try:
    import bybit_core
except ImportError:
    bybit_core = None  # type: ignore

try:
    import micro_profit
except ImportError:
    micro_profit = None  # type: ignore

try:
    import bybit_smart_order
except ImportError:
    bybit_smart_order = None  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("bybit_micro_scalper")

BYBIT_API_URL = "https://api.bybit.com"


def _api(
    method: str,
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    signed: bool = False,
) -> Dict[str, Any]:
    if bybit_core and hasattr(bybit_core, "api_request"):
        return bybit_core.api_request(method, endpoint, params=params, signed=signed)
    import requests

    url = f"{BYBIT_API_URL}{endpoint}"
    resp = (
        requests.get(url, params=params, timeout=10)
        if method.upper() == "GET"
        else requests.post(url, json=params, timeout=10)
    )
    return resp.json()


def get_orderbook(
    symbol: str, limit: int = 25, category: str = "linear"
) -> Optional[Dict[str, List[List[float]]]]:
    """Fetch and parse V5 L2 orderbook into {'bids': [[px, qty], ...], 'asks': [[px, qty], ...]}."""
    data = _api(
        "GET",
        "/v5/market/orderbook",
        params={"category": category, "symbol": symbol.upper(), "limit": limit},
    )
    if data.get("retCode") != 0:
        return None
    res = data.get("result", {})
    bids = [[float(b[0]), float(b[1])] for b in res.get("b", []) if len(b) >= 2]
    asks = [[float(a[0]), float(a[1])] for a in res.get("a", []) if len(a) >= 2]
    if not bids or not asks:
        return None
    return {"bids": bids, "asks": asks}


def get_klines(
    symbol: str, interval: str = "1", limit: int = 60, category: str = "linear"
) -> Optional[List[Dict[str, float]]]:
    """Fetch V5 klines and return in chronological (oldest-first) order."""
    data = _api(
        "GET",
        "/v5/market/kline",
        params={
            "category": category,
            "symbol": symbol.upper(),
            "interval": str(interval),
            "limit": limit,
        },
    )
    if data.get("retCode") != 0:
        return None
    rows = data.get("result", {}).get("list", [])
    if not rows:
        return None
    # Bybit V5 returns newest-first; reverse to chronological order
    if len(rows) >= 2 and float(rows[0][0]) > float(rows[-1][0]):
        rows = list(reversed(rows))
    return [
        {
            "timestamp": float(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in rows
    ]


def get_account_balance(coin: str = "USDT") -> Optional[float]:
    """Fetch available USDT balance from V5 UNIFIED wallet."""
    data = _api(
        "GET",
        "/v5/account/wallet-balance",
        params={"accountType": "UNIFIED", "coin": coin},
        signed=True,
    )
    if data.get("retCode") != 0:
        return None
    for acct in data.get("result", {}).get("list", []):
        avail = float(acct.get("totalAvailableBalance") or 0.0)
        if avail > 0:
            return avail
        for c in acct.get("coin", []):
            if c.get("coin") == coin:
                eq = float(c.get("equity") or c.get("walletBalance") or 0.0)
                im = float(c.get("totalPositionIM") or 0.0) + float(
                    c.get("totalOrderIM") or 0.0
                )
                return max(0.0, eq - im)
    return 0.0


def calculate_ema(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1.0 - k)
    return ema


def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def analyze_scalp_signal(
    symbol: str,
    orderbook: Dict[str, List[List[float]]],
    klines: List[Dict[str, float]],
    obi_threshold: float = 0.15,
) -> Dict[str, Any]:
    """Combine decay-weighted orderbook imbalance with EMA9/EMA21 + RSI14."""
    bids = orderbook["bids"]
    asks = orderbook["asks"]
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid_px = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / max(mid_px, 1e-9)) * 10000.0

    bid_w = sum(b[1] / (i + 1) for i, b in enumerate(bids[:15]))
    ask_w = sum(a[1] / (i + 1) for i, a in enumerate(asks[:15]))
    obi = (bid_w - ask_w) / max(bid_w + ask_w, 1e-9)

    closes = [k["close"] for k in klines]
    ema_fast = calculate_ema(closes, 9) or closes[-1]
    ema_slow = calculate_ema(closes, 21) or closes[-1]
    rsi = calculate_rsi(closes, 14) or 50.0

    signal = "HOLD"
    side: Optional[str] = None
    score = 0.0

    if obi >= obi_threshold and ema_fast >= ema_slow and rsi < 72:
        signal = "BUY"
        side = "Buy"
        score = round(min(100.0, 50.0 + obi * 40.0 + (70.0 - rsi) * 0.4), 2)
    elif obi <= -obi_threshold and ema_fast <= ema_slow and rsi > 28:
        signal = "SELL"
        side = "Sell"
        score = round(min(100.0, 50.0 + abs(obi) * 40.0 + (rsi - 30.0) * 0.4), 2)

    return {
        "symbol": symbol.upper(),
        "signal": signal,
        "side": side,
        "confidence": score,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": round(spread_bps, 2),
        "orderbook_imbalance": round(obi, 4),
        "ema_fast": round(ema_fast, 6),
        "ema_slow": round(ema_slow, 6),
        "rsi": round(rsi, 2),
    }


def run(
    symbol: Any = "BTCUSDT",
    risk_pct: float = 1.0,
    leverage: int = 20,
    tp_pct: float = 0.35,
    sl_pct: float = 0.25,
    post_only: bool = True,
    execute: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Programmatic and LLM-tool entrypoint for Bybit Micro-Scalper."""
    if isinstance(symbol, argparse.Namespace):
        ns = symbol
        symbol = getattr(ns, "symbol", "BTCUSDT")
        risk_pct = float(getattr(ns, "risk_pct", 1.0))
        leverage = int(getattr(ns, "leverage", 20))
        tp_pct = float(getattr(ns, "tp_pct", 0.35))
        sl_pct = float(getattr(ns, "sl_pct", 0.25))
        post_only = bool(getattr(ns, "post_only", True))
        execute = bool(getattr(ns, "execute", False))

    symbol = str(symbol).upper().strip()
    ob = get_orderbook(symbol)
    if not ob:
        return {"status": "error", "error": f"Failed to fetch orderbook for {symbol}"}

    kl = get_klines(symbol, interval="1", limit=60)
    if not kl:
        return {"status": "error", "error": f"Failed to fetch klines for {symbol}"}

    sig = analyze_scalp_signal(symbol, ob, kl)
    balance = get_account_balance("USDT") or float(kwargs.get("balance") or 0.0)

    plan: Dict[str, Any] = {}
    if micro_profit and sig["side"]:
        plan = micro_profit.calculate_micro_profit(
            symbol=symbol,
            side=sig["side"].lower(),
            qty=float(kwargs.get("qty") or 0.0),
            balance=balance,
            leverage=float(leverage),
            post_only=post_only,
            bids=ob["bids"],
            asks=ob["asks"],
        )

    result: Dict[str, Any] = {
        "status": "ok",
        "symbol": symbol,
        "analysis": sig,
        "balance_usdt": balance,
        "micro_profit_plan": plan,
        "executed": False,
    }

    if execute and sig["side"] and bybit_smart_order:
        entry_px = sig["best_bid"] if sig["side"] == "Buy" else sig["best_ask"]
        order_res = bybit_smart_order.execute_smart_order(
            symbol=symbol,
            side=sig["side"],
            order_type="Limit" if post_only else "Market",
            entry_price=entry_px if post_only else None,
            time_in_force="PostOnly" if post_only else "IOC",
            leverage=leverage,
            risk_pct=risk_pct,
            qty=plan.get("trading_qty") if isinstance(plan, dict) else None,
            sl_price=plan.get("stop_loss_price") if isinstance(plan, dict) else None,
            tp_price=plan.get("target_exit_price") if isinstance(plan, dict) else None,
            dry_run=False,
        )
        result["executed"] = bool(order_res.get("success"))
        result["order_result"] = order_res

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit Micro-Scalper V5")
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--leverage", type=int, default=20)
    parser.add_argument("--tp-pct", type=float, default=0.35)
    parser.add_argument("--sl-pct", type=float, default=0.25)
    parser.add_argument("--post-only", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true", default=False)
    args = parser.parse_args()
    res = run(args)
    out_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    payload = json.dumps(res, indent=2) + "\n"
    if out_path in ("/dev/stdout", "-"):
        sys.stdout.write(payload)
    else:
        Path(out_path).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
