#!/usr/bin/env python3
"""Self-check for the engine Lua library index (docs/21).

Proves the index is real rather than a stale file: the host entry still exists in the shipped archive,
every indexed chunk loads as Lua 5.1, and a library function that mission scripts call is findable.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import library_index as LI                     # noqa: E402
import lua51 as L                              # noqa: E402

failures = []


def ck(name, ok, detail=""):
    if not ok:
        failures.append(f"{name}: {detail}".rstrip(": "))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")


try:
    data = LI._host_bytes()
except SystemExit as exc:
    print(f"FAIL  host library container: {exc}")
    sys.exit(1)

ck("the host container is present", len(data) > 1_000_000, f"{len(data)} bytes")

index = LI._load_index()
ck("the index is populated", len(index) >= 820, f"{len(index)} chunks")

missing = [r["key"] for r in index if not os.path.exists(os.path.join(LI.LIB_DIR, r["key"] + ".luac"))]
ck("every indexed chunk is on disk", not missing, f"{len(missing)} missing")

bad = []
for r in index:
    chunk = open(os.path.join(LI.LIB_DIR, r["key"] + ".luac"), "rb").read()
    try:
        L.load(chunk)
    except Exception as exc:
        bad.append((r["key"], str(exc)[:40]))
ck("every chunk loads as Lua 5.1", not bad, f"{len(bad)} bad, first {bad[:1]}")

names = {g for r in index for g in r["globals"]}
for want in ("mission_DefineRetryStates",):
    ck(f"library defines {want}", want in names, "" if want in names else "not found in any chunk's globals")

print(f"{'ok' if not failures else 'FAILED'}: {len(index)} chunks, "
      f"{sum(r['insns'] for r in index)} instructions, {len(names)} library globals")
sys.exit(0 if not failures else 1)
