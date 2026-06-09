#!/usr/bin/env bash
# ==============================================================================
# Refactored Web Search Utility
#
# @describe Perform a web search
# @option --query! <TEXT> Search query
# @option --limit <NUM> Maximum results (default: 10)
# @option --include-domains <DOMAINS> Comma-separated domains to include
# @option --exclude-domains <DOMAINS> Comma-separated domains to exclude
# @flag --no-cache Do not use cached results
# ==============================================================================

set -euo pipefail

main() {
    CACHE_DIR="${WEBSEARCH_CACHE_DIR:-$HOME/.cache/websearch}"
    CONFIG_DIR="${WEBSEARCH_CONFIG_DIR:-$HOME/.config/websearch}"
    PYTHON_SCRIPT="/data/data/com.termux/files/home/.config/aichat/llm-functions/tools/web_search.py"

    mkdir -p "$CACHE_DIR" "$CONFIG_DIR"

    # Use argc variables (provided by eval)
    local query="${argc_query}"
    local limit="${argc_limit:-10}"
    local inc="${argc_include_domains:-}"
    local exc="${argc_exclude_domains:-}"

    # Generate cache key
    local cache_key
    cache_key=$(printf '%s' "$query$limit$inc$exc" | sha256sum | cut -d' ' -f1)
    local file="${CACHE_DIR}/${cache_key}.json"

    # Helper to handle output
    handle_output() {
        local content="$1"
        echo "$content"
        if [[ -n "${LLM_OUTPUT:-}" ]]; then
            printf '%s
' "$content" >> "$LLM_OUTPUT"
        fi
    }

    # Try cache
    if [[ "${argc_no_cache:-}" != "true" && -f "$file" ]]; then
        handle_output "$(cat "$file")"
        return 0
    fi

    # Invoke Python
    local resp
    resp=$(python3 "$PYTHON_SCRIPT" "$query" --limit "$limit" ${inc:+--include-domains "$inc"} ${exc:+--exclude-domains "$exc"})
    echo "$resp" >"$file"
    handle_output "$resp"
}

eval "$(argc --argc-eval "$0" "$@")"
main "$@"
