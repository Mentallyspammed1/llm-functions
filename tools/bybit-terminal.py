#!/usr/bin/env python3
"""Backward-compatible entry point for BybitRealm terminal module (tools/bybit/terminal.py)."""

import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from bybit.base import BybitBaseClient, TradeJournal, TradingConfig, logger  # noqa: E402,F401
from bybit.terminal import BybitRealm, SignalManager, run  # noqa: E402,F401

__all__ = [
    "BybitBaseClient",
    "BybitRealm",
    "SignalManager",
    "TradeJournal",
    "TradingConfig",
    "logger",
    "run",
]

if __name__ == "__main__":
    import json

    print(json.dumps(run(action="list_actions"), indent=2))
