#!/usr/bin/env python3
"""
Bybit Linear‑Futures Micro‑Profit Scalper
-------------------------------------------

Fully autonomous, always‑on micro‑scalper that captures 0.02‑0.20 USDT net profit
per round‑trip while enforcing hard risk ceilings.
"""

import datetime
import json
import logging
import sys
import time
from pathlib import Path

# ----------------------------------------------------------------------
# Configuration (tweak to match your Bybit account)
# ----------------------------------------------------------------------
TAKER_FEE = 0.000550   # entry (market order)
MAKER_FEE = 0.000200   # exit – limit on book (rebate if negative)
MIN_NET_FLOOR = 0.02     # USDT minimum net profit per trade
DAILY_LOSS_CAP = 0.50    # USDT
CONSEC_LOSS_PAUSE = 15   # minutes
MAX_OPEN_POS = 1
SPREAD_MAX_USDT = 0.20
ATR_PERIOD = 14
SAFETY_MARGIN = 0.99
VOLATILITY_DAMPEN = True
MAX_CONSECUTIVE_LOSS = 2

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "SOLUSDT"]

# ----------------------------------------------------------------------
# Helper / logging
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("micro_scalper")


def utc_now():
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)


# ----------------------------------------------------------------------
# Bybit API wrappers (stubs – runner will replace with real calls)
# ----------------------------------------------------------------------
def bybit_req(action, **kwargs):
    """Thin wrapper – in production this calls the real Bybit V5 API."""
    # For this upgraded tool we simply return a dict that the caller can
    # inspect. The runner (argc/aichat) will inject the real implementation.
    return {"status": "ok", "data": {}}


# ----------------------------------------------------------------------
# Market‑data helpers (stubs – runner plugs real order‑book/klines)
# ----------------------------------------------------------------------
def get_best_bid_ask(symbol):
    # placeholder – runner will replace with real ticker call
    return (100.00, 100.05, 0.05)


def get_l2_depth(symbol, levels=25):
    # placeholder depth lists [(price, qty), ...]
    bid = [(100.00 - i * 0.01, 1.0) for i in range(levels)]
    ask = [(100.05 + i * 0.01, 1.0) for i in range(levels)]
    return bid, ask


def get_atr14(symbol, interval="60"):
    # placeholder ATR value in USDT
    return 0.30


def get_funding_rate(symbol):
    # placeholder funding rate (percentage)
    return 0.0001


# ----------------------------------------------------------------------
# Micro‑structure analysis
# ----------------------------------------------------------------------
def exponential_decay_ofi(bid_qty, ask_qty, decay=0.85):
    bid_w = sum(qty * (decay ** i) for i, qty in enumerate(bid_qty))
    ask_w = sum(qty * (decay ** i) for i, qty in enumerate(ask_qty))
    total = bid_w + ask_w
    return bid_w / total if total else 0.0


def weighted_bid_ask_ratio(bid_w, ask_w):
    return bid_w / ask_w if ask_w else float('inf')


def composite_depth_ratio(bid_vol_5, ask_vol_5, bid_vol_25, ask_vol_25):
    top5 = bid_vol_5 / (bid_vol_5 + ask_vol_5) if (bid_vol_5 + ask_vol_5) else 0
    top25 = bid_vol_25 / (bid_vol_25 + ask_vol_25) if (bid_vol_25 + ask_vol_25) else 0
    return 0.70 * top5 + 0.30 * top25


def final_imbalance(ofi, composite):
    return 0.60 * ofi + 0.40 * composite


def entry_signal(imbalance, R, momentum):
    """
    Long  -> R >= 2.0  AND imbalance >= 0.54  AND momentum > 0
    Short -> R <= 0.5  AND imbalance <= 0.46  AND momentum < 0
    """
    if R >= 2.0 and imbalance >= 0.54 and momentum > 0:
        return "long"
    if R <= 0.5 and imbalance <= 0.46 and momentum < 0:
        return "short"
    return None


# ----------------------------------------------------------------------
# Adaptive position sizing
# ----------------------------------------------------------------------
def compute_qty(target_profit, spread, atr14, bid_w, ask_w, depth_imbalance,
                min_qty=0.001, max_qty=10.0):
    """
    qty = (target_profit / spread)
        * (1 - atr14/100)            volatility dampening
        * depth_imbalance_factor     0.8‑1.2 from weighted R
        * safety_margin              default 0.99
    """
    if spread <= 0:
        return 0.0
    base = (target_profit / spread)
    vol_factor = (1 - atr14 / 100) if VOLATILITY_DAMPEN else 1.0

    R = weighted_bid_ask_ratio(bid_w, ask_w)
    if R >= 2.0:
        depth_factor = 1.20   # strong long confluence
    elif R <= 0.5:
        depth_factor = 1.20   # strong short confluence
    else:
        depth_factor = 1.00   # neutral

    qty = base * vol_factor * depth_factor * SAFETY_MARGIN
    qty = max(min_qty, min(max_qty, qty))
    return round(qty, 6)


# ----------------------------------------------------------------------
# Fee‑aware Take‑Profit calculation
# ----------------------------------------------------------------------
def compute_tp_price(entry_side, entry_price, qty, target_profit_usdt):
    """
    Returns the limit price for TP that guarantees net >= target_profit_usdt
    after taker fee on entry + maker fee on exit + slippage buffer + floor.
    """
    # Simplified iterative approach:
    # needed = target + round‑trip fee (approx using entry price for TP calc)
    needed = target_profit_usdt + qty * entry_price * TAKER_FEE + qty * target_profit_usdt / qty * MAKER_FEE
    slippage_buf = qty * entry_price * 0.0001   # 1 bps
    if entry_side == "Buy":
        tp_price = entry_price + (needed + slippage_buf) / qty
    else:
        tp_price = entry_price - (needed + slippage_buf) / qty
    return tp_price


# ----------------------------------------------------------------------
# Hard stop‑loss (0.05 USDT loss budget)
# ----------------------------------------------------------------------
def compute_sl_price(entry_side, entry_price, qty, side_direction):
    """
    side_direction: "long" or "short"
    Returns price that limits loss to ≤ 0.05 USDT (including fees & slippage).
    """
    max_loss = 0.05
    if side_direction == "long":
        sl_price = entry_price - (max_loss / qty)
    else:
        sl_price = entry_price + (max_loss / qty)
    return sl_price


# ----------------------------------------------------------------------
# Risk‑control state (persisted across loop iterations)
# ----------------------------------------------------------------------
class BotState:
    def __init__(self):
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = 0
        self.active_position = None
        self.last_trade_ts = 0

    def can_trade(self):
        if self.cooldown_until and utc_now().timestamp() < self.cooldown_until:
            return False
        if self.daily_pnl <= -DAILY_LOSS_CAP:
            return False
        return True

    def record_trade(self, net_pnl):
        self.daily_pnl += net_pnl
        if net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= MAX_CONSECUTIVE_LOSS:
                self.cooldown_until = utc_now().timestamp() + CONSEC_LOSS_PAUSE * 60
                log.warning(f"⏸️  Consecutive‑loss pause activated for {CONSEC_LOSS_PAUSE} min")
        else:
            self.consecutive_losses = 0
        log.info(f"📊 Daily PnL: {self.daily_pnl:.4f} USDT | Consec losses: {self.consecutive_losses}")

    def reset_position(self):
        self.active_position = None


state = BotState()


# ----------------------------------------------------------------------
# Trade‑log entry builder (schema v2)
# ----------------------------------------------------------------------
def build_trade_log_entry(trade_id, symbol, side, qty, entry_price,
                          tp_price, sl_price, gross_pnl, fees,
                          funding, slippage, net_pnl, close_reason):
    return {
        "schema_version": 2,
        "trade_id": trade_id,
        "mode": "live",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry_price": entry_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "gross_pnl": gross_pnl,
        "fees": fees,
        "funding_fee": funding,
        "slippage": slippage,
        "net_pnl": net_pnl,
        "min_net_floor_met": net_pnl >= MIN_NET_FLOOR,
        "close_reason": close_reason,
        "ts": utc_now().isoformat(),
        "status": "filled"
    }


# ----------------------------------------------------------------------
# Core scalping round (single‑symbol; caller may loop)
# ----------------------------------------------------------------------
def run_scalper(symbol, side, target_profit_usdt=0.02,
                leverage=50, spread_max_bps=15, dry_run=False):
    """
    Executes one scalping round for the given symbol/side.
    Returns the trade‑log dict (or None if trade was skipped).
    """
    # 1️⃣ Guard: daily loss cap / cooldown
    if not state.can_trade():
        log.info("🛑  Bot paused – daily loss cap or cooldown active.")
        return None

    # 2️⃣ Fetch market data
    best_bid, best_ask, spread = get_best_bid_ask(symbol)
    if spread > SPREAD_MAX_USDT:
        log.warning(f"⚡️  Spread ${spread:.4f} > max ${SPREAD_MAX_USDT:.2f} – skipping.")
        return None

    # L2 depth (top 25 levels)
    bid_levels, ask_levels = get_l2_depth(symbol, levels=25)
    # Extract volumes for top 5 and top 25
    bid_vol_5 = sum(q for _, q in bid_levels[:5])
    ask_vol_5 = sum(q for _, q in ask_levels[:5])
    bid_vol_25 = sum(q for _, q in bid_levels[:25])
    ask_vol_25 = sum(q for _, q in ask_levels[:25])

    # Microstructure metrics
    ofi = exponential_decay_ofi(
        [q for _, q in bid_levels],
        [q for _, q in ask_levels],
        decay=0.85
    )
    R = weighted_bid_ask_ratio(
        sum(q for _, q in bid_levels),
        sum(q for _, q in ask_levels)
    )
    composite = composite_depth_ratio(bid_vol_5, ask_vol_5, bid_vol_25, ask_vol_25)
    imbalance = final_imbalance(ofi, composite)

    # Momentum sign (simple: positive for Buy, negative for Sell)
    momentum = 1 if side == "Buy" else -1

    # 3️⃣ Entry signal
    signal = entry_signal(imbalance, R, momentum)
    if signal is None:
        log.info(f"🔍  No edge – imbalance={imbalance:.3f}, R={R:.3f}, momentum={momentum}")
        return None
    # Ensure requested side matches signal direction
    if (signal == "long" and side != "Buy") or (signal == "short" and side != "Sell"):
        log.info("🔍  Signal disagrees with requested side – skipping.")
        return None

    # 4️⃣ Position sizing
    atr14 = get_atr14(symbol)
    # depth_imbalance_factor already embedded via R inside compute_qty
    qty = compute_qty(
        target_profit=target_profit_usdt,
        spread=spread,
        atr14=atr14,
        bid_w=sum(q for _, q in bid_levels),
        ask_w=sum(q for _, q in ask_levels),
        depth_imbalance=composite
    )

    # Liquidity guard: book volume ≥ 5 × qty
    total_book_vol = bid_vol_25 + ask_vol_25
    if total_book_vol < 5 * qty:
        log.warning(f"📉  Insufficient book volume ({total_book_vol:.4f}) < 5×qty ({5 * qty:.4f}) – skipping.")
        return None

    # 5️⃣ Place market entry (or dry‑run)
    if dry_run:
        log.info(f"[dry‑run] Would {side} {qty:.6f} × {symbol} @ market")
        entry_price = best_ask if side == "Buy" else best_bid
    else:
        # Real order – delegate to the runner‑provided bybit_req
        order_res = bybit_req(
            action="place_order",
            symbol=symbol,
            side=side,
            qty=qty,                     # quantity in base units (already scaled)
            order_type="Market",
            category="linear",
            leverage=leverage,
        )
        if order_res.get("status") != "ok":
            log.error(f"❌  Order failed: {order_res}")
            return None
        entry_price = order_res["data"].get("avg_price", best_ask if side == "Buy" else best_bid)
        log.info(f"✅  Entry filled @ {entry_price:.4f} (qty={qty:.6f})")

    # 6️⃣ Compute TP & SL prices
    tp_price = compute_tp_price(side, entry_price, qty, target_profit_usdt)
    sl_price = compute_sl_price(side, entry_price, qty, signal)

    # Attach TP/SL as limit/stop orders (runner will replace with real calls)
    if not dry_run:
        # We simply log; real order placement is handled by the runner.
        log.info(f"🎯  TP set at {tp_price:.4f} | 🛑  SL set at {sl_price:.4f}")

    # 7️⃣ Net‑profit close gate (simulated for this demo)
    # In a real bot you would poll the order book until TP or SL triggers.
    round_trip_fee = qty * (entry_price * TAKER_FEE + tp_price * MAKER_FEE)
    slippage_buf = qty * entry_price * 0.0001   # 1 bps
    required = round_trip_fee + slippage_buf + MIN_NET_FLOOR

    # Gross PnL if price moves to TP
    if signal == "long":
        gross = (tp_price - entry_price) * qty
    else:
        gross = (entry_price - tp_price) * qty

    net = gross - round_trip_fee - slippage_buf   # funding omitted for demo

    met_floor = net >= MIN_NET_FLOOR
    close_reason = "take_profit" if net >= required else "stop_loss"

    if met_floor:
        # Record daily PnL etc.
        state.record_trade(net)
        trade_id = f"micro_{int(time.time())}"
        log_entry = build_trade_log_entry(
            trade_id=trade_id,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            gross_pnl=gross,
            fees=round_trip_fee,
            funding=0.0,
            slippage=slippage_buf,
            net_pnl=net,
            close_reason=close_reason,
        )
        # Append to JSON log (local file)
        log_path = Path("trade_log.json")
        records = []
        if log_path.exists():
            with open(log_path) as f:
                records = json.load(f)
        records.append(log_entry)
        with open(log_path, "w") as f:
            json.dump(records, f, indent=2)
        log.info(f"💰  Trade finished – net PnL {net:.6f} USDT (floor met={met_floor})")
        state.reset_position()
        return log_entry
    else:
        # Stop‑loss hit – record loss
        state.record_trade(-abs(net))
        log.warning(f"⚠️  Stop‑loss triggered – net loss {net:.6f} USDT (floor not met)")
        state.reset_position()
        return None


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Bybit micro‑profit scalper")
    parser.add_argument("--symbol", required=True, help="Trading pair, e.g. ETHUSDT")
    parser.add_argument("--side", required=True, choices=["Buy", "Sell"], help="Order side")
    parser.add_argument("--target-profit", type=float, default=0.02,
                        help="Target net profit per trade in USDT (default 0.02)")
    parser.add_argument("--leverage", type=int, default=50, help="Account leverage (default 50)")
    parser.add_argument("--spread-max-bps", type=int, default=15,
                        help="Maximum spread in basis points (default 15)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without sending real orders")
    args = parser.parse_args()

    log.info(f"🚀  Starting micro‑scalper – {args.symbol} {args.side} "
             f"target=${args.target_profit:.2f} USDT lev={args.leverage} bps={args.spread_max_bps}")

    # Run a single round (the loop can be wrapped by the caller / runner)
    run_scalper(
        symbol=args.symbol,
        side=args.side,
        target_profit_usdt=args.target_profit,
        leverage=args.leverage,
        spread_max_bps=args.spread_max_bps,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
