#!/usr/bin/env python3
"""Read and edit `cementfiles.p3d`, the engine's archive mount manifest.

The game reads this document at startup to decide which `.rcf` archives to
mount. It ships next to `prototype2.exe`, and because the ASI loader prefers
loose files, a loose `cementfiles.p3d` overrides the shipped one — so a mod can
add its own archive without replacing anything.

Structure:

    0xff443350  root
      0x07f00000  ContentCementFileDefinition   (name fields in the header)
        0x07f00001  leaf holding the payload:

            "META"
            u32 group_count
            group_count x {
                u32 name_len; char name[name_len];      // no NUL terminator
                u32 entry_count;
                entry_count x { u32 len; char file[len]; }
            }

Groups are `default` (the archives to mount, in order) and `cached`.

Usage:
  python3 tools/cementfiles.py <file> list
  python3 tools/cementfiles.py <file> add <archive.rcf> [...] [--out FILE]
  python3 tools/cementfiles.py <file> remove <archive.rcf> [...] [--out FILE]
"""

import argparse
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p3d import parse  # noqa: E402

ROOT, HOLDER, LEAF = 0xFF443350, 0x07F00000, 0x07F00001


class Groups:
    def __init__(self, prefix, groups, suffix):
        self.prefix = prefix      # bytes before "META" inside the leaf payload
        self.groups = groups      # list of (name, [entries])
        self.suffix = suffix      # bytes after the parsed block


def load(path):
    buf = open(path, "rb").read()
    root = parse(buf)
    holder = root.children[0]
    leaf = holder.children[0]
    payload = leaf.data[12:]
    start = payload.find(b"META")
    if start < 0:
        raise SystemExit("no META block in cementfiles payload")
    p = start + 4
    (n_groups,) = struct.unpack_from("<I", payload, p)
    p += 4
    groups = []
    for _ in range(n_groups):
        (ln,) = struct.unpack_from("<I", payload, p)
        p += 4
        name = payload[p:p + ln].decode("latin-1")
        p += ln
        (cnt,) = struct.unpack_from("<I", payload, p)
        p += 4
        entries = []
        for _ in range(cnt):
            (el,) = struct.unpack_from("<I", payload, p)
            p += 4
            entries.append(payload[p:p + el].decode("latin-1"))
            p += el
        groups.append([name, entries])
    return Groups(payload[:start], groups, payload[p:]), holder, root


def emit(g):
    out = bytearray(b"META")
    out += struct.pack("<I", len(g.groups))
    for name, entries in g.groups:
        raw = name.encode("latin-1")
        out += struct.pack("<I", len(raw)) + raw
        out += struct.pack("<I", len(entries))
        for e in entries:
            b = e.encode("latin-1")
            out += struct.pack("<I", len(b)) + b
    return bytes(out)


def rebuild(path_or_buf, g):
    """Re-assemble a full document, recomputing every chunk size."""
    buf = path_or_buf
    root = parse(buf)
    holder = root.children[0]
    leaf = holder.children[0]
    holder_extra = holder.data[12:]                 # name fields, kept verbatim
    leaf_payload = g.prefix + emit(g) + g.suffix

    leaf_body = b""
    # leaf is a leaf: header_size == total_size == 12 + payload
    leaf_bytes = struct.pack("<3I", LEAF, 12 + len(leaf_payload), 12 + len(leaf_payload)) + leaf_payload
    holder_total = 12 + len(holder_extra) + len(leaf_bytes)
    holder_bytes = struct.pack("<3I", HOLDER, 12 + len(holder_extra), holder_total) + holder_extra + leaf_bytes
    return struct.pack("<3I", ROOT, 12, 12 + len(holder_bytes)) + holder_bytes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("cmd", choices=["list", "add", "remove", "roundtrip"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--out", default=None)
    ap.add_argument("--group", default="default")
    a = ap.parse_args(argv)

    raw = open(a.path, "rb").read()
    g, _holder, _root = load(a.path)

    if a.cmd == "list":
        for name, entries in g.groups:
            print(f"[{name}] {len(entries)} archive(s)")
            for e in entries:
                print(f"   {e}")
        return 0

    if a.cmd == "roundtrip":
        out = rebuild(raw, g)
        print(f"original {len(raw)} bytes, rebuilt {len(out)} bytes, identical={out == raw}")
        return 0

    group = next((x for x in g.groups if x[0] == a.group), None)
    if group is None:
        raise SystemExit(f"no group named {a.group!r}")

    if a.cmd == "add":
        for n in a.names:
            if n not in group[1]:
                group[1].append(n)
                print(f"added   {n}")
            else:
                print(f"present {n}")
    else:
        for n in a.names:
            if n in group[1]:
                group[1].remove(n)
                print(f"removed {n}")
            else:
                print(f"absent  {n}")

    out = rebuild(raw, g)
    dest = a.out or a.path
    open(dest, "wb").write(out)
    print(f"wrote {dest} ({len(raw)} -> {len(out)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
