#!/usr/bin/env python3
"""Re-emit a mission fig.p3d FSM graph from parsed structure.

The record grammar was solved by measurement (542/542 records, 0 mismatches):

    u32 0 | f32 -1.0 | u32 len | payload[len] | pad (-len)%4 | u64 0

This module rebuilds every record from those fields and concatenates the result
with the untouched container headers and any bytes the parser does not recognise.
`verify` then demands the whole file come back byte-identical - the same standard
meta_add.py is held to, and the precondition for editing records.

    python3 tools/fsm_write.py verify <fig.p3d[.rz]> ...

Nothing is written to disk by `verify`; it is a proof, not a mutation.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rz import unwrap, wrap    # noqa: E402
import fsm_chunk
import fsm_graph as F          # noqa: E402
import fsm_items as I          # noqa: E402

RECORD_ID = F.RECORD_ID
HDR_6C = F.HDR_6C
SENTINEL = F.SENTINEL


def emit_record(payload, zero=0.0, neg=-1.0):
    """Build one record: header + body, sizes computed from the payload length.

    The first body field is a FLOAT that is normally 0.0 but occasionally carries a
    value (1.0 and 2.0 both occur in shipped data), so it must be preserved rather than
    assumed to be zero. The second is always -1.0 across 3071 records measured.
    """
    length = len(payload)
    pad = (-length) % 4
    body = struct.pack("<ffI", zero, neg, length) + payload + b"\x00" * pad + b"\x00" * 8
    head = struct.pack("<5I", RECORD_ID, HDR_6C, len(body), SENTINEL, 0)
    return head + body


def split_record_ex(leaf, off, size):
    """(payload, first_float, sentinel_float) or None if the body breaks the grammar."""
    f1, f2, length = struct.unpack_from("<ffI", leaf, off + 20)
    pad = (-length) % 4
    if 12 + length + pad + 8 != size:
        return None
    if abs(f2 + 1.0) > 1e-6:
        return None
    return leaf[off + 32:off + 32 + length], f1, f2


def split_record(leaf, off, size):
    """Payload only, or None - kept for callers that do not need the floats."""
    got = split_record_ex(leaf, off, size)
    return None if got is None else got[0]


def rebuild(leaf, nodes):
    """Re-emit a region; unknown dwords are copied through unchanged."""
    out = bytearray()
    for kind, _what, off, a, _b, kids in nodes:
        if kind == "record":
            payload = split_record(leaf, off, a)
            if payload is None:
                out += leaf[off:off + 20 + a]          # keep as-is
            else:
                out += emit_record(payload)
        elif kind == "container":
            off_end = off + 16 + a
            out += leaf[off:off + 16]                  # header kept verbatim
            out += rebuild(leaf, kids)
            del off_end
        else:
            out += leaf[off:off + 4]
    return bytes(out)


def verify(path, table):
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, _ = F.payload(blob)
    start = F.graph_start(leaf)
    nodes = F.parse_region(leaf, start, len(leaf), table)
    # rebuild_sized preserves each record's two floats; the older rebuild() dropped
    # them, which made verify flag any file containing a non-zero first float.
    rebuilt = leaf[:start] + rebuild_sized(leaf, nodes, None)
    same = rebuilt == leaf
    print(f"{os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))}"
          f"/{os.path.basename(path)}: {'identical' if same else 'DIFFERS'} "
          f"({len(leaf)} -> {len(rebuilt)} bytes)")
    if not same:
        for i in range(min(len(leaf), len(rebuilt))):
            if leaf[i] != rebuilt[i]:
                print(f"  first difference at {i:#x}")
                break
    return same


def retarget(blob, old, new):
    """Replace every u64 `old` with `new` inside record payloads.

    Only payload bytes are touched: the record is then re-emitted from the
    grammar, so sizes and headers are recomputed rather than patched.
    """
    table = F.name_table()
    leaf, wrap = F.payload(blob), blob[:2] == b"RZ"
    raw = unwrap(blob) if wrap else blob
    leaf2, leaf_off = F.payload(raw)
    start = F.graph_start(leaf2)
    nodes = F.parse_region(leaf2, start, len(leaf2), table)
    hits = [0]

    def walk(items):
        out = bytearray()
        for kind, _what, off, a, _b, kids in items:
            if kind == "record":
                payload = split_record(leaf2, off, a)
                if payload is None:
                    out += leaf2[off:off + 20 + a]
                else:
                    if len(payload) % 8 == 0:
                        buf = bytearray(payload)
                        for i in range(0, len(buf) - 7, 8):
                            if struct.unpack_from("<Q", buf, i)[0] == old:
                                struct.pack_into("<Q", buf, i, new)
                                hits[0] += 1
                        payload = bytes(buf)
                    out += emit_record(payload)
            elif kind == "container":
                out += leaf2[off:off + 16]
                out += walk(kids)
            else:
                out += leaf2[off:off + 4]
        return bytes(out)

    rebuilt_leaf = leaf2[:start] + walk(nodes)
    result = raw[:leaf_off] + rebuilt_leaf + raw[leaf_off + len(leaf2):]
    return result, wrap, hits[0]


def cmd_retarget(path, old, new, out_path):
    raw = open(path, "rb").read()
    result, was_wrapped, hits = retarget(raw, old, new)
    if was_wrapped:
        result = wrap(result)          # keep .rz a .rz
    open(out_path, "wb").write(result)
    diff = sum(1 for a, b in zip(raw, result) if a != b) if len(raw) == len(result) else -1
    print(f"replaced {hits} occurrence(s) of {old:#018x} -> {new:#018x}")
    print(f"{len(raw)} -> {len(result)} bytes; {diff} byte(s) differ")
    return 0


def find_u64(leaf, nodes, value, in_records=False):
    """Offsets of `value` as an 8-byte aligned u64 inside container contents.

    Container contents are where the name ids live (`event`, `conditionGroup`,
    `__anchorRef`) - record payloads hold no resolvable names, verified by scan -
    so this is the only name-addressable layer.
    """
    hits = []
    for kind, _w, off, a, _b, kids in nodes:
        if kind == "container":
            cstart, cend = off + 16, off + 16 + a
            i = cstart
            while i + 8 <= cend:
                if struct.unpack_from("<Q", leaf, i)[0] == value:
                    hits.append(i)
                i += 8
            hits += find_u64(leaf, kids, value, in_records)
        elif in_records and kind == "record":
            p = split_record(leaf, off, a)
            if p:
                for i in range(0, len(p) - 7, 8):
                    if struct.unpack_from("<Q", p, i)[0] == value:
                        hits.append(off + 32 + i)
    return hits


def patch_u64(path, out_path, old, new, allow_records=False):
    """Replace same-size u64 ids in place, then prove structure is unchanged."""
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, leaf_off = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
    before = (sum(1 for _ in _flat(nodes, "container")), sum(1 for _ in _flat(nodes, "record")))
    hits = find_u64(leaf, nodes, old, allow_records)
    patched = bytearray(blob)
    for h in hits:
        struct.pack_into("<Q", patched, leaf_off + h, new)
    out = wrap(bytes(patched)) if raw[:2] == b"RZ" else bytes(patched)
    open(out_path, "wb").write(out)

    # re-parse the result and demand identical structure
    blob2 = unwrap(out) if out[:2] == b"RZ" else out
    leaf2, _ = F.payload(blob2)
    nodes2 = F.parse_region(leaf2, F.graph_start(leaf2), len(leaf2), table)
    after = (sum(1 for _ in _flat(nodes2, "container")), sum(1 for _ in _flat(nodes2, "record")))
    diff = sum(1 for a, b in zip(blob, blob2) if a != b)
    print(f"patched {len(hits)} id(s): {old:#018x} -> {new:#018x}")
    print(f"size {len(blob)} -> {len(blob2)}; {diff} byte(s) differ; "
          f"structure {before} -> {after} "
          f"{'UNCHANGED' if before == after else 'CHANGED'}")
    return 0 if before == after and diff == 8 * len(hits) else 1


def _flat(nodes, kind):
    for k, _w, off, a, _b, kids in nodes:
        if k == kind:
            yield off
        if k == "container":
            yield from _flat(kids, kind)


def patch_text(path, out_path, old, new):
    """Replace a Lua-text payload in place; lengths must match.

    Record payloads come in two kinds: binary (prefix 0x7f 0x00) and raw Lua source
    with no prefix at all - `fe_FadeFromBlack(.5)` and friends. Because a shorter or
    longer payload changes the record size and therefore every ancestor container's
    size field, this only permits equal-length replacements. That is a real limit,
    but it makes the edit provably safe: the file cannot grow or shrink.
    """
    if len(old) != len(new):
        print(f"refusing: lengths differ ({len(old)} vs {len(new)}); "
              "size reflow is not implemented")
        return 2
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    patched = bytearray(blob)
    hits = 0
    i = blob.find(old)
    while i >= 0:
        patched[i:i + len(old)] = new
        hits += 1
        i = blob.find(old, i + len(old))
    if hits == 0:
        print("no occurrence found")
        return 1
    out = wrap(bytes(patched)) if raw[:2] == b"RZ" else bytes(patched)
    open(out_path, "wb").write(out)
    # structure must be identical after the edit
    l1, _ = F.payload(blob)
    l2, _ = F.payload(out if out[:2] != b"RZ" else unwrap(out))
    table = F.name_table()
    a = F.parse_region(l1, F.graph_start(l1), len(l1), table)
    b = F.parse_region(l2, F.graph_start(l2), len(l2), table)
    before = (sum(1 for _ in _flat(a, "container")), sum(1 for _ in _flat(a, "record")))
    after = (sum(1 for _ in _flat(b, "container")), sum(1 for _ in _flat(b, "record")))
    diff = sum(1 for x, y in zip(blob, out if out[:2] != b"RZ" else unwrap(out)) if x != y)
    print(f"replaced {hits} occurrence(s); {diff} byte(s) differ; "
          f"structure {before} -> {after} {'UNCHANGED' if before == after else 'CHANGED'}")
    return 0 if before == after else 1


def chunk_path(blob, target):
    """Offsets of the leaf chunk and every chunk whose extent contains it.

    Children begin at `off + hdr` (root: 0 + 12 = 0xc; 0x20000701: 0xc + 44 = 0x38),
    and `F.payload` returns the leaf chunk *including* its 12-byte header, so `target`
    is the leaf chunk start - not its payload, and not `target - 12` as an earlier
    version assumed (that landed on header data, which reads as ASCII "Scen").
    """
    known = (0xFF443350, 0x20000701, 0x20000702)
    out = []
    for o in range(0, target + 1, 4):
        if o + 12 > len(blob):
            break
        typ, hdr, tot = struct.unpack_from("<3I", blob, o)
        if hdr < 12 or tot < 12 or o + tot > len(blob):
            continue
        if o <= target < o + tot and (o == target or typ in known):
            out.append(o)
    return out


def _chunk_path_old(blob, target):
    """Offsets of every chunk containing `target`, outermost first."""
    path = []

    def walk(off, end):
        while off + 12 <= end:
            typ, hdr, tot = struct.unpack_from("<3I", blob, off)
            if hdr < 12 or tot < 12 or off + tot > end:
                return False
            if off <= target < off + tot:
                path.append(off)
                if off != target:
                    walk(off + 12 + hdr, off + tot)
                return True
            off += tot
        return False

    walk(0, len(blob))
    return path


def rebuild_sized(leaf, nodes, replace=None, insert_after=None, count_delta=None,
                  body_replace=None):
    """Re-emit, recomputing every container's size from its actual content.

    `replace` maps a record offset to a new payload, so payload length may change.
    `body_replace` maps a container offset to a whole new content block; `count_delta`
    adjusts a container's stated item count. Together they let an item be added or
    removed without any other byte moving.
    """
    out = bytearray()
    for kind, _w, off, a, _b, kids in nodes:
        if kind == "record":
            got = split_record_ex(leaf, off, a)
            if got is None:
                out += leaf[off:off + 20 + a]
            elif replace and off in replace:
                out += emit_record(replace[off], got[1], got[2])
            else:
                out += emit_record(got[0], got[1], got[2])
            if insert_after and off in insert_after:
                out += emit_record(insert_after[off])
        elif kind == "container":
            if body_replace and off in body_replace:
                content = body_replace[off]
            else:
                content = rebuild_sized(leaf, kids, replace, insert_after, count_delta,
                                        body_replace)
            name = leaf[off:off + 8]
            count = struct.unpack_from("<I", leaf, off + 12)[0]
            if count_delta and off in count_delta:
                count += count_delta[off]
            out += name + struct.pack("<II", len(content), count) + content
        else:
            out += leaf[off:off + 4]
    return bytes(out)


def duplicate_record(path, out_path, container_name, index=0):
    """Copy one record inside a named container and bump that container's count.

    This is the difference between editing a node and adding one: the copy gets a
    fresh record, and its container's `count` is incremented to match, with all
    ancestor sizes reflowed.
    """
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, leaf_off = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)

    target = None
    for kind, what, off, a, _b, kids in _walk_containers(nodes):
        if kind == "container" and what == container_name:
            recs = [k for k in kids if k[0] == "record"]
            if recs:
                target = (off, recs)
                break
    if target is None:
        print(f"no record inside a container named {container_name!r}")
        return 1
    coff, recs = target
    if index >= len(recs):
        print(f"index {index} out of range ({len(recs)} record(s))")
        return 1
    roff = recs[index][2]
    payload = split_record(leaf, roff, recs[index][3])
    if payload is None:
        print("selected record does not match the solved grammar")
        return 1

    newleaf = leaf[:F.graph_start(leaf)] + rebuild_sized(
        leaf, nodes, None, {roff: payload}, {coff: 1})
    delta = len(newleaf) - len(leaf)
    patched = bytearray(blob[:leaf_off] + bytes(newleaf) + blob[leaf_off + len(leaf):])
    bump_chunk_sizes(patched, blob, leaf_off, delta, chunk_path(blob, leaf_off))

    chk = bytes(patched)
    l2, _ = F.payload(chk)
    n2 = F.parse_region(l2, F.graph_start(l2), len(l2), table)
    before = (sum(1 for _ in _flat_items(nodes) if _[0] == "container"),
              sum(1 for _ in _flat_items(nodes) if _[0] == "record"))
    after = (sum(1 for _ in _flat_items(n2) if _[0] == "container"),
             sum(1 for _ in _flat_items(n2) if _[0] == "record"))
    print(f"duplicated record in {container_name!r}: file {len(blob)} -> {len(chk)} "
          f"(delta {delta}); structure {before} -> {after}")
    if after != (before[0], before[1] + 1):
        print("refusing to write: expected exactly one extra record")
        return 1
    out = wrap(chk) if raw[:2] == b"RZ" else chk
    open(out_path, "wb").write(out)
    print(f"wrote {out_path}")
    return 0


def patch_any_resize(path, out_path, old, new):
    """Replace a byte string anywhere in the graph, reflowing container sizes.

    Works on any occurrence, including condition predicate text that lives directly in
    container contents. Container sizes grow by (occurrences inside) * delta, counted in
    the ORIGINAL coordinates - counting in rebuilt coordinates against old ranges was a
    real bug that corrupted the structure on the first attempt.
    """
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, leaf_off = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)

    hits, i = [], leaf.find(old)
    while i >= 0:
        hits.append(i)
        i = leaf.find(old, i + len(old))
    if not hits:
        print("no occurrence in the graph")
        return 1

    step = len(new) - len(old)
    pieces, cursor = [], 0
    for pos in hits:
        pieces.append(leaf[cursor:pos])
        pieces.append(new)
        cursor = pos + len(old)
    pieces.append(leaf[cursor:])
    newleaf = bytearray(b"".join(pieces))

    # records whose payload contains a hit must update their own len and size
    for kind, what, off, a, _b, _k in _flat_items(nodes):
        if kind != "record":
            continue
        try:
            zero, neg, ln = struct.unpack_from("<IfI", leaf, off + 20)
        except struct.error:
            continue
        if zero != 0 or abs(neg + 1.0) > 1e-6:
            continue
        lo, hi = off + 32, off + 32 + ln
        inside = sum(1 for q in hits if lo <= q < hi)
        if not inside:
            continue
        new_len = ln + inside * step
        new_size = 12 + new_len + (-new_len) % 4 + 8
        shifted = off + step * sum(1 for q in hits if q < off)
        struct.pack_into("<I", newleaf, shifted + 12, new_size)
        struct.pack_into("<I", newleaf, shifted + 28, new_len)

    grown = []
    for kind, what, off, a, _b, kids in _walk_containers(nodes):
        if kind != "container":
            continue
        lo, hi = off + 16, off + 16 + a
        inside = sum(1 for q in hits if lo <= q < hi)
        if inside:
            # the container's own offset shifts by every earlier replacement
            shifted = off + step * sum(1 for q in hits if q < off)
            struct.pack_into("<I", newleaf, shifted + 8, a + inside * step)
            grown.append(what)

    patched = bytearray(blob[:leaf_off] + bytes(newleaf) + blob[leaf_off + len(leaf):])
    total = step * len(hits)
    bump_chunk_sizes(patched, blob, leaf_off, total, chunk_path(blob, leaf_off))

    out = wrap(bytes(patched)) if raw[:2] == b"RZ" else bytes(patched)
    chk = unwrap(out) if out[:2] == b"RZ" else out
    l2, _ = F.payload(chk)
    n2 = F.parse_region(l2, F.graph_start(l2), len(l2), table)
    before = (sum(1 for _ in _flat_items(nodes) if _[0] == "container"),
              sum(1 for _ in _flat_items(nodes) if _[0] == "record"))
    after = (sum(1 for _ in _flat_items(n2) if _[0] == "container"),
             sum(1 for _ in _flat_items(n2) if _[0] == "record"))
    ok = before == after
    # stronger than a structure check: the result must re-emit byte-identically
    l3, _ = F.payload(l2)
    round_trip = (l2[:F.graph_start(l2)]
                  + rebuild_sized(l2, F.parse_region(l2, F.graph_start(l2), len(l2), table),
                                  None)) == l2
    print(f"replaced {len(hits)} occurrence(s) ({total:+d} bytes); "
          f"{len(grown)} container(s) resized; structure {before} -> {after} "
          f"{'UNCHANGED' if ok else 'CHANGED'}; round-trip "
          f"{'clean' if round_trip else 'BROKEN'}")
    if not ok or not round_trip:
        print("refusing to write: output would not re-emit identically")
        return 1
    open(out_path, "wb").write(out)
    print(f"wrote {out_path}")
    return 0


def patch_string(path, out_path, old, new):
    """Edit a length-prefixed, zero-padded string; `old` must be the whole string.

    Layout, confirmed at a live occurrence:

        0xc754: u32 51                       length of the text, excluding padding
        0xc758: "IsObjectInVolume( PLAYER, "a1_TankCommanderCam_1" )"   51 bytes
        0xc78b: 00                          padding to 4-byte alignment

    The padding matters: container content must stay a multiple of 4 in length, because
    the parser walks in 4-byte steps and silently drops a trailing remainder. Replacing
    text without repadding is what broke alignment earlier.
    """
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, leaf_off = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)

    hits = []
    i = leaf.find(old)
    while i >= 0:
        if i >= 4:
            n = struct.unpack_from("<I", leaf, i - 4)[0]
            if n == len(old) and leaf[i:i + n] == old:
                hits.append((i - 4, n + (-n) % 4))
        i = leaf.find(old, i + 1)
    if not hits:
        print("no length-prefixed occurrence found (pass the whole string)")
        return 1

    pad = (-len(new)) % 4
    stored = new + b"\x00" * pad
    pieces, cursor = [], 0
    for pos, total in hits:
        pieces.append(leaf[cursor:pos])
        pieces.append(struct.pack("<I", len(new)))
        pieces.append(stored)
        cursor = pos + 4 + total
    pieces.append(leaf[cursor:])
    newleaf = bytearray(b"".join(pieces))
    step = len(stored) - (hits[0][1])
    total_delta = sum(len(stored) - t for _p, t in hits)

    for kind, what, off, a, _b, _k in _walk_containers(nodes):
        if kind != "container":
            continue
        lo, hi = off + 16, off + 16 + a
        inside = sum(1 for q, _t in hits if lo <= q < hi)
        if inside:
            shifted = off + step * sum(1 for q, _t in hits if q < off)
            struct.pack_into("<I", newleaf, shifted + 8, a + inside * step)

    patched = bytearray(blob[:leaf_off] + bytes(newleaf) + blob[leaf_off + len(leaf):])
    bump_chunk_sizes(patched, blob, leaf_off, total_delta, chunk_path(blob, leaf_off))

    out = wrap(bytes(patched)) if raw[:2] == b"RZ" else bytes(patched)
    chk = unwrap(out) if out[:2] == b"RZ" else out
    l2, _ = F.payload(chk)
    n2 = F.parse_region(l2, F.graph_start(l2), len(l2), table)

    def audit(ns, bad):
        for it in ns:
            if it[0] == "container":
                span = sum(16 + c[3] if c[0] == "container"
                           else (20 + c[3] if c[0] == "record" else 4) for c in it[5])
                if span != it[3]:
                    bad.append((it[1], it[3], span))
                audit(it[5], bad)
        return bad
    bad = audit(n2, [])
    rt = (l2[:F.graph_start(l2)]
          + rebuild_sized(l2, F.parse_region(l2, F.graph_start(l2), len(l2), table),
                          None)) == l2
    print(f"replaced {len(hits)} string(s); {total_delta:+d} bytes; "
          f"{len(bad)} misaligned container(s); round-trip {'clean' if rt else 'BROKEN'}")
    if bad or not rt:
        print("refusing to write")
        return 1
    open(out_path, "wb").write(out)
    print(f"wrote {out_path}")
    return 0


def _walk_containers(nodes):
    for item in nodes:
        yield item
        if item[0] == "container":
            yield from _walk_containers(item[5])


def bump_chunk_sizes(patched, blob, leaf_off, delta, chunk_offsets):
    """Update every size-like field that must track a chunk's total.

    The leaf chunk carries three of them: +4 and +8 both equal the chunk total in a
    clean file, and +12 equals total-16. Updating only +8 (the obvious one) leaves two
    stale lengths, and the engine rejects the file at load - which is what crashed the
    game three times while our own round-trip check passed.
    """
    for c in chunk_offsets:
        old_tot = struct.unpack_from("<I", blob, c + 8)[0]
        for field in (4, 8, 12):
            v = struct.unpack_from("<I", patched, c + field)[0]
            if abs(v - old_tot) <= 16:
                struct.pack_into("<I", patched, c + field, v + delta)


def edit_text_payload(blob, old, new):
    """Record-aware text edit on a raw (unwrapped) graph blob; returns new bytes.

    Unlike mission_fsm.patch_bytes this updates the RECORD's size field and its
    alignment padding as well as the length, which is what `./proto2 fsm --replace`
    was missing: it produced files that re-emitted 2 bytes short and crashed the game
    right after fig.p3d.rz loaded.
    """
    blob = bytes(blob)
    leaf, leaf_off = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)

    hits = {}
    for kind, _w, off, a, _b, _k in _flat_items(nodes):
        if kind == "record":
            p = split_record(leaf, off, a)
            if p and old in p:
                hits[off] = p.replace(old, new)
    if not hits:
        raise ValueError("no record payload contains that text")

    newleaf = leaf[:F.graph_start(leaf)] + rebuild_sized(leaf, nodes, hits)
    patched = bytearray(blob[:leaf_off] + newleaf + blob[leaf_off + len(leaf):])
    delta = len(newleaf) - len(leaf)
    bump_chunk_sizes(patched, blob, leaf_off, delta, chunk_path(blob, leaf_off))
    out = bytes(patched)

    # prove it: structure preserved AND a no-op rebuild is byte-identical
    l2, _ = F.payload(out)
    n2 = F.parse_region(l2, F.graph_start(l2), len(l2), table)
    before = (sum(1 for _ in _flat_items(nodes) if _[0] == "container"),
              sum(1 for _ in _flat_items(nodes) if _[0] == "record"))
    after = (sum(1 for _ in _flat_items(n2) if _[0] == "container"),
             sum(1 for _ in _flat_items(n2) if _[0] == "record"))
    if before != after:
        raise ValueError(f"structure changed {before} -> {after}")
    if (l2[:F.graph_start(l2)]
            + rebuild_sized(l2, F.parse_region(l2, F.graph_start(l2), len(l2), table),
                            None)) != l2:
        raise ValueError("result does not re-emit identically")
    return out


def apply_file_edit(path, out_path, old, new):
    """Edit a file on disk, preserving its RZ wrapper, and verify the result.

    This exists because `edit_text_payload` works on unwrapped bytes, and copying its
    output over a `fig.p3d.rz` installs an uncompressed file under a `.rz` name - which
    the engine rejects at load. Two game launches were wasted on exactly that mistake.
    Callers that touch files on disk must go through here.
    """
    raw = open(path, "rb").read()
    wrapped = raw[:2] == b"RZ"
    blob = unwrap(raw) if wrapped else raw
    out = edit_text_payload(blob, old, new)
    data = wrap(out) if wrapped else out
    open(out_path, "wb").write(data)
    head = data[:2]
    print(f"wrote {out_path} ({len(data)} bytes, wrapper {head.decode('latin-1', 'replace')})")
    if wrapped and head != b"RZ":
        raise ValueError("wrapper lost")
    return data


def patch_text_resize(path, out_path, old, new):
    """Replace Lua text in a payload, reflowing container and chunk sizes."""
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, leaf_off = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)

    hits = []
    for kind, _w, off, a, _b, _k in _flat_items(nodes):
        if kind == "record":
            p = split_record(leaf, off, a)
            if p and old in p:
                hits.append((off, p.replace(old, new)))
    if not hits:
        print("no record payload contains that text")
        return 1

    replace = dict(hits)
    newleaf = leaf[:F.graph_start(leaf)] + rebuild_sized(leaf, nodes, replace)
    delta = len(newleaf) - len(leaf)
    patched = bytearray(blob[:leaf_off] + newleaf + blob[leaf_off + len(leaf):])
    bump_chunk_sizes(patched, blob, leaf_off, delta, chunk_path(blob, leaf_off))
    out = wrap(bytes(patched)) if raw[:2] == b"RZ" else bytes(patched)

    chk = unwrap(out) if out[:2] == b"RZ" else out
    l2, _ = F.payload(chk)
    n2 = F.parse_region(l2, F.graph_start(l2), len(l2), table)
    before = (sum(1 for _ in _flat_items(nodes) if _[0] == "container"),
              sum(1 for _ in _flat_items(nodes) if _[0] == "record"))
    after = (sum(1 for _ in _flat_items(n2) if _[0] == "container"),
             sum(1 for _ in _flat_items(n2) if _[0] == "record"))
    print(f"replaced {len(hits)} payload(s); file {len(blob)} -> {len(chk)} bytes "
          f"(delta {delta}); structure {before} -> {after} "
          f"{'UNCHANGED' if before == after else 'CHANGED'}")
    if before != after:
        print("refusing to write: reflow changed the graph structure "
              "(see docs/19); use equal-length patch-text instead")
        return 1
    open(out_path, "wb").write(out)
    print(f"wrote {out_path}")
    return 0


def _flat_items(nodes):
    for item in nodes:
        yield item
        if item[0] == "container":
            yield from _flat_items(item[5])


def _body_item_spans(body):
    """(offset, len) of each item in a rebuilt body, or [] if it does not tile."""
    off = 0
    out = []
    while off + I.ITEM_HDR <= len(body):
        ln = struct.unpack_from("<I", body, off + 8)[0]
        total = I.ITEM_HDR + ln + ((-ln) % 4) + F.ITEM_TRAILER
        if off + total > len(body):
            return []
        out.append((off, ln))
        off += total
    if off + 4 != len(body) + 4 and off != len(body) and off + 4 != len(body):
        return []
    return out


def _ancestors(nodes, off):
    """Offsets of the containers that strictly contain `off`, outermost first."""
    for kind, _what, coff, size, _count, kids in nodes:
        if kind != "container":
            continue
        if coff < off < coff + 16 + size:
            return [coff] + _ancestors(kids, off)
    return []


def list_containers(path, only=None):
    """Every container with its offset, stated count and whether its body tiles.

    An `items` column equal to `count` means the body is an item list the item
    operations accept; anything else they refuse. The offset is the selector, because
    a mission has one `conditions` container per transition.
    """
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, _ = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
    print(f"{'offset':>8}  {'name':<32} {'size':>6} {'count':>5} {'items':>6}  editable")
    n = 0
    for what, off, size, count, _kids in I.containers(leaf, nodes):
        if only and what != only:
            continue
        items = I.parse_items(leaf, off + 16, off + 16 + size)
        got = "no" if items is None else str(len(items))
        print(f"{off:#8x}  {what:<32} {size:>6} {count:>5} {got:>6}  "
              + ("yes" if (items is not None and items and len(items) == count) else ""))
        n += 1
    print(f"{n} container(s)")
    return 0


def item_edit(path, out_path, container, index, add, attested=None):
    """Add a copy of one item, or remove one, inside a single container.

    This is the difference between editing a graph and extending it. It refuses unless
    the body parses as an item list AND its item count equals `count`, so `count` is
    only ever maintained on bodies that can actually be enumerated.

    The result is proven two ways before it is written: an INDEPENDENT splice of the
    original leaf - patching only the container's and its ancestors' size fields - must
    equal the rebuild byte for byte, and the edited body must re-parse with the new
    count. Otherwise nothing is written.
    """
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, leaf_off = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)

    by_offset = isinstance(container, int)
    found = []
    for what, off, size, count, _kids in I.containers(leaf, nodes):
        if (off != container) if by_offset else (what != container):
            continue
        items = I.parse_items(leaf, off + 16, off + 16 + size)
        if items is None or not items or len(items) != count:
            continue
        found.append((off, size, count, items))
    label = f"offset {container:#x}" if by_offset else repr(container)
    if not found:
        print(f"refusing: no container at {label} whose body parses as an item list "
              f"with count == items")
        return 1
    if len(found) > 1:
        print(f"refusing: {len(found)} containers at {label} pass the gate")
        return 1
    coff, size, count, items = found[0]
    if index is None:
        index = len(items) - 1 if add else 0
    if not (0 <= index < len(items)):
        print(f"refusing: index {index} out of range ({len(items)} item(s))")
        return 1

    target = items[index]
    moved = leaf[target.off:target.off + target.total]
    if add:
        new_body = I.body_bytes(leaf, items, insert_after=(target.off, moved))
        delta, count_delta = target.total, 1
    else:
        if len(items) == 1:
            print("refusing: that is the only item; an empty body is not a shape we "
                  "have seen the engine load")
            return 1
        new_body = I.body_bytes(leaf, items, drop=target.off)
        delta, count_delta = -target.total, -1

    # ATTESTATION GATE: refuse a shape the shipped game never contains. The loader
    # itself accepts such a shape (nothing compares multisets), but after one crash on a
    # duplicated `false` - which occurs zero times in 8244 shipped containers - a shape
    # the corpus does not contain is not something the writer is allowed to emit.
    if attested is not None:
        shape = tuple(sorted(bytes(new_body[i:i + I.ITEM_HDR + ln + ((-ln) % 4) + F.ITEM_TRAILER])
                             for i, ln in _body_item_spans(new_body)))
        name = None
        for what, o, _s, _c, _k in I.containers(leaf, nodes):
            if o == coff:
                name = what
        if (name, shape) not in attested:
            print("refusing: the resulting item multiset never occurs in shipped data")
            print(f"  container {name!r} would hold {len(shape)} item(s) in a combination "
                  f"the corpus does not attest")
            return 1

    newleaf = bytearray(leaf[:F.graph_start(leaf)] + rebuild_sized(
        leaf, nodes, body_replace={coff: new_body}, count_delta={coff: count_delta}))

    # The FSM branch that contains this container grows with it. Neither size chain covers
    # it: the P3D chunk chain is patched by bump_chunk_sizes() and the container chain by
    # rebuild_sized(), but the branch header sits between the chunk block and the container.
    # Left stale it puts `delta` bytes into the block that no branch claims - the block ends
    # up longer than its branches plus separators, which is a desync the engine meets at
    # mission load. This was the defect behind the story_a1 item-duplicate crash.
    info = fsm_chunk.parse_chunk(leaf)
    branch = None
    for b in (info["branches"] if info else []):
        if b["off"] <= coff < b["off"] + b["size"]:
            branch = b
            break

    expected = bytearray(leaf)
    for anc in _ancestors(nodes, coff) + [coff]:
        struct.pack_into("<I", expected, anc + 8,
                         struct.unpack_from("<I", expected, anc + 8)[0] + delta)
    struct.pack_into("<I", expected, coff + 12, count + count_delta)
    if branch is not None:
        at = branch["off"] - 4          # the branch's own u32 size field
        struct.pack_into("<I", newleaf, at,
                         struct.unpack_from("<I", newleaf, at)[0] + delta)
        struct.pack_into("<I", expected, at,
                         struct.unpack_from("<I", expected, at)[0] + delta)
    if add:
        at = target.off + target.total
        expected[at:at] = moved
    else:
        del expected[target.off:target.off + target.total]
    if bytes(expected) != bytes(newleaf):
        first = next((i for i in range(min(len(expected), len(newleaf)))
                      if expected[i] != newleaf[i]), min(len(expected), len(newleaf)))
        print(f"refusing to write: rebuild differs from an independent splice at "
              f"{first:#x} ({len(expected)} vs {len(newleaf)} bytes)")
        return 1

    patched = bytearray(blob[:leaf_off] + newleaf + blob[leaf_off + len(leaf):])
    bump_chunk_sizes(patched, blob, leaf_off, len(newleaf) - len(leaf),
                     chunk_path(blob, leaf_off))
    chk = bytes(patched)
    l2, _ = F.payload(chk)
    n2 = F.parse_region(l2, F.graph_start(l2), len(l2), table)
    edited = [c for c in I.containers(l2, n2) if c[1] == coff]
    if len(edited) != 1:
        print(f"refusing to write: container at {coff:#x} not found once after the edit")
        return 1
    got = I.parse_items(l2, edited[0][1] + 16, edited[0][1] + 16 + edited[0][2])
    if got is None or edited[0][3] != count + count_delta or len(got) != count + count_delta:
        print("refusing to write: after the edit the body does not parse "
              f"({None if got is None else len(got)} items, count {edited[0][3]})")
        return 1

    out = wrap(chk) if raw[:2] == b"RZ" else chk
    verb = "duplicated" if add else "removed"
    print(f"{verb} item {index} at {label}: {len(chk)} bytes ({len(chk)-len(blob):+d}); "
          f"count {count} -> {edited[0][3]}; {len(got)} items tile; "
          f"independent splice agrees")
    open(out_path, "wb").write(out)
    print(f"wrote {out_path}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["verify", "retarget", "patch-id", "patch-text",
                             "patch-text-resize", "duplicate", "patch-any",
                             "patch-string", "add-item", "del-item", "containers"])
    ap.add_argument("paths", nargs="+")
    a = ap.parse_args(argv)
    if a.command == "containers":
        return list_containers(a.paths[0], a.paths[1] if len(a.paths) > 1 else None)
    if a.command in ("add-item", "del-item"):
        if len(a.paths) < 2:
            print(f"usage: {a.command} <file> <name|offset> [index] [out]")
            return 2
        sel = a.paths[1]
        sel = int(sel, 0) if sel.lower().startswith("0x") else sel
        idx = int(a.paths[2]) if len(a.paths) > 2 and a.paths[2] != "-" else None
        out = a.paths[3] if len(a.paths) > 3 else "tmp/fsmcheck/items.p3d"
        corpus = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tmp", "corpus")
        attested = I.attested_shapes(corpus) if os.path.isdir(corpus) else None
        return item_edit(a.paths[0], out, sel, idx, a.command == "add-item", attested)
    if a.command == "patch-string":
        if len(a.paths) < 3:
            print("usage: patch-string <file> <old> <new>")
            return 2
        return patch_string(a.paths[0], "tmp/fsmcheck/string-patched.p3d",
                            a.paths[1].encode(), a.paths[2].encode())
    if a.command == "patch-any":
        if len(a.paths) < 3:
            print("usage: patch-any <file> <old> <new>")
            return 2
        return patch_any_resize(a.paths[0], "tmp/fsmcheck/any-patched.p3d",
                                a.paths[1].encode(), a.paths[2].encode())
    if a.command == "duplicate":
        if len(a.paths) < 2:
            print("usage: duplicate <file> <container> [index]")
            return 2
        idx = int(a.paths[2]) if len(a.paths) > 2 else 0
        return duplicate_record(a.paths[0], "tmp/fsmcheck/duplicated.p3d",
                                a.paths[1], idx)
    if a.command == "patch-text-resize":
        if len(a.paths) < 3:
            print("usage: patch-text-resize <file> <old-text> <new-text>")
            return 2
        return patch_text_resize(a.paths[0], "tmp/fsmcheck/text-resized.p3d",
                                 a.paths[1].encode(), a.paths[2].encode())
    if a.command == "patch-text":
        if len(a.paths) < 3:
            print("usage: patch-text <file> <old-text> <new-text>  (equal lengths)")
            return 2
        return patch_text(a.paths[0], "tmp/fsmcheck/text-patched.p3d",
                          a.paths[1].encode(), a.paths[2].encode())
    if a.command == "patch-id":
        if len(a.paths) < 3:
            print("usage: patch-id <file> <old-id> <new-id>")
            return 2
        return patch_u64(a.paths[0], "tmp/fsmcheck/patched.p3d",
                         int(a.paths[1], 0), int(a.paths[2], 0))
    if a.command == "retarget":
        if len(a.paths) < 3:
            print("usage: retarget <file> <old-id> <new-id>")
            return 2
        return cmd_retarget(a.paths[0], int(a.paths[1], 0), int(a.paths[2], 0),
                            "tmp/fsmcheck/retargeted.p3d")
    table = F.name_table()
    ok = True
    for p in a.paths:
        ok &= verify(p, table)
    print("all byte-identical" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
