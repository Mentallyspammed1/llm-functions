#!/usr/bin/env python3
# @describe Fetch HTTP headers.
# @option --url! <URL>
import urllib.request, os

def run(url: str) -> str:
    try:
        with urllib.request.urlopen(url) as r:
            return str(r.info())
    except Exception as e:
        return f"Error: {e}"

if __name__ == "__main__":
    kwargs = {k[5:]: v for k, v in os.environ.items() if k.startswith("argc_")}
    print(run(**kwargs))
