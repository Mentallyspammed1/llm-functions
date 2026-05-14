#!/usr/bin/env python3
"""
Bybit Get Balance Tool – Retrieve wallet balances using the centralized dispatcher.
"""
import json
import argparse
from bybit_tool import run

def get_balance(account_type: str = "UNIFIED") -> str:
    """Wrapper for the BybitToolDispatcher to get balance."""
    result = run(
        action="get_wallet_balance",
        account_type=account_type
    )
    return json.dumps(result, indent=2)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retrieve Bybit wallet balance.")
    parser.add_argument("--account-type", default="UNIFIED", help="Account type (UNIFIED, CONTRACT, SPOT)")
    args = parser.parse_args()
    print(get_balance(args.account_type))
