#!/usr/bin/env python3
"""Add a placed object to a META container by duplicating an existing one.

This is the writer counterpart to `tools/meta_layout.py`, which can only read.
Each placed object is a self-contained `0x07f00000` chunk whose single child is
its `0x07f00001` META payload, so there is **no global object count to patch**:
adding an object means duplicating that chunk subtree and growing the
`total_size` of every ancestor, exactly like `tools/mission_fsm.py` reflows the
FSM.

The duplicate keeps the original's name unless `--rename` is given, in which case
the replacement must be the same byte length (the name is a length-prefixed
string; changing its length would need the payload reflowed as well).

    python3 tools/meta_add.py prop_joined.p3d.rz --list
    python3 tools/meta_add.py prop_joined.p3d.rz --duplicate sign004Spawn1 -o out.p3d
    python3 tools/meta_add.py prop_joined.p3d.rz --duplicate sign004Spawn1 \\
        --rename sign004Spawn2 -o out.p3d

Every output is re-parsed and refused unless the object count went up by exactly
one and the whole tree still tiles.
"""

import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import meta_layout as ML      # noqa: E402
from p3d import parse as p3d_parse, walk as p3d_walk   # noqa: E402
from rz import unwrap, wrap                             # noqa: E402

META_OBJECT = 0x07F00000


def load(path):
    blob = open(path, "rb").read()
    wrapped = blob[:2] == b"RZ"
    return (unwrap(blob) if wrapped else blob), wrapped


def objects(buf):
    """[(name, MetaObject)] for every placed object, in file order."""
    out = []
    for mo in ML.iter_meta_objects(buf):
        out.append((mo.name, mo))
    return out


def ancestors(root, offset):
    """Chunk path from root down to the chunk starting at `offset`.

    Matched by offset, not identity: the chunk handed in comes from a different
    parse of the same bytes, so `is` never matches and no ancestor would be
    resized - which silently truncates the tree.
    """
    path = []

    def walk(c):
        path.append(c)
        if c.offset == offset:
            return True
        for k in c.children:
            if walk(k):
                return True
        path.pop()
        return False

    return list(path) if walk(root) else []


def duplicate(buf, mo, rename=None):
    """Return new bytes with the object's chunk copied immediately after itself."""
    chunk = mo.chunk
    body = bytes(buf[chunk.offset:chunk.end])
    if rename is not None:
        old = mo.name.encode("latin-1")
        if len(rename) != len(old):
            raise SystemExit(
                f"error: --rename must be {len(old)} bytes to stay in place, "
                f"got {len(rename)} ({rename!r})")
        body = body.replace(old, rename.encode("latin-1"), 1)

    delta = len(body)
    out = bytearray(buf[:chunk.end]) + bytearray(body) + bytearray(buf[chunk.end:])

    # Every ancestor's total_size grows; the chunk's own sizes are unchanged
    # because the copy is a sibling, not a child.
    root = p3d_parse(buf)
    chain = ancestors(root, chunk.offset)
    if not chain:
        raise SystemExit(f"error: could not locate chunk at {chunk.offset:#x}")
    if chain[-1].offset != chunk.offset:
        raise SystemExit("error: ancestor lookup landed on the wrong chunk")
    for anc in chain[:-1]:
        struct.pack_into("<I", out, anc.offset + 8, anc.total_size + delta)
    return bytes(out), delta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--duplicate", default=None, metavar="NAME")
    ap.add_argument("--rename", default=None, metavar="NEW")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args(argv)

    buf, wrapped = load(a.path)
    objs = objects(buf)
    if a.list:
        print(f"{a.path}: {len(objs)} placed object(s)")
        for name, mo in objs:
            ch = mo.chunk
            print(f"  {name:<32} {ch.total_size:6} bytes @ {ch.offset:#08x}")
        return 0

    if not a.duplicate:
        ap.error("need --list or --duplicate")

    matches = [(n, mo) for n, mo in objs if n == a.duplicate]
    if not matches:
        near = [n for n, _ in objs if a.duplicate.lower() in n.lower()][:8]
        print(f"error: no object named {a.duplicate!r}", file=sys.stderr)
        if near:
            print("  did you mean: " + ", ".join(near), file=sys.stderr)
        return 2
    if len(matches) > 1:
        print(f"error: {a.duplicate!r} appears {len(matches)} times; "
              f"names are not unique", file=sys.stderr)
        return 2
    name, mo = matches[0]

    out, delta = duplicate(buf, mo, a.rename)
    try:
        new_objs = objects(out)
    except Exception as exc:
        print(f"error: result does not parse: {exc}", file=sys.stderr)
        return 3
    if len(new_objs) != len(objs) + 1:
        print(f"error: object count {len(objs)} -> {len(new_objs)}, expected +1",
              file=sys.stderr)
        return 3
    top = p3d_parse(out)
    if top.total_size != len(out):
        print(f"error: tree does not tile ({top.total_size} != {len(out)})",
              file=sys.stderr)
        return 3

    dest = a.out or (a.path + ".added.p3d")
    open(dest, "wb").write(out)
    print(f"duplicated {name!r}: {len(buf)} -> {len(out)} bytes (+{delta})")
    print(f"objects {len(objs)} -> {len(new_objs)}, tree tiles, re-parsed OK")
    if wrapped:
        open(dest + ".rz", "wb").write(wrap(out))
        print(f"wrote {dest} and {dest}.rz (RZ-wrapped)")
    else:
        print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
