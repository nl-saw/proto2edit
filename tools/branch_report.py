#!/usr/bin/env python3
"""Classify every `differs` record: which are proof candidates, and which need the decompiler.

For each record whose round-trip verdict is not `identical`, decompile it, recompile the result, and compare the
two chunks with `lua_equiv`. The point is to replace "393 records differ" with a worklist:

  layout      the same instructions and control-flow graph under a register renaming. A mechanical proof can
              discharge these; they are not decompiler bugs.
  structural  opcodes, blocks or operands do not correspond. The decompiled source is wrong, and each such record
              is decompiler work (route A).

`--write` stores the per-record detail in artifacts/branch-diffs.json so the classification is evidence, not a
console line that scrolls away.
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lua51 as L51           # noqa: E402
import hostpaths as HP         # noqa: E402
import lua_equiv as EQ        # noqa: E402
import recovery_report as R   # noqa: E402
import script_edit as SE      # noqa: E402

ROOT = SE.ROOT
OUT = os.path.join(ROOT, "artifacts", "branch-diffs.json")
MARK = re.compile(r"DECOMPILER ERROR at PC(\d+):\s*(.+)")
TRAILER = re.compile(r"DECOMPILER ERROR:\s*(.+)")


def recompile(src):
    """Bytes of the chunk the decompiled source compiles to, or (None, reason)."""
    luac = os.path.join(SE.LUADEC_DIR, "luac")
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "tmp")) as td:
        lua = os.path.join(td, "a.lua")
        out = os.path.join(td, "a.luac")
        open(lua, "w", encoding="latin-1").write(src)
        # no -s: the debug info is what maps a divergent instruction back to a line of the decompiled source
        c = subprocess.run([luac, "-o", out, lua], capture_output=True, text=True)
        if c.returncode != 0:
            return None, (c.stderr.strip().splitlines() or ["luac failed"])[0]
        return open(out, "rb").read(), ""


def markers(src):
    return [m.group(2).strip() for m in MARK.finditer(src)]


def first_divergence(pa, pb, src):
    """(instruction index, source line text) of the first differing token, best effort."""
    ta = EQ.tokens(pa)
    tb = EQ.tokens(pb)
    for i in range(min(len(ta), len(tb))):
        if ta[i] != tb[i]:
            line = pb.line_info[i] if i < len(pb.line_info) else 0
            text = ""
            if line:
                lines = src.splitlines()
                if 0 < line <= len(lines):
                    text = lines[line - 1].strip()[:100]
            return i, line, text
    return None, None, ""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N records (debug)")
    ap.add_argument("--write", action="store_true", help="write artifacts/branch-diffs.json")
    ap.add_argument("--mission", help="only this mission")
    a = ap.parse_args(argv)

    records = []
    classes = collections.Counter()
    messages = collections.Counter()
    with_marker = 0
    for mission, variant, model in R.missions():
        if a.mission and mission != a.mission:
            continue
        rows = SE.records_with_roles(model)
        dec = SE._decoded(model)
        for i, ((it, payload, role), (_r, text, _how, _x)) in enumerate(zip(rows, dec)):
            if text is not None:
                continue
            chunk = SE.bytecode_chunk(payload)
            if chunk is None:
                continue
            status, _detail = SE.roundtrip_equivalent(payload)
            if status == "equivalent":
                continue
            src, note = SE.decompile_payload(payload)
            if src is None:
                classes["no-decompile"] += 1
                records.append({"mission": mission, "variant": variant, "record": i, "role": role,
                                "class": "no-decompile", "detail": note})
                continue
            recomp, why = recompile(src)
            if recomp is None:
                classes["compile-failed"] += 1
                records.append({"mission": mission, "variant": variant, "record": i, "role": role,
                                "class": "compile-failed", "detail": why})
                continue
            got = EQ.analyse(chunk, recomp)
            klass = got["status"]
            marks = markers(src)
            if marks:
                with_marker += 1
            for m in marks:
                messages[re.sub(r"R\d+|'[^']*'|\b\d+\b", "#", m).strip()[:70]] += 1
            classes[klass] += 1
            pa, pb = L51.load(chunk), L51.load(recomp)
            pc, line, text_line = first_divergence(list(L51.walk(pa))[0], list(L51.walk(pb))[0], src)
            records.append({"mission": mission, "variant": variant, "record": i, "role": role,
                            "class": klass, "detail": HP.scrub(got["detail"]),
                            "markers": len(marks), "pc": pc, "line": line, "source": text_line,
                            "protos": len(list(L51.walk(pa)))})
            if a.limit and len(records) >= a.limit:
                break
        if a.limit and len(records) >= a.limit:
            break

    total = sum(classes.values())
    print(f"records analysed   {total}" + ("  (partial)" if a.limit else ""))
    for k, n in classes.most_common():
        print(f"  {k:<16} {n:>4}  ({100.0 * n / max(1, total):.1f}%)")
    print(f"with DECOMPILER ERROR markers  {with_marker} / {total}")

    if messages:
        print("\ntop decompiler messages (structural worklist):")
        for m, n in messages.most_common(8):
            print(f"  {n:>3}  {m}")

    structural = [r for r in records if r["class"] == "structural"]
    if structural:
        per = collections.Counter(r["mission"] for r in structural)
        print("\nstructural records by mission:")
        for m, n in per.most_common(10):
            print(f"  {m:<26} {n}")

    if a.write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        payload = {"corpus": {"records": total, "classes": dict(classes), "with_markers": with_marker},
                   "markers": dict(messages), "records": records}
        json.dump(payload, open(OUT, "w"), indent=1, sort_keys=True)
        print(f"\nwrote {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
