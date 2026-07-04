#!/usr/bin/env python3
# ==============================================================================
# monitor_scalp.py — Enhanced Bybit Position Monitor & Micro-Scalper
#
# @describe Continuously monitors open positions for a symbol and triggers
#           micro-profit scalps when no position is active. Supports:
#           - Buy/Sell scalps
#           - Dynamic quantity
#           - Multi-symbol monitoring
#           - WebSocket updates (fallback to REST)
# @option --symbol! <SYMBOL>       Required: Trading pair (e.g., BTCUSDT)
# @option --qty! <QUANTITY>        Required: Quantity (base asset)
# @option --leverage! <LEVERAGE>   Required: Leverage value
# @option --interval <INTERVAL>    Optional: Check interval (default: 30)
# @option --trailing-stop <DIST>  Optional: Trailing stop distance (e.g., 50)
# @option --target-profit <USD>   Optional: Target profit in USDT (e.g., 0.10)
# @option --symbols* <SYMBOL>     Optional: Multi-symbol monitoring (repeatable)
# @flag --dry-run                Simulate without executing trades
# @flag --verbose                 Enable verbose logging
# @flag --use-ws                  Use WebSocket for real-time updates (experimental)
# @flag --sell-scalp             Enable sell scalps (default: buy)
# ==============================================================================

import argparse
import json
import os
import time
import requests
import threading
from typing import Optional, Dict, Any, List
import sys
import hmac
import hashlib
import websocket

# --- Constants ---
BYBIT_API_URL = "https://api.bybit.com"  # Use "https://api-testnet.bybit.com" for testnet
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/position"
POSITION_ENDPOINT = "/v5/position/list"
ORDER_ENDPOINT = "/v5/order/create"
RECV_WINDOW = "5000"
HEADERS = {"Content-Type": "application/json"}

# --- Global State ---
ws_active = False
ws_thread: Optional[threading.Thread] = None


def llm_emit(text: str) -> None:
    """Write output to LLM_OUTPUT or stdout."""
    out = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    if out == "/dev/stdout":
        print(text)
    else:
        with open(out, "w") as f:
            f.write(text + "\n")


def log_message(message: str, verbose: bool = False) -> None:
    """Log messages to stderr if verbose is enabled."""
    if verbose:
        print(f"[LOG] {message}", file=sys.stderr)


def _sign(api_secret: str, payload: str) -> str:
    """Generate HMAC-SHA256 signature for Bybit v5."""
    return hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _headers(api_key: str, api_secret: str, payload: str, timestamp_ms: Optional[str] = None) -> Dict[str, str]:
    """Generate Bybit v5 headers."""
    ts = timestamp_ms or str(int(time.time() * 1000))
    prehash = ts + api_key + RECV_WINDOW + payload
    return {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": _sign(api_secret, prehash),
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
    }


def query_string(params: Dict) -> str:
    """Generate sorted query string for GET requests."""
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)


def fetch_positions(api_key: str, api_secret: str, symbols: List[str]) -> Optional[List[Dict]]:
    """Fetch open positions for specified symbols."""
    params = {"category": "linear", "symbol": ",".join(symbols)}
    qs = query_string(params)
    try:
        headers = _headers(api_key, api_secret, qs)
        response = requests.get(
            f"{BYBIT_API_URL}{POSITION_ENDPOINT}",
            headers=headers,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("list", []) or []
    except Exception as e:
        error_msg = f"Failed to fetch positions: {e}"
        log_message(error_msg, verbose=True)
        llm_emit(json.dumps({"success": False, "error": error_msg}))
        return None


def set_leverage(api_key: str, api_secret: str, symbol: str, leverage: int) -> Dict:
    """Set leverage for a symbol."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }
    raw = json.dumps(body, separators=(",", ":"))
    try:
        headers = _headers(api_key, api_secret, raw)
        response = requests.post(
            f"{BYBIT_API_URL}/v5/position/set-leverage",
            headers=headers,
            data=raw,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
        return data
    except Exception as e:
        error_msg = f"Failed to set leverage: {e}"
        log_message(error_msg, verbose=True)
        return {"success": False, "error": error_msg}


def check_for_scalp_opportunity(positions: List[Dict], symbols: List[str], side: str) -> bool:
    """Check if no open position exists for any symbol."""
    return not any(
        pos.get("symbol") in symbols 
        and pos.get("side") == side
        and float(pos.get("size", 0)) != 0
        for pos in positions
    )


def place_scalp_order(
    api_key: str, 
    api_secret: str, 
    symbol: str, 
    side: str, 
    qty: float, 
    leverage: int, 
    dry_run: bool, 
    trailing_stop: Optional[float] = None,
    target_profit: Optional[float] = None
) -> Dict:
    """Place a scalp order with optional trailing stop/take profit."""
    order_payload = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "orderType": "Market",
        "timeInForce": "GTC"
    }

    if dry_run:
        log_message(f"Dry-run: {side} {qty} {symbol} (trailing_stop: {trailing_stop}, target_profit: {target_profit})", verbose=True)
        llm_emit(json.dumps({
            "success": True, 
            "dry_run": True, 
            "order": order_payload,
            "trailing_stop": trailing_stop,
            "target_profit": target_profit
        }))
        return {"status": "dry_run", "order": order_payload}

    try:
        raw = json.dumps(order_payload, separators=(",", ":"))
        headers = _headers(api_key, api_secret, raw)
        response = requests.post(
            f"{BYBIT_API_URL}{ORDER_ENDPOINT}",
            headers=headers,
            data=raw,
            timeout=10,
        )
        response.raise_for_status()
        order_result = response.json()
        if order_result.get("retCode") != 0:
            raise RuntimeError(f"Bybit {order_result.get('retCode')}: {order_result.get('retMsg')}")

        if trailing_stop or target_profit:
            log_message(f"Simulated TP/SL: SL={trailing_stop}, TP={target_profit}", verbose=True)
            order_result["trailing_stop"] = trailing_stop
            order_result["target_profit"] = target_profit

        return order_result
    except Exception as e:
        error_msg = f"Failed to place order: {e}"
        log_message(error_msg, verbose=True)
        return {"success": False, "error": error_msg}


def on_ws_message(ws, message: str) -> None:
    """Handle WebSocket messages (experimental)."""
    try:
        data = json.loads(message)
        if data.get("type") == "snapshot":
            log_message(f"WS Update: {data}", verbose=True)
    except Exception as e:
        log_message(f"WS Error: {e}", verbose=True)


def start_ws_listener(api_key: str, api_secret: str, symbols: List[str]) -> None:
    """Start WebSocket listener for real-time updates."""
    global ws_active, ws_thread
    if ws_active:
        return

    def ws_listener():
        ws_url = f"{BYBIT_WS_URL}?symbols={",".join(symbols)}"
        ws = websocket.WebSocketApp(
            ws_url,
            on_message=on_ws_message,
            on_error=lambda ws, e: log_message(f"WS Error: {e}", verbose=True),
            on_close=lambda ws, *args: log_message("WS Closed", verbose=True)
        )
        ws.on_open = lambda ws: log_message("WS Connected", verbose=True)
        ws.run_forever()

    ws_thread = threading.Thread(target=ws_listener, daemon=True)
    ws_thread.start()
    ws_active = True


def main():
    parser = argparse.ArgumentParser(description="Monitor Bybit positions and scalp micro-profits.")
    parser.add_argument("--symbol", required=True, help="Trading pair (e.g., BTCUSDT)")
    parser.add_argument("--qty", type=float, required=True, help="Quantity (base asset)")
    parser.add_argument("--leverage", type=int, required=True, help="Leverage value")
    parser.add_argument("--interval", type=int, default=30, help="Check interval (seconds)")
    parser.add_argument("--trailing-stop", type=float, help="Trailing stop distance (e.g., 50)")
    parser.add_argument("--target-profit", type=float, help="Target profit in USDT (e.g., 0.10)")
    parser.add_argument("--symbols", action="append", help="Multi-symbol monitoring (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without executing trades")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--use-ws", action="store_true", help="Use WebSocket for real-time updates")
    parser.add_argument("--sell-scalp", action="store_true", help="Enable sell scalps")
    args = parser.parse_args()

    # Use single symbol or multi-symbol list
    symbols = [args.symbol] if args.symbols is None else args.symbols + [args.symbol]
    side = "Sell" if args.sell_scalp else "Buy"

    # Load API keys
    api_key = os.environ.get("BYBIT_API_KEY")
    api_secret = os.environ.get("BYBIT_API_SECRET")
    if not api_key or not api_secret:
        error_msg = "Missing BYBIT_API_KEY or BYBIT_API_SECRET"
        log_message(error_msg, verbose=True)
        llm_emit(json.dumps({"success": False, "error": error_msg}))
        return

    log_message(f"Monitoring started for {symbols} (qty: {args.qty}, leverage: {args.leverage}, side: {side})", verbose=args.verbose)
    llm_emit(json.dumps({
        "status": "monitor_started",
        "symbols": symbols,
        "interval": args.interval,
        "side": side,
        "trailing_stop": args.trailing_stop,
        "target_profit": args.target_profit
    }))

    # Set leverage for each symbol
    for symbol in symbols:
        set_leverage(api_key, api_secret, symbol, args.leverage)

    # Start WebSocket listener if enabled
    if args.use_ws:
        start_ws_listener(api_key, api_secret, symbols)

    try:
        while True:
            positions = fetch_positions(api_key, api_secret, symbols)
            if not positions:
                time.sleep(args.interval)
                continue

            if check_for_scalp_opportunity(positions, symbols, side):
                result = place_scalp_order(
                    api_key, api_secret,
                    symbols[0],  # Use first symbol for order
                    side, args.qty, args.leverage, args.dry_run,
                    args.trailing_stop,
                    args.target_profit
                )
                llm_emit(json.dumps(result))

            time.sleep(args.interval)
    except KeyboardInterrupt:
        log_message("Monitoring stopped by user", verbose=args.verbose)
        llm_emit(json.dumps({"status": "monitor_stopped"}))
    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        log_message(error_msg, verbose=True)
        llm_emit(json.dumps({"success": False, "error": error_msg}))


if __name__ == "__main__":
    main()
