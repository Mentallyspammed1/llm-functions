#!/usr/bin/env python3
# @describe All-in-One ETHUSDT Scalper: Analysis, Risk Management, Order Ops
# @option --auto-trade <BOOL> Execute trades automatically
# @option --qty <QTY> Order quantity
# @option --min-profit <USD> Min profit target

import time
import json
import argparse
import logging
import bybit_core

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

class Scalper:
    def __init__(self, qty, min_profit):
        self.qty = qty
        self.min_profit = min_profit
        self.symbol = "ETHUSDT"
        self.category = "linear"

    def get_market_data(self):
        # Orderbook
        ob = bybit_core.api_request("GET", "/v5/market/orderbook", {"category": self.category, "symbol": self.symbol, "limit": 50})
        # Position
        pos = bybit_core.api_request("GET", "/v5/position/list", {"category": self.category, "symbol": self.symbol}, signed=True)
        # Orders
        orders = bybit_core.api_request("GET", "/v5/order/realtime", {"category": self.category, "symbol": self.symbol}, signed=True)
        return ob, pos, orders

    def place_order(self, side, order_type="Market", price=None):
        params = {"category": self.category, "symbol": self.symbol, "side": side, "orderType": order_type, "qty": str(self.qty)}
        if price: params["price"] = str(price)
        return bybit_core.api_request("POST", "/v5/order/create", params=params, signed=True)

    def cancel_all(self):
        return bybit_core.api_request("POST", "/v5/order/cancel-all", {"category": self.category, "symbol": self.symbol}, signed=True)

    def run(self, auto_trade):
        logger.info("Bot started. Monitoring...")
        while True:
            ob, pos, orders = self.get_market_data()
            
            # Position logic
            active_pos = next((p for p in pos.get("result", {}).get("list", []) if float(p["size"]) > 0), None)
            if active_pos:
                pnl = float(active_pos["unrealisedPnl"])
                if pnl >= self.min_profit:
                    logger.info(f"Profit target reached ({pnl}). Closing.")
                    self.place_order("Sell" if active_pos["side"] == "Buy" else "Buy")
            
            # Entry logic
            elif auto_trade:
                bids = ob.get("result", {}).get("b", [])
                asks = ob.get("result", {}).get("a", [])
                if bids and asks:
                    imbalance = (sum(float(b[1]) for b in bids[:10]) - sum(float(a[1]) for a in asks[:10]))
                    if imbalance > 50: self.place_order("Buy")
                    elif imbalance < -50: self.place_order("Sell")
            
            time.sleep(5)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-trade", type=lambda x: x.lower()=="true", default=False)
    parser.add_argument("--qty", default="0.01")
    parser.add_argument("--min-profit", type=float, default=1.0)
    args = parser.parse_args()
    bot = Scalper(args.qty, args.min_profit)
    bot.run(args.auto_trade)
        for sym_cfg in config.symbols:
            cancel_all_orders(sym_cfg.symbol)
            pos = get_position_info_for_symbol(sym_cfg.symbol)
            if pos and pos.size > 0:
                close_position(sym_cfg, pos, "Emergency Shutdown")

    if tor_manager:
        # Close WS connections
        if bybit_core and bybit_core.websocket_manager:
            bybit_core.websocket_manager.disconnect()
    
    save_config()
    logger.critical("Bot stopped gracefully.")

# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run_strategy_for_symbol(sym_cfg: SymbolConfig) -> None:
    """Strategy loop for a single symbol."""
    sym = sym_cfg.symbol
    
    # 1. Update state
    get_open_orders_for_symbol(sym)
    pos = get_position_info_for_symbol(sym)
    current_positions[sym] = pos
    
    # 2. Market Data
    resp = bybit_core.api_request(
        "GET", "/v5/market/orderbook",
        params={"category": config.category, "symbol": sym, "limit": sym_cfg.orderbook_depth}
    )
    if resp.get("retCode") != 0:
        logger.error("[%s] OB fetch error: %s", sym, resp.get("retMsg"))
        return
        
    res = resp.get("result", {})
    bids, asks = res.get("b", []), res.get("a", [])
    if not bids or not asks:
        return
        
    price = float(bids[0][0])
    current_prices[sym] = price
    
    # Metrics & Trends
    metrics = analyze_orderbook_advanced(bids, asks, sym_cfg)
    orderbook_history_by_symbol[sym].append(metrics)
    trends = analyze_orderbook_trends(orderbook_history_by_symbol[sym])
    
    # 3. Position/Risk Management
    if pos and pos.size > 0:
        if check_profit_close_conditions(sym_cfg, pos, price):
            return # Closed
        
    # 4. Entry Signals
    if config.auto_trade and (not pos or pos.size == 0 or sym_cfg.hedge_mode):
        if not open_orders_by_symbol.get(sym):
            signals = generate_advanced_signals(metrics, price, trends, pos)
            for sig in signals:
                if sig.signal_type in ("LONG_ENTRY", "SHORT_ENTRY"):
                    # Calculate safe size
                    qty = calculate_safe_position_size(sym_cfg, price)
                    side = "Buy" if "LONG" in sig.signal_type else "Sell"
                    
                    place_order(
                        sym_cfg, side, qty, price,
                        order_type="Conditional" if sym_cfg.use_conditional else "Limit",
                        tp=sig.take_profit, sl=sig.stop_loss,
                        trigger_price=sig.trigger_price,
                        reason=sig.reason
                    )

def main():
    parser = argparse.ArgumentParser(description="Multi-Symbol Trading Scalper")
    parser.add_argument("--auto-trade", type=lambda x: x.lower()=="true", default=False)
    parser.add_argument("--qty", type=float, default=0.2)
    parser.add_argument("--min-profit", type=float, default=2.0)
    parser.add_argument("--use-tor", type=lambda x: x.lower()=="true", default=True)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    load_config()
    # Apply CLI overrides if provided
    config.auto_trade = args.auto_trade
    config.use_tor = args.use_tor
    
    initialize_networking(config.use_tor, config.tor_socks_port, config.api_key, config.api_secret)
    initialize_all_symbol_states()
    
    # Pre-init symbols
    for sym_cfg in config.symbols:
        initialize_trading_environment_for_symbol(sym_cfg)
    
    setup_websocket_streams()
    
    # Main loop
    try:
        while not emergency_stop:
            get_account_balance()
            update_daily_pnl()
            if not check_risk_limits():
                emergency_shutdown("Risk breach")
                break
                
            for sym_cfg in config.symbols:
                run_strategy_for_symbol(sym_cfg)
            
            time.sleep(config.interval)
            
    except KeyboardInterrupt:
        logger.info("Stopping...")
    finally:
        emergency_shutdown("User exit")

if __name__ == "__main__":
    main()
        for sym_cfg in config.symbols:
            cancel_all_orders(sym_cfg.symbol)
            pos = get_position_info_for_symbol(sym_cfg.symbol)
            if pos and pos.size > 0:
                close_position(sym_cfg, pos, "Emergency Shutdown")

    if tor_manager:
        if bybit_core and bybit_core.websocket_manager:
            bybit_core.websocket_manager.disconnect()
    
    save_config()
    logger.critical("Bot stopped gracefully.")

# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run_strategy_for_symbol(sym_cfg: SymbolConfig) -> None:
    """Strategy loop for a single symbol."""
    sym = sym_cfg.symbol
    
    # 1. Update state
    get_open_orders_for_symbol(sym)
    pos = get_position_info_for_symbol(sym)
    current_positions[sym] = pos
    
    # 2. Market Data
    resp = bybit_core.api_request(
        "GET", "/v5/market/orderbook",
        params={"category": config.category, "symbol": sym, "limit": sym_cfg.orderbook_depth}
    )
    if resp.get("retCode") != 0:
        logger.error("[%s] OB fetch error: %s", sym, resp.get("retMsg"))
        return
        
    res = resp.get("result", {})
    bids, asks = res.get("b", []), res.get("a", [])
    if not bids or not asks:
        return
        
    price = float(bids[0][0])
    current_prices[sym] = price
    
    # Metrics & Trends
    metrics = analyze_orderbook_advanced(bids, asks, sym_cfg)
    orderbook_history_by_symbol[sym].append(metrics)
    trends = analyze_orderbook_trends(orderbook_history_by_symbol[sym])
    
    # 3. Position/Risk Management
    if pos and pos.size > 0:
        if check_profit_close_conditions(sym_cfg, pos, price):
            return # Closed
        
    # 4. Entry Signals
    if config.auto_trade and (not pos or pos.size == 0 or sym_cfg.hedge_mode):
        if not open_orders_by_symbol.get(sym):
            signals = generate_advanced_signals(metrics, price, trends, pos)
            for sig in signals:
                if sig.signal_type in ("LONG_ENTRY", "SHORT_ENTRY"):
                    # Calculate safe size
                    qty = calculate_safe_position_size(sym_cfg, price)
                    side = "Buy" if "LONG" in sig.signal_type else "Sell"
                    
                    place_order(
                        sym_cfg, side, qty, price,
                        order_type="Conditional" if sym_cfg.use_conditional else "Limit",
                        tp=sig.take_profit, sl=sig.stop_loss,
                        trigger_price=sig.trigger_price,
                        reason=sig.reason
                    )

def main():
    parser = argparse.ArgumentParser(description="Multi-Symbol Trading Scalper")
    parser.add_argument("--auto-trade", type=lambda x: x.lower()=="true", default=False)
    parser.add_argument("--qty", type=float, default=0.2)
    parser.add_argument("--min-profit", type=float, default=2.0)
    parser.add_argument("--use-tor", type=lambda x: x.lower()=="true", default=True)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    load_config()
    # Apply CLI overrides if provided
    config.auto_trade = args.auto_trade
    config.use_tor = args.use_tor
    
    initialize_networking(config.use_tor, config.tor_socks_port, config.api_key, config.api_secret)
    initialize_all_symbol_states()
    
    # Pre-init symbols
    for sym_cfg in config.symbols:
        initialize_trading_environment_for_symbol(sym_cfg)
    
    setup_websocket_streams()
    
    # Main loop
    try:
        while not emergency_stop:
            get_account_balance()
            update_daily_pnl()
            if not check_risk_limits():
                emergency_shutdown("Risk breach")
                break
                
            for sym_cfg in config.symbols:
                run_strategy_for_symbol(sym_cfg)
            
            time.sleep(config.interval)
            
    except KeyboardInterrupt:
        logger.info("Stopping...")
    finally:
        emergency_shutdown("User exit")

if __name__ == "__main__":
    main()
