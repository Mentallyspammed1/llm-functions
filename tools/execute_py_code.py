#!/usr/bin/env python3
# @describe Execute the python code.
# @option --code! <TEXT> Python code to execute.

import sys, os, io, traceback, contextlib

# @env LLM_OUTPUT=/dev/stdout The output path.

def run(code: str) -> str:
    stdout_buf = io.StringIO()
    try:
        with io.StringIO() as buf, contextlib.redirect_stdout(buf):
            exec(code, {"__name__": "__main__"}, {})
            return buf.getvalue()
    except Exception:
        return traceback.format_exc()

def main():
    code = os.environ.get("argc_code", "")
    output = run(code)
    output_path = os.environ.get("LLM_OUTPUT", "/dev/stdout")
    
    if output_path == "/dev/stdout":
        print(output)
    else:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(output + "\n")

if __name__ == "__main__":
    main()
