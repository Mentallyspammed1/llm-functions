#!/usr/bin/env python3
"""Manage open positions on Bybit exchange."""
import os
import json
import bybit_core

def run(
    symbol: str = "BTCUSDT",
    action: str = "be",
    profit_usdt: int = 50,
    fee_rate: float = 0.0006,
):
    """Manage open positions: Move to Break-Even or Close if net profit threshold reached
    Args:
        symbol: Symbol (e.g., BTCUSDT)
        action: Action: 'be' (move to break-even), 'close' (close if in profit)
        profit_usdt: Target USDT profit (after fees) to trigger action
        fee_rate: Taker fee rate (default: 0.0006)
    """
    # Get positions
    try:
        data = bybit_core.api_request("GET", "/v5/position/list", params={"category": "linear", "symbol": symbol}, signed=True)
        if data.get("retCode") != 0:
            return {"success": False, "error": data.get("retMsg")}
        
        positions = data.get("result", {}).get("list", [])
        position = next((p for p in positions if float(p.get("size", 0)) > 0 and p.get("symbol") == symbol), None)
        
        if not position:
            return {"success": False, "error": "No open position found"}
        
        size = float(position.get("size", 0))
        entry_price = float(position.get("avgPrice", 0))
        side = position.get("side", "")
        unrealized_pnl = float(position.get("unrealizedPnl", 0))
        
    except Exception as e:
        return {"success": False, "error": str(e)}
    
    # Get current price
    try:
        data = bybit_core.api_request("GET", "/v5/market/tickers", params={"category": "linear", "symbol": symbol}, signed=False)
        if data.get("retCode") == 0:
            current_price = float(data.get("result", {}).get("list", [{}])[0].get("lastPrice", 0))
        else:
            return {"success": False, "error": "Failed to get current price"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    
    # Calculate fees and net profit
    fee = size * current_price * fee_rate
    net_profit = unrealized_pnl - fee
    
    # Check if action should be taken
    if action == "close":
        if net_profit < profit_usdt:
            return {
                "success": False,
                "message": f"Net profit {net_profit:.2f} USDT is below threshold {profit_usdt} USDT",
                "net_profit": net_profit,
                "threshold": profit_usdt
            }
    elif action == "be":
        # Move to break-even
        params = {
            "category": "linear",
            "symbol": symbol,
            "stopLoss": str(entry_price)
        }
        
        try:
            data = bybit_core.api_request("POST", "/v5/position/trading-stop", params=params, signed=True)
            if data.get("retCode") == 0:
                return {
                    "success": True,
                    "action": "move_to_break_even",
                    "symbol": symbol,
                    "new_stop_loss": entry_price,
                    "message": f"Stop loss moved to entry price {entry_price}"
                }
            else:
                return {"success": False, "error": data.get("retMsg")}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # Close position
    params = {
        "category": "linear",
        "symbol": symbol,
        "side": "Sell" if side == "Buy" else "Buy",
        "orderType": "Market",
        "qty": str(size),
        "timeInForce": "GTC",
        "reduceOnly": True
    }
    
    try:
        data = bybit_core.api_request("POST", "/v5/order/create", params=params, signed=True)
        if data.get("retCode") == 0:
            return {
                "success": True,
                "action": "close_position",
                "symbol": symbol,
                "size": size,
                "entry_price": entry_price,
                "exit_price": current_price,
                "unrealized_pnl": unrealized_pnl,
                "fees": fee,
                "net_profit": net_profit
            }
        else:
            return {"success": False, "error": data.get("retMsg")}
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    from argparse import ArgumentParser
    
    parser = ArgumentParser(description="Manage position on Bybit")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--action", default="be")
    parser.add_argument("--profit-usdt", type=int, default=50)
    parser.add_argument("--fee-rate", type=float, default=0.0006)
    args = parser.parse_args()
    
    result = run(
        symbol=args.symbol,
        action=args.action,
        profit_usdt=args.profit_usdt,
        fee_rate=args.fee_rate
    )
    print(json.dumps(result, indent=2))
