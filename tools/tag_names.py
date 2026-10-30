#!/usr/bin/env python3
"""Every tag in the mission corpus, with the best name the evidence supports.

Criterion 1 of the decode goal asks that no tag remain an unexplained `0x...`. Three kinds of tag
exist in the corpus, and they need different treatment:

  uid tags          the tag is uid(<engine string>); the name comes from the engine string dump.
  coded tags        the tag is NOT a uid - its high half is a small code (0x6c, 0x6e) or zero, and
                    no engine string hashes to it. It has no string name to recover, so it is
                    named by its decoded ROLE, which is established by structure + field schema.
  runtime tags      the tag has uid-shaped halves but matches no string in the engine DLL or the
                    EXE (468,636 printable runs hashed). It is computed at runtime and cannot be
                    named from the shipped binaries.

The tool reports which kind each tag is, so a role name is never presented as if it were an engine
name.
"""
import collections
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_chunk as C
import fsm_graph as F
import fsm_items as I
import uid as U

# coded tags: high half is a small code, no engine string matches
# recovered by scanning the running game for strings whose hash matches the tag
# (tools/asi proto2_scan, PASS C). Verified: uid("event_MissionString") == 0xcdd48f0984102d90.
RUNTIME_RECOVERED = {
    0xCDD48F0984102D90: "event_MissionString",
}

CODED_ROLE = {
    0x0000006C357B9D3E: "record (script payload: f32 timeBegin | f32 timeEnd | string)",
    0x0000006E3695C3B7: "zero-field condition (coded 0x6e)",
    0x00000000006F1B23: "zero-field condition (coded 0x00)",
}


def lookup(tag, table=None):
    """(name, evidence) for one tag, or (None, None) when no evidence names it.

    `evidence` is the kind documented in this module's docstring: "uid", "coded"
    or "runtime". Callers that display the name should show the hex tag instead
    when the name is None rather than inventing a label.
    """
    table = F.name_table() if table is None else table
    # Coded roles first: they describe a structure derived from the header, and a generic engine string can
    # collide with the same 64-bit tag (the record container hashed to the string "lua", which hid its role).
    if tag in CODED_ROLE:
        return CODED_ROLE[tag], "coded"
    if tag in RUNTIME_RECOVERED:
        return RUNTIME_RECOVERED[tag], "runtime"
    if tag in table:
        return table[tag], "uid"
    return None, None


def display(tag, table=None):
    """The tag's decoded name, or its full hex when no evidence names it."""
    name, _evidence = lookup(tag, table)
    return name if name else f"{tag:#018x}"


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corpus = os.path.join(here, "tmp", "corpus")
    engine = json.load(open(os.path.join(here, "artifacts", "engine-class-fields.json")))
    table = F.name_table()
    counts = collections.Counter()
    for dirpath, _dirs, names in os.walk(corpus):
        if "fig.p3d.rz" not in names:
            continue
        leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
        info = C.parse_chunk(leaf)
        if info is None:
            counts["mission does not parse"] += 1
            continue
        for b in info["branches"]:
            counts[("branch", b["tag"])] += 1
        for what, off, size, _c, _k in I.containers(leaf, F.parse_region(leaf, F.graph_start(leaf),
                                                                       len(leaf), table)):
            items = I.parse_items(leaf, off + 16, off + 16 + size)
            if not items:
                continue
            for it in items:
                counts[("item", it.tag)] += 1
    rows = []
    for key, n in counts.items():
        if not isinstance(key, tuple):
            continue
        kind, tag = key
        name, ev = lookup(tag, table)
        rows.append((kind, tag, name, ev or "runtime"))
    rows.sort(key=lambda r: (r[3], -counts[(r[0], r[1])]))
    named = sum(1 for r in rows if r[2])
    print(f"{'kind':<8}{'tag':<20}{'n':>6}  {'evidence':<8} name")
    for kind, tag, name, ev in rows:
        print(f"{kind:<8}{tag:#018x}{counts[(kind, tag)]:>6}  {ev:<8} {name if name else '(none)'}")
    print(f"\ntags: {len(rows)}   named: {named}   unnamed: {len(rows) - named}")
    return 0 if named == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
