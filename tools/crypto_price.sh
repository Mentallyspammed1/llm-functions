#!/usr/bin/env bash
# @describe Get current price of a cryptocurrency (e.g., bitcoin, ethereum).
# @arg coin! The coin name.
main() {
    # This tool fetches price data as JSON using curl.
    # The output is not directly colorized by curl in this mode.
    curl -s "https://api.coingecko.com/api/v3/simple/price?ids=${argc_coin}&vs_currencies=usd"
}
eval "$(argc --argc-eval "$0" "$@")"
