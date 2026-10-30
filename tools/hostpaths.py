#!/usr/bin/env python3
"""Keep host paths out of generated artifacts.

Compiler errors and tool exceptions quote absolute paths (such as /mnt/<dir>/bin/luac), which land in
`artifacts/*.json` and would be published. The artifacts are evidence, not logs, so the path is replaced with a
stable placeholder at write time. URLs are left alone.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ABS = re.compile(r"(?:/[A-Za-z0-9_.+-]+){2,}/?")


def scrub(text):
    """`text` with the repository and absolute paths replaced by placeholders."""
    if text is None:
        return text
    text = str(text)
    if "://" in text:                      # a URL, not a file path
        return text.replace(ROOT, "<repo>")
    text = text.replace(ROOT, "<repo>")
    return _ABS.sub("<path>", text)
