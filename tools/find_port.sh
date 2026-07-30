#!/data/data/com.termux/files/usr/bin/env bash
# @describe Find process on port.
# @option --port! <PORT>

main() {
    lsof -i :"$argc_port"
}

eval "$(argc --argc-eval "$0" "$@")"
