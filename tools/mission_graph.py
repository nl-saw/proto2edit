#!/usr/bin/env python3
"""Build a per-mission state graph from the decoded FSM.

Criterion 2 of the toolchain goal: for every mission, list the states and the transitions between
them, with each transition's target resolved to a recovered name where one is known and to its uid
otherwise. Coverage is reported explicitly so a silent gap cannot hide in an average.

Where names come from
----------------------
Every element prefix starts with the uid of the element's own name, and a state-change's `stateName`
is the uid of the target state's name. Turning those uids back into strings uses three sources, in
order of confidence:

    known    a small table recovered and verified during the decode (Complete, Fail, ...)
    runtime  names the engine hashed during a real session (proto2_uid.log, if present)
    engine   every string in the engine string dump, hashed with the engine's own function

The engine dump is 126k strings and takes a few seconds to hash, so the result is cached in
artifacts/uid-names.json and only rebuilt with --rebuild-names.

    python3 tools/mission_graph.py --corpus            # coverage over all 434 missions
    python3 tools/mission_graph.py story_a1            # one mission, readable
    python3 tools/mission_graph.py --json artifacts/mission-graphs.json
"""
import collections
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_chunk as C
import fsm_graph as F
import config  # noqa: E402
import fsm_items as I
import fsm_schema as S
import uid as U
from uid import uid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "tmp", "corpus")
NAMES_JSON = os.path.join(ROOT, "artifacts", "uid-names.json")
ENGINE_STRINGS = os.path.join(ROOT, "work", "strings", "prototype2engine.dll.strings")
GAME_LOG = os.path.join(config.game_dir(), "proto2_uid.log") if config.game_dir() else ""

# recovered during the decode and each verified to hash to its uid
KNOWN = {
    0xCA57F1F2F0F4C2DD: "Complete",
    0x004633DBC8C3FD98: "Fail",
    0x8F5AD09277139B57: "Failed",
    0x4569BDAF777A910D: "Success",
    0x2D9F03EF2328A7A5: "Outside",
    0x1D68A189754232C9: "OnActive",
    0x320496F423F6951E: "Initialize",
}


def build_names(rebuild=False):
    """uid -> name, from the known table plus every string we can hash."""
    if os.path.exists(NAMES_JSON) and not rebuild:
        with open(NAMES_JSON) as fh:
            data = json.load(fh)
        return {int(k, 16): v for k, v in data.items()}

    names = {}
    for u, n in KNOWN.items():
        names[u] = n

    if os.path.exists(GAME_LOG):
        import re
        with open(GAME_LOG, "rb") as fh:
            for line in fh:
                m = re.match(rb"\s*\d+\s+\d+\s+([0-9a-f]{16}) (.*)$", line.rstrip(b"\n"))
                if m:
                    try:
                        names.setdefault(int(m.group(1), 16), m.group(2).decode("latin-1"))
                    except Exception:
                        pass

    # mission-definition scripts: state names are authored there, and they are hashed the same way
    import re as _re
    lua_dir = os.path.join(ROOT, "work", "lua")
    if os.path.isdir(lua_dir):
        for fn in sorted(os.listdir(lua_dir)):
            if not fn.endswith(".txt"):
                continue
            try:
                with open(os.path.join(lua_dir, fn), "rb") as fh:
                    blob = fh.read()
            except OSError:
                continue
            for m in _re.finditer(rb"[\x20-\x7e]{2,80}", blob):
                try:
                    txt = m.group().decode("latin-1")
                except Exception:
                    continue
                names.setdefault(uid(txt), txt)

    if os.path.exists(ENGINE_STRINGS):
        import re
        with open(ENGINE_STRINGS, "rb") as fh:
            blob = fh.read()
        for m in re.finditer(rb"[\x20-\x7e]{2,80}", blob):
            s = m.group().decode("latin-1")
            h = uid(s)
            names.setdefault(h, s)

    os.makedirs(os.path.dirname(NAMES_JSON), exist_ok=True)
    with open(NAMES_JSON, "w") as fh:
        json.dump({f"{k:016x}": v for k, v in names.items()}, fh)
    return names


def elements(leaf, off, end, table, depth=0, parent=None):
    """walk element-framed nodes; yields dicts with tag/off/size/name/siblings/depth/parent"""
    out = []
    tag = struct.unpack_from("<Q", leaf, off)[0]
    size = struct.unpack_from("<I", leaf, off + 8)[0]
    body = off + 12
    bend = body + size
    if bend > end:
        return out
    name = struct.unpack_from("<Q", leaf, body)[0]
    _kids, _ok, stop = C.walk_children(leaf, body + 16, bend)
    p = stop
    children = []
    while p + 12 <= bend:
        t = struct.unpack_from("<Q", leaf, p)[0]
        if t == 0:
            break
        s = struct.unpack_from("<I", leaf, p + 8)[0]
        if p + 12 + s > bend:
            break
        children.append((p, s))
        p += 12 + s + 8
    node = {"tag": table.get(tag, f"{tag:#018x}"), "off": off, "size": size,
            "name": name, "siblings": len(children), "depth": depth, "parent": parent}
    out.append(node)
    for q, s in children:
        out.extend(elements(leaf, q, q + 12 + s, table, depth + 1, off))
    return out


TARGET_BEARING = ("transition", "node", "reference_statemachine")


def transitions_of(leaf, node, table, what_map):
    """target uids named by the condition items of a target-bearing element.

    Three element kinds carry state-change conditions: `transition` (465 in the corpus), `node`
    (165) and `reference_statemachine` (39). Counting only transitions would report 442 of 669
    edges and leave the rest silently missing, so all three are walked.
    """
    targets = []
    tag_uid = struct.unpack_from("<Q", leaf, node["off"])[0]
    if table.get(tag_uid) not in TARGET_BEARING:
        return targets
    body = node["off"] + 12
    bend = body + node["size"]
    for what, off, size, _c, _k in what_map:
        if not (body <= off < bend):
            continue
        items = I.parse_items(leaf, off + 16, off + 16 + size)
        if not items:
            continue
        for it in items:
            if it.tag != RSC:
                continue
            b = leaf[it.off + I.ITEM_HDR:it.off + I.ITEM_HDR + it.length]
            f = S.decode_body("conditions", it.tag, b)
            if f:
                targets.append((f["stateName"], f["template"], f["referenceStatemachineBranch"]))
    return targets


RSC = U.uid("referenceStatemachineStateChange")


def mission_graph(leaf, table, names):
    info = C.parse_chunk(leaf)
    if not info:
        return None
    gs = F.graph_start(leaf)
    nodes = []
    for b in info["branches"]:
        nodes.extend(elements(leaf, b["off"] - 12, b["off"] + b["size"] + 8, table))
    what_map = [(w, o, s, c, k) for w, o, s, c, k in
                I.containers(leaf, F.parse_region(leaf, gs, len(leaf), table))]
    states = [n for n in nodes if n["tag"] == "state"]
    # a transition belongs to the nearest enclosing state; without this the graph's `from` was the
    # transition element's own (usually unnamed) uid, so filtering by state matched nothing
    by_off = {n["off"]: n for n in nodes}

    def owner_state(node):
        p = node.get("parent")
        guard = 0
        while p is not None and guard < 64:
            par = by_off.get(p)
            if par is None:
                return None
            if par["tag"] == "state":
                return par
            p = par.get("parent")
            guard += 1
        return None
    transitions = [n for n in nodes if n["tag"] in TARGET_BEARING]
    edges = []
    resolved = unresolved = 0
    for t in transitions:
        for target, tmpl, branch in transitions_of(leaf, t, table, what_map):
            nm = names.get(target)
            resolved += 1 if nm else 0
            unresolved += 0 if nm else 1
            owner = owner_state(t)
            from_name = names.get(owner["name"], f"{owner['name']:#018x}") if owner else None
            edges.append({"from": from_name or names.get(t["name"], f"{t['name']:#018x}"),
                          "from_uid": owner["name"] if owner else t["name"],
                          "transition_uid": t["name"], "kind": t["tag"],
                          "to": nm, "to_uid": target, "template": tmpl, "branch": branch})
    return {
        "branches": [table.get(b["tag"], f"{b['tag']:#018x}") for b in info["branches"]],
        # `depth` and `parent_uid` are kept: states nest (story_a1 has two at depth 3, story_FinalBoss ten at
        # depth 2), and dropping them let the graph view present a sub-state as a peer of its parent.
        "states": [{"name": names.get(s["name"]), "uid": s["name"], "siblings": s["siblings"],
                    "depth": s["depth"], "parent_uid": s["parent"],
                    "parent": names.get(s["parent"]) if s["parent"] else None}
                   for s in states],
        "transitions": [{"name": names.get(t["name"]), "uid": t["name"], "kind": t["tag"]}
                        for t in transitions],
        "edges": edges,
        "targets_named": resolved,
        "targets_unnamed": unresolved,
        "states_named": sum(1 for s in states if names.get(s["name"])),
        "states_total": len(states),
    }


def graph_for_bytes(inner, table=None, names=None):
    """Graph for a mission whose file bytes are already in memory (the GUI's path).

    `inner` is the unwrapped fig.p3d bytes; the leaf chunk is located the same way the CLI does it, so
    the GUI and the command line cannot disagree about the graph.
    """
    table = table if table is not None else F.name_table()
    names = names if names is not None else build_names()
    leaf, _off = F.payload(inner)
    return mission_graph(leaf, table, names)


def corpus():
    table = F.name_table()
    names = build_names()
    graphs = {}
    for dirpath, _dirs, fns in os.walk(CORPUS):
        if "fig.p3d.rz" not in fns:
            continue
        parts = [p for p in dirpath.split("/") if p]
        mission, variant = parts[-2], parts[-1]
        leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
        g = mission_graph(leaf, table, names)
        if g is None:
            continue
        graphs.setdefault(mission, {})[variant] = g
    return graphs


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--log" not in argv and not GAME_LOG:
        config.require_game_dir()
    if "--rebuild-names" in argv:
        build_names(rebuild=True)
        argv.remove("--rebuild-names")
    if argv and not argv[0].startswith("-"):
        table = F.name_table()
        names = build_names()
        want = argv[0]
        for dirpath, _dirs, fns in os.walk(CORPUS):
            if "fig.p3d.rz" not in fns or f"/{want}/" not in dirpath:
                continue
            leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
            g = mission_graph(leaf, table, names)
            if not g:
                continue
            variant = [p for p in dirpath.split("/") if p][-1]
            print(f"== {want} / {variant}")
            print(f"   branches: {', '.join(g['branches'])}")
            print(f"   states  : {g['states_total']} ({g['states_named']} named)")
            for st in g["states"][:12]:
                label = st["name"] or f"{st['uid']:#018x}"
                print(f"      {label:<28} siblings={st['siblings']}")
            print(f"   edges   : {len(g['edges'])} ({g['targets_named']} named, {g['targets_unnamed']} unnamed)")
            for e in g["edges"][:12]:
                label = e["to"] or f"{e['to_uid']:#018x}"
                print(f"      {e['from']:<28} -> {label}")
            return 0
        print(f"mission {want!r} not found")
        return 2

    graphs = corpus()
    total_edges = sum(len(v["edges"]) for m in graphs.values() for v in m.values())
    named = sum(v["targets_named"] for m in graphs.values() for v in m.values())
    unnamed = sum(v["targets_unnamed"] for m in graphs.values() for v in m.values())
    st = sum(v["states_total"] for m in graphs.values() for v in m.values())
    stn = sum(v["states_named"] for m in graphs.values() for v in m.values())
    print(f"missions with a graph : {len(graphs)}")
    print(f"states                : {st}   named {stn} ({stn*100//max(st,1)}%)")
    print(f"transitions           : {total_edges}")
    print(f"  target named        : {named} ({named*100//max(total_edges,1)}%)")
    print(f"  target unnamed      : {unnamed}")
    if "--json" in argv:
        out = argv[argv.index("--json") + 1]
        with open(out, "w") as fh:
            json.dump(graphs, fh, indent=1)
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
