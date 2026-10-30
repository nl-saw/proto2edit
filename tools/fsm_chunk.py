#!/usr/bin/env python3
"""Parse the on-disk `fight::Chunk` header that wraps every mission FSM.

This closes what docs/19 called gap 1. The file was never a different format from the
runtime stream - the fields are simply written UNTAGGED, in the fixed order that
`Chunk::parse` (FUN_10704a20) reads them. Verified against the code's read order and
against every shipped mission.

THE GRAMMAR, derived from FUN_10704a20 and confirmed on the corpus:

    chunk  := u64 uid("chunk")          the tag the parser verifies
              u32 block_size            bytes that follow this field
              block

    block  := u32 version               always 1 in shipped data
              u64 name                  per-mission id, one per mission (active/open pair)
              u64 context               uid("Scenario") in 434 of 434 missions
              string path               u32 length + bytes, padded to 4; "/" in 434 of 434
              u32 sibling_index         -1 in shipped data
              u32 item_count            141 in story_a1; NOT the branch count
              branch*                   children
              u64 0                     terminator

    branch := u64 tag | u32 size | body[size]

Why the tags are absent: docs/19 recovered the tag vocabulary (version, name, context,
path, siblingIndex, itemCount, ...) from the uid initialiser table, and searched every
mission for those uids as u64 immediates - they occur ZERO times. Only uid("chunk") occurs,
at the one place FUN_10716530 hands the reader to Chunk::parse. So in the binary reader the
tag argument of `vtbl+0x44` is used for validation only, and the field order carries the
meaning.

MEASURED:

    missions whose chunk block parses and tiles : 432 / 434
    branches found per chunk                    : exactly 1
    that branch's tag                          : uid("statemachine"), 433 / 433 sightings
    context == uid("Scenario")                 : 434 / 434
    path == "/"                                : 434 / 434
    version == 1                               : 434 / 434

The 2 failures and the `statemachine` branch BODY are not decoded here: the branch body is
NOT another block of this shape (parsing it as one overflows immediately), so the state
machine layer has its own layout and is still unknown.

    python3 tools/fsm_chunk.py <file> [...]    # parse and report one or more missions
    python3 tools/fsm_chunk.py --corpus        # the numbers above
    python3 tools/fsm_chunk.py --tree          # corpus coverage of the state-machine layer
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rz import unwrap          # noqa: E402
import fsm_graph as F          # noqa: E402
import uid as U                # noqa: E402

CHUNK = U.uid("chunk")


def read_string(leaf, off, limit=300):
    """(bytes, next_off) for `u32 length | bytes | pad to 4`, or (None, off)."""
    if off + 4 > len(leaf):
        return None, off
    n = struct.unpack_from("<I", leaf, off)[0]
    if n > limit or off + 4 + n > len(leaf):
        return None, off
    return leaf[off + 4:off + 4 + n], (off + 4 + n + 3) & ~3


def parse_chunk(leaf):
    """The chunk header and its branches, or None if the grammar does not hold."""
    off = F.graph_start(leaf)
    # "fig0" magic then two u32s precede the chunk tag
    if leaf[off - 4:off] != b"fig0":
        return None
    off += 8
    if off + 12 > len(leaf) or struct.unpack_from("<Q", leaf, off)[0] != CHUNK:
        return None
    block_size = struct.unpack_from("<I", leaf, off + 8)[0]
    block_start = off + 12
    block_end = block_start + block_size
    if block_end > len(leaf):
        return None
    o = block_start
    version = struct.unpack_from("<I", leaf, o)[0]
    o += 4
    name, context = struct.unpack_from("<QQ", leaf, o)
    o += 16
    path, o = read_string(leaf, o)
    if path is None:
        return None
    sibling, items = struct.unpack_from("<II", leaf, o)
    o += 8
    branches = []
    while o + 12 <= block_end:
        tag = struct.unpack_from("<Q", leaf, o)[0]
        size = struct.unpack_from("<I", leaf, o + 8)[0]
        if tag == 0 or size > block_end - o - 12:
            return None
        branches.append({"tag": tag, "size": size, "off": o + 12})
        o += 12 + size
        # each branch is followed by a u64 zero SEPARATOR (not a terminator)
        if o + 8 <= block_end and struct.unpack_from("<Q", leaf, o)[0] == 0:
            o += 8
        else:
            break
    # A chunk with NO branches simply ends after itemCount - there is no separator to read.
    if o == block_end:
        return {"version": version, "name": name, "context": context, "path": path,
                "sibling": sibling, "items": items, "branches": branches,
                "terminator_at": None, "block_end": block_end, "tiles": True}
    return None


# --- the state machine layer -------------------------------------------------
# element := u64 tag | u32 size | prefix(16) | children
# child   := u64 tag | u32 size | u32 count | content[size] | u32 0
# `size` includes an inner terminator; the extra u32 0 is a separator AFTER it.
# Measured: 9021/9021 containers have both zeros. See docs/19 HANDOFF 13.

STATEMACHINE = U.uid("statemachine")
ITEM_RECORD = 0x0000006C357B9D3E
STATE = U.uid("state")
NODE = U.uid("node")
TRANSITION = U.uid("transition")
REF = U.uid("reference_statemachine")


def walk_children(leaf, start, end, limit=None):
    """Children framed `tag|size|count|content[size]|u32 0`, or (children, None, where)."""
    out = []
    p = start
    while p + 16 <= end:
        tag, size, count = struct.unpack_from("<QII", leaf, p)
        if tag == 0 or size > end - p - 16 or count > 4096:
            return out, False, p
        out.append({"tag": tag, "size": size, "count": count, "off": p + 16})
        p += 16 + size
        if p + 4 <= end and struct.unpack_from("<I", leaf, p)[0] == 0:
            p += 4
        else:
            return out, False, p
        if limit is not None and len(out) >= limit:
            break
    return out, p == end, p


def walk_states(leaf, sm_start, sm_end):
    """The statemachine's children, plus a state expansion when it is present."""
    kids, complete, stop = walk_children(leaf, sm_start + 16, sm_end)
    states = []
    p = stop
    while p + 28 <= sm_end and struct.unpack_from("<Q", leaf, p)[0] == STATE:
        size = struct.unpack_from("<I", leaf, p + 8)[0]
        end = p + 12 + size
        if end > sm_end:
            break
        sub, sub_complete, sub_stop = walk_children(leaf, p + 28, end)
        states.append({"off": p, "size": size, "children": sub,
                       "complete": sub_complete, "stop": sub_stop})
        p = end
    return kids, complete, states, p


def parse_element(leaf, off):
    """Read one element-framed record, read-only.

    element := u64 tag | u32 size | prefix(16) | extra | children | u64 0
    prefix  := u64 name | u32 reserved | u32 siblingCount
    extra   := transition            : u32 len | chars | pad4 | u32
               reference_statemachine: u32 len | chars | pad4 | u32 | u32
               everything else       : empty
    children := container* element*

    `size` counts prefix + extra + children; the final u64 0 sits outside it.
    The same framing is the one fsm_rebuild registers and re-emits, and the
    434/434 byte-identical rebuild is the regression test for this reader.

    Returns a dict of offsets and decoded header fields, or None if the
    framing does not hold. `containers` are the child-framed containers in
    document order; `children` are the offsets of element-framed children.
    """
    if off + 12 > len(leaf):
        return None
    tag, size = struct.unpack_from("<QI", leaf, off)
    body_start = off + 12
    body_end = body_start + size
    if size < 16 or body_end + 8 > len(leaf):
        return None
    name, reserved, siblings = struct.unpack_from("<QII", leaf, body_start)
    q = body_start + 16
    extra = None
    if tag == TRANSITION:
        if q + 4 > body_end:
            return None
        ln = struct.unpack_from("<I", leaf, q)[0]
        pad = (ln + 3) & ~3
        if q + 4 + pad + 4 > body_end:
            return None
        extra = {"type": "transition", "text": leaf[q + 4:q + 4 + ln],
                 "word": struct.unpack_from("<I", leaf, q + 4 + pad)[0]}
        q += 4 + pad + 4
    elif tag == REF:
        if q + 4 > body_end:
            return None
        ln = struct.unpack_from("<I", leaf, q)[0]
        pad = (ln + 3) & ~3
        if q + 4 + pad + 8 > body_end:
            return None
        w0, w1 = struct.unpack_from("<II", leaf, q + 4 + pad)
        extra = {"type": "reference_statemachine",
                 "text": leaf[q + 4:q + 4 + ln], "w0": w0, "w1": w1}
        q += 4 + pad + 8
    containers, _complete, stop = walk_children(leaf, q, body_end)
    children = []
    p = stop
    while p + 12 <= body_end:
        child_tag = struct.unpack_from("<Q", leaf, p)[0]
        if child_tag == 0:
            break
        child_size = struct.unpack_from("<I", leaf, p + 8)[0]
        nxt = p + 12 + child_size + 8
        if nxt > body_end:
            return None
        children.append(p)
        p = nxt
    if p != body_end:
        return None
    return {"off": off, "tag": tag, "size": size,
            "body_start": body_start, "body_end": body_end,
            "name": name, "reserved": reserved, "siblingCount": siblings,
            "extra": extra, "content_start": q, "containers": containers,
            "children": children, "next_off": body_end + 8}


def describe_tree(path):
    leaf = load(path)
    info = parse_chunk(leaf)
    if info is None:
        return None
    out = {"chunk": info, "statemachine": None}
    for b in info["branches"]:
        if b["tag"] == STATEMACHINE:
            sm = b["off"]
            end = sm + b["size"]
            kids, complete, states, stop = walk_states(leaf, sm, end)
            out["statemachine"] = {
                "off": sm, "size": b["size"],
                "prefix": struct.unpack_from("<4I", leaf, sm),
                "children": kids, "complete": complete,
                "states": states, "stop": stop,
            }
    return out


def outline(path, table=None, max_items=6):
    """A human-readable description of one mission's FSM, top to bottom.

    Every level printed here is one of the framings verified in docs/19: chunk block
    (433/434), statemachine children, state children, reference_statemachine, transition,
    node, and the item lists underneath (which fsm_items has read since HANDOFF 3).
    """
    import fsm_items as I
    table = table or F.name_table()
    leaf = load(path)
    info = parse_chunk(leaf)
    if info is None:
        return None
    lines = []
    def nm(t):
        if t == ITEM_RECORD:
            return "record"
        return table.get(t, "<%#x>" % t)

    def show_items(start, end, indent, limit=max_items):
        items = I.parse_items(leaf, start, end)
        if items is None:
            lines.append(" " * indent + "(body does not tile)")
            return
        for k, it in enumerate(items[:limit]):
            extra = ""
            if it.tag == ITEM_RECORD:
                import struct as _s
                f1, f2, ln = _s.unpack_from("<ffI", leaf, it.off + 20)
                d = leaf[it.off + 32:it.off + 32 + ln]
                if d[:2] == b"\x7f\x00":
                    extra = "  (deflate Lua, %d bytes)" % ln
                else:
                    txt = d[:40].decode("latin-1").split("\n")[0].strip()
                    extra = "  %r" % txt if txt else ""
            lines.append(" " * indent + "- " + nm(it.tag) + " len=%d%s" % (it.length, extra))
        if len(items) > limit:
            lines.append(" " * indent + "... %d more" % (len(items) - limit))

    def show_children(start, end, indent, depth=0):
        if end - start <= 4:
            return
        kids, ok, _stop = walk_children(leaf, start, end)
        for k in kids:
            tag = k["tag"]
            lines.append(" " * indent + nm(tag) + "  size=%d count=%d" % (k["size"], k["count"]))
            body = k["off"]
            bend = body + k["size"]
            items = I.parse_items(leaf, body, bend)
            if items is not None and len(items) == k["count"] and items:
                show_items(body, bend, indent + 4)
            elif tag == STATE:
                show_state(body - 16, bend, indent + 2, depth)
            else:
                show_children(body, bend, indent + 4, depth + 1)
        if not ok:
            show_elements(_stop, end, indent, depth)

    def show_elements(p, end, indent, depth):
        """Elements after a tracks-style child list use the 28-byte element framing."""
        if depth > 8:
            return
        shown = 0
        while p + 28 <= end and shown < 4096:
            tag = struct.unpack_from("<Q", leaf, p)[0]
            size = struct.unpack_from("<I", leaf, p + 8)[0]
            eend = p + 12 + size
            if eend > end or tag not in (STATE, NODE, TRANSITION, REF):
                break
            if tag == STATE:
                lines.append(" " * indent + "state  size=%d" % size)
                start = p + 28
            elif tag == NODE:
                lines.append(" " * indent + "node  size=%d" % size)
                start = p + 28
            elif tag == TRANSITION:
                lines.append(" " * indent + "transition  size=%d" % size)
                start = p + 36
            else:
                n = struct.unpack_from("<I", leaf, p + 28)[0]
                txt = leaf[p + 32:p + 32 + n].decode("latin-1", "replace")
                lines.append(" " * indent + "reference_statemachine  size=%d  %r" % (size, txt))
                start = ((p + 32 + n + 3) & ~3) + 8
            show_children(start, eend, indent + 4, depth + 1)
            p = eend
            shown += 1
        if shown == 0:
            lines.append(" " * indent + "(children do not tile past %#x)" % p)

    lines.append("chunk   context=%s path=%r version=%d items=%d"
                 % (nm(info["context"]), info["path"], info["version"], info["items"]))
    for b in info["branches"]:
        lines.append("  " + nm(b["tag"]) + "  size=%d" % b["size"])
        if b["tag"] == STATEMACHINE:
            sm = b["off"]
            smend = sm + b["size"]
            show_children(sm + 16, smend, 4)
    return "\n".join(lines)


def coverage(leaf):
    """Is every byte of the leaf accounted for by the decoded structure?

    leaf := 16-byte chunk header | "fig0" | u32 | u32 | chunk tag | u32 size | block | u64 0

    Measured 433/433: the trailer is exactly 8 bytes and exactly zero, and the "fig0"
    magic sits at 0x10. This is the check that says the decode is complete rather than
    merely self-consistent.
    """
    info = parse_chunk(leaf)
    if info is None:
        return None
    end = info["block_end"]
    return {
        "leaf": len(leaf),
        "fig0_at_0x10": leaf[16:20] == b"fig0",
        "block_end": end,
        "trailer": len(leaf) - end,
        "trailer_zero": len(leaf) - end == 8 and leaf[end:] == b"\x00" * 8,
        "tiles": info["tiles"],
    }


def derived_sizes(leaf, table=None):
    """Recompute the size and count fields from the parsed structure.

    This is the check that matters for a writer: if every size can be recomputed from what
    the structure says, the writer never has to copy a number it does not understand.

        block_size      = (4+8+8) + pathblock + (4+4) + sum(12 + branch.size) + 8
        container size  = sum(item.total) + 4          (the inner terminator)
        container count = len(items)
    """
    import fsm_items as I
    info = parse_chunk(leaf)
    if info is None:
        return None
    gs = F.graph_start(leaf)
    block_start = gs + 20
    disk = info["block_end"] - block_start
    pathblk = (4 + len(info["path"]) + 3) & ~3
    # one 8-byte zero separator follows EACH branch; zero branches means no separator
    expect = ((4 + 8 + 8) + pathblk + (4 + 4)
              + sum(12 + b["size"] for b in info["branches"])
              + 8 * len(info["branches"]))
    out = {"block_size_ok": expect == disk, "block_delta": expect - disk,
           "size_ok": 0, "size_total": 0, "count_ok": 0, "count_total": 0}
    table = table or F.name_table()
    nodes = F.parse_region(leaf, gs, len(leaf), table)
    for _w, off, size, count, _k in I.containers(leaf, nodes):
        items = I.parse_items(leaf, off + 16, off + 16 + size)
        if items is None:
            continue
        out["size_total"] += 1
        out["size_ok"] += 1 if size == sum(i.total for i in items) + 4 else 0
        out["count_total"] += 1
        out["count_ok"] += 1 if count == len(items) else 0
    return out


def load(path):
    raw = open(path, "rb").read()
    return F.payload(unwrap(raw) if raw[:2] == b"RZ" else raw)[0]


def describe(path):
    leaf = load(path)
    info = parse_chunk(leaf)
    if info is None:
        return None
    table = F.name_table()
    return {
        "version": info["version"],
        "name": table.get(info["name"], hex(info["name"])),
        "context": table.get(info["context"], hex(info["context"])),
        "path": info["path"],
        "sibling": info["sibling"],
        "items": info["items"],
        "branches": [(table.get(b["tag"], hex(b["tag"])), b["size"]) for b in info["branches"]],
        "tiles": info["tiles"],
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--tree":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        table = F.name_table()
        ok = sm_complete = total_states = states_complete = 0
        for root, _d, names in os.walk(os.path.join(here, "tmp", "corpus")):
            if "fig.p3d.rz" not in names:
                continue
            t = describe_tree(os.path.join(root, "fig.p3d.rz"))
            if t is None or t["statemachine"] is None:
                continue
            ok += 1
            sm = t["statemachine"]
            sm_complete += 1 if sm["complete"] else 0
            total_states += len(sm["states"])
            states_complete += sum(1 for s in sm["states"] if s["complete"])
        print(f"missions with a statemachine: {ok}")
        print(f"  statemachine children tile : {sm_complete}")
        print(f"  states found               : {total_states}")
        print(f"  states that tile           : {states_complete}")
        return 0
    if argv[0] == "--derived":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        table = F.name_table()
        block_ok = block_tot = size_ok = size_tot = count_ok = count_tot = 0
        for root, _d, names in os.walk(os.path.join(here, "tmp", "corpus")):
            if "fig.p3d.rz" not in names:
                continue
            r = derived_sizes(load(os.path.join(root, "fig.p3d.rz")), table)
            if r is None:
                continue
            block_tot += 1
            block_ok += 1 if r["block_size_ok"] else 0
            size_ok += r["size_ok"]; size_tot += r["size_total"]
            count_ok += r["count_ok"]; count_tot += r["count_total"]
        print(f"block_size derived from parsed fields : {block_ok}/{block_tot}")
        print(f"container size == sum(items) + 4      : {size_ok}/{size_tot}")
        print(f"container count == len(items)         : {count_ok}/{count_tot}")
        return 0
    if argv[0] == "--coverage":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        total = ok = trailer_zero = fig0 = tiled = 0
        bad = []
        for root, _d, names in os.walk(os.path.join(here, "tmp", "corpus")):
            if "fig.p3d.rz" not in names:
                continue
            total += 1
            c = coverage(load(os.path.join(root, "fig.p3d.rz")))
            if c is None:
                bad.append(os.path.join(root, "fig.p3d.rz"))
                continue
            ok += 1
            trailer_zero += 1 if c["trailer_zero"] else 0
            fig0 += 1 if c["fig0_at_0x10"] else 0
            tiled += 1 if c["tiles"] else 0
        print(f"missions              : {total}")
        print(f"  chunk block parsed  : {ok}")
        print(f"  fig0 at 0x10        : {fig0}")
        print(f"  8-byte zero trailer : {trailer_zero}")
        print(f"  branches tile       : {tiled}")
        for b in bad[:3]:
            print(f"  unparsed: {b}")
        return 0
    if argv[0] == "--outline":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        table = F.name_table()
        for spec in argv[1:]:
            print(f"=== {spec}")
            text = outline(spec, table)
            print(text if text is not None else "does not match the chunk grammar")
        return 0
    if argv[0] == "--corpus":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ok = bad = 0
        versions = {}
        contexts = {}
        paths = {}
        tags = {}
        per_chunk = {}
        for root, _d, names in os.walk(os.path.join(here, "tmp", "corpus")):
            if "fig.p3d.rz" not in names:
                continue
            info = describe(os.path.join(root, "fig.p3d.rz"))
            if info is None:
                bad += 1
                continue
            ok += 1
            versions[info["version"]] = versions.get(info["version"], 0) + 1
            contexts[info["context"]] = contexts.get(info["context"], 0) + 1
            paths[info["path"]] = paths.get(info["path"], 0) + 1
            for t, _s in info["branches"]:
                tags[t] = tags.get(t, 0) + 1
            per_chunk[len(info["branches"])] = per_chunk.get(len(info["branches"]), 0) + 1
        print(f"missions parsed: {ok}   failed: {bad}")
        print(f"  version   : {versions}")
        print(f"  context   : {contexts}")
        print(f"  path      : {paths}")
        print(f"  branches per chunk: {per_chunk}")
        print(f"  branch tags: {tags}")
        return 0
    for p in argv:
        info = describe(p)
        if info is None:
            print(f"{p}: does not match the chunk grammar")
            continue
        print(f"{p}")
        print(f"  version={info['version']} context={info['context']} path={info['path']!r} "
              f"sibling={info['sibling']} items={info['items']} tiles={info['tiles']}")
        print(f"  name={info['name']}")
        for t, s in info["branches"]:
            print(f"  branch {t} size={s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
