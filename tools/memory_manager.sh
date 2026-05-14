#!/usr/bin/env bash
set -eo pipefail

# @describe Manage persistent memory for AIChat conversations and context
# @option --action![store|retrieve|search|clear|export|import] Action
# @option --key Memory key or identifier
# @option --value Value to store or search query
# @option --type[conversation|preference|context|knowledge] Memory type
# @option --session Session identifier
# @option --tags Comma-separated tags for categorization

declare -a ALLOWED_TYPES=("conversation" "preference" "context" "knowledge")

cleanup() {
    local exit_code=$?
    exit $exit_code
}

trap cleanup EXIT

_validate_type() {
    local type_to_validate="$1"
    local valid=0
    for allowed_type in "${ALLOWED_TYPES[@]}"; do
        if [[ "$type_to_validate" == "$allowed_type" ]]; then
            valid=1
            break
        fi
    done
    if [[ "$valid" -eq 0 ]]; then
        echo "error: Invalid memory type '$type_to_validate'. Allowed types are: ${ALLOWED_TYPES[*]}" >&2
        exit 1
    fi
}

_output() {
    if [[ -n "${LLM_OUTPUT:-}" ]]; then
        echo "$1" >> "$LLM_OUTPUT"
    else
        echo "$1"
    fi
}

main() {
    local root_dir="${LLM_ROOT_DIR:-.}"
    local memory_dir="$root_dir/memory"
    mkdir -p "$memory_dir"
    
    case "$argc_action" in
        "store") store_memory "$memory_dir" ;;
        "retrieve") retrieve_memory "$memory_dir" ;;
        "search") search_memory "$memory_dir" ;;
        "clear") clear_memory "$memory_dir" ;;
        "export") export_memory "$memory_dir" ;;
        "import") import_memory "$memory_dir" ;;
        *) echo "error: Unknown action: $argc_action" >&2; exit 1 ;;
    esac
}

store_memory() {
    local memory_dir="$1"
    local key="${argc_key:-$(date +%s)}"
    local value="$argc_value"
    if [[ -z "$value" ]]; then
        echo "error: --value is required for store action" >&2
        exit 1
    fi
    local type="${argc_type:-context}"
    local session="${argc_session:-default}"
    local tags="${argc_tags:-}"
    
    _validate_type "$type"
    
    local memory_file="$memory_dir/${type}.jsonl"
    local timestamp=$(date -Iseconds)
    
    local cleaned_tags
    if [[ -n "$tags" ]]; then
        cleaned_tags=$(echo "$tags" | tr ',' '\n' | grep -v '^\s*$' | jq -R . | jq -s .)
    else
        cleaned_tags="[]"
    fi

    local entry
    entry=$(jq -n \
        --arg key "$key" \
        --arg value "$value" \
        --arg type "$type" \
        --arg session "$session" \
        --argjson tags "$cleaned_tags" \
        --arg timestamp "$timestamp" \
        '{
            key: $key,
            value: $value,
            type: $type,
            session: $session,
            tags: $tags,
            timestamp: $timestamp
        }') || { echo "error: jq command failed during entry creation." >&2; exit 1; }
    
    echo "$entry" >> "$memory_file"
    _output "Memory stored: $key"
}

retrieve_memory() {
    local memory_dir="$1"
    local key="$argc_key"
    if [[ -z "$key" ]]; then
        echo "error: --key is required for retrieve action" >&2
        exit 1
    fi
    
    local type_filter="${argc_type:-}"
    if [[ -n "$type_filter" ]]; then
        _validate_type "$type_filter"
        local memory_file="$memory_dir/${type_filter}.jsonl"
        if [[ -f "$memory_file" ]]; then
            local result
            result=$(jq -r --arg key "$key" 'select(.key == $key) | .value' "$memory_file") || { echo "error: jq command failed during retrieve." >&2; exit 1; }
            if [[ -n "$result" ]]; then
                _output "$result"
            else
                _output "No memory found for key: $key in type '$type_filter'."
            fi
        else
            _output "No memory file found for type: $type_filter."
        fi
    else
        local found_any=0
        for f in "$memory_dir"/*.jsonl; do
            [[ -e "$f" ]] || continue
            local result
            result=$(jq -r --arg key "$key" 'select(.key == $key) | .value' "$f") || { echo "error: jq command failed during retrieve." >&2; exit 1; }
            if [[ -n "$result" ]]; then
                _output "$result"
                found_any=1
            fi
        done
        if [[ "$found_any" -eq 0 ]]; then
            _output "No memory found for key: $key."
        fi
    fi
}

search_memory() {
    local memory_dir="$1"
    local query="${argc_value}"
    if [[ -z "$query" ]]; then
        echo "error: --value (query) is required for search action" >&2
        exit 1
    fi
    
    local type_filter="${argc_type:-}"
    local search_files=()
    if [[ -n "$type_filter" ]]; then
        _validate_type "$type_filter"
        search_files+=("$memory_dir/${type_filter}.jsonl")
    else
        search_files+=("$memory_dir"/*.jsonl)
    fi
    
    local found_any=0
    for f in "${search_files[@]}"; do
        [[ -e "$f" ]] || continue
        if jq -r --arg query "$query" \
                'select((.value | test($query; "i")) or (.tags[]? | test($query; "i")) or (.key | test($query; "i")))' \
                "$f"; then
            found_any=1
        else
            echo "error: jq command failed during search in file '$f'." >&2
        fi
    done
    
    if [[ "$found_any" -eq 0 ]]; then
        _output "No memory found matching query: \"$query\"."
    fi
}

clear_memory() {
    local memory_dir="$1"
    local type="${argc_type:-context}"
    _validate_type "$type"
    
    local memory_file="$memory_dir/${type}.jsonl"
    if [[ -f "$memory_file" ]]; then
        > "$memory_file"
        echo "Memory cleared for type: $type" >> "${LLM_OUTPUT:-/dev/stdout}"
    else
        echo "info: No memory file found for type '$type', nothing to clear." >> "${LLM_OUTPUT:-/dev/stdout}"
    fi
}

export_memory() {
    local memory_dir="$1"
    local type="${argc_type:-context}"
    _validate_type "$type"
    
    local memory_file="$memory_dir/${type}.jsonl"
    if [[ -f "$memory_file" ]]; then
        jq -s '.' "$memory_file" >> "${LLM_OUTPUT:-/dev/stdout}" || { echo "error: jq command failed during export." >&2; exit 1; }
    else
        echo "info: No memory file found for type '$type', nothing to export." >> "${LLM_OUTPUT:-/dev/stdout}"
    fi
}

import_memory() {
    local memory_dir="$1"
    local type="${argc_type:-context}"
    _validate_type "$type"
    
    local memory_file="$memory_dir/${type}.jsonl"
    if [ ! -t 0 ]; then
        cat >> "$memory_file"
        echo "Memory imported for type: $type" >> "${LLM_OUTPUT:-/dev/stdout}"
    else
        echo "info: No input piped for import. Import aborted." >> "${LLM_OUTPUT:-/dev/stdout}"
    fi
}

eval "$(argc --argc-eval "$0" "$@")"
