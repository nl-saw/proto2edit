#!/usr/bin/env python3
"""The override pack: the 43 records that cannot be regenerated from your own game files.

WHY THIS EXISTS. The shipped tooling converts 951 bytecode records automatically - decompile, recompile,
compare, and accept only if the instruction stream is identical (the recovery rules in tools/refold.py
cover 8 of the records that used to need hand work; docs/32). Those need nothing from this pack: run
`./proto2 corpus` and `./proto2 convert-all` against your own install and you get them.

The remaining 43 records are the ones no decompiler reproduces. They were reconstructed by hand from their
bytecode listings, and that work cannot be re-derived by a program. This pack carries it - **locally**.

WHAT IT IS. One directory of Lua text plus a manifest. Each entry records the mission, variant, record
index, role, container form, the sha256 of the **shipped** chunk it applies to, and the sha256 of the
source. Nothing else: no archives, no bytecode, no game data. `apply` refuses any record whose shipped
chunk does not match, so the pack is inert without a matching install and useless without the game.

The pack **ships with this repository** in `overrides/` (docs/26): 43 records written here, not decompiler
output. A clone converts 951 of the 994 bytecode records with the decompiler alone; the pack covers the other
43. See docs/26 and docs/32.

    ./proto2 overrides export [--dest overrides]     # build the pack from what is staged now
    ./proto2 overrides apply  [--pack overrides]     # verify against your install, then stage
    ./proto2 overrides list   [--pack overrides]     # what the pack contains
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import script_edit as SE                          # noqa: E402
import recovery_report as R                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PACK = os.path.join(ROOT, "overrides")
MANIFEST = "manifest.json"
VERSION = 1


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _source_bytes(text):
    return text.encode("latin-1") if isinstance(text, str) else bytes(text)


def _clean(src):
    """Drop LuaDec's banner lines, which name the machine that ran it.

    LuaDec prefixes its output with `-- Decompiled using luadec ...` and `-- Command line: /mnt/.../a.luac`. The
    comments do not affect the compiled chunk, so a reconstruction accepted by the gate can carry them - and a
    developer path inside a published file is exactly what the leak gate refuses. Strip them on export.
    """
    keep = []
    for line in _source_bytes(src).decode("latin-1").splitlines(True):
        stripped = line.lstrip()
        if stripped.startswith("-- Decompiled using") or stripped.startswith("-- Command line:"):
            continue
        keep.append(line)
    return "".join(keep).encode("latin-1")


def _verdict(payload):
    cached = SE.cached_verdict(payload)
    if cached:
        return cached
    status, _detail = SE.roundtrip_equivalent(payload)
    return status


def _changed_records():
    """(mission, variant, index, role, form, shipped payload, staged text) for hand-only records."""
    for mission, variant, _model in R.missions():
        staged = SE.open_side(mission, variant, "staged")
        shipped = SE.open_side(mission, variant, "shipped")
        if staged is None or shipped is None:
            continue
        s_rows = SE.records_with_roles(staged[3])
        h_rows = SE.records_with_roles(shipped[3])
        dec = SE._decoded(staged[3])
        for i, ((it, payload, role), (_hit, hpayload, _hrole)) in enumerate(zip(s_rows, h_rows)):
            if bytes(payload) == bytes(hpayload):
                continue
            text = dec[i][1]
            if text is None:
                continue
            if _verdict(hpayload) == "equivalent":
                continue                      # regenerable from the user's own files; not shipped
            yield (mission, variant, i, role, (it.get("fields") or {}).get("form"),
                   hpayload, _clean(text))


def cmd_export(args):
    dest = args.dest
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(dest):
        if name.endswith(".lua"):
            os.remove(os.path.join(dest, name))
    records = []
    total = 0
    for mission, variant, index, role, form, hpayload, src in _changed_records():
        chunk = SE.bytecode_chunk(hpayload)
        if chunk is None:
            print(f"  skip {mission}/{variant}#{index}: shipped payload is not a Lua chunk")
            continue
        name = _sha(chunk)[:16] + ".lua"
        open(os.path.join(dest, name), "wb").write(src)
        records.append({
            "mission": mission,
            "variant": variant,
            "index": index,
            "role": role,
            "form": form,
            "file": name,
            "chunk_sha256": _sha(chunk),
            "source_sha256": _sha(src),
            "source_bytes": len(src),
        })
        total += len(src)
    records.sort(key=lambda r: (r["mission"], r["variant"], r["index"]))
    json.dump({"version": VERSION, "records": records},
              open(os.path.join(dest, MANIFEST), "w"), indent=1)
    print(f"exported {len(records)} records, {total} bytes of source -> {os.path.relpath(dest, ROOT)}/")
    print(f"  {MANIFEST} pins each record to the sha256 of its shipped chunk")
    return 0


def _load_pack(path):
    manifest_path = os.path.join(path, MANIFEST)
    if not os.path.exists(manifest_path):
        raise SystemExit(f"no pack at {path}: {MANIFEST} missing\n"
                         "  the pack ships in overrides/ (docs/26); without it the 43 hand-written records\n"
                         "  stay bytecode. Recreate it with './proto2 overrides export' from a staged copy.")
    data = json.load(open(manifest_path))
    if data.get("version") != VERSION:
        raise SystemExit(f"pack version {data.get('version')} is not {VERSION}")
    return data["records"]


def find(mission, variant, index, path=DEFAULT_PACK):
    """The pack entry for one record, or None.

    Callers use this to tell a user why a record reads as read-only even though a hand-written reconstruction for it
    exists: without the hint the UI just says the decompilation is unverified, which is true but misleading.
    """
    try:
        for r in _load_pack(path):
            if r["mission"] == mission and r["variant"] == variant and r["index"] == index:
                return r
    except SystemExit:
        return None
    return None


def cmd_list(args):
    records = _load_pack(args.pack)
    print(f"{'mission':34} {'#':>4} {'role':14} {'form':9} {'bytes':>6}")
    for r in records:
        print(f"{r['mission']:34} {r['index']:>4} {r['role']:14} {str(r['form']):9} {r['source_bytes']:>6}")
    print(f"{len(records)} records, {sum(r['source_bytes'] for r in records)} bytes of source")
    return 0


def apply_pack(pack, verbose=True):
    """Stage every pack record that this install can accept. Returns (applied, already_in_place)."""
    records = _load_pack(pack)
    applied = skipped = mismatched = 0
    problems = []
    for r in records:
        mission, variant, index = r["mission"], r["variant"], r["index"]
        src_path = os.path.join(pack, r["file"])
        if not os.path.exists(src_path):
            problems.append(f"{mission}#{index}: missing {r['file']}")
            continue
        src = open(src_path, "rb").read()
        if _sha(src) != r["source_sha256"]:
            problems.append(f"{mission}#{index}: source does not match the manifest")
            continue
        shipped = SE.open_side(mission, variant, "shipped")
        if shipped is None:
            problems.append(f"{mission}#{index}: mission not in this install")
            continue
        rows = SE.records_with_roles(shipped[3])
        if not (0 <= index < len(rows)):
            problems.append(f"{mission}#{index}: index out of range for this install")
            continue
        _it, hpayload, _role = rows[index]
        chunk = SE.bytecode_chunk(hpayload)
        if chunk is None or _sha(chunk) != r["chunk_sha256"]:
            mismatched += 1
            problems.append(f"{mission}#{index}: shipped chunk differs from the pack (different game build?)")
            continue
        staged = SE.open_side(mission, variant, "staged")
        if staged is not None:
            dec = SE._decoded(staged[3])
            current = dec[index][1] if index < len(dec) else None
            if current is not None and _source_bytes(current) == src:
                skipped += 1
                continue
        with contextlib.redirect_stdout(io.StringIO()):
            SE.stage_record_source(mission, variant, index, src.decode("latin-1"))
        applied += 1
    if verbose:
        print(f"applied {applied}, already in place {skipped}, refused {mismatched}")
        for p in problems[:12]:
            print(f"  {p}")
        if len(problems) > 12:
            print(f"  ... {len(problems) - 12} more")
    return applied, skipped, problems


def cmd_apply(args):
    applied, skipped, problems = apply_pack(args.pack)
    print(f"applied {applied}, already in place {skipped}, refused {len(problems)}")
    for p in problems[:12]:
        print(f"  {p}")
    if len(problems) > 12:
        print(f"  ... {len(problems) - 12} more")
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="the hand-reconstructed override pack")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("export", help="build the pack from the current staging")
    p.add_argument("--dest", default=DEFAULT_PACK)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("apply", help="verify against this install and stage the records")
    p.add_argument("--pack", default=DEFAULT_PACK)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("list", help="what the pack contains")
    p.add_argument("--pack", default=DEFAULT_PACK)
    p.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
