---
name: bybit-realm-testing
description: How to test and use the Bybit Realm trading tool (tools/bybit_realm.py). Covers environment setup, geo-IP bypass, API credentials, and testing workflow.
---

# Bybit Realm Testing & Usage

## Environment Requirements

- Python 3.10+
- `tor` and `torsocks` installed (for geo-blocked regions)
- PySocks: `pip install pysocks requests websocket-client`
- argc tool framework (for CLI declarations): see `scripts/build-declarations.py`

## Geo-IP Bypass Setup

Bybit API is geo-blocked from US/certain regions. The tool uses a 3-tier fallback:
1. **SOCKS5 proxy** (PySocks via Tor port 9050) - primary
2. **torsocks binary** (curl wrapper) - fallback
3. **Direct connection** - last resort (403 in blocked regions)

```bash
# Install and start Tor
sudo apt-get install -y tor torsocks
sudo systemctl start tor
sudo systemctl enable tor

# Verify Tor is running
curl --socks5 127.0.0.1:9050 https://check.torproject.org/api/ip
```

## API Credentials

Set as environment variables (saved as permanent Devin secrets):
```bash
export BYBIT_API_KEY="your_key"
export BYBIT_API_SECRET="your_secret"
```

## Testing Workflow

### Quick smoke test
```python
import sys; sys.path.insert(0, '/home/ubuntu/repos/llm-functions')
from tools.bybit_realm import run
result = run(action='health_check')
print(result)  # Should show status=ok, circuit=CLOSED
```

### Test categories (in order)
1. System: `health_check`, `connection_health`, `get_server_time`
2. Market data: `get_ticker`, `get_orderbook`, `get_klines`
3. Account: `get_wallet_balance`, `get_positions`, `get_fee_rate`
4. Indicators: `calculate_bollinger_bands`, `calculate_macd`, `calculate_rsi`
5. Analysis: `spread_analysis`, `market_health`, `comprehensive_trend`
6. Sizing: `calculate_position_size`, `calculate_breakeven`, `adaptive_position_size`
7. Macros: `macro_dca_plan`, `macro_grid_plan`, `macro_smart_entry`
8. Orders: `place_order` (use low price like $50k for limit buy to avoid fill)

### Common parameter mapping
- `price` = entry price (NOT `entry_price`)
- `sl_price` = stop loss price
- `risk_usdt` = risk amount in USDT (NOT `risk_amount`)
- `starting_capital` = starting balance (NOT `balance`)
- `stop_loss` / `take_profit` = SL/TP for `set_trading_stop`
- `symbols` = comma-separated string for multi-symbol actions

### Known gotchas
- `retCode=110043` = "leverage not modified" (not an error, already set)
- `retCode=110092` = trigger price validation (e.g., buy trigger below current price)
- `get_liquidations` returns 404 via SOCKS5 - possible endpoint deprecation
- `move_sl_to_breakeven` / `auto_sl_tp` fail with "Invalid position data" when no position is open
- GET params MUST be sorted alphabetically for HMAC signature consistency across tiers
- Tor circuit renewal may fail if control port auth is password-protected; SOCKS5 still works

### argc build verification
```bash
cd /home/ubuntu/repos/llm-functions
argc build@tool bybit_realm.py
# Should generate functions.json and bin/bybit_realm without errors
```

### Syntax/lint checks
```bash
python3 -m py_compile tools/bybit_realm.py
python3 -m pyflakes tools/bybit_realm.py  # Only warnings, no errors expected
```
