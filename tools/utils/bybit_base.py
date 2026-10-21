#!/usr/bin/env python3
"""
Bybit API Base Module (Professional Edition - Optimized)
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def get_config() -> Dict[str, Any]:
    # Load .env if not loaded
    if "BYBIT_API_KEY" not in os.environ:
        for path in [
            ".env",
            "../.env",
            os.path.expanduser("~/.config/aichat/llm-functions/.env"),
        ]:
            if os.path.isfile(path):
                with open(path) as f:
                    for line in f:
                        if "=" in line:
                            k, v = line.strip().split("=", 1)
                            os.environ[k.strip()] = v.strip().strip('"').strip("'")
                break

    testnet = os.environ.get("BYBIT_TESTNET", "false").lower() in ("true", "1", "yes")
    return {
        "api_key": os.environ.get("BYBIT_API_KEY", ""),
        "api_secret": os.environ.get("BYBIT_API_SECRET", ""),
        "base_url": "https://api-testnet.bybit.com"
        if testnet
        else "https://api.bybit.com",
        "recv_window": os.environ.get("BYBIT_RECV_WINDOW", "5000"),
    }


# --------------------------------------------------------------------------
# Signature & Request Core Logic
# --------------------------------------------------------------------------
def make_bybit_request(
    endpoint: str,
    method: str = "GET",
    payload_data: Optional[Dict[str, Any]] = None,
    recv_window: int = 5000,
) -> Dict[str, Any]:
    """
    Signs and executes a pure Python HTTP request to the Bybit V5 API.
    """
    config = get_config()
    timestamp = str(int(time.time() * 1000))
    url = config["base_url"] + endpoint

    # 1. Format the payload string based on HTTP Method
    if method == "GET" and payload_data:
        # Sort query keys alphabetically and URL encode them
        sorted_params = sorted(payload_data.items())
        payload_str = urllib.parse.urlencode(sorted_params)
        url += "?" + payload_str
        req_body = None
    elif method == "POST" and payload_data:
        # Strict JSON string with NO white spaces between keys/values
        payload_str = json.dumps(payload_data, separators=(",", ":"), sort_keys=True)
        req_body = payload_str.encode("utf-8")
    else:
        payload_str = ""
        req_body = None

    # 2. Generate the Bybit cryptographic signature
    # Sequence: timestamp + API_KEY + recv_window + payload_string
    param_str = timestamp + config["api_key"] + str(recv_window) + payload_str
    signature = hmac.new(
        bytes(config["api_secret"], "utf-8"), bytes(param_str, "utf-8"), hashlib.sha256
    ).hexdigest()

    # 3. Construct required HTTP headers
    headers = {
        "X-BAPI-API-KEY": config["api_key"],
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": str(recv_window),
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json",
    }

    # 4. Execute the network request safely using urllib
    req = urllib.request.Request(url, data=req_body, headers=headers, method=method)

    # Apply proxy if configured in environment
    if os.environ.get("HTTPS_PROXY"):
        proxy_handler = urllib.request.ProxyHandler(
            {"https": os.environ["HTTPS_PROXY"]}
        )
        opener = urllib.request.build_opener(proxy_handler)
        urllib.request.install_opener(opener)

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            res_body = response.read().decode("utf-8")
            return json.loads(res_body)
    except HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            return json.loads(error_body)
        except:
            return {"error": f"HTTP {e.code}: {error_body}"}
    except URLError as e:
        return {"error": str(e.reason)}


# Alias for compatibility with existing scripts
def api_request(
    method: str,
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    signed: bool = False,
) -> Dict[str, Any]:
    return make_bybit_request(endpoint, method, params)


def _direction(side: str) -> float:
    return 1.0 if str(side).strip().lower() in ("buy", "long", "b", "l") else -1.0


def _fees_in_usdt(
    fee_rate: float, entry_price: float, exit_price: float, size: float
) -> float:
    return abs(fee_rate) * size * (float(entry_price) + float(exit_price))


def calculate_pnl(
    entry_price: float,
    exit_price: float,
    size: float,
    side: str,
    fees: float = 0.0,
    leverage: float = 1.0,
) -> dict:
    entry = float(entry_price)
    exit_ = float(exit_price)
    qty = float(size)
    d = _direction(side)
    gross = (exit_ - entry) * qty * d
    if 0 < float(fees) < 0.01:
        fee_usdt = _fees_in_usdt(float(fees), entry, exit_, qty)
    else:
        fee_usdt = float(fees)
    net = gross - fee_usdt
    margin = (entry * qty) / max(float(leverage) or 1.0, 1.0)
    return {
        "gross_pnl": round(gross, 6),
        "fees": round(fee_usdt, 6),
        "net_pnl": round(net, 6),
        "roe_pct": round(net / margin * 100, 4) if margin else 0.0,
        "side": side,
        "leverage": float(leverage),
    }


def calculate_exit_price(
    entry_price: float,
    target_pnl: float,
    size: float,
    side: str,
    fees: float = 0.0,
) -> float:
    entry = float(entry_price)
    qty = float(size)
    d = _direction(side)
    target = float(target_pnl)
    if qty <= 0:
        raise ValueError("size must be positive")
    if 0 < float(fees) < 0.01:
        rate = float(fees)
        num = target + qty * d * entry + rate * qty * entry
        den = qty * d - rate * qty
    else:
        num = (target + float(fees)) + qty * d * entry
        den = qty * d
    if abs(den) < 1e-15:
        raise ValueError("denominator is zero; cannot solve for exit price")
    return round(num / den, 8)


def format_qty_to_step(qty: float, qty_step: Any) -> str:
    step = float(qty_step or 0)
    if step <= 0:
        return str(qty)
    prec = len(str(step).split(".")[-1]) if "." in str(step) else 0
    stepped = (float(qty) // step) * step
    return f"{stepped:.{prec}f}"


def format_price_to_tick(price: float, tick_size: Any) -> str:
    tick = float(tick_size or 0)
    if tick <= 0:
        return str(price)
    prec = len(str(tick).split(".")[-1]) if "." in str(tick) else 0
    rounded = round(float(price) / tick) * tick
    return f"{rounded:.{prec}f}"

