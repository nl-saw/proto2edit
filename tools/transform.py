#!/usr/bin/env python3
"""Read and write placed-object transforms inside a Pure3D meta-object payload.

Decoded layout (see docs/16-meta-object-layout.md). Inside chunk 0x07f00001:

    u32  payload_len
    "META"
    u32  version
    u32  entry_count
    entries:  u32 name_len, u8[name_len] name,
              u16 version, u32 classId, u8[blob] value

The blob size is implied by the class, not stored. For `MatrixAttribute` it is
**7 float32 = 28 bytes**: position.xyz then quaternion.xyzw. This is confirmed
by construction - the gap from the end of the class id to the next entry name is
exactly 34 bytes (2 + 4 + 28) in every object measured.

So a transform is editable in place with no size change, which means no chunk
size has to be rewritten and the asset stays structurally identical.

Usage:
  python3 tools/transform.py <file.p3d|.p3d.rz>              # list transforms
  python3 tools/transform.py <file> --object hellerRestraint  # filter
  python3 tools/transform.py <file> --set hellerRestraint001Spawn2:0.0,1.0,2.0
  python3 tools/transform.py <file> --set ... -o out.p3d     # write a copy

`--set` takes `<object-substring>:x,y,z` and rewrites only the position; the
quaternion is preserved. Matching is on the 0x07f00000 long name, and a
substring that matches more than one object is refused rather than guessed.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p3d import parse, walk  # noqa: E402
from rz import unwrap  # noqa: E402

META_OBJECT = 0x07F00000
META_DATA = 0x07F00001
MATRIX_ATTR = b"MatrixAttribute"
MATRIX_BLOB = 28          # 7 float32
REC_TAIL = 2 + 4 + MATRIX_BLOB   # u16 version + u32 classId + blob


def read_aligned_u8(b, o):
    """gibbed WriteStringAlignedU8: u8 n counts padding; n is always 1..4 mod 4."""
    if o >= len(b):
        return None, o
    n = b[o]
    if n == 0:
        return "", o + 1
    return b[o + 1:o + 1 + n].split(b"\0", 1)[0].decode("latin-1"), o + 1 + n


def collect(buf):
    """Single pass: [(longName, payload, blobOffset, values)] for every matrix attr.

    Must be one pass. Keying chunks by id() and re-parsing would give fresh
    objects whose ids no longer match, which silently produced '?' owners.
    """
    out = []
    for c in walk(parse(buf)):
        if c.type != META_OBJECT:
            continue
        body = c.data[12:]
        o = 0
        names = []
        for _ in range(3):
            s, o = read_aligned_u8(body, o)
            if s is None:
                names = None
                break
            names.append(s)
        if not names:
            continue
        for k in c.children:
            if k.type != META_DATA:
                continue
            payload = k.data[12:]
            off = matrix_offset(payload)
            if off is None:
                continue
            out.append((names[0], payload, off, read_xform(payload, off)))
    return out


def matrix_offset(payload):
    """Byte offset of the MatrixAttribute value blob inside a 0x07f00001 payload, or None."""
    i = payload.find(MATRIX_ATTR)
    if i < 0:
        return None
    start = i + len(MATRIX_ATTR)
    # u16 version + u32 classId precede the blob
    off = start + 2 + 4
    if off + MATRIX_BLOB > len(payload):
        return None
    return off


def read_xform(payload, off):
    return struct.unpack_from("<7f", payload, off)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--object", default=None, help="substring filter on the object name")
    ap.add_argument("--set", default=None, metavar="NAME:x,y,z",
                    help="rewrite the position of the uniquely matching object")
    ap.add_argument("-o", "--out", default=None, help="write here instead of in place")
    a = ap.parse_args(argv)

    blob = open(a.path, "rb").read()
    wrapped = blob[:2] == b"RZ"
    if wrapped:
        blob = unwrap(blob)

    rows = collect(blob)
    targets = [r for r in rows if not a.object or a.object.lower() in r[0].lower()]

    if a.set:
        key, _, xyz = a.set.rpartition(":")
        try:
            pos = [float(v) for v in xyz.split(",")]
            if len(pos) != 3:
                raise ValueError
        except ValueError:
            print(f"error: --set needs NAME:x,y,z, got {a.set!r}", file=sys.stderr)
            return 2
        hits = [t for t in targets if key.lower() in t[0].lower()]
        if len(hits) != 1:
            print(f"error: {key!r} matched {len(hits)} object(s); refusing to guess",
                  file=sys.stderr)
            for t in hits:
                print(f"   {t[0]}", file=sys.stderr)
            return 2
        owner, payload, off, vals = hits[0]
        # Chunk payloads are slices, so patch the whole-file buffer at the
        # absolute offset. Verify by read-back rather than trusting the maths.
        abs_off = blob.find(payload) + off
        if abs_off < off:
            print(f"error: could not locate {owner} payload in the file", file=sys.stderr)
            return 4
        edit = bytearray(blob)
        struct.pack_into("<3f", edit, abs_off, *pos)
        blob = bytes(edit)
        back = read_xform(blob, abs_off)[:3]
        if any(abs(back[i] - pos[i]) > 1e-5 for i in range(3)):
            print(f"error: read-back mismatch: {back} != {pos}", file=sys.stderr)
            return 4
        print(f"set {owner}\n  {tuple(round(v,4) for v in vals[:3])} -> {tuple(pos)}")
        dest = a.out or a.path
        if wrapped:
            print("error: input was RZ-compressed; re-encoding is not implemented",
                  file=sys.stderr)
            return 3
        open(dest, "wb").write(blob)
        print(f"wrote {dest}")
        return 0

    for owner, payload, off, vals in targets:
        p, q = vals[:3], vals[3:]
        print(f"{owner}")
        print(f"   pos=({p[0]:.4f}, {p[1]:.4f}, {p[2]:.4f})  "
              f"quat=({q[0]:.4f}, {q[1]:.4f}, {q[2]:.4f}, {q[3]:.4f})  @+{off}")
    print(f"-- {len(targets)} transform(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
