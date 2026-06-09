#!/usr/bin/env bash
# tools/web_search_google.sh
# shellcheck disable=SC2034,SC2154

# NOTE: Credentials are hardcoded as requested.
# WARNING: Rotate these keys immediately if repository becomes public.

set -uo pipefail

# @describe Perform advanced web search using Google Custom Search API with multiple
# filtering options including date range, site restriction, file type, language,
# and full image search with downloading, parsing, and base64 encoding for vision models.
# Use this when you need current information, images, or feel a search could provide
# a better answer. Returns structured results with titles, links, snippets, metadata,
# and optional downloaded images. Safe search is disabled.

# ── Web Search Options ────────────────────────────────────────────────────────
# @option --query!                          The search query string.
# @option --num-results=10                  Number of results to return (1-100).
# @option --start-index=1                   Pagination start index (1-91).
# @option --date-filter                     Restrict results by age: d[N], w[N], m[N], y[N].
# @option --site-filter                     Restrict results to a specific domain.
# @option --site-exclude                    Exclude results from a specific domain.
# @option --file-type                       Filter by file type: pdf, doc, docx, etc.
# @option --lang=en                         Language for search results (ISO 639-1).
# @option --ui-lang=en                      Language for UI (ISO 639-1).
# @option --country                         Restrict results to country (ISO 3166-1 alpha-2).
# @option --sort-by                         Sort order: relevance or date.
# @option --output-format=detailed          Output format: detailed, compact, json, parsed.
# @option --timeout=15                      Request timeout in seconds (1-60).
# @option --max-retries=2                   Maximum retry attempts (0-5).
# @option --cache-ttl=300                   Cache TTL in seconds (0=disabled, max=3600).
# @option --pages=1                         Number of result pages to fetch (1-10).
# @option --rate-limit-delay=1              Delay between API requests (0-10).
# @option --export-format                   Export format: json, csv, md, html.
# @option --freshness-weight=5              Weight recent results (0-10).
# @option --queries                         Multiple queries separated by pipe.
# @option --proxy                           Proxy URL (e.g., http://proxy:8080).
# @option --header                          Custom header (key:value).
# @option --download-limit=0                Download speed limit (KB/s).
# @option --exclude-terms                   Exclude terms from results.
# @option --safe                            Parental protection: active, off.
# @option --related-site                    Return pages related to a URL.
# @option --link-site                       Return pages linking to a URL.
# @flag   --exact-terms                     Treat query as exact phrase match.
# @flag   --no-duplicates                   Filter out duplicate domains.
# @flag   --no-cache                        Bypass response cache.
# @flag   --debug                           Enable verbose debug output.
# @flag   --show-cache-stats                Show cache statistics.
# @flag   --show-progress                   Show download progress bars.
# @flag   --pretty-print                    Format JSON response.

# ── Image Search Options ──────────────────────────────────────────────────────
# @option --search-type=web                 Search type: web or image.
# @option --image-size                      Filter images by size.
# @option --image-type                      Filter by image type.
# @option --image-color-type                Filter by color type.
# @option --image-dominant-color            Filter by dominant color.
# @option --image-rights                    Filter by usage rights.
# @option --image-format                    Preferred download format filter.
# @option --download-dir=./search_images    Directory to save downloaded images.
# @option --download-max=5                  Maximum images to download (1-100).
# @option --download-timeout=30             Per-image download timeout (5-120).
# @option --download-workers=3              Concurrent download workers (1-10).
# @option --image-min-width=0               Minimum image width in pixels.
# @option --image-min-height=0              Minimum image height in pixels.
# @flag   --download-images                 Download found images.
# @flag   --encode-base64                   Base64 encode images for vision models.
# @flag   --generate-manifest               Generate JSON manifest of downloads.
# @flag   --skip-existing                   Skip existing images.
# @flag   --verify-images                   Verify downloaded files.
# @flag   --create-thumbnails               Create 200x200 thumbnails.
# @flag   --show-image-metadata             Show detailed image metadata.
# @option --json-keys                      Comma-separated keys to extract (e.g. title,link,snippet).

# Hardcoded Google API credentials
# WARNING: Rotate immediately if repository becomes public
GOOGLE_API_KEY="AIzaSyBnMVWNJUwlah6vQSvqN-e6ZhOWS1ejgnI"
GOOGLE_CSE_ID="40de0ade1bbd147da"
YOU_API_KEY="ydc-sk-3be25b63a354f86f-cZsqdcYZe3xHo2qxVUZxEmTI1wAzlfG8-23e9d3b8"

# Input validation for --header and --proxy
if [[ -n "${argc_header:-}" ]]; then
    if ! [[ "${argc_header}" =~ ^[A-Za-z0-9_-]+:[[:space:]]*[^[:space:]]+$ ]]; then
        echo "[ERROR] Invalid --header format. Expected 'Key: value'." >&2
        exit 1
    fi
fi

if [[ -n "${argc_proxy:-}" ]]; then
    if ! [[ "${argc_proxy}" =~ ^https?:// ]]; then
        echo "[ERROR] Invalid --proxy format. Expected 'http://host:port'." >&2
        exit 1
    fi
fi

# @env LLM_OUTPUT=/dev/stdout  The output path

# ── Internal constants ────────────────────────────────────────────────────────
readonly TOOL_VERSION="4.3.0"
readonly TOOL_NAME="llm-functions-web-search"
readonly API_BASE_URL="https://www.googleapis.com/customsearch/v1"
readonly CACHE_BASE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/llm_web_search"
readonly MANIFEST_FILE="search_manifest.json"
readonly QUOTA_FILE="${CACHE_BASE_DIR}/quota.json"
readonly HISTORY_FILE="${CACHE_BASE_DIR}/search_history.json"
readonly ANALYTICS_FILE="${CACHE_BASE_DIR}/analytics.json"
readonly LOG_FILE="${CACHE_BASE_DIR}/search.log"
readonly MAX_DAILY_REQUESTS=100

# Per-minute rate limiting
readonly MAX_REQUESTS_PER_MINUTE=10
readonly RATE_LIMIT_FILE="${CACHE_BASE_DIR}/rate_limit.json"

# Domain blacklist for spam/ad filtering
readonly DOMAIN_BLACKLIST="ads.google.com|googleadservices.com|doubleclick.net|googlesyndication.com|google-analytics.com|facebook.com/tr|bing.com/ads|amazon.com/ads-|spammy.site|clickbait.io|scamalert.net"

# ── ANSI color codes (stderr only) ──────────────────────────────────────────
readonly _COL_RED='\033[0;31m'
readonly _COL_YEL='\033[0;33m'
readonly _COL_GRN='\033[0;32m'
readonly _COL_CYN='\033[0;36m'
readonly _COL_RST='\033[0m'

# Global temp dir
_TMPDIR=""

# ════════════════════════════════════════════════════════════════════════════════
# CLEANUP & SIGNAL HANDLING
# ════════════════════════════════════════════════════════════════════════════════

_cleanup() {
    local exit_code=$?
    local child_pids
    child_pids=$(jobs -p 2>/dev/null || true)
    [[ -n "$child_pids" ]] && kill $child_pids 2>/dev/null || true
    wait $child_pids 2>/dev/null || true
    [[ -n "$_TMPDIR" ]] && [[ -d "$_TMPDIR" ]] && rm -rf "$_TMPDIR"
    exit "$exit_code"
}

trap '_cleanup' EXIT INT TERM HUP

# ════════════════════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ════════════════════════════════════════════════════════════════════════════════

_log() {
    local level="$1" msg="$2"
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || printf "unknown")
    printf '{"timestamp":"%s","level":"%s","message":"%s"}\n' "$ts" "$level" "$msg" >> "$LOG_FILE"
}

_debug() { [[ "${argc_debug:-0}" == "1" ]] && _log "DEBUG" "$*" && printf "${_COL_CYN}[DEBUG]${_COL_RST} %s\n" "$*" >&2; }
_info()  { _log "INFO" "$*" && printf "${_COL_GRN}[INFO]${_COL_RST}  %s\n" "$*" >&2; }
_warn()  { _log "WARN" "$*" && printf "${_COL_YEL}[WARN]${_COL_RST}  %s\n" "$*" >&2; }
_error() { _log "ERROR" "$*" && printf "${_COL_RED}[ERROR]${_COL_RST} %s\n" "$*" >&2; }

_die() {
    _error "$@"
    printf "Error: %s\n" "$*" >> "${LLM_OUTPUT:-/dev/stdout}"
    exit 1
}

_get_tmpdir() {
    [[ -z "$_TMPDIR" ]] && _TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/llm_web_search_XXXXXX") || _die "Failed to create temp dir."
    printf "%s" "$_TMPDIR"
}

_urlencode() {
    local string="$1"
    if command -v python3 &>/dev/null; then
        python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1],safe=''))" "$string" 2>/dev/null && return 0
    fi
    if command -v jq &>/dev/null; then
        printf "%s" "$string" | jq -sRr @uri 2>/dev/null && return 0
    fi
    printf "%s" "$string" | sed \
        -e 's/%/%25/g' -e 's/ /%20/g' -e 's|!|%21|g' -e 's|"|%22|g' -e 's|#|%23|g' \
        -e 's|\$|%24|g' -e 's|&|%26|g' -e "s|'|%27|g" -e 's|(|%28|g' -e 's|)|%29|g' \
        -e 's|\*|%2A|g' -e 's|+|%2B|g' -e 's|,|%2C|g' -e 's|/|%2F|g' -e 's|:|%3A|g' \
        -e 's|;|%3B|g' -e 's|=|%3D|g' -e 's|?|%3F|g' -e 's|@|%40|g' -e 's|\[|%5B|g' -e 's|\]|%5D|g'
}

_validate_int_range() {
    local value="$1" min="$2" max="$3" name="$4"
    if ! [[ "$value" =~ ^[0-9]+$ ]] || (( value < min )) || (( value > max )); then
        _die "Invalid $name: '$value'. Must be between $min and $max."
    fi
}

_has_cmd() { command -v "$1" &>/dev/null; }

_sanitize_filename() {
    local input="$1" max_len="${2:-80}"
    printf "%s" "$input" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9._-]/_/g; s/__*/_/g; s/^_//; s/_$//' | cut -c1-"$max_len"
}

_url_extension() {
    local url="$1" path ext
    path="${url%%\?*}" path="${path%%#*}" ext="${path##*.}"
    if [[ "${#ext}" -ge 1 ]] && [[ "${#ext}" -le 5 ]] && [[ "$ext" =~ ^[a-zA-Z0-9]+$ ]]; then
        printf "%s" "${ext,,}"
    else
        printf "jpg"
    fi
}

_human_size() {
    local bytes="${1:-0}"
    if (( bytes < 1024 )); then
        printf "%d B" "$bytes"
    elif (( bytes < 1048576 )); then
        printf "%d KB" $(( bytes / 1024 ))
    elif (( bytes < 1073741824 )); then
        printf "%d MB" $(( bytes / 1048576 ))
    else
        printf "%d GB" $(( bytes / 1073741824 ))
    fi
}

_hash_string() {
    local str="$1"
    if _has_cmd sha256sum; then
        printf "%s" "$str" | sha256sum | cut -c1-16
    elif _has_cmd shasum; then
        printf "%s" "$str" | shasum -a 256 | cut -c1-16
    elif _has_cmd md5sum; then
        printf "%s" "$str" | md5sum | cut -c1-16
    elif _has_cmd md5; then
        printf "%s" "$str" | md5 | cut -c1-16
    else
        local h=5381 i
        for (( i=0; i<${#str}; i++ )); do
            h=$(( (h * 33) ^ $(printf "%d" "'${str:$i:1}") ))
        done
        printf "%016x" $(( h & 0xFFFFFFFFFFFFFFFF ))
    fi
}

_now() { date +%s 2>/dev/null || printf "0"; }

_repeat_char() {
    local char="$1" count="$2"
    printf "%${count}s" "" | tr ' ' "$char"
}

_is_blacklisted() {
    local domain="$1"
    [[ "$domain" =~ ^($DOMAIN_BLACKLIST)$ ]]
}

# ════════════════════════════════════════════════════════════════════════════════
# ⚡ RATE LIMITING (NEW - Per-minute)
# ════════════════════════════════════════════════════════════════════════════════

_rate_limit_check() {
    mkdir -p "$CACHE_BASE_DIR"
    local now minute_key current_count data
    now=$(_now)
    minute_key=$(( now / 60 ))

    data='{}'
    [[ -f "$RATE_LIMIT_FILE" ]] && data=$(jq '.' "$RATE_LIMIT_FILE" 2>/dev/null || printf '{}')

    current_count=$(printf "%s" "$data" | jq -r --arg k "$minute_key" '.[$k] // 0')

    if (( current_count >= MAX_REQUESTS_PER_MINUTE )); then
        _die "Rate limit exceeded: ${MAX_REQUESTS_PER_MINUTE} requests/minute."
    fi

    data=$(printf "%s" "$data" | jq --arg k "$minute_key" --argjson c "$((current_count + 1))" '.[$k] = $c')
    printf "%s" "$data" > "$RATE_LIMIT_FILE" 2>/dev/null || true
}

# ════════════════════════════════════════════════════════════════════════════════
# QUOTA TRACKING
# ════════════════════════════════════════════════════════════════════════════════

_quota_check() {
    mkdir -p "$CACHE_BASE_DIR"
    local today quota_date quota_count
    today=$(date +%Y-%m-%d 2>/dev/null || printf "unknown")

    if [[ -f "$QUOTA_FILE" ]]; then
        quota_date=$(jq -r '.date // "none"' "$QUOTA_FILE" 2>/dev/null || printf "none")
        quota_count=$(jq -r '.count // 0' "$QUOTA_FILE" 2>/dev/null || printf "0")
        [[ "$quota_date" != "$today" ]] && quota_count=0
    fi

    if (( quota_count >= MAX_DAILY_REQUESTS )); then
        _die "Daily API quota exhausted ($quota_count/$MAX_DAILY_REQUESTS)."
    elif (( quota_count >= (MAX_DAILY_REQUESTS * 80 / 100) )); then
        _warn "API quota at ${quota_count}/${MAX_DAILY_REQUESTS} (80%+ used)."
    fi
    _debug "API quota: ${quota_count}/${MAX_DAILY_REQUESTS}"
}

_quota_increment() {
    mkdir -p "$CACHE_BASE_DIR"
    local today quota_date quota_count
    today=$(date +%Y-%m-%d 2>/dev/null || printf "unknown")

    if [[ -f "$QUOTA_FILE" ]]; then
        quota_date=$(jq -r '.date // "none"' "$QUOTA_FILE" 2>/dev/null || printf "none")
        quota_count=$(jq -r '.count // 0' "$QUOTA_FILE" 2>/dev/null || printf "0")
        [[ "$quota_date" != "$today" ]] && quota_count=0
    fi

    quota_count=$(( quota_count + 1 ))
    jq -n --arg date "$today" --argjson count "$quota_count" \
        '{date: $date, count: $count, updated: now | todate}' > "$QUOTA_FILE" 2>/dev/null || true
    _debug "Quota: ${quota_count}/${MAX_DAILY_REQUESTS}"
}

# ════════════════════════════════════════════════════════════════════════════════
# 📦 RESPONSE CACHE (with gzip compression & size enforcement)
# ════════════════════════════════════════════════════════════════════════════════

_cache_path() {
    local params="$1"
    printf "%s/%s.json.gz" "$CACHE_BASE_DIR" "$(_hash_string "$params")"
}

_cache_read() {
    local params="$1"
    local ttl="${argc_cache_ttl:-300}"

    [[ "${argc_no_cache:-0}" == "1" ]] && return 1
    (( ttl == 0 )) && return 1

    local cache_file="$(_cache_path "$params")"
    [[ -f "$cache_file" ]] || return 1

    local cached_at now age
    cached_at=$(zcat "$cache_file" 2>/dev/null | jq -r '.__cached_at // 0' || printf "0")
    now=$(_now)
    age=$(( now - cached_at ))

    if (( age > ttl )); then
        _debug "Cache expired (age=${age}s ttl=${ttl}s)"
        rm -f "$cache_file"
        return 1
    fi

    _debug "Cache hit (age=${age}s)"
    zcat "$cache_file" 2>/dev/null | jq 'del(.__cached_at)' || return 1
}

_cache_write() {
    local params="$1" response="$2"
    local ttl="${argc_cache_ttl:-300}"

    [[ "${argc_no_cache:-0}" == "1" ]] && return 0
    (( ttl == 0 )) && return 0

    mkdir -p "$CACHE_BASE_DIR"
    local cache_file now
    cache_file="$(_cache_path "$params")"
    now=$(_now)

    printf "%s" "$response" | jq --argjson ts "$now" '. + {__cached_at: $ts}' | \
        gzip > "$cache_file" 2>/dev/null || _warn "Cache write failed: $cache_file"
    _debug "Cache written: $cache_file"
    _cache_enforce_size
}

_cache_enforce_size() {
    local max_mb=100 cur_mb
    cur_mb=$(du -sm "$CACHE_BASE_DIR" 2>/dev/null | cut -f1 || printf "0")
    if (( cur_mb > max_mb )); then
        _warn "Cache ${cur_mb}MiB > ${max_mb}MiB - pruning."
        find "$CACHE_BASE_DIR" -type f -name "*.json.gz" -printf "%T@ %p\n" 2>/dev/null | \
            sort -n | head -n 10 | while read -r _ path; do
                rm -f "$path"
                cur_mb=$(du -sm "$CACHE_BASE_DIR" 2>/dev/null | cut -f1 || printf "0")
                (( cur_mb <= max_mb )) && break
            done
    fi
}

_cache_prune() {
    [[ -d "$CACHE_BASE_DIR" ]] || return 0
    local max_age=3600 now pruned=0
    now=$(_now)

    while IFS= read -r -d '' cache_file; do
        local cached_at age
        cached_at=$(zcat "$cache_file" 2>/dev/null | jq -r '.__cached_at // 0' || printf "0")
        age=$(( now - cached_at ))
        if (( age > max_age )); then
            rm -f "$cache_file"
            (( pruned++ )) || true
        fi
    done < <(find "$CACHE_BASE_DIR" -maxdepth 1 -name "*.json.gz" \
             ! -name "$(basename "$QUOTA_FILE")" \
             ! -name "$(basename "$HISTORY_FILE")" \
             ! -name "$(basename "$ANALYTICS_FILE")" \
             -print0 2>/dev/null)

    (( pruned > 0 )) && _debug "Pruned $pruned entries."
}

_show_cache_stats() {
    [[ "${argc_show_cache_stats:-0}" == "1" ]] || return 0
    mkdir -p "$CACHE_BASE_DIR"

    local total_entries cache_size hit_count
    total_entries=$(find "$CACHE_BASE_DIR" -maxdepth 1 -name "*.json.gz" 2>/dev/null | wc -l)
    cache_size=$(du -sb "$CACHE_BASE_DIR" 2>/dev/null | cut -f1 || printf "0")
    hit_count=$(jq -r '.count // 0' "$QUOTA_FILE" 2>/dev/null || printf "0")

    _info "Cache: $total_entries entries, $(_human_size "$cache_size"), $hit_count API calls"
}

# ════════════════════════════════════════════════════════════════════════════════
# SEARCH HISTORY & ANALYTICS
# ════════════════════════════════════════════════════════════════════════════════

_add_to_history() {
    local query="$1" result_count="$2"
    mkdir -p "$CACHE_BASE_DIR"
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || printf "unknown")

    [[ -f "$HISTORY_FILE" ]] || echo "[]" > "$HISTORY_FILE"
    local new_entry=$(jq -n --arg query "$query" --argjson count "$result_count" --arg ts "$timestamp" \
        '{query: $query, result_count: $count, timestamp: $ts}')

    local updated=$(jq -s --argjson entry "$new_entry" '[($entry) + .] | .[0:100]' "$HISTORY_FILE" 2>/dev/null || printf "[%s]" "$new_entry")
    printf "%s" "$updated" > "$HISTORY_FILE" 2>/dev/null || true
}

_update_analytics() {
    local query="$1" result_count="$2"
    mkdir -p "$CACHE_BASE_DIR"
    [[ -f "$ANALYTICS_FILE" ]] || echo "{}" > "$ANALYTICS_FILE"

    local updated=$(jq --arg query "$query" --argjson count "$result_count" \
        '. as $orig | if (.queries == null) then {queries: {($query): {count: $count, last_seen: now | todate}}}
        else if (.queries[$query] == null) then (.queries += {($query): {count: $count, last_seen: now | todate}})
        else (.queries[$query].count += $count | .queries[$query].last_seen = (now | todate)) end end' \
        "$ANALYTICS_FILE" 2>/dev/null || printf '{"queries":{%s:{"count":%s}}}' "$query" "$result_count")

    printf "%s" "$updated" > "$ANALYTICS_FILE" 2>/dev/null || true
}

_get_suggestions() {
    local response="$1" suggestion
    suggestion=$(printf "%s" "$response" | jq -r '.spelling.correctedQuery // empty' 2>/dev/null || true)
    [[ -n "$suggestion" ]] && _warn "Did you mean: '$suggestion'?" && printf "%s" "$suggestion"
}

_group_by_domain() {
    local json_response="$1"
    local max_per_domain="${2:-3}"
    printf "%s" "$json_response" | jq --argjson max "$max_per_domain" \
        '.items as $all | (reduce $all[] as $item ({}; .[$item.displayLink] += [$item])) as $g |
        [$g | to_entries[] | .value[0:$max]] | flatten | .[0:20]' 2>/dev/null || printf "%s" "$json_response"
}

# ════════════════════════════════════════════════════════════════════════════════
# EXPORT FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════════

_export_to_csv() {
    local json_response="$1" query="$2"
    printf "Title,URL,Source,Snippet\n"
    printf "%s" "$json_response" | jq -r '.items[] |
        "\"\(.title // "" | gsub("\""; "\"\""))\",\"\(.link // "")\",\"\(.displayLink // "")\",
        \"\(.snippet // "" | gsub("\""; "\"\"") | gsub("\n"; " "))\""' 2>/dev/null
}

_export_to_markdown() {
    local json_response="$1" query="$2"
    printf "# Web Search Results: %s\n\n" "$query"
    printf "%s" "$json_response" | jq -r '.items[] |
        "* [\(.title // "Untitled")](\(.link // ""))",
        "  **Source:** \(.displayLink // "N/A")", "",
        "  \(.snippet // "No description")", ""' 2>/dev/null
}

_export_to_html() {
    local json_response="$1" query="$2"
    printf '<!DOCTYPE html>\n<html><head><title>Search: %s</title></head><body>\n' "$query"
    printf '<h1>Search Results: %s</h1>\n' "$query"
    printf "%s" "$json_response" | jq -r '.items[] |
        "<article><h2><a href=\"\(.link // "")\">\(.title // "Untitled")</a></h2>
        <p class=\"source\">\(.displayLink // "N/A")</p>
        <p class=\"snippet\">\(.snippet // "No description")</p></article>\n"' 2>/dev/null
    printf '</body></html>\n'
}

# ════════════════════════════════════════════════════════════════════════════════
# DEPENDENCY CHECKS
# ════════════════════════════════════════════════════════════════════════════════

_check_deps() {
    local missing=()
    for cmd in curl jq; do
        _has_cmd "$cmd" || missing+=("$cmd")
    done
    if [[ "${#missing[@]}" -gt 0 ]]; then
        _die "Missing required dependencies: ${missing[*]}. Install them and retry."
    fi

    if [[ "${argc_create_thumbnails:-0}" == "1" ]] || \
       [[ "${argc_image_min_width:-0}" != "0" ]] || \
       [[ "${argc_image_min_height:-0}" != "0" ]]; then
        _has_cmd convert  || _warn "ImageMagick 'convert' not found."
        _has_cmd identify || _warn "ImageMagick 'identify' not found."
    fi

    [[ "${argc_verify_images:-0}" == "1" ]] && ! _has_cmd file && _warn "'file' command not found."

    [[ "${argc_encode_base64:-0}" == "1" ]] && ! _has_cmd base64 && _die "'base64' is required."
}

# ════════════════════════════════════════════════════════════════════════════════
# PARAMETER VALIDATION
# ════════════════════════════════════════════════════════════════════════════════

_validate_params() {
    _validate_int_range "${argc_num_results:-10}" 1 100 "--num-results"
    _validate_int_range "${argc_start_index:-1}" 1 91 "--start-index"
    _validate_int_range "${argc_timeout:-15}" 1 60 "--timeout"
    _validate_int_range "${argc_max_retries:-2}" 0 5 "--max-retries"
    _validate_int_range "${argc_cache_ttl:-300}" 0 3600 "--cache-ttl"
    _validate_int_range "${argc_download_max:-5}" 1 10 "--download-max"
    _validate_int_range "${argc_download_timeout:-30}" 5 120 "--download-timeout"
    _validate_int_range "${argc_download_workers:-3}" 1 10 "--download-workers"
    _validate_int_range "${argc_image_min_width:-0}" 0 99999 "--image-min-width"
    _validate_int_range "${argc_image_min_height:-0}" 0 99999 "--image-min-height"
    _validate_int_range "${argc_pages:-1}" 1 10 "--pages"
    _validate_int_range "${argc_rate_limit_delay:-1}" 0 10 "--rate-limit-delay"
    _validate_int_range "${argc_freshness_weight:-5}" 0 10 "--freshness-weight"
    _validate_int_range "${argc_download_limit:-0}" 0 10000 "--download-limit"

    case "${argc_output_format:-detailed}" in
        detailed|compact|json|parsed) ;;
        *) _die "Invalid --output-format." ;;
    esac

    if [[ -n "${argc_export_format:-}" ]]; then
        case "${argc_export_format}" in
            json|csv|md|html) ;;
            *) _die "Invalid --export-format." ;;
        esac
    fi

    case "${argc_search_type:-web}" in
        web|image) ;;
        *) _die "Invalid --search-type." ;;
    esac

    if [[ -n "${argc_image_size:-}" ]]; then
        case "${argc_image_size}" in
            icon|small|medium|large|xlarge|xxlarge|huge) ;;
            *) _die "Invalid --image-size." ;;
        esac
    fi

    if [[ -n "${argc_image_type:-}" ]]; then
        case "${argc_image_type}" in
            clipart|face|lineart|stock|photo|animated) ;;
            *) _die "Invalid --image-type." ;;
        esac
    fi

    if [[ -n "${argc_image_color_type:-}" ]]; then
        case "${argc_image_color_type}" in
            color|gray|mono|trans) ;;
            *) _die "Invalid --image-color-type." ;;
        esac
    fi

    if [[ -n "${argc_date_filter:-}" ]]; then
        [[ "${argc_date_filter}" =~ ^[dwmy][0-9]*$ ]] || _die "Invalid --date-filter."
    fi

    if [[ -n "${argc_lang:-}" ]]; then
        [[ "${argc_lang}" =~ ^[a-zA-Z]{2}$ ]] || _die "Invalid --lang."
    fi

    if [[ -n "${argc_ui_lang:-}" ]]; then
        [[ "${argc_ui_lang}" =~ ^[a-zA-Z]{2}$ ]] || _die "Invalid --ui-lang."
    fi

    if [[ -n "${argc_country:-}" ]]; then
        [[ "${argc_country}" =~ ^[a-zA-Z]{2}$ ]] || _die "Invalid --country."
    fi

    _debug "Validation passed."
}

# ════════════════════════════════════════════════════════════════════════════════
# BUILD API QUERY PARAMETERS
# ════════════════════════════════════════════════════════════════════════════════

_build_params() {
    local query="$1"
    local encoded_query="$(_urlencode "$query")" || _die "Failed to encode query"
    local params="key=${GOOGLE_API_KEY}&cx=${GOOGLE_CSE_ID}&q=${encoded_query}&num=${argc_num_results:-10}&start=${argc_start_index:-1}&safe=${argc_safe:-off}"

    if [[ "${argc_search_type:-web}" == "image" ]]; then
        params+="&searchType=image"
        [[ -n "${argc_image_size:-}" ]] && params+="&imgSize=${argc_image_size}"
        [[ -n "${argc_image_type:-}" ]] && params+="&imgType=${argc_image_type}"
        [[ -n "${argc_image_color_type:-}" ]] && params+="&imgColorType=${argc_image_color_type}"
        [[ -n "${argc_image_dominant_color:-}" ]] && params+="&imgDominantColor=${argc_image_dominant_color}"
        [[ -n "${argc_image_rights:-}" ]] && params+="&rights=${argc_image_rights}"
    fi

    if [[ -n "${argc_lang:-}" ]]; then
        params+="&lr=lang_${argc_lang}&hl=${argc_ui_lang:-${argc_lang}}"
    elif [[ -n "${argc_ui_lang:-}" ]]; then
        params+="&hl=${argc_ui_lang}"
    fi

    [[ -n "${argc_country:-}" ]] && params+="&cr=country${argc_country^^}"
    [[ -n "${argc_date_filter:-}" ]] && params+="&dateRestrict=${argc_date_filter}"

    if [[ -n "${argc_site_filter:-}" ]]; then
        params+="&siteSearch=$(_urlencode "${argc_site_filter}")&siteSearchFilter=i"
    elif [[ -n "${argc_site_exclude:-}" ]]; then
        params+="&siteSearch=$(_urlencode "${argc_site_exclude}")&siteSearchFilter=e"
    fi

    [[ -n "${argc_file_type:-}" ]] && [[ "${argc_search_type:-web}" == "web" ]] && params+="&fileType=${argc_file_type,,}"
    [[ "${argc_sort_by:-relevance}" == "date" ]] && params+="&sort=date"
    [[ "${argc_exact_terms:-0}" == "1" ]] && params+="&exactTerms=${encoded_query}"
    [[ -n "${argc_exclude_terms:-}" ]] && params+="&excludeTerms=$(_urlencode "${argc_exclude_terms}")"
    [[ -n "${argc_or_terms:-}" ]] && params+="&orTerms=$(_urlencode "${argc_or_terms}")"
    [[ -n "${argc_related_site:-}" ]] && params+="&relatedSite=$(_urlencode "${argc_related_site}")"
    [[ -n "${argc_link_site:-}" ]] && params+="&linkSite=$(_urlencode "${argc_link_site}")"
    [[ "${argc_pretty_print:-0}" == "1" ]] && params+="&prettyPrint=true"

    printf "%s" "$params"
}

# ════════════════════════════════════════════════════════════════════════════════
# 🚀 HTTP REQUEST (with rate limiting + cache + retry + exponential backoff)
# ════════════════════════════════════════════════════════════════════════════════

_do_request() {
    local params="$1"
    local max_retries="${argc_max_retries:-2}"
    local timeout="${argc_timeout:-15}"
    local rate_delay="${argc_rate_limit_delay:-1}"

    (( rate_delay > 0 )) && sleep "$rate_delay"

    # Per-minute rate limit check (NEW)
    _rate_limit_check

    # Cache check
    local cached
    if cached="$(_cache_read "$params")"; then
        _debug "Serving from cache."
        printf "%s" "$cached"
        return 0
    fi

    _quota_check

    local url="${API_BASE_URL}?${params}"
    _debug "API URL: ${url//$GOOGLE_API_KEY/[REDACTED]}"

    local attempt=0 response http_code body
    while (( attempt <= max_retries )); do
        if (( attempt > 0 )); then
            local wait_secs=$(( (2 ** attempt) + (RANDOM % 5) ))
            _debug "Retry $attempt/$max_retries - waiting ${wait_secs}s"
            sleep "$wait_secs"
        fi

        local curl_args=(
            --silent --show-error --fail-with-body
            --max-time "$timeout" --connect-timeout 8
            --retry 0 --compressed
            --user-agent "${TOOL_NAME}/${TOOL_VERSION}"
            --write-out "\n__STATUS__%{http_code}"
        )
        [[ -n "${argc_proxy:-}" ]] && curl_args+=(--proxy "${argc_proxy}")
        [[ -n "${argc_header:-}" ]] && curl_args+=(-H "${argc_header}")

        response=$(curl "${curl_args[@]}" "$url" 2>&1) || true

        http_code=$(printf "%s" "$response" | grep -o '__STATUS__[0-9]*' | grep -o '[0-9]*' || printf "0")
        body=$(printf "%s" "$response" | sed 's/__STATUS__[0-9]*$//')

        _debug "HTTP: ${http_code}"

        case "$http_code" in
            200)
                _quota_increment
                _cache_write "$params" "$body"
                printf "%s" "$body"
                return 0
                ;;
            400) _error "Bad request (HTTP 400)."; printf "%s" "$body"; return 1 ;;
            403) _error "Access denied (HTTP 403)."; printf "%s" "$body"; return 1 ;;
            429)
                local retry_after=$(( (attempt + 1) * 10 ))
                _warn "Rate limited (HTTP 429) - waiting ${retry_after}s."
                sleep "$retry_after"
                ;;
            500|502|503|504) _warn "Server error (HTTP ${http_code})." ;;
            0) _warn "Network error." ;;
            *) _error "Unexpected HTTP ${http_code}."; printf "%s" "$body"; return 1 ;;
        esac
        (( attempt++ )) || true
    done

    _error "All ${max_retries} retries exhausted."
    printf "%s" "$body"
    return 1
}

# ════════════════════════════════════════════════════════════════════════════════
# ⚡ PARALLEL MULTI-PAGE FETCHING
# ════════════════════════════════════════════════════════════════════════════════

_fetch_multiple_pages() {
    local query="$1"
    local num_pages="${2:-1}"
    local tmpdir="$(_get_tmpdir)/pages"
    mkdir -p "$tmpdir"

    local pids=() start_idx=1

    for (( page=0; page<num_pages; page++ )); do
        (
            local page_params="$(_build_params "$query" | sed 's/num=[0-9]*/num=10/')&start=${start_idx}"
            local page_resp="$(_do_request "$page_params")" || exit 1
            printf "%s" "$page_resp" > "${tmpdir}/page_${page}.json"
        ) &
        pids+=($!)
        start_idx=$(( start_idx + 10 ))
    done

    for pid in "${pids[@]}"; do
        wait "$pid" || _warn "Page fetch failed for PID $pid"
    done

    local merged=""
    for (( page=0; page<num_pages; page++ )); do
        local file="${tmpdir}/page_${page}.json"
        [[ -f "$file" ]] || continue
        if [[ -z "$merged" ]]; then
            merged="$file"
        else
            local next_merged="$tmpdir/merged_${page}.json"
            jq -s '.[0].items += .[1].items | .[0]' "$merged" "$file" > "$next_merged"
            merged="$next_merged"
        fi
    done
    cat "$merged"
}

# ════════════════════════════════════════════════════════════════════════════════
# IMAGE PROCESSING
# ════════════════════════════════════════════════════════════════════════════════

_check_image_dimensions() {
    local filepath="$1"
    local min_w="${argc_image_min_width:-0}"
    local min_h="${argc_image_min_height:-0}"

    (( min_w == 0 && min_h == 0 )) && return 0
    ! _has_cmd identify && { _warn "'identify' not found."; return 0; }

    local dims width height
    dims=$(identify -format "%wx%h" "$filepath" 2>/dev/null | head -1) || { _warn "Could not read dimensions."; return 0; }
    width="${dims%%x*}" height="${dims##*x}"

    if ! [[ "$width" =~ ^[0-9]+$ ]] || ! [[ "$height" =~ ^[0-9]+$ ]]; then
        _warn "Non-numeric dimensions."
        return 0
    fi

    if (( min_w > 0 && width < min_w )) || (( min_h > 0 && height < min_h )); then
        _debug "Reject: ${width}x${height} < min ${min_w}x${min_h}"
        return 1
    fi
    _debug "Pass: ${width}x${height}"
    return 0
}

_verify_image() {
    local filepath="$1"
    [[ ! -s "$filepath" ]] && { _warn "Empty/missing: $(basename "$filepath")"; return 1; }

    if _has_cmd file; then
        local mime
        mime=$(file --mime-type -b "$filepath" 2>/dev/null || printf "unknown")
        _debug "MIME: $mime"
        case "$mime" in
            image/*|application/pdf) return 0 ;;
            *) _warn "Not an image: $mime"; return 1 ;;
        esac
    fi

    if _has_cmd xxd; then
        local magic
        magic=$(xxd -l 12 "$filepath" 2>/dev/null | awk 'NR==1{gsub(/ /,"",$0); print substr($0,10,24)}')
        _debug "Magic: $magic"
        case "$magic" in
            ffd8ff*|89504e47*|47494638*|52494646*|424d*|00000100*|3c737667*|3c3f786d*) return 0 ;;
            *) _warn "Unknown magic: $magic"; return 1 ;;
        esac
    fi
    return 0
}

_create_thumbnail() {
    local filepath="$1" thumb_dir="$2"
    ! _has_cmd convert && { _warn "ImageMagick not found."; return 1; }

    local thumb_path="${thumb_dir}/thumb_$(basename "$filepath")"
    convert "$filepath" -limit memory 64MB -limit map 128MB -thumbnail 200x200^ \
        -gravity center -extent 200x200 -strip -quality 80 "$thumb_path" 2>/dev/null || \
        { _warn "Thumbnail failed: $(basename "$filepath")"; return 1; }

    _debug "Thumbnail: $thumb_path"
    printf "%s" "$thumb_path"
}

_detect_mime() {
    local filepath="$1"
    if _has_cmd file; then
        local mime=$(file --mime-type -b "$filepath" 2>/dev/null)
        [[ -n "$mime" ]] && { printf "%s" "$mime"; return 0; }
    fi
    local ext="${filepath##*.}"
    case "${ext,,}" in
        jpg|jpeg) printf "image/jpeg" ;;
        png)      printf "image/png" ;;
        gif)      printf "image/gif" ;;
        webp)     printf "image/webp" ;;
        svg)      printf "image/svg+xml" ;;
        bmp)      printf "image/bmp" ;;
        ico)      printf "image/x-icon" ;;
        pdf)      printf "application/pdf" ;;
        *)        printf "application/octet-stream" ;;
    esac
}

# ════════════════════════════════════════════════════════════════════════════════
# 🔐 BASE64 ENCODE (with pure-bash fallback)
# ════════════════════════════════════════════════════════════════════════════════

_encode_image_base64() {
    local filepath="$1" mime_type="$2"

    if _has_cmd base64; then
        local b64
        if b64=$(base64 -w 0 < "$filepath" 2>/dev/null) || b64=$(base64 -b 0 < "$filepath" 2>/dev/null); then
            printf "data:%s;base64,%s" "$mime_type" "$b64"
            return 0
        fi
    fi

    # Pure bash fallback
    _warn "'base64' not found, using pure-bash fallback."
    _base64_fallback "$filepath" "$mime_type"
}

_base64_fallback() {
    local file="$1" mime="$2"
    local chars="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    local out="" val=0 bits=0

    while IFS= read -r -n1 -d '' byte; do
        val=$(( (val << 8) | $(printf "%d" "'$byte") ))
        bits=$(( bits + 8 ))
        while (( bits >= 6 )); do
            bits=$(( bits - 6 ))
            out+="${chars:$(( (val >> bits) & 63 )):1}"
        done
    done < "$file"

    while (( (${#out} % 4) )); do out+='='; done
    printf "data:%s;base64,%s" "$mime" "$out"
}

# ════════════════════════════════════════════════════════════════════════════════
# 📥 DOWNLOAD SINGLE IMAGE (with retries)
# ════════════════════════════════════════════════════════════════════════════════

_download_single_image() {
    local url="$1" dest_dir="$2" index="$3" title="$4"
    local dl_timeout="${argc_download_timeout:-30}"
    local skip_existing="${argc_skip_existing:-0}"
    local fmt_filter="${argc_image_format:-}"
    local resume="${argc_resume_downloads:-0}"
    local dl_limit="${argc_download_limit:-0}"

    # Format filter
    if [[ -n "$fmt_filter" ]]; then
        local url_ext filter_norm
        url_ext="$(_url_extension "$url")"
        filter_norm="${fmt_filter,,}"
        [[ "$filter_norm" == "jpg" ]] && filter_norm="jpeg"
        [[ "$url_ext" == "jpg" ]] && url_ext="jpeg"
        if [[ "$url_ext" != "$filter_norm" ]]; then
            _debug "Skip (format): $url"
            jq -n --arg url "$url" --arg reason "format_mismatch" '{skipped:true, reason:$reason, url:$url}'
            return 0
        fi
    fi

    local ext safe_title url_hash filename filepath
    ext="$(_url_extension "$url")"
    safe_title="$(_sanitize_filename "${title:-image}" 40)"
    url_hash="$(_hash_string "$url")"
    filename="${index}_${safe_title}_${url_hash}.${ext}"
    filepath="${dest_dir}/${filename}"

    local partial_file="${filepath}.part"

    # Skip if exists
    if [[ "$skip_existing" == "1" ]] && [[ -f "$filepath" ]]; then
        local fsize
        fsize=$(stat -c%s "$filepath" 2>/dev/null || stat -f%z "$filepath" 2>/dev/null || printf "0")
        _debug "Skip existing: $filename"
        jq -n --arg path "$filepath" --arg filename "$filename" --arg reason "exists" --argjson size "$fsize" \
            '{skipped:true, reason:$reason, path:$path, filename:$filename, size_bytes:$size}'
        return 0
    fi

    _debug "Downloading [$index]: $url"

    local curl_args=(
        --silent --show-error --location --max-redirs 5
        --max-time "$dl_timeout" --connect-timeout 8
        --retry 1 --retry-delay 2
        --user-agent "Mozilla/5.0 (compatible; ${TOOL_NAME}/${TOOL_VERSION})"
        --referer "https://www.google.com/"
        --output "$filepath"
        --write-out "%{http_code}|%{url_effective}|%{size_download}|%{content_type}"
    )

    # Resume support
    if [[ "$resume" == "1" ]] && [[ -f "$partial_file" ]]; then
        local partial_size
        partial_size=$(stat -c%s "$partial_file" 2>/dev/null || stat -f%z "$partial_file" 2>/dev/null || printf "0")
        curl_args+=(--range "${partial_size}-")
        mv "$partial_file" "$filepath"
    fi

    (( dl_limit > 0 )) && curl_args+=(--limit-rate "${dl_limit}k")
    [[ "${argc_show_progress:-0}" == "1" ]] && curl_args+=(--progress-bar)

    # Retry loop
    local max_dl_retries=2 dl_attempt=0 curl_out

    while (( dl_attempt <= max_dl_retries )); do
        curl_out=$(curl "${curl_args[@]}" "$url" 2>&1) && break
        (( dl_attempt++ ))
        _warn "Download attempt $dl_attempt failed."
        sleep $(( 2 ** dl_attempt ))
    done

    if [[ -z "$curl_out" ]]; then
        _warn "All download attempts failed."
        jq -n --arg url "$url" --arg reason "download_failed" '{skipped:true, reason:$reason, url:$url}'
        return 0
    fi

    local http_code dl_url dl_size content_type
    http_code=$(printf "%s" "$curl_out" | cut -d'|' -f1)
    dl_url=$(printf "%s" "$curl_out" | cut -d'|' -f2)
    dl_size=$(printf "%s" "$curl_out" | cut -d'|' -f3)
    content_type=$(printf "%s" "$curl_out" | cut -d'|' -f4 | cut -d';' -f1 | tr -d ' ')

    # Validate HTTP
    if [[ "$http_code" != "200" ]] && [[ "$http_code" != "206" ]]; then
        _warn "Download failed (HTTP $http_code)."
        rm -f "$filepath"
        jq -n --arg url "$url" --arg reason "http_${http_code}" '{skipped:true, reason:$reason, url:$url}'
        return 0
    fi

    [[ ! -s "$filepath" ]] && { _warn "Empty download."; rm -f "$filepath"; jq -n --arg url "$url" --arg reason "empty" '{skipped:true, reason:$reason, url:$url}'; return 0; }

    # Verify image
    if [[ "${argc_verify_images:-0}" == "1" ]] && ! _verify_image "$filepath"; then
        rm -f "$filepath"
        jq -n --arg url "$url" --arg reason "invalid_image" '{skipped:true, reason:$reason, url:$url}'
        return 0
    fi

    # Dimension filter
    if ! _check_image_dimensions "$filepath"; then
        rm -f "$filepath"
        jq -n --arg url "$url" --arg reason "below_min_dimensions" '{skipped:true, reason:$reason, url:$url}'
        return 0
    fi

    local mime_type; mime_type="$(_detect_mime "$filepath")"
    [[ "$content_type" =~ ^image/ ]] && mime_type="$content_type"

    local actual_dims="unknown"
    _has_cmd identify && actual_dims=$(identify -format "%wx%h" "$filepath" 2>/dev/null | head -1) || true

    local thumb_path=""
    if [[ "${argc_create_thumbnails:-0}" == "1" ]]; then
        local thumb_dir="${dest_dir}/thumbnails"
        mkdir -p "$thumb_dir"
        thumb_path=$(_create_thumbnail "$filepath" "$thumb_dir" 2>/dev/null || printf "")
    fi

    local b64_data=""
    if [[ "${argc_encode_base64:-0}" == "1" ]]; then
        b64_data=$(_encode_image_base64 "$filepath" "$mime_type" 2>/dev/null || printf "")
    fi

    _info "Downloaded [$index]: $filename ($(_human_size "${dl_size:-0}"))"

    jq -n \
        --argjson index "$index" \
        --arg url "$url" --arg final_url "$dl_url" --arg path "$filepath" \
        --arg filename "$filename" --arg title "${title:-}" \
        --arg mime_type "$mime_type" --arg dimensions "$actual_dims" \
        --arg thumbnail "$thumb_path" --arg base64 "$b64_data" \
        --argjson size_bytes "${dl_size:-0}" \
        --argjson http_status "${http_code:-0}" \
        '{skipped:false, index:$index, url:$url, final_url:$final_url, path:$path,
          filename:$filename, title:$title, mime_type:$mime_type, dimensions:$dimensions,
          size_bytes:$size_bytes, http_status:$http_status,
          thumbnail:(if $thumbnail!="" then $thumbnail else null end),
          base64:(if $base64!="" then $base64 else null end)}'
}

# ════════════════════════════════════════════════════════════════════════════════
# 📥 PARALLEL IMAGE DOWNLOADER
# ════════════════════════════════════════════════════════════════════════════════

_download_images() {
    local json_response="$1"
    local dest_dir="${argc_download_dir:-./search_images}"
    local max_dl="${argc_download_max:-5}"
    local workers="${argc_download_workers:-3}"

    mkdir -p "$dest_dir" || _die "Cannot create: $dest_dir"
    _info "Download dir: $(realpath "$dest_dir" 2>/dev/null || printf "%s" "$dest_dir")"

    local image_list
    image_list=$(printf "%s" "$json_response" | jq -r --argjson max "$max_dl" \
        '.items[0:$max] | to_entries[] | "\(.key)\t\(.value.link)\t\(.value.title // "image_\(.key)")"' 2>/dev/null) || true

    [[ -z "$image_list" ]] && { _warn "No image URLs."; printf "[]"; return 0; }

    local tmpdir="$(_get_tmpdir)/downloads"
    mkdir -p "$tmpdir"

    local fifo="${tmpdir}/worker_slots"
    mkfifo "$fifo" 2>/dev/null || true
    exec 8<>"$fifo"

    local i
    for (( i=0; i<workers; i++ )); do printf "." >&8; done

    local result_dir="${tmpdir}/results"
    mkdir -p "$result_dir"

    local pids=()
    while IFS=$'\t' read -r idx url title; do
        [[ -z "$url" ]] && continue
        read -r -n 1 token <&8 || true

        (
            local result
            result=$(_download_single_image "$url" "$dest_dir" "$(( idx + 1 ))" "$title")
            printf "%s\n" "$result" > "${result_dir}/${idx}.json"
            printf "." >&8
        ) &
        pids+=($!)
    done <<< "$image_list"

    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
    exec 8>&-

    local all_results=() dl_count=0 skip_count=0
    while IFS= read -r result_file; do
        [[ ! -f "$result_file" ]] && continue
        local result; result=$(cat "$result_file")
        all_results+=("$result")
        local skipped; skipped=$(printf "%s" "$result" | jq -r '.skipped // true' 2>/dev/null || printf "true")
        [[ "$skipped" == "false" ]] && (( dl_count++ )) || (( skip_count++ ))
    done < <(find "$result_dir" -maxdepth 1 -name "*.json" | sort -V)

    _info "Downloads: ${dl_count} ok, ${skip_count} skipped."

    if [[ "${argc_generate_manifest:-0}" == "1" ]]; then
        local manifest_path="${dest_dir}/${MANIFEST_FILE}"
        local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || printf "unknown")
        printf "%s\n" "${all_results[@]}" | jq -s \
            --arg query "${argc_query}" --arg ts "$timestamp" --arg dir "$dest_dir" \
            --argjson dl "$dl_count" --argjson sk "$skip_count" \
            '{generated_at:$ts, query:$query, download_dir:$dir, downloaded:$dl, skipped:$sk,
              total_processed:($dl+$sk), images:.}' > "$manifest_path" 2>/dev/null && \
            _info "Manifest: $manifest_path" || _warn "Manifest failed."
    fi

    if [[ ${#all_results[@]} -gt 0 ]]; then
        printf "%s\n" "${all_results[@]}" | jq -s '.' 2>/dev/null || printf "[]"
    else
        printf "[]"
    fi
}

# ════════════════════════════════════════════════════════════════════════════════
# 📤 FORMAT WEB SEARCH RESULTS
# ════════════════════════════════════════════════════════════════════════════════

_format_web_results() {
    local json_response="$1"
    local query="$2"
    local fmt="${argc_output_format:-detailed}"
    local no_dups="${argc_no_duplicates:-0}"

    local api_error
    api_error=$(printf "%s" "$json_response" | jq -r '.error.message // empty' 2>/dev/null || true)
    if [[ -n "$api_error" ]]; then
        local err_code; err_code=$(printf "%s" "$json_response" | jq -r '.error.code // "?"' 2>/dev/null || printf "?")
        _error "API error ($err_code): $api_error"
        printf "Search Error (%s): %s\n" "$err_code" "$api_error"
        return 1
    fi

    local item_count
    item_count=$(printf "%s" "$json_response" | jq '.items | length' 2>/dev/null || printf "0")
    if [[ "$item_count" == "0" ]] || [[ "$item_count" == "null" ]]; then
        printf 'No results for: "%s"\n' "$query"
        return 0
    fi

    # JSON keys extraction (NEW)
    if [[ -n "${argc_json_keys:-}" ]]; then
        local IFS=','; read -ra keys <<< "${argc_json_keys}"
        printf "%s" "$json_response" | jq -r --argjson ks "$(printf '%s' "${keys[*]}" | jq -R -s 'split(",")')" \
            '.items[] | [$ks[] as $k | .[$k] // ""] | @tsv' 2>/dev/null
        return 0
    fi

    local dedup=""
    [[ "$no_dups" == "1" ]] && dedup="| unique_by(.displayLink)"

    local sep70="$(_repeat_char '=' 70)"
    local sep_dash="$(_repeat_char '-' 70)"

    # Apply blacklist
    if [[ -n "$DOMAIN_BLACKLIST" ]]; then
        json_response=$(printf "%s" "$json_response" | jq \
            --arg blacklist "$DOMAIN_BLACKLIST" \
            '.items |= [.[] | if (.displayLink | test("^(\\($blacklist))$")) then empty else . end]' 2>/dev/null || \
            printf "%s" "$json_response")
    fi

    # Log all encountered links
    mkdir -p "$CACHE_BASE_DIR"
    printf "%s\n" "${json_response}" | jq -r '.items[].link // .items[].image.contextLink // empty' >> "${CACHE_BASE_DIR}/encountered_links.txt"

    case "$fmt" in
        json)
            printf "%s" "$json_response"
            ;;
        parsed)
            printf "%s" "$json_response" | jq -c '.items[] | {title: .title, link: .link, snippet: .snippet, pageThumbnail: (.pagemap.cse_image[0].src // null)}' 2>/dev/null
            ;;
        compact)
            printf "%s" "$json_response" | jq -r \
                --arg q "$query" --arg sep "$(_repeat_char '=' 60)" \
                '.searchInformation as $i |
                "Search : \($q)",
                "Found  : \($i.formattedTotalResults // "?") results in \($i.formattedSearchTime // "?")s",
                $sep,
                (.items[] '"$dedup"' | to_entries[] | "[\(.key+1)] \(.value.title)", "    \(.value.link)")' 2>/dev/null
            ;;
        detailed|*)
            printf "%s" "$json_response" | jq -r \
                --arg q "$query" --arg sep "$sep70" --arg sep_d "$sep_dash" \
                '.searchInformation as $i |
                $sep, "GOOGLE WEB SEARCH RESULTS", $sep,
                "Query       : \($q)",
                "Total Found : \($i.formattedTotalResults // "Unknown")",
                "Search Time : \($i.formattedSearchTime // "?")s",
                "Safe Search : OFF",
                $sep, "",
                ([.items[] '"$dedup"' | to_entries[] | .key as $k | .value |
                    "[\($k+1)] \(.title)",
                    "    URL     : \(.link)",
                    "    Source  : \(.displayLink)",
                    (if .pagemap.metatags[0]["article:published_time"] then
                        "    Date    : \(.pagemap.metatags[0]["article:published_time"])" else empty end),
                    "    Snippet : \(.snippet | gsub("\n"; " ") | gsub("  +"; " "))", ""] | .[]),
                $sep_d,
                "Powered by Google Custom Search API  |  Safe Search: OFF"' 2>/dev/null
            ;;
    esac
}

# ════════════════════════════════════════════════════════════════════════════════
# 📤 FORMAT IMAGE SEARCH RESULTS
# ════════════════════════════════════════════════════════════════════════════════

_format_image_results() {
    local json_response="$1"
    local query="$2"
    local download_results="${3:-}"
    local fmt="${argc_output_format:-detailed}"
    local show_meta="${argc_show_image_metadata:-0}"

    local api_error
    api_error=$(printf "%s" "$json_response" | jq -r '.error.message // empty' 2>/dev/null || true)
    if [[ -n "$api_error" ]]; then
        local err_code; err_code=$(printf "%s" "$json_response" | jq -r '.error.code // "?"' 2>/dev/null || printf "?")
        _error "API error ($err_code): $api_error"
        printf "Image Search Error (%s): %s\n" "$err_code" "$api_error"
        return 1
    fi

    local item_count
    item_count=$(printf "%s" "$json_response" | jq '.items | length' 2>/dev/null || printf "0")
    if [[ "$item_count" == "0" ]] || [[ "$item_count" == "null" ]]; then
        printf 'No images for: "%s"\n' "$query"
        return 0
    fi

    local sep70="$(_repeat_char '=' 70)"
    local sep_dash="$(_repeat_char '-' 70)"

    # Log all encountered links
    mkdir -p "$CACHE_BASE_DIR"
    printf "%s\n" "${json_response}" | jq -r '.items[].link // .items[].image.contextLink // empty' >> "${CACHE_BASE_DIR}/encountered_links.txt"

    case "$fmt" in
        json)
            if [[ -n "$download_results" ]] && [[ "$download_results" != "[]" ]]; then
                printf "%s" "$json_response" | jq --argjson dl "$download_results" '. + {download_results: $dl}' 2>/dev/null || \
                    printf "%s" "$json_response"
            else
                printf "%s" "$json_response"
            fi
            ;;
        parsed)
            printf "%s" "$json_response" | jq -c '.items[] | {title: .title, imageUrl: .link, contextPage: .image.contextLink, mimeType: .mime, width: .image.width, height: .image.height, thumbnail: .image.thumbnailLink}' 2>/dev/null
            ;;
        compact)
            printf "%s" "$json_response" | jq -r \
                --arg q "$query" --arg sep "$(_repeat_char '=' 60)" \
                '"Image Search : \($q)", "Safe Search  : OFF", $sep,
                (.items | to_entries[] | "[\(.key+1)] \(.title // "Untitled")",
                    "    Image : \(.value.link)",
                    "    Page  : \(.value.image.contextLink // "N/A")")' 2>/dev/null

            if [[ -n "$download_results" ]] && [[ "$download_results" != "[]" ]]; then
                local dl_c sk_c
                dl_c=$(printf "%s" "$download_results" | jq '[.[] | select(.skipped==false)] | length' 2>/dev/null || printf "0")
                sk_c=$(printf "%s" "$download_results" | jq '[.[] | select(.skipped==true)] | length' 2>/dev/null || printf "0")
                printf "\nDownloaded: %s  |  Skipped: %s\n" "$dl_c" "$sk_c"
            fi
            ;;
        detailed|*)
            printf "%s" "$json_response" | jq -r \
                --arg q "$query" --arg sep "$sep70" --arg sep_d "$sep_dash" --arg show_meta "$show_meta" \
                '.searchInformation as $i |
                $sep, "GOOGLE IMAGE SEARCH RESULTS", $sep,
                "Query       : \($q)",
                "Total Found : \($i.formattedTotalResults // "Unknown")",
                "Search Time : \($i.formattedSearchTime // "?")s",
                "Safe Search : OFF",
                $sep, "",
                (.items | to_entries[] | .key as $k | .value |
                    "[\($k+1)] \(.title // "Untitled")",
                    "    Image URL : \(.link)",
                    "    Page URL  : \(.image.contextLink // "N/A")",
                    "    Source    : \(.displayLink)",
                    (if $show_meta == "1" then
                        "    Width     : \(.image.width // "?")",
                        "    Height    : \(.image.height // "?")",
                        "    MIME      : \(.mime // "?")",
                        "    Thumbnail : \(.image.thumbnailLink // "N/A")",
                        "    Thumb Dim : \(.image.thumbnailWidth // "?")x\(.image.thumbnailHeight // "?")"
                    else empty end),
                    "    Snippet   : \(.snippet // "N/A" | gsub("\n"; " "))", "")' 2>/dev/null

            if [[ -n "$download_results" ]] && [[ "$download_results" != "[]" ]]; then
                printf "\n%s\nDOWNLOADED IMAGES\n%s\n" "$sep70" "$sep70"
                printf "%s" "$download_results" | jq -r \
                    '.[] | if .skipped then
                        "  [SKIP] \(.reason // "unknown")", "         URL : \(.url // .path // "N/A")", ""
                    else
                        "  [OK]   \(.filename // "unnamed")",
                        "         Path       : \(.path // "N/A")",
                        "         Dimensions : \(.dimensions // "unknown")",
                        "         MIME       : \(.mime_type // "unknown")",
                        "         Size       : \(.size_bytes // 0) bytes",
                        (if .thumbnail then "         Thumbnail  : \(.thumbnail)" else empty end),
                        (if .base64 then "         Base64    : [\( (.base64 | length) ) chars]" else empty end),
                        ""
                    end' 2>/dev/null

                local dl_c sk_c
                dl_c=$(printf "%s" "$download_results" | jq '[.[] | select(.skipped==false)] | length' 2>/dev/null || printf "0")
                sk_c=$(printf "%s" "$download_results" | jq '[.[] | select(.skipped==true)] | length' 2>/dev/null || printf "0")
                printf "%s\nTotal: %s downloaded, %s skipped.\n" "$sep_dash" "$dl_c" "$sk_c"
            fi
            ;;
    esac
}

# ════════════════════════════════════════════════════════════════════════════════
# 🔄 BATCH SEARCH PROCESSING
# ════════════════════════════════════════════════════════════════════════════════

_process_batch_queries() {
    local queries="$1"
    local all_results=()
    IFS='|' read -ra QUERY_ARRAY <<< "$queries"

    for q in "${QUERY_ARRAY[@]}"; do
        q=$(printf "%s" "$q" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [[ -z "$q" ]] && continue

        _info "Batch: $q"
        local response="$(_fetch_multiple_pages "$q" "${argc_pages:-1}")" || { _warn "Failed: $q"; continue; }
        local item_count; item_count=$(printf "%s" "$response" | jq '.items | length' 2>/dev/null || printf "0")
        _add_to_history "$q" "$item_count"
        _update_analytics "$q" "$item_count"
        all_results+=("$response")
    done

    [[ ${#all_results[@]} -gt 0 ]] && printf "%s" "${all_results[0]}" || printf "{}"
}

# ════════════════════════════════════════════════════════════════════════════════
# 🎯 MAIN
# ════════════════════════════════════════════════════════════════════════════════

main() {
    _check_deps
    _validate_params
    _show_cache_stats
    ( _cache_prune ) &>/dev/null &

    local query="${argc_query}"
    local search_type="${argc_search_type:-web}"

    [[ ${#query} -lt 2 ]] && _warn "Query very short ('$query')."

    _debug "Version   : $TOOL_VERSION"
    _debug "Type      : $search_type"
    _debug "Query     : $query"
    _debug "Results   : ${argc_num_results:-10}"
    _debug "Pages     : ${argc_pages:-1}"
    _debug "Cache TTL : ${argc_cache_ttl:-300}s"

    if [[ -n "${argc_queries:-}" ]]; then
        local batch_response
        batch_response="$(_process_batch_queries "${argc_queries}")" || {
            printf "Batch failed.\n" >> "${LLM_OUTPUT:-/dev/stdout}"
            exit 1
        }

        if [[ -n "${argc_export_format:-}" ]]; then
            case "${argc_export_format}" in
                csv)  _export_to_csv "$batch_response" "$query" ;;
                md)   _export_to_markdown "$batch_response" "$query" ;;
                html) _export_to_html "$batch_response" "$query" ;;
                json) printf "%s" "$batch_response" ;;
            esac
        else
            _format_web_results "$batch_response" "Batch: ${argc_queries}"
        fi
        return 0
    fi

    local num_results="${argc_num_results:-10}"
    local num_pages="${argc_pages:-1}"
    
    # Calculate pages needed
    local needed_pages=$(( (num_results + 9) / 10 ))
    if (( needed_pages > num_pages )); then
        num_pages=$needed_pages
    fi

    local response

    if (( num_pages > 1 )); then
        response="$(_fetch_multiple_pages "$query" "$num_pages")"
        # Optional: Trim to num_results
        response=$(printf "%s" "$response" | jq -c ".items |= .[0:${num_results}]")
    else
        local params; params="$(_build_params "$query")"
        response="$(_do_request "$params")"
    fi

    response="$response" || { printf "Search failed.\n" >> "${LLM_OUTPUT:-/dev/stdout}"; exit 1; }

    local item_count; item_count=$(printf "%s" "$response" | jq '.items | length' 2>/dev/null || printf "0")
    _add_to_history "$query" "$item_count"
    _update_analytics "$query" "$item_count"

    if [[ -n "${argc_export_format:-}" ]]; then
        case "${argc_export_format}" in
            csv)  _export_to_csv "$response" "$query" ;;
            md)   _export_to_markdown "$response" "$query" ;;
            html) _export_to_html "$response" "$query" ;;
            json) printf "%s" "$response" ;;
        esac
    elif [[ "$search_type" == "image" ]]; then
        local download_results="[]"
        [[ "${argc_download_images:-0}" == "1" ]] && download_results="$(_download_images "$response")"
        _format_image_results "$response" "$query" "$download_results" >> "${LLM_OUTPUT:-/dev/stdout}"
    else
        _format_web_results "$response" "$query" >> "${LLM_OUTPUT:-/dev/stdout}"
    fi
}

eval "$(argc --argc-eval "$0" "$@")"
