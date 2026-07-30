#!/data/data/com.termux/files/usr/bin/env bash
# @describe Get or set system clipboard.
# @option --get <BOOL> Get clipboard
# @option --set <TEXT> Set clipboard

main() {
    if [[ -n "$argc_get" ]]; then
        termux-clipboard-get
    elif [[ -n "$argc_set" ]]; then
        echo "$argc_set" | termux-clipboard-set
    fi
}

eval "$(argc --argc-eval "$0" "$@")"
