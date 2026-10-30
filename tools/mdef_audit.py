#!/usr/bin/env python3
"""Coverage audit of the mission-definition decode.

missiondefinit is a bytecode chunk whose 190 mission protos build plain tables. `mission_tree`
decodes each into Nodes and Leaves. This tool answers the question the goal asks: is every field
named and typed, or is something opaque?

For every mission, every view, every field it reports:

  * how many fields there are
  * how each leaf was reached (`const` operand or `global` name)
  * the value's python type
  * anything that is NOT a rendered scalar/string/bool/None - these are the real gaps, and each is
    printed with its path so it can be chased rather than hidden in a percentage

    python3 tools/mdef_audit.py                 # counts + any opaque fields
    python3 tools/mdef_audit.py --json OUT      # machine-readable detail
"""
import collections
import json
import os
import sys
import config

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mission_tree as MT

GAME = config.game_dir()
SCALARS = (str, int, float, bool, type(None))


def walk(node, path, out):
    """Collect leaves from a Node tree into `out` as (path, kind, value)."""
    if isinstance(node, MT.Node):
        for key, value in node.items:
            k = key.value if isinstance(key, MT.Leaf) else key
            walk(value, f"{path}.{k}", out)
    elif isinstance(node, MT.Leaf):
        out.append((path, node.kind, node.value))
    else:
        out.append((path, "raw", node))


def main(argv=None):
    config.require_game_dir()
    argv = list(sys.argv[1:] if argv is None else argv)
    from rcf import Rcf

    with Rcf(os.path.join(GAME, "boot.rcf")) as arc:
        blob = arc.read(arc.find("scripts\\missiondefinit.lua"))
    root = MT.load(blob)
    missions = list(MT.mission_protos(root))

    leaves = []
    per_mission = {}
    kinds = collections.Counter()
    types = collections.Counter()
    opaque = []
    empty_nodes = 0

    for _p, name, proto in missions:
        views = MT.views(proto)
        got = []
        for view_name, node in views.items():
            if node is None:
                continue
            if isinstance(node, MT.Node) and not node.items:
                empty_nodes += 1
            walk(node, f"{view_name}", got)
        per_mission[name] = len(got)
        for path, kind, value in got:
            kinds[kind] += 1
            if kind == "global":
                types["<global>"] += 1
                continue
            types[type(value).__name__] += 1
            if not isinstance(value, SCALARS):
                opaque.append((name, path, type(value).__name__, repr(value)[:80]))
        leaves.extend((name, p, k, v) for p, k, v in got)

    print(f"missions decoded        : {len(missions)}")
    print(f"fields decoded          : {len(leaves)}")
    print(f"  by operand kind       : {dict(kinds)}")
    print(f"  by python type        : {dict(types)}")
    print(f"empty nodes             : {empty_nodes}")
    print(f"opaque (non-scalar)     : {len(opaque)}")
    for name, path, t, r in opaque[:12]:
        print(f"    {name}.{path}  {t}  {r}")
    zero = [m for m, n in per_mission.items() if n == 0]
    print(f"missions with no fields : {len(zero)} {zero[:6]}")

    if "--json" in argv:
        out = argv[argv.index("--json") + 1]
        with open(out, "w") as fh:
            json.dump({"per_mission": per_mission,
                       "opaque": opaque,
                       "fields": [[m, p, k, repr(v)] for m, p, k, v in leaves]}, fh, indent=1)
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
