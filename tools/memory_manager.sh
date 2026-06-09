#!/usr/bin/env bash
# @describe Manage persistent memory for AIChat conversations and context
# @option --action! Action (store|retrieve|search|clear|export|import|cleanup)
# @option --key! Memory key or identifier (required for store/retrieve/search/clear)
# @option --value! Value to store (required for store)
# @option --type! Memory type (conversation|preference|context|knowledge) (default: context)
# @option --session! Session identifier (default: default)
# @option --tags! Comma-separated tags for categorisation (optional)
# @option --days! Retention period in days for cleanup (default: 30)

set -euo pipefail

# -------------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------------
LLM_ROOT_DIR="${LLM_ROOT_DIR:-$(pwd)}"
MEMORY_DIR="${LLM_ROOT_DIR}/memory"
mkdir -p "${MEMORY_DIR}"

# -------------------------------------------------------------------------
# Helper functions
# -------------------------------------------------------------------------
die() {
    local msg="${1:-Unknown error}"
    local code="${2:-1}"
    echo "ERROR: ${msg}" >&2
    exit "${code}"
}

# -------------------------------------------------------------------------
# Core actions
# -------------------------------------------------------------------------
case "${argc_action}" in
    store)
        [[ -n "${argc_key:-}" ]] || die "Missing --key"
        [[ -n "${argc_value:-}" ]] || die "Missing --value"

        local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
        local entry=$(jq -n \
            --arg key "${argc_key}" \
            --arg value "${argc_value}" \
            --arg type "${argc_type:-context}" \
            --arg session "${argc_session:-default}" \
            --arg tags "$(echo "${argc_tags:-}" | tr ' ' ',')" \
            --arg timestamp "${timestamp}" \
            '{
                key: $key,
                value: $value,
                type: $type,
                session: $session,
                tags: ($tags | split(",")),
                timestamp: $timestamp
            }')

        echo "${entry}" >> "${MEMORY_DIR}/${argc_type}.jsonl"
        echo "Memory stored: ${argc_key}" >&2
        ;;

    retrieve)
        [[ -n "${argc_key:-}" ]] || die "Missing --key"
        local type="${argc_type:-context}"
        local file="${MEMORY_DIR}/${type}.jsonl"
        [[ -f "${file}" ]] || die "No memory file for type '${type}'"
        jq -r --arg key "${argc_key}" 'select(.key == $key) | .value' "${file}" ;;

    search)
        [[ -n "${argc_value:-}" ]] || die "Missing --value (query)"
        local type="${argc_type:-context}"
        local file="${MEMORY_DIR}/${type}.jsonl"
        [[ -f "${file}" ]] || die "No memory file for type '${type}'"
        local query="${argc_value}"
        jq -r --arg query "${query}" \
            'select((.value | test($query; "i")) or (.tags[] | test($query; "i")))' "${file}" ;;

    clear)
        local type="${argc_type:-context}"
        local file="${MEMORY_DIR}/${type}.jsonl"
        [[ -f "${file}" ]] && > "${file}" && echo "Memory cleared for type '${type}'" ;;

    export)
        local type="${argc_type:-context}"
        local file="${MEMORY_DIR}/${type}.jsonl"
        [[ -f "${file}" ]] || die "No memory to export for type '${type}'"
        local export_file="${MEMORY_DIR}/${type}_export_$(date +%Y%m%d_%H%M%S).json"
        jq -s '.' "${file}" > "${export_file}"
        echo "Memory exported to: ${export_file}" ;;

    import)
        [[ -n "${argc_value:-}" ]] || die "Missing --value (file to import)"
        local import_file="${argc_value}"
        [[ -f "${import_file}" ]] || die "Import file not found: ${import_file}"
        local type="${argc_type:-context}"
        local file="${MEMORY_DIR}/${type}.jsonl"
        while IFS= read -r line; do
            echo "${line}" | base64 -d >> "${file}"
        done < <(base64 -w0 "${import_file}")
        echo "Memory imported from: ${import_file}" ;;

    cleanup)
        local days="${argc_days:-30}"
        local cutoff=$(date -u -d "-${days} days" +"%Y-%m-%dT%H:%M:%SZ")
        for f in "${MEMORY_DIR}"/*.jsonl; do
            [[ -f "${f}" ]] && jq --arg cutoff "${cutoff}" 'select(.timestamp >= $cutoff)' "${f}" > "${f}.tmp" && mv "${f}.tmp" "${f}"
        done
        echo "Cleanup complete – retained memories newer than ${days} days" ;;

    *)
        die "Unknown action: ${argc_action}" 1 ;;
esac
