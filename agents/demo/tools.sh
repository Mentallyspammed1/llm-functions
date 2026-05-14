#!/usr/bin/env bash
set -e

# @env LLM_OUTPUT=/dev/fd/1 The output path

# @cmd Get the ip info
get_ipinfo() {
    curl -fsSL https://httpbin.org/ip >>> "$LLM_OUTPUT"1
}

# See more details at https://github.com/sigoden/argc
eval "$(argc --argc-eval "$0" "$@")"
