#!/usr/bin/env python3
"""Self-check for the override pack (docs/26).

The pack ships in `overrides/` (docs/26): 43 records written here, so it gets its own guard: the manifest
must describe real files, every file must match
its declared hash, and every entry must pin the shipped chunk it applies to. A pack that drifts from its
manifest would apply the wrong source to a record. A clone without the pack skips this check - the 43 records
it does not carry stay bytecode there, which is the documented trade-off.
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overrides as OV                            # noqa: E402

fails = []


def ck(name, ok, detail=""):
    if not ok:
        fails.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")


pack = OV.DEFAULT_PACK
manifest_path = os.path.join(pack, OV.MANIFEST)
if not os.path.exists(manifest_path):
    print("overrides: no pack in this checkout (docs/26) - skipped")
    sys.exit(0)
ck("the pack has a manifest", True, os.path.relpath(manifest_path, OV.ROOT))

data = json.load(open(manifest_path))
records = data.get("records") or []
ck("the manifest has records", len(records) > 0, f"{len(records)} records")
ck("every record pins a shipped chunk", all(len(r.get("chunk_sha256", "")) == 64 for r in records))
ck("every record names a form", all(r.get("form") in ("bare", "prefixed") for r in records))

missing, drifted = [], []
for r in records:
    path = os.path.join(pack, r["file"])
    if not os.path.exists(path):
        missing.append(r["file"])
        continue
    body = open(path, "rb").read()
    if hashlib.sha256(body).hexdigest() != r["source_sha256"]:
        drifted.append(r["file"])
    if len(body) != r["source_bytes"]:
        drifted.append(r["file"])
ck("every record's file is present", not missing, f"{len(missing)} missing")
ck("every file matches its manifest hash", not drifted, f"{len(drifted)} drifted")

keys = [(r["mission"], r["variant"], r["index"]) for r in records]
ck("no record appears twice", len(keys) == len(set(keys)), f"{len(keys)} entries")

# The pack must stay inert without the game: no archives, no chunks, no host paths.
bad = [n for n in os.listdir(pack)
       if n != OV.MANIFEST and not n.endswith(".lua")]
ck("the pack holds only Lua text and its manifest", not bad, ", ".join(bad[:3]))


# Guard against the failure mode that bit us on 2026-10-27: reverting a hand-reconstructed record leaves it as
# bytecode, the decompiler refuses it ("unreachable block"), and the working copy carries a blocked record until
# someone remembers to run './proto2 overrides apply'. Fail loudly instead of drifting silently.
try:
    import script_edit as SE
    stuck = []
    for r in records:
        got = SE.open_side(r["mission"], r["variant"], "working")
        if got is None:
            continue
        rows = SE.records_with_roles(got[3])
        if not (0 <= r["index"] < len(rows)):
            continue
        if SE._decoded(got[3])[r["index"]][1] is None:
            stuck.append(f'{r["mission"]}#{r["index"]}')
    # A clean tree (./proto2 clean, or a fresh clone) has none of the pack applied, which is a valid state: the
    # guard is against a HALF-applied pack, where some records carry source and others are still bytecode.
    if len(stuck) == len(records):
        ck("every pack record is source in the working copy", True,
           "pack not applied (clean tree); ./proto2 overrides apply stages it")
    else:
        ck("every pack record is source in the working copy", not stuck,
           ("bytecode left in: " + ", ".join(stuck)) if stuck else f"{len(records)} checked")
except Exception as exc:
    ck("every pack record is source in the working copy", False, f"could not check: {exc}")

print(f"{'ok' if not fails else 'FAILED'}: {len(records)} records, "
      f"{sum(r['source_bytes'] for r in records)} bytes of source")
sys.exit(0 if not fails else 1)
