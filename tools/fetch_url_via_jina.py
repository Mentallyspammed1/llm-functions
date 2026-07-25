#!/usr/bin/env bash
set -euo pipefail

# @describe Extract content from a URL using Jina AI Reader.
# @option --url!                     The target URL to scrape.
# @option --timeout=30 <INT>         Request timeout in seconds.
# @option --format=text[text|json|markdown] Output format.
# @option --selector                 CSS selector to extract specific elements.
# @option --engine                   Parser/Browser engine (e.g. readerlm-v2).
# @flag   --no-cache                 Bypass Jina AI cache and fetch fresh content.
# @flag   --with-links               Append summary section of links/buttons.
# @flag   --with-images              Append summary section of image descriptions.

# @env JINA_API_KEY                  Optional API key for higher rate limits.
# @env LLM_OUTPUT=/dev/stdout        Output path for LLM integration.

main() {
    # 1. Dependency check for external binary
    if ! command -v curl >/dev/null 2>&1; then
        echo "Error: 'curl' is required but not installed or found in PATH." >&2
        exit 127
    fi

    local output_target="${LLM_OUTPUT:-/dev/stdout}"
    local format="${argc_format:-text}"
    local raw_url="${argc_url}"

    # 2. Input URL whitespace and control-character sanitization
    raw_url="$(echo "$raw_url" | tr -d '\r\n\t ')"

    # 3. Timeout argument validation
    local timeout_val="${argc_timeout:-30}"
    if [[ ! "$timeout_val" =~ ^[0-9]+$ ]] || [ "$timeout_val" -le 0 ]; then
        echo "Error: Invalid timeout value '${timeout_val}'. Must be a positive integer." >&2
        exit 1
    fi

    # 4. Auto-prepend scheme if missing
    if [[ ! "$raw_url" =~ ^https?:// ]]; then
        raw_url="https://${raw_url}"
    fi

    local target_url="https://r.jina.ai/${raw_url}"

    # 5. Robust Accept header mapping
    local accept_header="text/markdown"
    case "${format}" in
        json)
            accept_header="application/json"
            ;;
        text|markdown)
            accept_header="text/markdown"
            ;;
        *)
            accept_header="text/markdown"
            ;;
    esac

    # Calculate reasonable connection timeout
    local conn_timeout=10
    if [ "$timeout_val" -lt "$conn_timeout" ]; then
        conn_timeout="$timeout_val"
    fi

    # 6. Optimized curl arguments
    local curl_args=(
        --silent
        --show-error
        --location
        --max-redirs 5
        --connect-timeout "$conn_timeout"
        --max-time "$timeout_val"
        -H "Accept: ${accept_header}"
        -H "X-Timeout: ${timeout_val}"
        -A "aichat-jina-reader/2.2.0"
    )

    # Attach Authorization header if key exists
    if [[ -n "${JINA_API_KEY:-}" ]]; then
        curl_args+=(-H "Authorization: Bearer ${JINA_API_KEY}")
    fi

    # Optional Jina AI Feature Headers
    if [[ -n "${argc_selector:-}" ]]; then
        curl_args+=(-H "X-Target-Selector: ${argc_selector}")
    fi

    if [[ -n "${argc_engine:-}" ]]; then
        curl_args+=(-H "X-Engine: ${argc_engine}")
    fi

    if [[ "${argc_no_cache:-0}" -eq 1 ]]; then
        curl_args+=(-H "X-No-Cache: true")
    fi

    if [[ "${argc_with_links:-0}" -eq 1 ]]; then
        curl_args+=(-H "X-With-Links-Summary: true")
    fi

    if [[ "${argc_with_images:-0}" -eq 1 ]]; then
        curl_args+=(-H "X-With-Images-Summary: true")
    fi

    # 7. Secure temporary files with strict umask
    local old_umask
    old_umask="$(umask)"
    umask 077
    local tmp_file err_file
    tmp_file="$(mktemp)"
    err_file="$(mktemp)"
    umask "$old_umask"

    # 8. Comprehensive signal trapping
    trap 'rm -f "$tmp_file" "$err_file"' EXIT INT TERM HUP

    # 9. Capture HTTP status code and curl stderr
    local http_code
    http_code="$(curl "${curl_args[@]}" -w "%{http_code}" "$target_url" -o "$tmp_file" 2>"$err_file" || echo "000")"

    # 10. HTTP status validation & diagnostic error handling
    if [[ "$http_code" -ne 200 ]]; then
        echo "Error: Failed to fetch content from '${raw_url}' (HTTP Status: ${http_code})." >&2
        if [[ -s "$err_file" ]]; then
            cat "$err_file" >&2
        fi
        exit 1
    fi

    # 11. Stream redirection output routing
    if [[ "$output_target" == "/dev/stdout" || "$output_target" == "-" || "$output_target" == "/dev/fd/1" ]]; then
        cat "$tmp_file"
    else
        local target_dir
        target_dir="$(dirname "$output_target")"
        if [[ ! -d "$target_dir" ]]; then
            mkdir -p "$target_dir"
        fi
        cat "$tmp_file" > "$output_target"
    fi
}

eval "$(argc --argc-eval "$0" "$@")"
