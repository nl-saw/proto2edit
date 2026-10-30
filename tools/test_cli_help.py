#!/usr/bin/env python3
"""Every command the CLI dispatches must appear in `./proto2 help`.

This exists because the help text was a hard-coded line range (`sed -n '2,29p'`) and silently dropped every
command added after that line - including snapshot, convert-all and seed. The range is now dynamic; this check
keeps both sides honest.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    src = open(os.path.join(ROOT, "proto2")).read()
    dispatch = set(re.findall(r"^  ([a-z][a-z-]*)\)", src, re.M))
    # multi-command case labels such as  help|-h|--help)  and  status|state)
    for line in re.findall(r"^  ([a-z][a-z|-]*)\)", src, re.M):
        for cmd in line.split("|"):
            if re.fullmatch(r"[a-z][a-z-]*", cmd):
                dispatch.add(cmd)
    dispatch.discard("h")
    help_out = subprocess.run([os.path.join(ROOT, "proto2"), "help"], capture_output=True, text=True).stdout
    documented = set(re.findall(r"^  \./proto2 ([a-z][a-z-]*)", help_out, re.M))
    # 'help' documents itself via the -h alias line; state/status and set-title are listed under their names
    missing = sorted(dispatch - documented)
    if missing:
        print(f"FAIL commands dispatched but not documented: {', '.join(missing)}")
        return 1
    print(f"help covers all {len(dispatch)} dispatched command(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
