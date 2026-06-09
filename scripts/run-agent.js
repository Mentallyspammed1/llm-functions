#!/bin/bash
# json-viewer - pretty print JSON from a file or stdin
# Usage: json-viewer <file>  # read from file
#        json-viewer        # read from stdin

if [[ $# -gt 0 ]]; then
    INPUT="$1"
    if [[ -f "$INPUT" ]]; then
        python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" < "$INPUT"
    else
        echo "Error: file not found: $INPUT" >&2
        exit 1
    fi
else
    python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))"
fi
