#!/usr/bin/env python3
"""Attribute every `differs` record to a cause, so the decompiler work can be ordered by payoff.

`branch_report` classifies the *shape* of a divergence (structural vs layout) and counts marker messages. That is
not enough to choose what to fix: "393 structural records" is one number, while the fixable defects behind it are a
handful. This walks each record, finds the first divergence, and tags it with the defect it looks like:

  constant-order   instructions identical, only the constant table differs (soundly convertible by value)
  constructor-call a table constructor that spans a CALL before its SETLIST - the diagnosed DeclareLocals bug
  pending-register the decompiler reports "Overwrote pending register"
  register-confusion "Confused about usage of register" without any of the above
  control-flow     "LeaveBlock: unexpected jumping out ..." - real control-flow reconstruction
  silent           no marker at all, and not the constructor pattern: needs instruction-level study
  no-decompile     LuaDec produced nothing parseable

No game Lua text is stored in the report: mission, role, record index, prototype count, instruction delta, the
markers by family, and the cause. `artifacts/cause-report.json` is safe to publish under the project's rule.
"""
import argparse
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("PROTO2_NO_CACHE", "1")

import branch_report as BR      # noqa: E402
import hostpaths as HP         # noqa: E402
import lua51 as L51             # noqa: E402
import lua_equiv as EQ          # noqa: E402
import recovery_report as R     # noqa: E402
import script_edit as SE        # noqa: E402

OUT = os.path.join(ROOT, "artifacts", "cause-report.json")

FAMILY = (
    ("constant-order", re.compile(r"^$")),
    ("control-flow", re.compile(r"LeaveBlock")),
    ("pending-register", re.compile(r"Overwrote pending register")),
    ("register-confusion", re.compile(r"Confused about usage of register")),
    ("constructor-call", re.compile(r"No list found|SetList")),
)


def constructor_through_call(proto):
    """True when a NEWTABLE's register is filled by a SETLIST after an intervening CALL.

    That is the shape of `t = { f() }`: the engine builds the table, the call runs (pushing its results), and SETLIST
    then consumes everything above the table register. LuaDec materialises the pending table before the call and
    loses it, which shows up as "No list found" - but the marker is not always emitted, so the bytecode is checked
    as well.
    """
    toks = EQ.tokens(proto)
    pending = {}
    for i, t in enumerate(toks):
        if t[0] == "NEWTABLE":
            pending[t[1]] = i
        elif t[0] in ("SETLIST",) and t[1] in pending:
            start = pending.pop(t[1])
            window = toks[start:i]
            if any(w[0] in ("CALL", "TAILCALL") for w in window):
                return True
    return False


def classify(mission, variant, record, role, chunk, src, recomp, got):
    marks = BR.markers(src)
    families = collections.Counter()
    for m in marks:
        for name, pat in FAMILY:
            if pat.search(m):
                families[name] += 1
                break
    protos = list(L51.walk(L51.load(chunk)))
    ctor = any(constructor_through_call(p) for p in protos)
    if got["status"] == "identical":
        cause = "constant-order"
    elif ctor or families["constructor-call"]:
        cause = "constructor-call"
    elif families["pending-register"]:
        cause = "pending-register"
    elif families["control-flow"]:
        cause = "control-flow"
    elif families["register-confusion"]:
        cause = "register-confusion"
    elif marks:
        cause = "other-marker"
    else:
        cause = "silent"
    return {"mission": mission, "variant": variant, "record": record, "role": role,
            "cause": cause, "class": got["status"], "detail": HP.scrub(got["detail"])[:120],
            "markers": len(marks), "families": dict(families),
            "protos": len(protos),
            "constructor_pattern": ctor}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write artifacts/cause-report.json")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)

    records = []
    for mission, variant, model in R.missions():
        rows = SE.records_with_roles(model)
        dec = SE._decoded(model)
        for i, ((it, payload, role), (_r, text, _h, _x)) in enumerate(zip(rows, dec)):
            if text is not None:
                continue
            chunk = SE.bytecode_chunk(payload)
            if chunk is None:
                continue
            if SE.roundtrip_equivalent(payload)[0] == "equivalent":
                continue
            src, note = SE.decompile_payload(payload)
            if src is None:
                records.append({"mission": mission, "variant": variant, "record": i, "role": role,
                                "cause": "no-decompile", "class": "no-decompile", "detail": HP.scrub(note)[:120],
                                "markers": 0, "families": {}, "protos": 0, "constructor_pattern": False})
                continue
            recomp, why = BR.recompile(src)
            if recomp is None:
                records.append({"mission": mission, "variant": variant, "record": i, "role": role,
                                "cause": "compile-failed", "class": "compile-failed", "detail": HP.scrub(why)[:120],
                                "markers": 0, "families": {}, "protos": 0, "constructor_pattern": False})
                continue
            got = EQ.analyse(chunk, recomp)
            records.append(classify(mission, variant, i, role, chunk, src, recomp, got))
            if a.limit and len(records) >= a.limit:
                break
        if a.limit and len(records) >= a.limit:
            break

    causes = collections.Counter(r["cause"] for r in records)
    fams = collections.Counter()
    for r in records:
        fams.update(r.get("families") or {})
    print(f"records attributed   {len(records)}" + ("  (partial)" if a.limit else ""))
    for cause, n in causes.most_common():
        print(f"  {cause:<20} {n:>4}  {100.0 * n / max(1, len(records)):5.1f}%")
    print(f"\nconstructor pattern present (bytecode): "
          f"{sum(1 for r in records if r.get('constructor_pattern'))}")
    print("marker families:", dict(fams.most_common()))

    if a.write:
        json.dump({"total": len(records), "causes": dict(causes), "marker_families": dict(fams),
                   "records": records}, open(OUT, "w"), indent=1, sort_keys=True)
        print(f"\nwrote {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
