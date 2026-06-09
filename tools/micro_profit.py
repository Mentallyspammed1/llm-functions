#!/usr/bin/env python3
import json, logging, sys
from dataclasses import dataclass
from typing import List, Dict, Any

DEFAULT_TARGET = 5.0
DEFAULT_LEVERAGE = 1
DEFAULT_FEE_RATE = 0.0002
DEFAULT_TAKER_FEE = 0.00055
DEFAULT_KELLY_WIN_RATE = 0.55
DEFAULT_RISK_REWARD = 2.0
DEFAULT_DEPTH = 40

@dataclass
class OrderBookLevel:
    price: float
    qty: float

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])

def parse_order_book(bids_str: str, asks_str: str) -> Dict[str, List[OrderBookLevel]]:
    try:
        bids = json.loads(bids_str)
        asks = json.loads(asks_str)
    except json.JSONDecodeError as exc:
        logging.error("Failed to parse order book JSON: %s", exc)
        raise
    def to_levels(raw: List[List[Any]]) -> List[OrderBookLevel]:
        return [OrderBookLevel(float(price), float(qty)) for price, qty in raw]
    return {"bids": to_levels(bids), "asks": to_levels(asks)}

def calculate_micro_profit(
    symbol: str,
    side: str,
    qty: int,
    target: float = DEFAULT_TARGET,
    leverage: int = DEFAULT_LEVERAGE,
    maker_fee: float = DEFAULT_FEE_RATE,
    taker_fee: float = DEFAULT_TAKER_FEE,
    funding_rate: float = 0.0001,
    slippage: float = 0.0001,
    risk_reward: float = DEFAULT_RISK_REWARD,
    kelly_win: float = DEFAULT_KELLY_WIN_RATE,
    depth: int = DEFAULT_DEPTH,
    bids_json: str = "",
    asks_json: str = ""
) -> Dict[str, Any]:
    order_book = parse_order_book(bids_json, asks_json)
    bids = order_book["bids"]
    asks = order_book["asks"]
    if not bids or not asks:
        raise ValueError("Bid or ask data is empty.")
    best_bid_price = bids[0].price
    best_ask_price = asks[0].price
    spread = best_ask_price - best_bid_price
    risk_amount = qty * best_bid_price * (slippage + maker_fee + taker_fee)
    exit_price = best_bid_price * (1 + risk_reward * target / (best_bid_price * qty))
    result = {
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "entry_price": best_bid_price,
        "spread": spread,
        "target_profit_usdt": target,
        "leverage": leverage,
        "exit_price": exit_price,
        "risk_reward_ratio": risk_reward,
        "kelly_win_rate": kelly_win,
        "bids": [{"price": l.price, "qty": l.qty} for l in bids],
        "asks": [{"price": l.price, "qty": l.qty} for l in asks],
    }
    return result

def run(
    symbol: str,
    side: str,
    qty: int,
    target: float = DEFAULT_TARGET,
    leverage: int = DEFAULT_LEVERAGE,
    bids: str = "[]",
    asks: str = "[]",
    depth: int = DEFAULT_DEPTH,
) -> None:
    """
    Entry point called by the micro_profit tool.
    """
    try:
        result = calculate_micro_profit(
            symbol=symbol,
            side=side,
            qty=qty,
            target=target,
            leverage=leverage,
            bids_json=bids,
            asks_json=asks,
            depth=depth,
        )
        print(json.dumps(result, indent=2))
    except Exception as exc:
        logging.exception("Micro profit calculation failed")
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    run()
