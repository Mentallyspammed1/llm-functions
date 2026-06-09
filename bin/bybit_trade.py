#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
"""
Bybit Execution & Trade Tools
Requires API_KEY and API_SECRET
"""
import os
import json
from pybit.unified_trading import HTTP
from argc import argc as Argc

# Configuration
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
USE_TOR = os.getenv("USE_TOR", "false").lower() == "true"
TOR_PROXY = os.getenv("TOR_PROXY", "socks5h://127.0.0.1:9050")

session = HTTP(
    testnet=TESTNET,
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET"),
    proxy=TOR_PROXY if USE_TOR else None
)

# @cmd Place trading order
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --side! Side (Buy/Sell)
# @option --order-type! Type (Market/Limit)
# @option --qty! Quantity
# @option --price Price for limit orders
# @option --time-in-force Time in force (GTC/IOC/FOK/PostOnly)
# @option --reduce-only Reduce only position
# @option --close-on-trigger Close on trigger
def bybit_place_order(symbol, side, order_type, qty, price=None, time_in_force="GTC", reduce_only=False, close_on_trigger=False):
    """Place a new order (Market or Limit)"""
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
    if reduce_only:
        params["reduceOnly"] = "true"
    if close_on_trigger:
        params["closeOnTrigger"] = "true"
    
    result = session.place_order(**params)
    print(json.dumps(result))

# @cmd Cancel single order
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --order-id! Order ID
# @option --order-link-id Custom order link ID
def bybit_cancel_order(symbol, order_id=None, order_link_id=None):
    """Cancel a specific order by order ID"""
    params = {"category": "linear", "symbol": symbol}
    if order_id:
        params["orderId"] = order_id
    if order_link_id:
        params["orderLinkId"] = order_link_id
    
    result = session.cancel_order(**params)
    print(json.dumps(result))

# @cmd Cancel all orders
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --category Product type (linear/inverse/option/spot)
def bybit_cancel_all_orders(symbol, category="linear"):
    """Cancel all open orders for a symbol"""
    result = session.cancel_all_orders(category=category, symbol=symbol)
    print(json.dumps(result))

# @cmd Set leverage
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --leverage! Leverage value (1-100)
# @option --buy-leverage Buy side leverage
# @option --sell-leverage Sell side leverage
def bybit_set_leverage(symbol, leverage=None, buy_leverage=None, sell_leverage=None):
    """Set leverage for a symbol"""
    params = {"category": "linear", "symbol": symbol}
    
    if leverage:
        params["buyLeverage"] = str(leverage)
        params["sellLeverage"] = str(leverage)
    else:
        if buy_leverage:
            params["buyLeverage"] = str(buy_leverage)
        if sell_leverage:
            params["sellLeverage"] = str(sell_leverage)
    
    result = session.set_leverage(**params)
    print(json.dumps(result))

# @cmd Set TP/SL
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --tp Take profit price
# @option --sl Stop loss price
# @option --tp-trigger Trigger price for TP
# @option --sl-trigger Trigger price for SL
def bybit_set_trading_stop(symbol, tp=None, sl=None, tp_trigger=None, sl_trigger=None):
    """Set take profit and stop loss for open position"""
    params = {"category": "linear", "symbol": symbol}
    
    if tp:
        params["takeProfit"] = str(tp)
    if sl:
        params["stopLoss"] = str(sl)
    if tp_trigger:
        params["takeProfitTriggerBy"] = tp_trigger
    if sl_trigger:
        params["stopLossTriggerBy"] = sl_trigger
    
    result = session.set_trading_stop(**params)
    print(json.dumps(result))

# @cmd Amend order
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --order-id! Order ID
# @option --order-link-id Custom order link ID
# @option --qty New quantity
# @option --price New price
def bybit_amend_order(symbol, order_id=None, order_link_id=None, qty=None, price=None):
    """Amend an existing order"""
    params = {"category": "linear", "symbol": symbol}
    
    if order_id:
        params["orderId"] = order_id
    if order_link_id:
        params["orderLinkId"] = order_link_id
    if qty:
        params["qty"] = str(qty)
    if price:
        params["price"] = str(price)
    
    result = session.amend_order(**params)
    print(json.dumps(result))

# @cmd Set position mode
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --mode! Mode (0=One-Way, 3=Hedge)
def bybit_set_position_mode(symbol, mode):
    """Set position mode (One-Way or Hedge)"""
    result = session.switch_position_mode(category="linear", symbol=symbol, mode=int(mode))
    print(json.dumps(result))

# @cmd Set risk limit
# @option --symbol! Trading pair (e.g., BTCUSDT)
# @option --risk-id! Risk ID
def bybit_set_risk_limit(symbol, risk_id):
    """Set risk limit for a symbol"""
    result = session.set_risk_limit(category="linear", symbol=symbol, riskId=int(risk_id))
    print(json.dumps(result))

# @cmd Get execution list
# @option --symbol Trading pair (e.g., BTCUSDT)
# @option --order-id Order ID
# @option --limit Number of records
def bybit_get_executions(symbol=None, order_id=None, limit=50):
    """Get execution history"""
    params = {"category": "linear"}
    if symbol:
        params["symbol"] = symbol
    if order_id:
        params["orderId"] = order_id
    if limit:
        params["limit"] = limit
    
    result = session.get_executions(**params)
    print(json.dumps(result))

# @cmd Get borrow history
# @option --coin Coin name
# @option --limit Number of records
def bybit_get_borrow_history(coin=None, limit=50):
    """Get borrow history"""
    result = session.get_borrow_history(coin=coin, limit=limit)
    print(json.dumps(result))

# @cmd Get collateral info
def bybit_get_collateral_info():
    """Get collateral information"""
    result = session.get_collateral_info()
    print(json.dumps(result))

if __name__ == "__main__":
    Argc().run()
