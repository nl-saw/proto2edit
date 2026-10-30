#!/usr/bin/env python3
"""Parse every script in the debug-menu payload, because nothing else does.

`scripts/debug_boot.lua` loads the chain with `pcall(dofile, ...)`, so a file that does not compile
fails *silently*: the game boots, the menu opens, and the helpers in that file simply never exist.
That is not hypothetical - `scripts/proto2_extra.lua` carried a top-level `return PROTO2` at line 93
followed by 140 lines of code. In Lua a `return` must be the last statement of a block, so the chunk
ended there and the rest of the file was unreachable; worse, `dofile` parses the whole file before it
runs anything, so the file never loaded at all. Every `PROTO2_*` helper, including the XYZ readout
driver the debug page calls, was dead, and the failure looked exactly like "the menu button is wired
wrong" (docs/28, addendum).

Run by ./proto2 check. Needs the LuaDec kit, which is also what `./proto2 doctor` checks for.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(ROOT, "assets", "debugmenu", "scripts")
LUAC = os.path.join(ROOT, "build", "luadec-p2", "bin", "luac")

fails = []


def ck(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def scripts():
    if not os.path.isdir(PAYLOAD):
        return []
    return sorted(fn for fn in os.listdir(PAYLOAD) if fn.endswith(".lua"))


def check_source(path):
    """('ok', '') or ('error', 'line N: message')."""
    r = subprocess.run([LUAC, "-p", path], capture_output=True, text=True)
    if r.returncode == 0:
        return "ok", ""
    msg = (r.stderr or r.stdout).strip().split("\n")[0]
    where = msg.split(".lua:", 1)[-1] if ".lua:" in msg else msg
    return "error", where.strip()[:110]


def explain(detail):
    """Name the failure mode that produced this status, when the compiler's message allows it."""
    if "<eof>" in detail and "expected" in detail:
        return (detail + " - a top-level 'return' before the end of the file ends the chunk early, so "
                "everything after it is unreachable and the whole file fails to load")
    return detail


def main():
    names = scripts()
    ck("payload scripts exist", bool(names), f"{len(names)} file(s) under assets/debugmenu/scripts")

    if not os.access(LUAC, os.X_OK):
        ck("LuaDec kit present", False, "build/luadec-p2/bin/luac missing - run bash tools/build_luadec.sh")
    else:
        bad = []
        for fn in names:
            state, detail = check_source(os.path.join(PAYLOAD, fn))
            if state != "ok":
                bad.append(f"{fn}: {explain(detail)}")
        ck("every payload script compiles", not bad, "; ".join(bad))


    if fails:
        print("debug-scripts: FAILED", ", ".join(fails))
        return 1
    print("debug-scripts: all payload scripts parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
