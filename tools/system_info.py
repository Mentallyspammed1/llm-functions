#!/usr/bin/env python3
# @describe Get basic system info.
import platform
import os

def run() -> str:
    return f"OS: {platform.system()} {platform.release()}, CPU: {platform.processor()}"

if __name__ == "__main__":
    print(run())
