#!/usr/bin/env python3
"""Measure a LuaDec kit against the shipped corpus, and refuse to call a change an improvement if it breaks
anything that already converts.

This is the safety net for touching the decompiler. The rule it enforces is asymmetric on purpose: a patch may
gain records, but if a single record that is provably equivalent today stops being equivalent, the run fails.

    python3 tools/luadec_bench.py                     # current kit, compared with the recorded verdicts
    PROTO2_LUADEC_DIR=build/candidate/bin \
        python3 tools/luadec_bench.py                 # a candidate kit, same comparison

Exit status: 0 when nothing regressed, 1 when at least one record regressed. Gains are reported either way.
The recorded baseline is `artifacts/bytecode-verdicts.json`, keyed by chunk sha1, which `./proto2 progress`
maintains; this script only reads it.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N bytecode records (debug)")
    ap.add_argument("--kit", help="LuaDec kit directory (default: the pinned build)")
    a = ap.parse_args(argv)

    os.environ["PROTO2_NO_CACHE"] = "1"          # measure the shipped corpus, not the playthrough cache
    if a.kit:
        os.environ["PROTO2_LUADEC_DIR"] = os.path.abspath(a.kit)
    import lua51 as L51
    import recovery_report as R
    import script_edit as SE

    kit = SE.LUADEC_DIR
    luac = os.path.join(kit, "luac")
    if not (os.path.exists(os.path.join(kit, "luadec")) and os.path.exists(luac)):
        raise SystemExit(f"not a kit: {kit}")

    baseline = {}
    if os.path.exists(R.CACHE):
        baseline = {k: v["status"] for k, v in json.load(open(R.CACHE)).items()}

    counts = {"equivalent": 0, "differs": 0, "no-decompile": 0, "compile-failed": 0, "nochunk": 0}
    gained, regressed = [], []
    seen = 0
    for mission, variant, model in R.missions():
        for (it, payload, role), (_r, text, _h, _x) in zip(SE.records_with_roles(model), SE._decoded(model)):
            if text is not None:
                continue
            chunk = SE.bytecode_chunk(payload)
            if chunk is None:
                counts["nochunk"] += 1
                continue
            key = hashlib.sha1(chunk).hexdigest()
            src, kind = SE.verified_source(payload)
            if src is not None:
                status = "equivalent"
            elif kind == "no captured chunk":
                status = "nochunk"
            elif "no captured chunk" in kind or "decompile" in kind or "luadec" in kind:
                status = "no-decompile"
            else:
                status = "differs"
            counts[status] += 1
            was = baseline.get(key)
            if was == "equivalent" and status != "equivalent":
                regressed.append((mission, variant, role, status))
            elif was and was != "equivalent" and status == "equivalent":
                gained.append((mission, variant, role, was))
            seen += 1
            if a.limit and seen >= a.limit:
                break
        if a.limit and seen >= a.limit:
            break

    print(f"kit       {os.path.relpath(kit, ROOT)}")
    print(f"records   {seen} analysed" + ("  (partial)" if a.limit else ""))
    for k in ("equivalent", "differs", "no-decompile", "compile-failed", "nochunk"):
        if counts[k]:
            print(f"  {k:<15} {counts[k]:>4}")
    print(f"gained    {len(gained)}" + ("" if not gained else "  " + ", ".join(
        f"{m}/{v} {r} was {w}" for m, v, r, w in gained[:5])))
    print(f"regressed {len(regressed)}")
    for m, v, r, s in regressed[:10]:
        print(f"  {m}/{v} {r}: now {s}")

    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
