#!/usr/bin/env python3
"""Pure3D (.p3d) chunk reader for the Prototype 2 / Radical "Cement" engine.

Chunk layout (little-endian on PC), confirmed against prototype2engine.dll at
0x109518c0 (PushChunk) which reads exactly three u32s and stores them as
(type, headerSize, totalSize), and bounds a chunk by `pos < start + totalSize`:

    u32 type        chunk type id (root is 0xFF443350 = "P3D\\xff")
    u32 headerSize  bytes from chunk start to the chunk's payload
    u32 totalSize   bytes from chunk start to the chunk's end (incl. header)

`headerSize` is >= 12; the extra (headerSize-12) bytes are type-specific header
data (names, counts, hashes).  Bytes beyond headerSize up to totalSize are the
chunk payload and usually contain child chunks.
"""

import struct
import sys

ROOT_ID = 0xFF443350


class Chunk:
    __slots__ = ("type", "header_size", "total_size", "offset", "data", "end", "children")

    def __init__(self, type_, header_size, total_size, offset):
        self.type = type_
        self.header_size = header_size
        self.total_size = total_size
        self.offset = offset
        self.end = offset + total_size
        self.data = b""
        self.children = []

    @property
    def payload_offset(self):
        return self.offset + self.header_size

    def __repr__(self):
        return (f"<Chunk 0x{self.type:08x} hdr={self.header_size} "
                f"total={self.total_size} off=0x{self.offset:x} kids={len(self.children)}>")


class P3DError(Exception):
    pass


def parse_children(buf, start, end, depth=0, max_depth=64):
    out = []
    p = start
    while p + 12 <= end:
        type_, hsize, tsize = struct.unpack_from("<3I", buf, p)
        if tsize < 12 or hsize < 12 or hsize > tsize or p + tsize > end:
            break
        c = Chunk(type_, hsize, tsize, p)
        c.data = buf[p:hsize + p]
        if depth < max_depth:
            c.children = parse_children(buf, p + hsize, p + tsize, depth + 1, max_depth)
        out.append(c)
        p += tsize
    return out


def parse(buf):
    if len(buf) < 12:
        raise P3DError("file too small")
    type_, hsize, tsize = struct.unpack_from("<3I", buf, 0)
    if type_ != ROOT_ID:
        raise P3DError(f"bad root chunk id 0x{type_:08x} (expected 0x{ROOT_ID:08x})")
    if tsize > len(buf):
        raise P3DError(f"root totalSize {tsize} exceeds file size {len(buf)}")
    root = Chunk(type_, hsize, tsize, 0)
    root.data = buf[:hsize]
    root.children = parse_children(buf, hsize, tsize)
    return root


# -- type-specific header decorations -------------------------------------
def header_strings(chunk):
    """Extract the length-prefixed strings stored in a chunk's extra header.

    Radical writes names as `u8 len` followed by `len` bytes (NUL included),
    sometimes separated by small integer fields.  We scan only the header area.
    """
    out = []
    h = chunk.data[12:]
    i = 0
    while i < len(h):
        n = h[i]
        if 1 <= n <= 200 and i + 1 + n <= len(h):
            s = h[i + 1:i + 1 + n]
            if s.endswith(b"\0") and all(32 <= b < 127 for b in s[:-1]) and len(s) > 2:
                out.append(s[:-1].decode("latin-1"))
                i += 1 + ((n + 3) & ~3) if False else 1 + n
                continue
        i += 1
    return out


def walk(chunk):
    yield chunk
    for c in chunk.children:
        yield from walk(c)


def main(argv=None):
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("cmd", nargs="?", default="tree", choices=["tree", "json", "strings", "stats", "validate"])
    a = ap.parse_args(argv)
    buf = open(a.path, "rb").read()
    root = parse(buf)
    if a.cmd == "tree":
        def dump(c, ind=0):
            ss = header_strings(c)
            label = f"  {ss[:2]}" if ss else ""
            print("  " * ind + f"0x{c.type:08x} hdr={c.header_size} total={c.total_size} @0x{c.offset:x}{label}")
            for k in c.children:
                dump(k, ind + 1)
        dump(root)
    elif a.cmd == "json":
        def ser(c):
            return {"type": f"0x{c.type:08x}", "header_size": c.header_size, "total_size": c.total_size,
                    "offset": c.offset, "strings": header_strings(c),
                    "children": [ser(k) for k in c.children]}
        print(json.dumps(ser(root), indent=1))
    elif a.cmd == "strings":
        for c in walk(root):
            for s in header_strings(c):
                print(s)
    elif a.cmd == "stats":
        types = {}
        n = 0
        for c in walk(root):
            types[c.type] = types.get(c.type, 0) + 1
            n += 1
        print(f"chunks={n} distinct_types={len(types)}")
        for t, k in sorted(types.items(), key=lambda kv: -kv[1]):
            print(f"  0x{t:08x}  {k}")
    elif a.cmd == "validate":
        n = 0
        for c in walk(root):
            n += 1
            if c.total_size < 12 or c.header_size < 12 or c.header_size > c.total_size:
                raise SystemExit(f"invalid chunk at 0x{c.offset:x}")
            if c.end > len(buf):
                raise SystemExit(f"chunk overruns file at 0x{c.offset:x}")
        print(f"ok chunks={n} file={len(buf)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
