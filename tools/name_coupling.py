#!/usr/bin/env python3
"""How much of a mission's own name is baked into its data, and how much is symbolic?

Cloning a mission under a new name only works if we know every place the old name appears. Two very different
answers are possible and they imply very different work:

  symbolic   the name is threaded through `MISSION_NAME`, `activationData.rootPath` and `UID(name)`, so a clone
             needs almost no rewriting - the engine supplies the name.
  baked      the name is a literal (`"structure_yellow_group001|..."`, `$A1_...`, `story_a1`) inside records,
             address paths and text-bible keys, so every occurrence has to be rewritten and each one is a way to
             leave the clone half-renamed.

This measures the split instead of guessing: a raw substring count of the mission name in its own `fig.p3d.rz`
(Lua text and bytecode string constants alike), its `tod.p3d.rz`, the text bible and `missiondefinit.lua`.

    python3 tools/name_coupling.py                # whole corpus
    python3 tools/name_coupling.py --limit 40     # quick look
    python3 tools/name_coupling.py --json OUT     # machine-readable (default: work/lua/name-coupling.json)

Raw counts are an indicator, not a proof: a hit can be a comment, and a count of 1 does not say the reference is
load-bearing. Read it as "how much rewriting would a clone face", then confirm the load-bearing ones by eye.
"""
import argparse
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import rz  # noqa: E402
import lua51  # noqa: E402

CORPUS = os.path.join(ROOT, "tmp", "corpus")
CHUNK = os.path.join(ROOT, "work", "extracted", "boot", "scripts", "missiondefinit.lua")
DEFAULT_JSON = os.path.join(ROOT, "work", "lua", "name-coupling.json")


def packages(limit=0):
    out = []
    for dp, _dirs, files in os.walk(CORPUS):
        if "fig.p3d.rz" not in files:
            continue
        mission = os.path.basename(os.path.dirname(dp))
        variant = os.path.basename(dp)
        out.append((mission, variant, dp))
    out.sort()
    return out[:limit] if limit else out


def inner(path):
    try:
        blob = open(path, "rb").read()
    except OSError:
        return b""
    if blob[:2] == b"RZ":
        try:
            return rz.unwrap(blob)
        except Exception:
            return b""
    return blob


def bible_strings():
    """Every key and value in the English bible, as bytes, for counting."""
    try:
        import mission_gui as G
        _raw, _hs, h = G.Model(lambda _m: None).bible()
        out = set()
        for i, key in enumerate(h.keys):
            out.add(key.encode("latin-1", "replace"))
            value = h.value(i)
            if value:
                out.add(value.encode("latin-1", "replace"))
        return out
    except Exception:
        return set()


def chunk_constants():
    try:
        root = lua51.load(open(CHUNK, "rb").read())
        return [c for p in lua51.walk(root) for c in p.constants if isinstance(c, bytes)]
    except Exception:
        return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default=DEFAULT_JSON)
    a = ap.parse_args(argv)

    pkgs = packages(a.limit)
    if not pkgs:
        raise SystemExit("no corpus; run ./proto2 corpus")

    bible = bible_strings()
    chunk = chunk_constants()
    rows = []
    for mission, variant, dp in pkgs:
        name = mission.encode("latin-1", "replace")
        fig = inner(os.path.join(dp, "fig.p3d.rz"))
        tod = inner(os.path.join(dp, "tod.p3d.rz"))
        # one occurrence per package is the block header's own name (`<mission>_<variant>`), not a reference in
        # Lua; keeping it separate turns "442 literals" into "442 minus one per package"
        header = f"{mission}_{variant}".encode("latin-1", "replace")
        header_literals = fig.count(header)
        rows.append({
            "mission": mission, "variant": variant,
            "header_literals": header_literals,
            "fig_literals": fig.count(name) - header_literals,
            "tod_literals": tod.count(name),
            "bible_literals": sum(s.count(name) for s in bible),
            "definit_literals": sum(c.count(name) for c in chunk),
        })

    def dist(key):
        d = collections.Counter()
        for r in rows:
            n = r[key]
            d["0" if n == 0 else "1-2" if n <= 2 else "3-9" if n <= 9 else "10+"] += 1
        return {k: d[k] for k in ("0", "1-2", "3-9", "10+") if d[k]}

    print(f"packages scanned: {len(rows)}")
    print(f"{'location':16s} {'total':>7s}  distribution (0 / 1-2 / 3-9 / 10+)")
    for key, label in (("header_literals", "fig header"), ("fig_literals", "fig payload"),
                       ("tod_literals", "tod.p3d.rz"),
                       ("bible_literals", "text bible"), ("definit_literals", "missiondefinit")):
        total = sum(r[key] for r in rows)
        d = dist(key)
        print(f"{label:16s} {total:7d}  " + " / ".join(str(d.get(k, 0)) for k in ("0", "1-2", "3-9", "10+")))
    zero_fig = sum(1 for r in rows if r["fig_literals"] == 0)
    print(f"\npackages with no name reference outside the header: {zero_fig} of {len(rows)} "
          f"({100 * zero_fig / len(rows):.0f}%) - for those a clone rewrites the header, not the code")
    print("the header occurrence is the p3d block's own name (`<mission>_<variant>`), which a clone must rewrite")
    worst = sorted(rows, key=lambda r: -(r["fig_literals"] + r["tod_literals"]))[:5]
    print("most coupled (fig+tod literals):",
          [(r["mission"], r["fig_literals"] + r["tod_literals"]) for r in worst])
    os.makedirs(os.path.dirname(a.json), exist_ok=True)
    json.dump({"packages": rows,
               "zero_fig": zero_fig,
               "totals": {k: sum(r[k] for r in rows)
                          for k in ("fig_literals", "tod_literals", "bible_literals", "definit_literals")}},
              open(a.json, "w"), indent=1)
    print(f"wrote {os.path.relpath(a.json, ROOT)} (git-ignored: it contains mission names)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
