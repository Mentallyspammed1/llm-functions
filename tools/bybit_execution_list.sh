#!/data/data/com.termux/files/usr/bin/env bash
# ------------------------------------------------------------
# Bybit Execution List (Trade History) Tool (V5 API)
# ------------------------------------------------------------
# Retrieves recent execution records for the authenticated user.
# Supports an optional `symbol` filter and pagination via `cursor`.
# Writes a concise JSON summary to the `$LLM_OUTPUT` variable.
# ------------------------------------------------------------

# @describe Retrieve recent execution records for the authenticated user.
# @option --symbol <SYMBOL> Filter by trading symbol
# @option --cursor <CURSOR> Pagination cursor

set -euo pipefail

# ---- Load environment variables ---------------------------------------
if [[ -f "/data/data/com.termux/files/home/.config/aichat/llm-functions/.env" ]]; then
    # shellcheck source=/dev/null
    source "/data/data/com.termux/files/home/.config/aichat/llm-functions/.env"
fi

# Default values
API_KEY="${API_KEY:-}"
API_SECRET="${API_SECRET:-}"
PROXY_URL="${PROXY_URL:-}"
RECV_WINDOW="${RECV_WINDOW:-5000}"

# ---- Helper: generate request signature -------------------------------
_sign_request() {
    local method="$1"
    local endpoint="$2"
    local query="$3"
    local timestamp="$4"
    # Bybit signs: timestamp + HTTP method + endpoint + query string (no body for GET)
    local string_to_sign="${timestamp}${method}${endpoint}${query}"
    local sign
    sign=$(printf "%s" "$string_to_sign" | openssl dgst -sha256 -hmac "${API_SECRET}" -hex | sed 's/^.* //')
    echo "$sign"
}

# ---- Argument parsing -------------------------------------------------
usage() {
    cat <<EOF
Usage: $0 [--symbol <SYMBOL>] [--cursor <CURSOR>]
Fetch recent execution records. Optional symbol filter and pagination.
