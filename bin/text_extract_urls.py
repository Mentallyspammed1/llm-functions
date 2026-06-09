#!/usr/bin/env python3
# ==============================================================================
# text_extract_urls.py
#
# @describe Extract all URLs from text.
# @option --text! <TEXT> The text to search.
# ==============================================================================

import re


def run(text: str):
    """
    Extract all URLs from text.
    """
    urls = re.findall(r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+", text)
    return "\n".join(set(urls)) if urls else "No URLs found."
