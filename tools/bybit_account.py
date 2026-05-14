#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
"""
Bybit Account & Position Tools
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

# @cmd Get wallet balance
# @option --coin Coin name (default: USDT)
# @option --account-type Account type (UNIFIED/CONTRACT/SPOT)
def bybit_get_balance(coin=None, account_type="UNIFIED"):
    """Get wallet balance for specified coin or all coins"""
    result = session.get_wallet_balance(accountType=account_type, coin=coin)
    print(json.dumps(result))

# @cmd View positions
# @option --symbol Trading pair (e.g., BTCUSDT)
# @option --category Product type (linear/inverse)
def bybit_get_positions(symbol=None, category="linear"):
    """Get open positions for a symbol or all symbols"""
    result = session.get_positions(category=category, symbol=symbol)
    print(json.dumps(result))

# @cmd Get open orders
# @option --symbol Trading pair (e.g., BTCUSDT)
# @option --category Product type (linear/inverse/option/spot)
def bybit_get_open_orders(symbol=None, category="linear"):
    """Get all open orders for a symbol"""
    result = session.get_open_orders(category=category, symbol=symbol)
    print(json.dumps(result))

# @cmd Get closed PnL
# @option --symbol Trading pair (e.g., BTCUSDT)
# @option --limit Number of records (default: 50)
# @option --category Product type (linear/inverse)
def bybit_get_closed_pnl(symbol=None, limit=50, category="linear"):
    """Get closed PnL history"""
    result = session.get_closed_pnl(category=category, symbol=symbol, limit=limit)
    print(json.dumps(result))

# @cmd Get order history
# @option --symbol Trading pair (e.g., BTCUSDT)
# @option --limit Number of records (default: 50)
# @option --category Product type (linear/inverse/option/spot)
def bybit_get_order_history(symbol=None, limit=50, category="linear"):
    """Get historical orders (filled/cancelled)"""
    result = session.get_order_history(category=category, symbol=symbol, limit=limit)
    print(json.dumps(result))

# @cmd Get account info
# @option --account-type Account type (UNIFIED/CONTRACT/SPOT)
def bybit_get_account_info(account_type="UNIFIED"):
    """Get account information (margin, leverage, etc.)"""
    result = session.get_account_info(accountType=account_type)
    print(json.dumps(result))

# @cmd Get fee rate
# @option --symbol Trading pair (e.g., BTCUSDT)
# @option --category Product type (linear/inverse)
def bybit_get_fee_rate(symbol=None, category="linear"):
    """Get trading fee rate for a symbol"""
    result = session.get_fee_rate(category=category, symbol=symbol)
    print(json.dumps(result))

# @cmd Get leverage info
# @option --symbol Trading pair (e.g., BTCUSDT)
def bybit_get_leverage(symbol):
    """Get current leverage for a symbol"""
    result = session.get_leverage(category="linear", symbol=symbol)
    print(json.dumps(result))

# @cmd Get settlement coins
# @option --coin Coin name (e.g., USDT)
def bybit_get_settlement_coin(coin=None):
    """Get settlement coin information"""
    result = session.get_settlement_coin_info(coin=coin)
    print(json.dumps(result))

if __name__ == "__main__":
    Argc().run()
