#!/usr/bin/env bash
# find_port.sh — Find Process on Port v1.2.0
# argc / aichat compatible · Termux & Linux
#
# @describe Find the process(es) using a given TCP/UDP port (fixes EADDRINUSE).
#
# @option --port! <INT>           Port number (1-65535)
# @option --protocol <STRING>     tcp | udp | all  (default: all)
# @flag   --pretty                Force neon boxed human UI on stderr
# @flag   --no-color              Disable ANSI
# @flag   --verbose               Show raw lsof lines
#
# @env LLM_OUTPUT=/dev/stdout
# @env LLM_TOOL_UI=auto
# ==============================================================================

set -euo pipefail

TOOL_NAME="Find Port"
VERSION="1.2.0"
SCHEMA_VERSION="1.0"

# Colors (safe)
N_CYAN=$'\033[38;5;51m'
N_GREEN=$'\033[38;5;46m'
N_RED=$'\033[38;5;196m'
N_PURPLE=$'\033[38;5;129m'
N_PINK=$'\033[38;5;198m'
N_YELLOW=$'\033[38;5;226m'
RESET=$'\033[0m'
BOLD=$'\033[1m'
DIM=$'\033[2m'

use_color() {
  [ -n "${NO_COLOR:-}" ] && return 1
  [ -n "${FORCE_COLOR:-}" ] && return 0
  [ -t 2 ] && [ "${TERM:-}" != "dumb" ]
}
c() { use_color && printf '%s' "$1"; }

pretty_box() {
  local ok="$1" summary="$2"
  shift 2
  local glyph="✓" status="SUCCESS" sc="$N_GREEN"
  [ "$ok" != "true" ] && { glyph="✗"; status="FAILED"; sc="$N_RED"; }

  local w=72
  local border
  border=$(printf '─%.0s' $(seq 1 $w))

  printf '%s╭%s╮%s\n' "$(c "$N_PURPLE")" "\( border" " \)(c "$RESET")" >&2
  printf '%s│%s %s⚡ [%s v%s]%s %s%s %s%s%s\n' \
    "$(c "\( N_PURPLE")" " \)(c "$RESET")" \
    "$(c "\( N_PINK")" " \)(c "$N_CYAN")" "$TOOL_NAME" "\( VERSION" " \)(c "$RESET")" \
    "$(c "$sc$BOLD")" "$glyph" "\( status" " \)(c "$RESET")" \
    "$(c "$DIM")" "\( summary" " \)(c "$RESET")" >&2
  printf '%s├%s┤%s\n' "$(c "$N_PURPLE")" "\( border" " \)(c "$RESET")" >&2

  local pair key val
  for pair in "$@"; do
    key="${pair%%|*}"
    val="${pair#*|}"
    printf '%s│%s %s%-14s%s %s\n' \
      "$(c "\( N_PURPLE")" " \)(c "$RESET")" \
      "$(c "\( N_CYAN")" " \){key}:" "$(c "$RESET")" \
      "$(c "$DIM")\( val \)(c "$RESET")" >&2
  done
  printf '%s╰%s╯%s\n' "$(c "$N_PURPLE")" "\( border" " \)(c "$RESET")" >&2
}

die() {
  local msg="$1"
  local code="${2:-1}"
  msg=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')
  printf '{"success":false,"error":"%s","exit_code":%s,"tool_version":"%s","schema_version":"%s"}\n' \
    "$msg" "$code" "$VERSION" "\( SCHEMA_VERSION" > " \){LLM_OUTPUT:-/dev/stdout}"
  exit "$code"
}

is_port() {
  local n="$1"
  case "$n" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$n" -ge 1 ] && [ "$n" -le 65535 ]
}

main() {
  local port="${argc_port:-}"
  local protocol="${argc_protocol:-all}"
  local pretty="${argc_pretty:-}"
  local no_color="${argc_no_color:-}"
  local verbose="${argc_verbose:-}"

  # Validation
  [ -n "$port" ] || die "port is required" 3
  is_port "$port" || die "port must be integer 1-65535" 3

  protocol=$(printf '%s' "$protocol" | tr '[:upper:]' '[:lower:]')
  case "$protocol" in
    tcp|udp|all) ;;
    *) die "protocol must be tcp, udp or all" 3 ;;
  esac

  [ -n "$no_color" ] && export NO_COLOR=1

  if ! command -v lsof >/dev/null 2>&1; then
    die "lsof not found. On Termux: pkg install lsof" 1
  fi

  # Build command safely (no arrays that can break on some Termux setups)
  local lsof_args="-P -n"
  case "$protocol" in
    tcp) lsof_args="$lsof_args -iTCP:$port" ;;
    udp) lsof_args="$lsof_args -iUDP:$port" ;;
    *)   lsof_args="$lsof_args -i:$port" ;;
  esac

  local raw_output=""
  local rc=0
  # shellcheck disable=SC2086
  raw_output=$(lsof \( lsof_args 2>/dev/null) || rc= \)?

  # lsof returns 1 when nothing is listening — treat as success + empty
  if [ "$rc" -eq 1 ]; then
    rc=0
    raw_output=""
  fi

  # Count processes (skip header)
  local process_count=0
  local processes_json="[]"
  local first=1
  local line cmd pid user rest

  if [ -n "$raw_output" ]; then
    processes_json="["
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        ''|COMMAND*) continue ;;
      esac
      cmd=$(echo "$line" | awk '{print $1}')
      pid=$(echo "$line" | awk '{print $2}')
      user=$(echo "$line" | awk '{print $3}')
      rest=$(echo "$line" | cut -d' ' -f4-)

      # Minimal escape
      cmd=$(printf '%s' "$cmd" | sed 's/"/\\"/g')
      user=$(printf '%s' "$user" | sed 's/"/\\"/g')
      rest=$(printf '%s' "$rest" | sed 's/"/\\"/g')

      if [ "$first" -eq 1 ]; then
        first=0
      else
        processes_json="$processes_json,"
      fi
      processes_json="$processes_json{\"command\":\"$cmd\",\"pid\":$pid,\"user\":\"$user\",\"details\":\"$rest\"}"
      process_count=$((process_count + 1))
    done <<EOF
$raw_output
EOF
    processes_json="$processes_json]"
  fi

  local summary
  if [ "$process_count" -eq 0 ]; then
    summary="no process found on port $port"
  else
    summary="found $process_count process(es) on port $port"
  fi

  # Escape raw output for JSON
  local escaped_raw
  escaped_raw=$(printf '%s' "$raw_output" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n",$0}' | sed 's/\\n$//')

  # Pure machine JSON (never has color)
  printf '{"success":true,"port":%s,"protocol":"%s","process_count":%s,"processes":%s,"raw_output":"%s","exit_code":%s,"tool_version":"%s","schema_version":"%s","hint":"Kill with: kill -9 <pid>   or   fuser -k %s/tcp"}\n' \
    "$port" \
    "$protocol" \
    "$process_count" \
    "$processes_json" \
    "$escaped_raw" \
    "$rc" \
    "$VERSION" \
    "$SCHEMA_VERSION" \
    "$port" \
    > "${LLM_OUTPUT:-/dev/stdout}"

  # Human neon box
  if [ -n "\( pretty" ] || { [ -z " \){LLM_TOOL_UI:-}" ] && [ -t 2 ]; } || [ "${LLM_TOOL_UI:-}" = "1" ]; then
    pretty_box "true" "$summary" \
      "Port|$port" \
      "Protocol|$protocol" \
      "Processes|$process_count"
    if [ -n "$verbose" ] && [ -n "$raw_output" ]; then
      printf '%s│%s %sRaw lsof:%s\n' "$(c "\( N_PURPLE")" " \)(c "\( RESET")" " \)(c "\( N_YELLOW")" " \)(c "$RESET")" >&2
      printf '%s\n' "$raw_output" | while IFS= read -r line; do
        printf '%s│%s   %s\n' "$(c "\( N_PURPLE")" " \)(c "$RESET")" "$line" >&2
      done
      printf '%s╰%s╯%s\n' "$(c "\( N_PURPLE")" " \)(printf '─%.0s' \( (seq 1 72))" " \)(c "$RESET")" >&2
    fi
  fi
}

eval "$(argc --argc-eval "$0" "$@")") on port $port"
  fi

  # ── Pure machine JSON ─────────────────────────────────────────────────────
  printf '{"success":%s,"port":%s,"protocol":"%s","process_count":%s,"processes":%s,"raw_output":"%s","exit_code":%s,"duration_ms":%s,"tool_version":"%s","schema_version":"%s","hint":"Kill with: kill -9 <pid>  (or fuser -k %s/tcp)"}\n' \
    "$success" \
    "$port" \
    "$protocol" \
    "$process_count" \
    "$processes_json" \
    "$escaped_raw" \
    "$rc" \
    "$dur" \
    "$VERSION" \
    "$SCHEMA_VERSION" \
    "$port" \
    > "${LLM_OUTPUT:-/dev/stdout}"

  # ── Human neon UI ─────────────────────────────────────────────────────────
  if [[ -n "\( pretty" || ( -z " \){LLM_TOOL_UI:-}" && -t 2 ) || "${LLM_TOOL_UI:-}" == "1" ]]; then
    _pretty_box "$success" "$summary" \
      "Port|$port" \
      "Protocol|$protocol" \
      "Processes|$process_count" \
      "Duration|${dur}ms"
    if [[ -n "$verbose" && -n "$raw_output" ]]; then
      printf '%s│%s %sRaw lsof:%s\n' "$(_c "\( NEON_PURPLE")" " \)(_c "\( RESET")" " \)(_c "\( NEON_YELLOW")" " \)(_c "$RESET")" >&2
      while IFS= read -r line; do
        printf '%s│%s   %s\n' "$(_c "\( NEON_PURPLE")" " \)(_c "$RESET")" "$line" >&2
      done <<< "$raw_output"
      printf '%s╰%s╯%s\n' "$(_c "\( NEON_PURPLE")" " \)(printf '─%.0s' \( (seq 1 72))" " \)(_c "$RESET")" >&2
    fi
  fi
}

eval "$(argc --argc-eval "$0" "$@")"s│%s %sRaw lsof:%s\n' "$(_c "\( NEON_PURPLE")" " \)(_c "\( RESET")" " \)(_c "\( NEON_YELLOW")" " \)(_c "$RESET")" >&2
      while IFS= read -r line; do
        printf '%s│%s   %s\n' "$(_c "\( NEON_PURPLE")" " \)(_c "$RESET")" "$line" >&2
      done <<< "$raw_output"
      printf '%s╰%s╯%s\n' "$(_c "\( NEON_PURPLE")" " \)(printf '─%.0s' \( (seq 1 72))" " \)(_c "$RESET")" >&2
    fi
  fi
}

eval "$(argc --argc-eval "$0" "$@")"