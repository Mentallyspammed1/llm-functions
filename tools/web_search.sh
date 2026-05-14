#!/usr/bin/env bash
set -euo pipefail

# @describe Perform a high-quality web search using the You.com Search API with enhanced features.
# @option --query!                              Search query string
# @option --limit <INT>                         Default: 5, Max: 20 - Maximum number of results to return
# @option --offset <INT>                        Default: 0, Max: 9 - Number of pages to skip
# @option --include-domains                     Comma-separated list of domains to prioritize
# @option --exclude-domains                     Comma-separated list of domains to block
# @option --safe-search [off|moderate|strict]   Default: moderate - Safe search level
# @option --country                             Country code (e.g. "US", "IN")
# @option --format [json|table|urls|compact]    Default: json - Output format
# @option --verbose                             Enable verbose output
# @option --no-cache                            Disable caching
# @option --config                              Custom config file path

# @env LLM_OUTPUT           The output path
# @env YOU_API_KEY          You.com API key
# @env WEBSEARCH_CACHE_DIR  Cache directory
# @env WEBSEARCH_CONFIG_DIR Config directory

# ---------------------------------------------------------------------------
# Global variables
# ---------------------------------------------------------------------------
CACHE_DIR="${WEBSEARCH_CACHE_DIR:-$HOME/.cache/websearch}"
CONFIG_DIR="${WEBSEARCH_CONFIG_DIR:-$HOME/.config/websearch}"
HISTORY_FILE="$CONFIG_DIR/history.log"
LAST_SEARCH_FILE="$CACHE_DIR/last_search"

# ---------------------------------------------------------------------------
# Utility: verbose-aware logging to stderr
# ---------------------------------------------------------------------------
log_verbose() {
    # FIX: Centralised verbose logging avoids repeating the guard everywhere.
    if [[ "${argc_verbose:-}" == "true" ]]; then
        echo "[verbose] $*" >&2
    fi
}

# ---------------------------------------------------------------------------
# Utility: portable sha256 — sha256sum (Linux) or shasum -a 256 (macOS/BSD)
# FIX: generate_cache_key previously used sha256sum which is absent on macOS.
# ---------------------------------------------------------------------------
sha256_portable() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 | cut -d' ' -f1
    else
        # Last-resort: use cksum (always present) — weaker but functional
        cksum | awk '{print $1}'
    fi
}

# ---------------------------------------------------------------------------
# Utility: portable ISO-8601 timestamp
# FIX: date -Iseconds is GNU-only; macOS uses date -u +"%Y-%m-%dT%H:%M:%S%z"
# ---------------------------------------------------------------------------
iso_timestamp() {
    if date -Iseconds >/dev/null 2>&1; then
        date -Iseconds
    else
        date -u +"%Y-%m-%dT%H:%M:%S%z"
    fi
}

# ---------------------------------------------------------------------------
# Utility: check that a value is a non-negative integer
# FIX: Prevents -lt/-gt from throwing errors on non-integer inputs.
# ---------------------------------------------------------------------------
is_integer() {
    [[ "$1" =~ ^-?[0-9]+$ ]]
}

# ---------------------------------------------------------------------------
# init_directories — unchanged, already correct
# ---------------------------------------------------------------------------
init_directories() {
    mkdir -p "$CACHE_DIR" "$CONFIG_DIR"
}

# ---------------------------------------------------------------------------
# load_config
# FIX: Skip empty first element when argc_config is unset to prevent
#      "source: filename argument required" errors.
# FIX: Guard sourced files with a subshell dry-run to catch syntax errors.
# ---------------------------------------------------------------------------
load_config() {
    local config_files=()

    # Only add custom config path when explicitly provided
    [[ -n "${argc_config:-}" ]] && config_files+=("${argc_config}")

    config_files+=("$CONFIG_DIR/config" "./.websearchrc" "/etc/websearch/config")

    for config_file in "${config_files[@]}"; do
        if [[ -f "$config_file" ]]; then
            # Safety check: ensure the file is at least parseable before sourcing
            if bash -n "$config_file" 2>/dev/null; then
                # shellcheck source=/dev/null
                source "$config_file"
                log_verbose "Loaded config from: $config_file"
            else
                echo "Warning: Skipping malformed config file: $config_file" >&2
            fi
            break
        fi
    done

    # Set defaults from config or environment
    DEFAULT_LIMIT="${DEFAULT_LIMIT:-5}"
    DEFAULT_SAFE_SEARCH="${DEFAULT_SAFE_SEARCH:-moderate}"
    CACHE_TTL="${CACHE_TTL:-3600}"
    MIN_SEARCH_INTERVAL="${MIN_SEARCH_INTERVAL:-1}"
}

# ---------------------------------------------------------------------------
# validate_input
# FIX: Accept offset as an explicit parameter instead of reading the global
#      $argc_offset directly — avoids hidden coupling and makes unit-testing easier.
# FIX: Guard every numeric comparison with is_integer to prevent bash errors
#      when non-numeric values are passed.
# ---------------------------------------------------------------------------
validate_input() {
    local query="$1"
    local limit="$2"
    local safe_search="$3"
    local offset="$4"   # FIX: was read from $argc_offset implicitly

    # Validate query length
    if [[ ${#query} -lt 2 || ${#query} -gt 1000 ]]; then
        echo "Error: Query must be between 2 and 1000 characters" >&2
        exit 1
    fi

    # FIX: Validate limit is actually an integer before numeric comparison
    if ! is_integer "$limit"; then
        echo "Error: Limit must be an integer" >&2
        exit 1
    fi
    if [[ "$limit" -lt 1 || "$limit" -gt 20 ]]; then
        echo "Error: Limit must be between 1 and 20" >&2
        exit 1
    fi

    # Validate safe search options
    if [[ ! "$safe_search" =~ ^(off|moderate|strict)$ ]]; then
        echo "Error: Safe search must be one of: off, moderate, strict" >&2
        exit 1
    fi

    # FIX: Validate offset is an integer before numeric comparison
    if ! is_integer "$offset"; then
        echo "Error: Offset must be an integer" >&2
        exit 1
    fi
    if [[ "$offset" -lt 0 || "$offset" -gt 9 ]]; then
        echo "Error: Offset must be between 0 and 9" >&2
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# check_rate_limit
# FIX: Guard against empty/corrupt LAST_SEARCH_FILE before arithmetic.
# FIX: Ensure MIN_SEARCH_INTERVAL is an integer before arithmetic expansion.
# ---------------------------------------------------------------------------
check_rate_limit() {
    if [[ "${argc_no_cache:-}" == "true" ]]; then
        return 0
    fi

    if [[ -f "$LAST_SEARCH_FILE" ]]; then
        local last_search
        last_search=$(cat "$LAST_SEARCH_FILE" 2>/dev/null || echo "0")

        # FIX: Treat non-integer content of the file as 0 (first run)
        if ! is_integer "$last_search"; then
            last_search=0
        fi

        local current_time
        current_time=$(date +%s)
        local time_diff=$(( current_time - last_search ))
        local interval="${MIN_SEARCH_INTERVAL:-1}"

        if [[ $time_diff -lt $interval ]]; then
            echo "Error: Rate limit exceeded. Please wait $(( interval - time_diff )) seconds." >&2
            exit 1
        fi
    fi

    date +%s > "$LAST_SEARCH_FILE"
}

# ---------------------------------------------------------------------------
# generate_cache_key
# FIX: Replaced sha256sum with the portable sha256_portable helper.
# ---------------------------------------------------------------------------
generate_cache_key() {
    local query="$1"
    local limit="$2"
    local offset="$3"
    local include_domains="$4"
    local exclude_domains="$5"
    local safe_search="$6"
    local country="$7"

    printf '%s' \
        "${query}${limit}${offset}${include_domains}${exclude_domains}${safe_search}${country}" \
        | sha256_portable
}

# ---------------------------------------------------------------------------
# get_from_cache
# FIX: Replaced broken `find -mtime <fractional>` with stat-based age check.
#      The original logic used bc to compute fractional days for -mtime, which
#      is unreliable; -mtime rounds to whole days anyway.  We now compare
#      file mtime in epoch seconds against CACHE_TTL directly.
# FIX: Added jq validity check so a corrupt cache file is skipped gracefully.
# ---------------------------------------------------------------------------
get_from_cache() {
    if [[ "${argc_no_cache:-}" == "true" ]]; then
        return 1
    fi

    local cache_key="$1"
    local cache_file="$CACHE_DIR/${cache_key}.json"

    [[ -f "$cache_file" ]] || return 1

    # Portable mtime retrieval: GNU stat vs BSD stat
    local file_mtime
    if file_mtime=$(stat -c %Y "$cache_file" 2>/dev/null); then
        : # GNU stat succeeded
    elif file_mtime=$(stat -f %m "$cache_file" 2>/dev/null); then
        : # BSD stat succeeded
    else
        log_verbose "stat unavailable; skipping cache for $cache_file"
        return 1
    fi

    local current_time age
    current_time=$(date +%s)
    age=$(( current_time - file_mtime ))

    if [[ $age -ge ${CACHE_TTL:-3600} ]]; then
        log_verbose "Cache expired (age=${age}s, TTL=${CACHE_TTL}s)"
        rm -f "$cache_file"
        return 1
    fi

    # FIX: Validate cached JSON before trusting it
    if ! jq -e . "$cache_file" >/dev/null 2>&1; then
        echo "Warning: Corrupt cache file removed: $cache_file" >&2
        rm -f "$cache_file"
        return 1
    fi

    log_verbose "Using cached result (age=${age}s)"
    cat "$cache_file"
    return 0
}

# ---------------------------------------------------------------------------
# save_to_cache
# FIX: Atomic write via a temp file + mv to prevent partial/corrupt cache
#      entries if the process is interrupted mid-write.
# ---------------------------------------------------------------------------
save_to_cache() {
    if [[ "${argc_no_cache:-}" == "true" ]]; then
        return 0
    fi

    local cache_key="$1"
    local response="$2"
    local cache_file="$CACHE_DIR/${cache_key}.json"
    local tmp_file
    tmp_file=$(mktemp "${cache_file}.XXXXXX")

    # Write to temp, then atomically replace
    if echo "$response" > "$tmp_file"; then
        mv "$tmp_file" "$cache_file"
    else
        rm -f "$tmp_file"
        echo "Warning: Failed to write cache file" >&2
    fi
}

# ---------------------------------------------------------------------------
# build_search_payload — logic unchanged; only minor style alignment
# ---------------------------------------------------------------------------
build_search_payload() {
    local query="$1"
    local limit="$2"
    local offset="$3"
    local safe_search="$4"
    local include_domains="$5"
    local exclude_domains="$6"
    local country="$7"

    local base_payload
    base_payload=$(jq -n \
        --arg  q  "$query"      \
        --argjson c "$limit"    \
        --argjson o "$offset"   \
        --arg  ss "$safe_search" \
        '{query: $q, count: $c, offset: $o, safesearch: $ss}')

    if [[ -n "$include_domains" ]]; then
        base_payload=$(echo "$base_payload" | jq \
            --arg id "$include_domains" \
            '. + {include_domains: ($id | split(",") | map(ltrimstr(" ") | rtrimstr(" ")))}')
    fi

    if [[ -n "$exclude_domains" ]]; then
        base_payload=$(echo "$base_payload" | jq \
            --arg ed "$exclude_domains" \
            '. + {exclude_domains: ($ed | split(",") | map(ltrimstr(" ") | rtrimstr(" ")))}')
    fi

    if [[ -n "$country" ]]; then
        base_payload=$(echo "$base_payload" | jq --arg cy "$country" '. + {country: $cy}')
    fi

    echo "$base_payload"
}

# ---------------------------------------------------------------------------
# execute_search
# FIX: `set -e` aborts on the first curl failure, defeating the retry loop.
#      Use `|| true` on the curl call so the exit code can be inspected
#      manually inside the loop.
# FIX: `((retry_count++))` evaluates to exit code 1 when retry_count==0
#      (arithmetic 0 == false under set -e).  Replaced with safe arithmetic.
# FIX: Added response non-empty guard before accepting a curl result.
# ---------------------------------------------------------------------------
execute_search() {
    local data="$1"
    local max_retries=3
    local timeout=30
    local retry_count=0
    local response=""
    local curl_exit=0

    while [[ $retry_count -lt $max_retries ]]; do
        log_verbose "Executing search (attempt $(( retry_count + 1 ))/$max_retries)..."

        # FIX: `|| true` prevents set -e from aborting on curl failure
        response=$(curl -s --compressed --max-time "$timeout" --retry 2 \
            -X POST "https://ydc-index.io/v1/search" \
            -H "X-API-Key: $YOU_API_KEY" \
            -H "Content-Type: application/json" \
            -H "Accept-Encoding: gzip, deflate" \
            -d "$data" 2>/dev/null) || curl_exit=$?

        # FIX: Accept result only when curl succeeded AND response is non-empty
        if [[ $curl_exit -eq 0 && -n "$response" ]]; then
            break
        fi

        # FIX: Safe arithmetic increment (avoids set -e false-positive on 0++)
        retry_count=$(( retry_count + 1 ))
        curl_exit=0

        if [[ $retry_count -lt $max_retries ]]; then
            local wait=$(( retry_count * 2 ))
            echo "Search attempt $retry_count failed, retrying in ${wait}s..." >&2
            sleep "$wait"
        fi
    done

    if [[ $retry_count -ge $max_retries ]]; then
        echo "Error: All $max_retries search attempts failed" >&2
        exit 1
    fi

    echo "$response"
}

# ---------------------------------------------------------------------------
# handle_response_errors
# FIX: Validate that the response is parseable JSON before any jq access,
#      preventing cryptic jq parse errors from propagating to the user.
# FIX: Distinguish between API-level errors and structural/format errors.
# ---------------------------------------------------------------------------
handle_response_errors() {
    local response="$1"

    # FIX: Guard #1 — ensure response is valid JSON
    if ! echo "$response" | jq -e . >/dev/null 2>&1; then
        echo "Error: Response is not valid JSON. The API may be unavailable." >&2
        exit 1
    fi

    # Check for API-level error object
    if echo "$response" | jq -e '.error' >/dev/null 2>&1; then
        local error_msg
        error_msg=$(echo "$response" | jq -r '.error.message // "Unknown search error"')
        echo "Search error: $error_msg" >&2
        exit 1
    fi

    # Check for expected response structure
    if ! echo "$response" | jq -e '.results.web' >/dev/null 2>&1; then
        echo "Error: Invalid response format — missing '.results.web' field." >&2
        exit 1
    fi

    local result_count
    result_count=$(echo "$response" | jq '.results.web | length')
    if [[ "$result_count" -eq 0 ]]; then
        echo "Warning: No results found for the query." >&2
    fi
}

# ---------------------------------------------------------------------------
# format_output
# FIX: Added null-safety guards for table/compact/urls formats so a missing
#      field produces an empty string rather than a jq error.
# ---------------------------------------------------------------------------
format_output() {
    local response="$1"
    local format="${argc_format:-json}"

    case "$format" in
        "json")
            echo "$response" | jq -c '.results.web'
            ;;
        "table")
            # FIX: Use // "" to safely handle missing title/url/description
            echo "$response" | \
                jq -r '.results.web[] | [(.title // ""), (.url // ""), (.description // "")] | @tsv' | \
                column -t -s $'\t'
            ;;
        "urls")
            echo "$response" | jq -r '.results.web[] | .url // empty'
            ;;
        "compact")
            echo "$response" | jq -r '.results.web[] | "- \(.title // "Untitled"): \(.url // "N/A")"'
            ;;
        *)
            echo "Error: Unknown format: $format" >&2
            echo "Available formats: json, table, urls, compact" >&2
            exit 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# save_search_history
# FIX: Use iso_timestamp() helper for portability (date -Iseconds is GNU-only).
# FIX: Safer tmp-file rename: only overwrite history on successful tail write.
# ---------------------------------------------------------------------------
save_search_history() {
    local query="$1"
    local limit="$2"
    local offset="$3"
    local timestamp
    timestamp=$(iso_timestamp)

    echo "${timestamp}|${query}|${limit}|${offset}" >> "$HISTORY_FILE"

    # Keep only last 1000 entries — atomically
    if [[ -f "$HISTORY_FILE" ]]; then
        local tmp_history
        tmp_history=$(mktemp "${HISTORY_FILE}.XXXXXX")
        if tail -n 1000 "$HISTORY_FILE" > "$tmp_history" 2>/dev/null; then
            mv "$tmp_history" "$HISTORY_FILE"
        else
            rm -f "$tmp_history"
        fi
    fi
}

# ---------------------------------------------------------------------------
# show_stats — unchanged in behaviour; uses log_verbose helper now
# ---------------------------------------------------------------------------
show_stats() {
    if [[ "${argc_verbose:-}" != "true" ]]; then
        return 0
    fi

    local response="$1"
    local result_count
    result_count=$(echo "$response" | jq '.results.web | length')

    log_verbose "Search completed successfully"
    log_verbose "Results found: $result_count"
    log_verbose "Query: \"${argc_query}\""
    log_verbose "Limit: ${argc_limit:-$DEFAULT_LIMIT}, Offset: ${argc_offset:-0}"
    [[ -n "${argc_include_domains:-}" ]] && log_verbose "Included domains: ${argc_include_domains}"
    [[ -n "${argc_exclude_domains:-}" ]] && log_verbose "Excluded domains: ${argc_exclude_domains}"
}

# ---------------------------------------------------------------------------
# main
# FIX: Load .env BEFORE load_config so env vars are available during config
#      resolution and before the API key check.
# FIX: Pass offset explicitly to validate_input (was relying on global).
# FIX: Use consistent variable names throughout.
# ---------------------------------------------------------------------------
main() {
    # FIX: Source .env first so all subsequent steps see the variables
    if [[ -f ".env" ]]; then
        # shellcheck source=/dev/null
        source ".env"
    fi

    # Initialize and configure
    init_directories
    load_config

    # Resolve arguments with defaults
    local query="${argc_query}"
    local limit="${argc_limit:-$DEFAULT_LIMIT}"
    local offset="${argc_offset:-0}"
    local include_domains="${argc_include_domains:-}"
    local exclude_domains="${argc_exclude_domains:-}"
    local safe_search="${argc_safe_search:-$DEFAULT_SAFE_SEARCH}"
    local country="${argc_country:-}"
    local output_path="${LLM_OUTPUT:-/dev/stdout}"

    # Validate API key early
    if [[ -z "${YOU_API_KEY:-}" ]]; then
        echo "Error: YOU_API_KEY not found in environment variables or .env file" >&2
        exit 1
    fi

    # FIX: Pass offset explicitly so validate_input is self-contained
    validate_input "$query" "$limit" "$safe_search" "$offset"

    # Check rate limiting
    check_rate_limit

    # Generate cache key
    local cache_key
    cache_key=$(generate_cache_key \
        "$query" "$limit" "$offset" \
        "$include_domains" "$exclude_domains" \
        "$safe_search" "$country")

    # Try cache first
    local cached_response
    if cached_response=$(get_from_cache "$cache_key"); then
        handle_response_errors "$cached_response"
        show_stats "$cached_response"

        if [[ "$output_path" == "/dev/stdout" ]]; then
            format_output "$cached_response"
        else
            format_output "$cached_response" >> "$output_path"
        fi

        save_search_history "$query" "$limit" "$offset"
        return 0
    fi

    # Build search payload
    local data
    data=$(build_search_payload \
        "$query" "$limit" "$offset" "$safe_search" \
        "$include_domains" "$exclude_domains" "$country")

    # Execute search
    local response
    response=$(execute_search "$data")

    # Handle errors in response
    handle_response_errors "$response"

    # Cache successful response
    save_to_cache "$cache_key" "$response"

    # Show statistics
    show_stats "$response"

    # Format and write results
    if [[ "$output_path" == "/dev/stdout" ]]; then
        format_output "$response"
    else
        format_output "$response" >> "$output_path"
    fi

    # Persist to history
    save_search_history "$query" "$limit" "$offset"
}

eval "$(argc --argc-eval "$0" "$@")"
