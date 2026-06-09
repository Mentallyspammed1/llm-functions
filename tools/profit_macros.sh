#!/usr/bin/env bash
# ==============================================================================
# profit_macros.sh – Ready‑to‑run macro shortcuts for micro_profit.py
# ==============================================================================
# Each function wraps a common micro‑profit configuration.
# To use, source this file or call the functions directly, e.g.:
#     source profit_macros.sh && macro_basic_scalp
# ==============================================================================

# ------------------------------------------------------------------------------
# 1️⃣ Basic Micro‑Scalp Macro (≈ 5 USDT target)
# ------------------------------------------------------------------------------
macro_basic_scalp() {
  # Sample depth – replace with real order‑book data from your exchange
  local bids='[[50000, 10], [49990, 15], [49980, 20]]'
  local asks='[[50010, 10], [50020, 15], [50030, 20]]'

  python3 micro_profit.py \
    --symbol BTCUSDT \
    --side Buy \
    --qty 0.001 \
    --target 5.0 \
    --bids "$bids" \
    --asks "$asks" \
    --maker-fee 0.0002 \
    --taker-fee 0.00055 \
    --leverage 1.0 \
    --risk-reward 2.0 \
    --post-only
}

# ------------------------------------------------------------------------------
# 2️⃣ Leveraged Momentum Macro (≈ 10 USDT target)
# ------------------------------------------------------------------------------
macro_leverage_momentum() {
  # Sample depth – replace with real order‑book data
  local bids='[[3000, 100], [2995, 200], [2990, 150]]'
  local asks='[[3005, 100], [3010, 200], [3015, 150]]'

  python3 micro_profit.py \
    --symbol ETHUSDT \
    --side Buy \
    --qty 0.01 \
    --target 10.0 \
    --bids "$bids" \
    --asks "$asks" \
    --maker-fee 0.0002 \
    --taker-fee 0.00055 \
    --leverage 3.0 \
    --risk-reward 2.5 \
    --kelly-win 0.55
}

# ------------------------------------------------------------------------------
# 3️⃣ Order‑Book Wall‑Surfing Macro (≈ 15 USDT target)
# ------------------------------------------------------------------------------
macro_wall_surfing() {
  # Sample depth – replace with real order‑book data
  local bids='[[150, 500], [149.5, 800], [149, 300]]'
  local asks='[[150.5, 500], [151, 800], [151.5, 300]]'

  python3 micro_profit.py \
    --symbol SOLUSDT \
    --side Sell \
    --qty 0.1 \
    --target 15.0 \
    --bids "$bids" \
    --asks "$asks" \
    --maker-fee 0.0002 \
    --taker-fee 0.00055 \
    --leverage 1.0 \
    --ignore-regime \
    --post-only \
    --reduce-only
}

# ------------------------------------------------------------------------------
# Optional helper – print usage
# ------------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $0 [macro_name]

Available macros:
  basic_scalp          – Small 5 USDT scalp (BTCUSDT) with sample depth
  leverage_momentum    – 10 USDT with 3× leverage (ETHUSDT) with sample depth
  wall_surfing         – 15 USDT riding order‑book walls (SOLUSDT) with sample depth

Example:
  $0 basic_scalp   # runs the basic scalp macro (uses built‑in sample bids/asks)
EOF
}

# ------------------------------------------------------------------------------
# Entry point – allow direct execution
# ------------------------------------------------------------------------------
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  if [[ -z "$1" ]]; then
    usage
    exit 1
  fi
  case "$1" in
    basic_scalp)      macro_basic_scalp ;;
    leverage_momentum) macro_leverage_momentum ;;
    wall_surfing)     macro_wall_surfing ;;
    *)                usage; exit 1 ;;
  esac
fi
