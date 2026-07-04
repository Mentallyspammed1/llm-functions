#!/usr/bin/env bash
# @describe Get or set system clipboard.
# @option --get <BOOL> Get clipboard
# @option --set <TEXT> Set clipboard

if [[ -n "$argc_get" ]]; then termux-clipboard-get;
elif [[ -n "$argc_set" ]]; then echo "$argc_set" | termux-clipboard-set; fi
