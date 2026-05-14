#!/usr/bin/env python3
"""Place a smart order with automatic position sizing and risk management."""
import os
import json
import bybit_core

def run(
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    risk_pct: float = 1.0,
    sl_dist: float = None,
    sl_price: float = None,
    tp_price: float = None,
):
    """Place a smart order with automatic position sizing and risk management
    Args:
        symbol: Symbol (e.g., BTCUSDT)
        side: Side (Buy/Sell)
        risk_pct: % of balance to risk (default: 1.0)
        sl_dist: Stop loss distance in price (optional)
        sl_price: Absolute Stop Loss price (optional)
        tp_price: Absolute Take Profit price (optional)
    """
    # Get current price
    try:
        data = bybit_core.api_request("GET", "/v5/market/tickers", params={"category": "linear", "symbol": symbol}, signed=False)
        if data.get("retCode") != 0:
            return {"success": False, "error": "Failed to get ticker: " + data.get("retMsg")}
        ticker = data.get("result", {}).get("list", [{}])[0]
        current_price = float(ticker.get("lastPrice", 0))
    except Exception as e:
        return {"success": False, "error": str(e)}
    
    # Get balance
    try:
        data = bybit_core.api_request("GET", "/v5/account/wallet", params={"accountType": "UNIFIED"}, signed=True)
        if data.get("retCode") != 0:
            return {"success": False, "error": "Failed to get balance: " + data.get("retMsg")}
        coins = data.get("result", {}).get("list", [{}])[0].get("coin", [])
        usdt_balance_entry = next((c for c in coins if c.get("coin") == "USDT"), {})
        balance = float(usdt_balance_entry.get("availableToWithdraw", 0))
    except Exception as e:
        return {"success": False, "error": str(e)}
    
    # Calculate position size
    risk_amount = balance * (risk_pct / 100)
    
    # Get ATR for stop loss calculation
    try:
        data = bybit_core.api_request("GET", "/v5/market/klines", params={"category": "linear", "symbol": symbol, "interval": "60", "limit": "100"}, signed=False)
        if data.get("retCode") == 0:
            klines = data.get("result", {}).get("list", [])
            closes = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            atr = _atr(highs, lows, closes, 14)
        else:
            atr = current_price * 0.01  # Default 1% if ATR unavailable
    except:
        atr = current_price * 0.01
    
    # Calculate stop loss
    if sl_price:
        stop_loss = sl_price
    elif sl_dist:
        stop_loss = current_price - sl_dist if side == "Buy" else current_price + sl_dist
    else:
        stop_loss = current_price - (atr * 2) if side == "Buy" else current_price + (atr * 2)
    
    # Calculate position size based on risk
    price_diff = abs(current_price - stop_loss)
    if price_diff > 0:
        qty = risk_amount / price_diff
    else:
        qty = risk_amount / current_price
    
    # Round to appropriate precision (simplified)
    qty = round(qty, 3)
    
    # Calculate take profit
    if tp_price:
        take_profit = str(tp_price)
    else:
        tp_distance = price_diff * 2  # 2:1 risk:reward
        take_profit = str(current_price + tp_distance) if side == "Buy" else str(current_price - tp_distance)
    
    # Place order
    params = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "GTC",
        "stopLoss": str(stop_loss),
        "takeProfit": take_profit
    }
    
    try:
        data = bybit_core.api_request("POST", "/v5/order/create", params=params, signed=True)
        if data.get("retCode") == 0:
            return {
                "success": True,
                "order_id": data.get("result", {}).get("orderId"),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "entry_price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "risk_pct": risk_pct,
                "balance": balance,
                "data": data.get("result")
            }
        else:
            return {"success": False, "error": data.get("retMsg"), "retCode": data.get("retCode")}
    except Exception as e:
        return {"success": False, "error": str(e)}

def _atr(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return None
    tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(highs))]
    return sum(tr[-period:]) / period

if __name__ == "__main__":
    from argparse import ArgumentParser
    
    parser = ArgumentParser(description="Place smart order on Bybit")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--side", default="Buy")
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--sl-dist", type=float, default=None)
    parser.add_argument("--sl-price", type=float, default=None)
    parser.add_argument("--tp-price", type=float, default=None)
    args = parser.parse_args()
    
    result = run(
        symbol=args.symbol,
        side=args.side,
        risk_pct=args.risk_pct,
        sl_dist=args.sl_dist,
        sl_price=args.sl_price,
        tp_price=args.tp_price
    )
    print(json.dumps(result, indent=2))
