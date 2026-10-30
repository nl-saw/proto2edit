#!/usr/bin/env python3
"""Guard the FSM graph decoder.

The FSM Graph tab was empty for every mission for a while because artifacts/tag-names.json only named record
containers, not the element tags (`state`, `transition`) the graph filters on. Nothing failed - the view just
rendered zero rows. This check makes that state impossible: a graph must have states and edges, and the two tag
names must resolve.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_graph as F                              # noqa: E402
import mission_graph as MG                         # noqa: E402
import uid as U                                    # noqa: E402

fails = []


def ck(name, ok, detail=""):
    if not ok:
        fails.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")


table = F.name_table()
ck("element tag 'state' resolves", table.get(U.uid("state")) == "state",
   f"{table.get(U.uid('state'))!r}")
ck("element tag 'transition' resolves", table.get(U.uid("transition")) == "transition",
   f"{table.get(U.uid('transition'))!r}")

graphs = MG.corpus()
states = sum(len(g["states"]) for v in graphs.values() for g in v.values())
edges = sum(len(g["edges"]) for v in graphs.values() for g in v.values())
ck("the corpus yields a graph", bool(graphs), f"{len(graphs)} missions")
ck("graphs have states", states > 0, f"{states} states")
ck("graphs have transitions", edges > 0, f"{edges} edges")

named = sum(1 for v in graphs.values() for g in v.values() for s in g["states"] if s["name"])
ck("some states have recovered names", named > 0, f"{named} named of {states}")

print(f"{'ok' if not fails else 'FAILED'}: {len(graphs)} missions, {states} states, {edges} edges")
sys.exit(0 if not fails else 1)
