#!/usr/bin/env bash
set -euo pipefail

# @describe Fetch content from a URL using curl.
# @option --url!                      The URL to fetch.
# @option --timeout=30 <INT>          Request timeout in seconds.
# @option --max-time=60 <INT>         Maximum transfer time in seconds.
# @option --connect-timeout=10 <INT>  Connection timeout in seconds.
# @option --user-agent=curl_tool/1.0  Custom User-Agent header.
# @option --output=                   Output file path (default: LLM_OUTPUT or stdout).
# @option --method=GET                HTTP method (GET, POST, PUT, DELETE, PATCH).
# @option --data=                     Request body data for POST/PUT.
# @option --headers=                  Comma-separated headers (e.g., "Key: Val,Key2: Val2").
# @flag --follow-redirects            Follow HTTP redirects.
# @option --max-redirects=5 <INT>     Maximum number of redirects.
# @flag --verify-ssl                  Verify SSL certificates.
# @flag --compressed                  Request compressed response.
# @flag --silent                      Silent mode.
# @flag --verbose                     Verbose output.
# @flag --include                     Include HTTP headers in output.
# @option --retry=3 <INT>             Number of retry attempts.
# @option --retry-delay=1 <INT>       Delay between retries in seconds.
# @option --limit-rate=               Limit download rate (e.g., 100k, 1m).

# @env LLM_OUTPUT=/dev/stdout The output path.

main() {
    local output_target="${argc_output:-${LLM_OUTPUT:-/dev/stdout}}"
    
    curl_args=(
        --location --fail --show-error
        --max-time "${argc_max_time:-60}"
        --connect-timeout "${argc_connect_timeout:-10}"
        --user-agent "${argc_user_agent:-curl_tool/1.0}"
        --retry "${argc_retry:-3}"
        --retry-delay "${argc_retry_delay:-1}"
    )

    [[ "${argc_follow_redirects:-false}" == "true" ]] && curl_args+=(--max-redirs "${argc_max_redirects:-5}") || curl_args+=(--no-location)
    [[ "${argc_verify_ssl:-false}" != "true" ]] && curl_args+=(--insecure)
    [[ "${argc_compressed:-false}" == "true" ]] && curl_args+=(--compressed)
    [[ "${argc_silent:-false}" == "true" ]] && curl_args+=(--silent)
    [[ "${argc_verbose:-false}" == "true" ]] && curl_args+=(--verbose)
    [[ "${argc_include:-false}" == "true" ]] && curl_args+=(--include)
    [[ -n "${argc_limit_rate:-}" ]] && curl_args+=(--limit-rate "${argc_limit_rate}")
    [[ -n "${argc_method:-}" ]] && curl_args+=(--request "${argc_method}")
    [[ -n "${argc_data:-}" ]] && curl_args+=(--data "${argc_data}")

    if [[ -n "${argc_headers:-}" ]]; then
        IFS=',' read -ra headers_array <<< "$argc_headers"
        for header in "${headers_array[@]}"; do curl_args+=(--header "$header"); done
    fi

    curl "${curl_args[@]}" "$argc_url" -o "$output_target"
}

eval "$(argc --argc-eval "$0" "$@")"
