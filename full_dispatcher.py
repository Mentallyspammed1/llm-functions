        if action == "health_check":
            return bot.health_check()

        # ── Account ──────────────────────────────────────────────────────────
        elif action == "get_wallet_balance":
            return bot.get_wallet_balance(account_type=account_type)

        elif action == "get_account_info":
            return bot.get_account_info()

        elif action == "get_fee_rate":
            return bot.get_fee_rate(category=category, symbol=symbol)

        elif action == "get_positions":
            return bot.get_positions(
                category=category,
                symbol=symbol,
                settle_coin=settle_coin,
            )

        elif action == "get_position_risk":
            return bot.get_position_risk(
                category=category, symbol=symbol
            )
        
        elif action == "panic_close":
            return bot.panic_close(category=category)
        
        elif action == "bulk_update_tp_sl":
            return bot.bulk_update_tp_sl(category=category, tp=take_profit, sl=stop_loss)
            
        elif action == "check_balance":
            return bot.get_wallet_balance(account_type=account_type)

        elif action == "close_position":
            return bot.close_position(symbol=symbol, category=category)
            
        elif action == "get_account_summary":
            return bot.get_account_summary()
        
        elif action == "send_open_positions_summary":
            return bot.get_open_positions_summary(category=category)

        elif action == "alert":
            return {"status": "ok" if bot.alert(message=kwargs.get("message", "Test Alert"), level=kwargs.get("level", "INFO")) else "error"}

        elif action == "export_trade_history":
            return bot.export_trade_history(symbol=symbol, filename=kwargs.get("filename", "trade_history.csv"))
        
        elif action == "set_tp_sl":
            return bot.set_tp_sl(symbol=symbol, tp=take_profit, sl=stop_loss, category=category)
        
        elif action == "calculate_rsi":
            return bot.calculate_rsi(symbol=symbol, interval=interval, period=int(kwargs.get("period", 14)))
            
        elif action == "calculate_macd":
            return bot.calculate_macd(symbol=symbol, interval=interval, fast=int(kwargs.get("fast", 12)), slow=int(kwargs.get("slow", 26)), signal=int(kwargs.get("signal", 9)))
            
        elif action == "calculate_adx":
            return bot.calculate_adx(symbol=symbol, interval=interval, period=int(kwargs.get("period", 14)))
            
        elif action == "calculate_cci":
            return bot.calculate_cci(symbol=symbol, interval=interval, period=int(kwargs.get("period", 20)))
            
        elif action == "calculate_ichimoku":
            return bot.calculate_ichimoku(symbol=symbol, interval=interval, tenkan=int(kwargs.get("tenkan", 9)), kijun=int(kwargs.get("kijun", 26)), senkou_b=int(kwargs.get("senkou_b", 52)))
            
        elif action == "calculate_sma":
            return bot.calculate_sma(symbol=symbol, interval=interval, period=int(kwargs.get("period", 50)))
            
        elif action == "calculate_ema":
            return bot.calculate_ema(symbol=symbol, interval=interval, period=int(kwargs.get("period", 20)))
            
        elif action == "calculate_bollinger_bands":
            return bot.calculate_bollinger_bands(symbol=symbol, interval=interval, period=int(kwargs.get("period", 20)))
            
        elif action == "calculate_vwap":
            return bot.calculate_vwap(symbol=symbol, interval=interval)
            
        elif action == "calculate_atr":
            return bot.calculate_atr(symbol=symbol, interval=interval, period=int(kwargs.get("period", 14)))
            
        elif action == "calculate_stochastic":
            return bot.calculate_stochastic(symbol=symbol, interval=interval, period=int(kwargs.get("period", 14)), smooth_k=int(kwargs.get("smooth_k", 3)), smooth_d=int(kwargs.get("smooth_d", 3)))
        
        elif action == "micro_scalp":
            return bot.micro_scalp(
                symbol=symbol,
                qty=float(kwargs.get("qty", 0.01)),
                fee_rate=float(kwargs.get("fee_rate", 0.0005)),
                target_profit=float(kwargs.get("target_profit", 0.05)),
                category=category
            )
            
        elif action == "stream_orderbook":
            ws = BybitWebSocketManager(bot.config)
            asyncio.run(ws.stream_orderbook(symbol=symbol, duration=int(kwargs.get("duration", 10))))
            return {"status": "ok", "msg": "Stream ended"}
            
        elif action == "calculate_all_indicators":
            return bot.calculate_all_indicators(symbol=symbol, interval=interval)
        
        elif action == "calculate_hma":
            return bot.calculate_hma(symbol=symbol, interval=interval, period=int(kwargs.get("period", 20)))
            
        elif action == "calculate_fractals":
            return bot.calculate_fractals(symbol=symbol, interval=interval)
            
        elif action == "calculate_pivot_points":
            return bot.calculate_pivot_points(symbol=symbol, interval=interval)
            
        elif action == "calculate_klinger":
            return bot.calculate_klinger(symbol=symbol, interval=interval, fast=int(kwargs.get("fast", 34)), slow=int(kwargs.get("slow", 55)))
            
        elif action == "scan_scalping_opportunities":
            return bot.scan_scalping_opportunities(symbol=symbol, interval=interval)
        
        elif action == "get_pnl_summary":
            return bot.get_pnl_summary(days=int(kwargs.get("days", 7)))

        elif action == "update_trailing_stop":
            return bot.update_trailing_stop(symbol=symbol, trailing_stop_pct=trailing_stop_pct, category=category)

        elif action == "check_risk_limit":
            return bot.check_risk_limit(symbol=symbol, qty=qty, price=price)

        elif action == "set_leverage":
            return bot.set_leverage(
                symbol=symbol,
                leverage=leverage,
                category=category,
            )

        elif action == "set_trading_stop":
            return bot.set_trading_stop(
                symbol=symbol,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop=trailing_stop,
                category=category,
            )

        elif action == "place_breakeven_order":
            return bot.place_breakeven_order(symbol=symbol, fee_rate=float(kwargs.get("fee_rate", 0.0005)), category=category)

        elif action == "set_position_mode":
            return bot.set_position_mode(
                coin=kwargs.get("coin", "USDT"),
                mode=kwargs.get("mode", 0),
                category=category,
            )

        elif action == "get_executions":
            return bot.get_executions(
                category=category, symbol=symbol, limit=limit
            )

        elif action == "get_pnl_history":
            return bot.get_pnl_history(
                category=category, symbol=symbol, limit=limit
            )

        # ── Orders ───────────────────────────────────────────────────────────
        elif action == "place_order":
            return bot.place_order(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                order_type=order_type,
                category=category,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop=trailing_stop,
                reduce_only=reduce_only,
                time_in_force=time_in_force,
                client_oid=client_oid,
                trigger_price=trigger_price,
                trigger_by=trigger_by,
                tp_order_type=tp_order_type,
                sl_order_type=sl_order_type,
                **kwargs,
            )

        elif action == "place_smart_trade":
            return bot.place_smart_trade(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                trailing_stop_pct=trailing_stop_pct,
                category=category,
            )

        elif action == "amend_order":
            return bot.amend_order(
                symbol=symbol,
                order_id=order_id,
                client_oid=client_oid,
                qty=qty,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                category=category,
            )

        elif action == "cancel_order":
            return bot.cancel_order(
                symbol=symbol,
                order_id=order_id,
                client_oid=client_oid,
                category=category,
            )

        elif action == "cancel_all_orders":
            return bot.cancel_all_orders(
                symbol=symbol, category=category
            )

        elif action == "get_open_orders":
            return bot.get_open_orders(
                symbol=symbol, category=category, limit=limit
            )

        elif action == "get_order_history":
            return bot.get_order_history(
                symbol=symbol, category=category, limit=limit
            )

        elif action == "batch_place_orders":
            if not orders:
                return {
                    "status": "error",
                    "msg": "orders list is required for batch_place_orders",
                }
            return bot.batch_place_orders(
                orders=orders, category=category
            )

        # ── Market Data ──────────────────────────────────────────────────────
        elif action == "get_ticker":
            return bot.get_ticker(symbol=symbol, category=category)

        elif action == "get_orderbook":
            return bot.get_orderbook(
                symbol=symbol, limit=limit, category=category
            )

        elif action == "get_klines":
            return bot.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit,
                category=category,
            )

        elif action == "get_recent_trades":
            return bot.get_recent_trades(
                symbol=symbol, limit=limit, category=category
            )

        elif action == "get_instruments_info":
            return bot.get_instruments_info(
                category=category, symbol=symbol, limit=limit
            )

        elif action == "get_funding_rate":
            return bot.get_funding_rate(
                symbol=symbol, category=category, limit=limit
            )

        elif action == "get_open_interest":
            return bot.get_open_interest(
                symbol=symbol,
                interval=interval,
                category=category,
                limit=limit,
            )

        elif action == "get_volatility_index":
            return bot.get_volatility_index(category=category)

        # ── Advanced Analysis ─────────────────────────────────────────────────
        elif action == "get_liquidity_concentration":
            res = bot.get_liquidity_concentration(symbol=symbol, depth=int(kwargs.get("depth", 50)))
            return {k: (bot.neon(str(v), "yellow") if isinstance(v, (str, float, int)) else v) for k, v in res.items()}

        elif action == "get_volume_imbalance":
            period = int(kwargs.get("period") or 20)
            res = bot.get_volume_imbalance(symbol=symbol, interval=interval, period=period)
            return {k: (bot.neon(str(v), "green") if isinstance(v, (str, float, int)) else v) for k, v in res.items()}

        elif action == "calculate_support_resistance_levels":
            res = bot.calculate_support_resistance_levels(symbol=symbol, interval=interval, depth=int(kwargs.get("depth", 50)), wall_multiplier=float(kwargs.get("wall_multiplier", 3.0)))
            return res

        elif action == "calculate_fibonacci_levels":
            res = bot.calculate_fibonacci_levels(symbol=symbol, interval=interval, lookback=int(kwargs.get("lookback", 50)))
            return res
            
        elif action == "calculate_volume_profile":
            return bot.calculate_volume_profile(symbol=symbol, interval=interval)

        elif action == "calculate_dynamic_levels":
            return bot.calculate_dynamic_levels(symbol=symbol, interval=interval)
            
        elif action == "calculate_dynamic_qty":
            return {"status": "ok", "qty": bot.calculate_dynamic_qty(symbol=symbol, bid=float(kwargs.get("bid", 0)), max_usdt=float(kwargs.get("max_usdt", 100)))}

        elif action == "get_orderbook_analysis":
            res = bot.get_orderbook_analysis(
                symbol=symbol,
                depth=depth,
                category=category,
                wall_multiplier=wall_multiplier,
            )
            return {k: (bot.neon(str(v), "cyan") if isinstance(v, (str, float, int)) else v) for k, v in res.items()}
        elif action == "get_volume_at_price":
            return bot.get_volume_at_price(symbol=symbol, depth=depth, category=category)

        elif action == "get_market_regime":
            return bot.get_market_regime(
                symbol=symbol,
                interval=interval,
                lookback=lookback,
                category=category,
            )

        elif action == "scan_symbols":
            if not symbols:
                return {
                    "status": "error",
                    "msg": "symbols list is required for scan_symbols",
                }
            return bot.scan_symbols(
                symbols=symbols,
                category=category,
                include_regime=include_regime,
            )

        # ── Journal ───────────────────────────────────────────────────────────
        elif action == "get_pnl_summary":
            return bot.get_pnl_summary(symbol=symbol, limit=limit)

        elif action == "market_summary":
            return bot.get_market_summary()

        elif action == "analyze_symbol":
            return bot.analyze_symbol(symbol=symbol)

        elif action == "get_journal":
            entries = bot.journal.get_entries(
                symbol=journal_symbol, limit=journal_limit
            )
            return {
                "status": "ok",
                "count": len(entries),
                "entries": entries,
            }

        else:
            return {
                "status": "error",
                "msg": (
                    f"Unknown action '{action}'. "
                    "Call run('health_check') to verify connection."
                ),
                "available_actions": [
                    "health_check", "get_wallet_balance", "get_account_info",
                    "get_fee_rate", "get_positions", "get_position_risk",
                    "set_leverage", "set_trading_stop", "set_position_mode",
                    "get_executions", "get_pnl_history", "get_pnl_summary",
                    "place_order", "amend_order", "cancel_order",
                    "cancel_all_orders", "get_open_orders", "get_order_history",
                    "batch_place_orders",
                    "get_ticker", "get_orderbook", "get_klines",
                    "get_recent_trades", "get_instruments_info",
                    "get_funding_rate", "get_open_interest",
                    "get_volatility_index",
                    "get_orderbook_analysis", "get_market_regime",
                    "scan_symbols", "get_journal", "analyze_symbol", "market_summary",
                    "calculate_orderflow_delta", "calculate_orderbook_imbalance",
                    "calculate_liquidity_heatmap", "calculate_market_depth_profile",
                ],
            }

    except Exception as exc:
        logger.error("run(%s) raised: %s", action, exc, exc_info=True)
        return {"status": "error", "action": action, "msg": str(exc)}


def pretty_print_result(action: str, result: dict):
    """Prints results in a human-friendly format."""
    if result.get("status") == "error":
        print(f"\033[91mERROR: {result.get('msg')}\033[0m")
        return

    # If it's a simple status message
    if "msg" in result and len(result) <= 2:
        print(f"\033[92m{result['msg']}\033[0m")
        return

    print(f"\n\033[94m═══ {action.upper()} ═══\033[0m")
    
    # Custom formatters for specific actions
    if action == "get_positions":
        positions = result.get("list", [])
        if not positions:
            print("No open positions.")
        for pos in positions:
            symbol = pos.get("symbol")
            side = pos.get("side")
            size = pos.get("size")
            pnl = float(pos.get("unrealisedPnl", 0))
            color = "\033[92m" if pnl > 0 else "\033[91m"
            print(f"{symbol} | {side} | Size: {size} | PnL: {color}{pnl:+.4f}\033[0m")

    elif action == "get_wallet_balance":
        coins = result.get("list", [{}])[0].get("coin", [])
        for c in coins:
            balance = c.get("walletBalance", "0")
            if float(balance) > 0:
                print(f"{c['coin']}: {balance}")

    elif action == "analyze_symbol":
        print(f"Symbol: {result.get('symbol')} | Price: {result.get('last_price')}")
        print("-" * 40)
        for tf, data in result.get("analysis", {}).items():
            print(f"[{tf:>3}] Regime: {data['regime']:<15} | RSI: {data['rsi']:>5} | EMA: {data['ema']}")

    elif action == "get_pnl_summary":
        print(f"Trades: {result.get('trades_analyzed')} | Win Rate: {result.get('win_rate')}%")
        print(f"Total PnL: {result.get('total_pnl')} | Net: {result.get('net_pnl')}")

    elif action == "calculate_fibonacci_levels":
        print(f"Fibonacci Levels for {result.get('status')}:")
        for level, price in result.get("levels", {}).items():
            print(f"{level:>6}: {price:.4f}")

    elif action == "calculate_volume_profile":
        print(f"Volume Profile (POC): {result['poc']} (Vol: {result['volume_at_poc']})")

    elif action == "calculate_dynamic_levels":
        print(f"Dynamic S/R (EMA 50/200): {result['ema50']} / {result['ema200']}")

    elif action == "get_journal":

        symbols = result.get("symbols", [])
        print(f"{'Symbol':<10} | {'Price':<10} | {'Change%':<8} | {'Regime'}")
        print("-" * 50)
        for s in symbols:
            color = "\033[92m" if s['change_24h_pct'] > 0 else "\033[91m"
            print(f"{s['symbol']:<10} | {s['last_price']:<10.4f} | {color}{s['change_24h_pct']:>+8.2f}%\033[0m | {s.get('regime', 'N/A')}")

    elif action == "calculate_orderbook_imbalance":
        sep   = "─" * 62
        sep2  = "═" * 62
        sym   = result.get("symbol", "")
        mid   = result.get("mid_price", 0)
        bias  = result.get("book_bias", "NEUTRAL")
        ratio = result.get("overall_ratio", 1.0)
        spoof = result.get("spoof_detected", False)

        bias_icon = "🟢" if bias == "BULLISH" else "🔴" if bias == "BEARISH" else "⚪"

        print(f"\n{sep2}")
        print(f"  ORDERBOOK IMBALANCE  │  {sym}  │  Mid: {mid}")
        print(f"{sep2}")
        print(f"  Book Bias  : {bias_icon} {bias}")
        print(f"  Ratio B/A  : {ratio:.4f}   (>1.0 = more bids)")
        print(f"  Bid Vol    : {result.get('total_bid_vol', 0):>12,.2f}")
        print(f"  Ask Vol    : {result.get('total_ask_vol', 0):>12,.2f}")
        print(f"  Spread     : {result.get('spread', 0):.6f}  ({result.get('spread_pct', 0):.4f}%)")
        print(f"  Spoof Alert: {'⚠️  YES — Tiers: ' + str(result.get('spoofed_tiers')) if spoof else '✅ None detected'}")

        print(f"\n  {'TIER ANALYSIS':─<57}")
        print(f"  {'Tier':>4}  {'Bid Vol':>12}  {'Ask Vol':>12}  {'Ratio':>7}  {'Bias':>8}  {'Spoof':>5}")
        print(f"  {sep}")
        for t in result.get("tiers", []):
            spf   = "⚠️" if t["spoof_flag"] else "  "
            prs   = "🟢" if t["pressure"] == "BULLISH" else "🔴" if t["pressure"] == "BEARISH" else "⚪"
            print(f"  {t['tier']:>4}  {t['bid_volume']:>12,.2f}  "
                  f"{t['ask_volume']:>12,.2f}  {t['imbalance_ratio']:>7.4f}  "
                  f"{prs} {t['pressure']:>7}  {spf}")

    elif action == "calculate_liquidity_heatmap":
        sep2 = "═" * 62
        sym  = result.get("symbol", "")
        lc   = result.get("last_close", 0)
        pr   = result.get("price_range", {})

        print(f"\n{sep2}")
        print(f"  LIQUIDITY HEATMAP  │  {sym}  │  Close: {lc}")
        print(f"{sep2}")
        print(f"  Range  : {pr.get('low', 0):.4f} → {pr.get('high', 0):.4f}")
        print(f"  Bucket : {result.get('bucket_size', 0):.4f}  per zone")
        print(f"  Hottest: {result.get('hottest_level', 0):.4f}")
        print()
        print(f"  {'Bkt':>3}  {'Mid Price':>11}  {'OB Vol':>12}  "
              f"{'Kline Vol':>12}  {'Total':>12}  {'Heat':>5}  {'%':>5}  Side")
        print(f"  {'─'*70}")

        for b in result.get("buckets", []):
            heat_icon = "🔥" if b["heat"] == "HOT" else "🌡" if b["heat"] == "WARM" else "❄️ "
            current   = " ◄" if abs(b["mid_price"] - lc) / lc < 0.002 else ""
            print(f"  {b['bucket']:>3}  {b['mid_price']:>11.4f}  "
                  f"{b['ob_volume']:>12,.2f}  {b['kline_volume']:>12,.2f}  "
                  f"{b['total_volume']:>12,.2f}  {heat_icon}  "
                  f"{b.get('heat_pct',0):>4.1f}%  {b['ob_side']:>5}{current}")

    elif action == "calculate_market_depth_profile":
        sep2 = "═" * 62
        sym  = result.get("symbol", "")
        mid  = result.get("mid_price", 0)

        print(f"\n{sep2}")
        print(f"  MARKET DEPTH PROFILE  │  {sym}  │  Mid: {mid}")
        print(f"{sep2}")
        print(f"  Total Bid Liquidity : ${result.get('total_bid_usdt', 0):>14,.2f}")
        print(f"  Total Ask Liquidity : ${result.get('total_ask_usdt', 0):>14,.2f}")
        print(f"  Depth Asymmetry     :  {result.get('depth_asymmetry', 1.0):>8.4f}  (>1 = more bids)")
        icb = "⚠️  POSSIBLE ICEBERG" if result.get("iceberg_detected_bid") else "✅ Clean"
        ica = "⚠️  POSSIBLE ICEBERG" if result.get("iceberg_detected_ask") else "✅ Clean"
        print(f"  Bid Iceberg         : {icb}")
        print(f"  Ask Iceberg         : {ica}")

        print(f"\n  {'SLIPPAGE ESTIMATES':─<57}")
        print(f"  {'Order ($)':>10}  {'Avg Fill (Bid)':>15}  "
              f"{'Slip%':>6}  {'Lvls':>5}  {'Avg Fill (Ask)':>15}  {'Slip%':>6}")
        print(f"  {'─'*65}")
        for bs, as_ in zip(result.get("bid_slippage", []), result.get("ask_slippage", [])):
            ff_b = "✓" if bs["fully_filled"] else "✗"
            ff_a = "✓" if as_["fully_filled"] else "✗"
            print(f"  {bs['order_usdt']:>10,.0f}  "
                  f"{bs['avg_fill_price']:>15.4f} {bs['slippage_pct']:>5.4f}%{ff_b}"
                  f"  {bs['levels_consumed']:>5}  "
                  f"{as_['avg_fill_price']:>15.4f} {as_['slippage_pct']:>5.4f}%{ff_a}")

    elif action == "calculate_orderflow_delta":
        return bot.calculate_orderflow_delta(
            symbol=symbol,
            interval=interval,
            limit=int(kwargs.get("limit", 100)),
        )

    elif action == "calculate_orderbook_imbalance":
        return bot.calculate_orderbook_imbalance(
            symbol=symbol,
            depth=int(kwargs.get("depth", 50)),
            tier_size=int(kwargs.get("tier_size", 10)),
            spoof_threshold=float(kwargs.get("spoof_threshold", 5.0)),
        )

    elif action == "calculate_liquidity_heatmap":
        return bot.calculate_liquidity_heatmap(
            symbol=symbol,
            interval=interval,
            depth=int(kwargs.get("depth", 100)),
            bucket_count=int(kwargs.get("bucket_count", 20)),
            kline_limit=int(kwargs.get("kline_limit", 100)),
        )

    elif action == "calculate_market_depth_profile":
        order_sizes_raw   = kwargs.get("order_sizes", "100,500,1000,5000")
        distance_pcts_raw = kwargs.get("distance_pcts", "0.1,0.25,0.5,1.0,2.0")
        order_sizes_list  = [float(x) for x in str(order_sizes_raw).split(",")]
        distance_pcts_list = [float(x) for x in str(distance_pcts_raw).split(",")]
        return bot.calculate_market_depth_profile(
            symbol=symbol,
            depth=int(kwargs.get("depth", 200)),
            order_sizes=order_sizes_list,
            distance_pcts=distance_pcts_list,
        )

    else:
        # Fallback to JSON
        print(json.dumps(result, indent=2, ensure_ascii=False))
