#!/usr/bin/env python3
# @describe Bybit Trading & Execution Tools - Place orders, cancel, set leverage/TP/SL
# @option --symbol!        Trading pair (e.g., BTCUSDT)
# @option --side           Order side: Buy|Sell
# @option --order_type     Order type: Market|Limit (default: Market)
# @option --qty            Order quantity
# @option --price         Limit price (required for Limit orders)
# @option --time_in_force TIF: GTC|IOC|FOK|PostOnly (default: GTC)
# @option --action        Action: place_order|cancel|cancel_all|leverage|trading_stop|order_history
# @option --order_id      Order ID for cancel
# @option --leverage     Leverage value (1-100)
# @option --tp           Take profit price
# @option --sl            Stop loss price
# @option --use_tor      Route through Tor proxy (default: true)
"""
Bybit Trading & Execution Tools
Requires BYBIT_API_KEY and BYBIT_API_SECRET
"""
import os
import json
import argparse
from pathlib import Path

# Load .env if exists
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key, val)

from pybit.unified_trading import HTTP

# Tor proxy support
USE_TOR = os.getenv("BYBIT_USE_TOR", "true").lower() == "true"
PROXY = "socks5h://127.0.0.1:9050" if USE_TOR else None

TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

session = HTTP(
    testnet=TESTNET,
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET"),
    proxy=PROXY
)


def bybit_place_order(symbol, side, order_type, qty, price=None, time_in_force="GTC"):
    """Place a trading order"""
    try:
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": time_in_force
        }
        if price:
            params["price"] = str(price)
        
        result = session.place_order(**params)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_cancel_order(symbol, order_id):
    """Cancel a single order"""
    try:
        result = session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_cancel_all_orders(symbol):
    """Cancel all open orders for a symbol"""
    try:
        result = session.cancel_all_orders(category="linear", symbol=symbol)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_set_leverage(symbol, leverage):
    """Set leverage for a symbol"""
    try:
        result = session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage)
        )
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_set_trading_stop(symbol, tp=None, sl=None):
    """Set take profit and stop loss for open position"""
    try:
        params = {"category": "linear", "symbol": symbol}
        if tp:
            params["takeProfit"] = str(tp)
        if sl:
            params["stopLoss"] = str(sl)
        
        result = session.set_trading_stop(**params)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def bybit_get_order_history(symbol=None, limit=20):
    """Get order history (filled/cancelled)"""
    try:
        result = session.get_order_history(category="linear", symbol=symbol, limit=limit)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def main():
    parser = argparse.ArgumentParser(description="Bybit Trading Tools")
    parser.add_argument("--action", default="place_order", help="Action to perform")
    parser.add_argument("--symbol", required=True, help="Trading pair")
    parser.add_argument("--side", default="Buy", help="Buy or Sell")
    parser.add_argument("--order_type", default="Market", help="Market or Limit")
    parser.add_argument("--qty", type=float, default=None, help="Quantity")
    parser.add_argument("--price", type=float, default=None, help="Limit price")
    parser.add_argument("--time_in_force", default="GTC", help="Time in force")
    parser.add_argument("--order_id", default=None, help="Order ID")
    parser.add_argument("--leverage", type=int, default=None, help="Leverage")
    parser.add_argument("--tp", type=float, default=None, help="Take profit")
    parser.add_argument("--sl", type=float, default=None, help="Stop loss")
    args = parser.parse_args()

    if args.action == "place_order":
        if not args.qty:
            print(json.dumps({"error": "--qty required"}))
            return
        bybit_place_order(args.symbol, args.side, args.order_type, args.qty, args.price, args.time_in_force)
    elif args.action == "cancel":
        if not args.order_id:
            print(json.dumps({"error": "--order_id required"}))
            return
        bybit_cancel_order(args.symbol, args.order_id)
    elif args.action == "cancel_all":
        bybit_cancel_all_orders(args.symbol)
    elif args.action == "leverage":
        if not args.leverage:
            print(json.dumps({"error": "--leverage required"}))
            return
        bybit_set_leverage(args.symbol, args.leverage)
    elif args.action == "trading_stop":
        bybit_set_trading_stop(args.symbol, args.tp, args.sl)
    elif args.action == "order_history":
        bybit_get_order_history(args.symbol)
    else:
        print(json.dumps({"error": f"Unknown action: {args.action}"}))


if __name__ == "__main__":
    main()
