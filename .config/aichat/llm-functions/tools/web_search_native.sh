#!/usr/bin/env bash
set -e

# @describe Perform web search using Google search results with no API keys required
# @option --query! The search query string
# @option --limit Default: 5, Max: 10, Number of results to return

main() {
    local limit="${argc_limit:-5}"
    
    # Validate limit
    if [[ "$limit" -gt 10 ]]; then
        limit=10
    fi
    
    # Call the web-search MCP server
    node /data/data/com.termux/files/home/web-search/build/index.js "$argc_query" "$limit" >> "$LLM_OUTPUT"
}

eval "$(argc --argc-eval "$0" "$@")"
