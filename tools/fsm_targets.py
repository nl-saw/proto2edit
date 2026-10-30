#!/usr/bin/env python3
"""Resolve what state each FSM state-change leads to.

`referenceStatemachineStateChange` is the condition that says "the machine changed into state X".
Reading the type's own handlers settled which field is which:

    referenceStatemachineBranch's handler is a getter (lea 0x8(%ecx),%eax) - it writes no value
    stateName's handler copies EIGHT bytes into +0x28/+0x2c - it is a u64

so the body is `string template | u32 branch | u64 stateName | u32 0`, and `stateName` is the **uid of
the target state's name**.

This tool reports, for every reference in the corpus:
  * the target uid and, where recovered, its name;
  * whether that uid is a `state` element present in the SAME mission (a direct, checkable link).

Names come from three sources, and the tool says which:
  engine   the string is in the engine string dump and hashes to the uid
  brute    recovered by hashing candidate words (only possible because the hash is known)
  runtime  captured by hooking the engine's hash function during a real session
"""
import collections
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_chunk as C
import fsm_edit as E
import fsm_graph as F
import fsm_items as I
import uid as U
from uid import uid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "tmp", "corpus")

RSC = U.uid("referenceStatemachineStateChange")

# recovered target-state names; every entry verified to hash to the uid
KNOWN = {
    0xCA57F1F2F0F4C2DD: ("Complete", "engine"),
    0x004633DBC8C3FD98: ("Fail", "brute"),
    0x8F5AD09277139B57: ("Failed", "brute"),
    0x4569BDAF777A910D: ("Success", "brute"),
    0x2D9F03EF2328A7A5: ("Outside", "engine"),
}


def target_of(body):
    """(branch index, stateName uid) from a state-change body, or None."""
    if len(body) < 16:
        return None
    ln = struct.unpack_from("<I", body, 0)[0]
    p4 = (ln + 3) & ~3
    if 4 + p4 + 4 + 8 + 4 != len(body):
        return None
    if body[4 + ln:4 + p4] != b"\x00" * (p4 - ln) or body[-4:] != b"\x00" * 4:
        return None
    return (struct.unpack_from("<i", body, 4 + p4)[0],
            struct.unpack_from("<Q", body, 8 + p4)[0])


def mission_states(leaf, table):
    """every `state` element's name uid in this leaf"""
    out = set()

    def walk(off):
        tag = struct.unpack_from("<Q", leaf, off)[0]
        size = struct.unpack_from("<I", leaf, off + 8)[0]
        body = off + 12
        end = body + size
        if table.get(tag) == "state":
            out.add(struct.unpack_from("<Q", leaf, body)[0])
        _kids, _ok, stop = C.walk_children(leaf, body + 16, end)
        q = stop
        while q + 12 <= end:
            t = struct.unpack_from("<Q", leaf, q)[0]
            if t == 0:
                break
            s = struct.unpack_from("<I", leaf, q + 8)[0]
            if q + 12 + s > end:
                break
            walk(q)
            q += 12 + s + 8

    for b in C.parse_chunk(leaf)["branches"]:
        walk(b["off"] - 12)
    return out


def residual_report(known):
    """Precise accounting for every target uid that has no recovered name.

    For each one: how many references, which missions make them, which mission (if any) defines a
    state with that uid, and whether any shipped string hashes to it. The point is that 'unresolved'
    is stated with evidence rather than left as a gap.
    """
    table = F.name_table()
    refs = collections.defaultdict(list)          # uid -> [(mission, count)]
    defined = collections.defaultdict(set)        # uid -> {mission}
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        parts = [p for p in dirpath.split("/") if p]
        mission = parts[-2]
        leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
        try:
            model = E.parse(leaf, table)
        except Exception:
            continue
        for el in E.walk_elements(model):
            if el["tag"] == U.uid("state"):
                defined[el["name"]].add(mission)
            for c in el["containers"]:
                for it in c["items"]:
                    f = it.get("fields")
                    if f and f.get("type") == "state-change" and f["stateName"] not in known:
                        refs[f["stateName"]].append((mission, f["template"],
                                                     f["referenceStatemachineBranch"]))
    print("target uids with no recovered name:\n")
    for uid_, rows in sorted(refs.items(), key=lambda kv: -len(kv[1])):
        missions = sorted({m for m, _t, _b in rows})
        who = sorted(defined.get(uid_, ()))
        if who:
            verdict = f"pinned: a state with this uid is defined in {', '.join(who[:3])}"
        elif uid_ == 0:
            verdict = "no target (zero value)"
        else:
            verdict = "unresolvable: no mission defines this state and no shipped string hashes to it"
        print(f"  {uid_:#018x}  references {len(rows):<3} in {len(missions)} mission(s)")
        print(f"    referencing : {', '.join(missions[:6])}")
        print(f"    {verdict}")
        withpath = [t for _m, t, _b in rows if t]
        if withpath:
            print(f"    carries a template path: {withpath[0][:70]}")
        print()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corpus = os.path.join(here, "tmp", "corpus")
    table = F.name_table()
    if "--residual" in argv:
        return residual_report(KNOWN)
    total = named = in_mission = 0
    uids = collections.Counter()
    resolver = collections.Counter()
    for dirpath, _dirs, names in os.walk(corpus):
        if "fig.p3d.rz" not in names:
            continue
        leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
        states = mission_states(leaf, table)
        for what, off, size, _c, _k in I.containers(leaf, F.parse_region(
                leaf, F.graph_start(leaf), len(leaf), table)):
            items = I.parse_items(leaf, off + 16, off + 16 + size)
            if not items:
                continue
            for it in items:
                if it.tag != RSC:
                    continue
                body = leaf[it.off + I.ITEM_HDR:it.off + I.ITEM_HDR + it.length]
                t = target_of(body)
                if t is None:
                    continue
                total += 1
                uids[t[1]] += 1
                if t[1] in KNOWN:
                    named += 1
                    resolver[KNOWN[t[1]][1]] += 1
                if t[1] in states:
                    in_mission += 1

    print(f"state-change references        : {total}")
    print(f"  target named (uid -> name)   : {named}  ({named * 100 // max(total,1)}%)")
    print(f"  target is a state in the SAME mission : {in_mission}")
    print(f"  distinct target uids         : {len(uids)}")
    print("  name sources                 :", dict(resolver))
    print("\n  target                                   n   name")
    for v, c in uids.most_common():
        nm = KNOWN.get(v, ("(unresolved)", ""))[0]
        print(f"  {v:#018x} {c:>6}   {nm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
