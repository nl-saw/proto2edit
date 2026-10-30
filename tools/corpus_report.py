#!/usr/bin/env python3
"""Corpus-level measurements that used to be ad-hoc Python: coverage and duplicate checks.

    python3 tools/corpus_report.py coverage [--top N|--all] [--json PATH] [--convertible]
    python3 tools/corpus_report.py dupes

`coverage` classifies every mission record (source text / Lua bytecode / unknown), deduplicated by
mission+variant. `--convertible` additionally runs the decompile/recompile equivalence test per bytecode
record (slow: it invokes LuaDec for each one).

`dupes` fails when the same mission+variant exists in more than one extracted tree, which silently inflated
the totals in the past.
"""
import argparse
import collections
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import script_edit as SE

ROOT = SE.ROOT
CORPUS = SE.CORPUS


MISSING = """no extracted corpus under {where}.

tmp/ is scratch and may have been cleaned; rebuild it from the game archives in one command:

    ./proto2 corpus

Then re-run this tool."""


def require_corpus():
    if not os.path.isdir(CORPUS) or not any(
            "fig.p3d.rz" in names for _dp, _d, names in os.walk(CORPUS)):
        raise SystemExit(MISSING.format(where=os.path.relpath(CORPUS, ROOT)))


def variants():
    """[(mission, variant, path)] with duplicates collapsed onto the canonical copy."""
    require_corpus()
    found = collections.defaultdict(list)
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        parts = os.path.normpath(dirpath).split(os.sep)
        found[(parts[-2], parts[-1])].append(os.path.join(dirpath, "fig.p3d.rz"))
    out = []
    for (mission, variant), paths in sorted(found.items()):
        chosen = SE.find_mission(mission, variant)
        out.append((mission, variant, chosen))
    return out, found


def classify(mission, variant):
    got = SE.open_side(mission, variant, "shipped")
    if got is None:
        return None
    counts = collections.Counter()
    rows = SE.records_with_roles(got[3])
    for (it, payload, _role), (_r, text, _h, _x) in zip(rows, SE._decoded(got[3])):
        counts[SE.record_kind(payload, text)] += 1
    return counts, rows


def cmd_coverage(a):
    vs, found = variants()
    totals = collections.Counter()
    per = []
    for mission, variant, path in vs:
        got = classify(mission, variant)
        if got is None:
            continue
        counts, _rows = got
        for k, n in counts.items():
            totals[k] += n
        origin = os.path.relpath(path, CORPUS).split(os.sep)[0]
        per.append({"mission": mission, "variant": variant, "archive": origin,
                    **{k: counts.get(k, 0) for k in ("source", "bytecode", "unknown")}})
    n = sum(totals.values())
    print(f"mission/variants : {len(per)}")
    print(f"records          : {n}")
    for k in ("source", "bytecode", "unknown"):
        print(f"  {k:<9} {totals[k]:>5}  ({100.0 * totals[k] / max(1, n):.1f}%)")
    cache = os.path.join(ROOT, "artifacts", "lua-cache.json")
    if os.path.exists(cache):
        print(f"runtime cache    : {len(json.load(open(cache)))} stream(s)")

    ranked = sorted(per, key=lambda r: (-r["unknown"], r["mission"]))
    shown = ranked if a.all else ranked[:a.top]
    if shown:
        print(f"\n{'mission':<24} {'var':<7} {'src':>4} {'bc':>4} {'unk':>4}  archive")
        for r in shown:
            print(f"{r['mission']:<24} {r['variant']:<7} {r['source']:>4} {r['bytecode']:>4} "
                  f"{r['unknown']:>4}  {r['archive']}")

    if a.convertible:
        print("\nconvertibility of bytecode records (decompile -> recompile -> compare):")
        tot = collections.Counter()
        for r in shown:
            if not r["bytecode"]:
                continue
            got = classify(r["mission"], r["variant"])
            counts = collections.Counter()
            for (it, payload, _role), (_rl, text, _h, _x) in zip(got[1], SE._decoded(
                    SE.open_side(r["mission"], r["variant"], "shipped")[3])):
                if text is not None:
                    continue
                if SE.bytecode_chunk(payload) is None:
                    counts["nochunk"] += 1
                else:
                    counts[SE.roundtrip_equivalent(payload)[0]] += 1
            tot.update(counts)
            print(f"  {r['mission']:<24} {r['variant']:<7} equivalent {counts['equivalent']:>3}  "
                  f"differs {counts['differs']:>3}  nochunk {counts['nochunk']:>3}")
        print(f"  total: {dict(tot)}")

    if a.json:
        os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
        with open(a.json, "w") as fh:
            json.dump({"totals": dict(totals), "missions": per, "duplicates": len(vs) != len(found)},
                      fh, indent=1, sort_keys=True)
        print(f"\nwrote {a.json}")
    return 0


def cmd_dupes(a):
    vs, found = variants()
    dupes = {k: v for k, v in found.items() if len(v) > 1}
    print(f"mission/variants : {len(found)}")
    print(f"duplicated       : {len(dupes)}")
    bad = 0
    for (mission, variant), paths in sorted(dupes.items()):
        hashes = [hashlib.sha256(open(p, "rb").read()).hexdigest()[:12] for p in paths]
        same = len(set(hashes)) == 1
        bad += 0 if same else 1
        print(f"  {mission}/{variant}: {len(paths)} copies, identical={same}")
        for h, p in zip(hashes, paths):
            print(f"      {h}  {os.path.relpath(p, CORPUS)}")
    if not dupes:
        print("corpus is canonical: every mission/variant exists exactly once")
    return 0 if bad == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coverage")
    c.add_argument("--top", type=int, default=15)
    c.add_argument("--all", action="store_true")
    c.add_argument("--json", default=None)
    c.add_argument("--convertible", action="store_true")
    c.set_defaults(func=cmd_coverage)
    d = sub.add_parser("dupes")
    d.set_defaults(func=cmd_dupes)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
