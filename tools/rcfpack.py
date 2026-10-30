#!/usr/bin/env python3
"""Build a Cement (.rcf) archive from a directory tree.

The layout is the inverse of `rcf.py` and was validated by rebuilding a
directory tree and re-reading it with both `rcf.py verify` and the Rust
`proto2 verify` implementation.

Layout rules confirmed from the shipped archives:

* header is 0x3C bytes, index starts immediately at 0x3C;
* the index holds `count * 12` bytes of `{hash, offset, size}` sorted by hash
  (the engine binary-searches it);
* the metadata table starts at the next 2048 boundary after the index and
  holds `u32 alignment, u32 0`, then one record per file in authoring order;
* payloads start at the next 2048 boundary after the metadata table and each
  payload is written at a 2048-aligned offset, zero padded to the boundary.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rcf import MAGIC, HEADER_SIZE, hash_filename  # noqa: E402

ALIGNMENT = 2048


def align(n, a=ALIGNMENT):
    return (n + a - 1) // a * a


def build(entries):
    """entries: list of (name, payload). Returns archive bytes."""
    entries = sorted(entries, key=lambda e: hash_filename(e[0]))
    count = len(entries)

    index_size = count * 12
    meta_offset = align(HEADER_SIZE + index_size)
    meta_size = 8 + sum(16 + len(n.encode("latin-1")) + 1 + 3 for n, _ in entries)
    data_offset = align(meta_offset + meta_size)

    # Assign aligned offsets.
    placed = []
    cursor = data_offset
    for name, payload in entries:
        placed.append((name, payload, cursor))
        cursor = align(cursor + len(payload))

    total = cursor

    out = bytearray(total)
    out[0:24] = MAGIC
    out[0x20] = 2       # major
    out[0x21] = 1       # minor
    out[0x22] = 0       # little endian
    out[0x23] = 1       # unknown1
    struct.pack_into(
        "<6I", out, 0x24,
        HEADER_SIZE, index_size, meta_offset, meta_size, 0, count,
    )

    for i, (name, payload, off) in enumerate(placed):
        struct.pack_into("<3I", out, HEADER_SIZE + i * 12,
                         hash_filename(name), off, len(payload))
        out[off:off + len(payload)] = payload

    struct.pack_into("<2I", out, meta_offset, ALIGNMENT, 0)
    p = meta_offset + 8
    for name, payload, off in placed:
        raw = name.encode("latin-1") + b"\0"
        struct.pack_into("<4I", out, p, 0, ALIGNMENT, 0, len(raw))
        out[p + 16:p + 16 + len(raw)] = raw
        p += 16 + len(raw) + 3
        assert p <= meta_offset + meta_size, "metadata accounting drifted"
    assert p == meta_offset + meta_size, (p, meta_offset + meta_size)
    return bytes(out)


def collect(root, pattern=None):
    entries = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("/", "\\")
            if pattern and pattern.lower() not in rel.lower():
                continue
            with open(full, "rb") as fh:
                entries.append((rel, fh.read()))
    return entries


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pack a directory tree into a .rcf archive")
    ap.add_argument("srcdir")
    ap.add_argument("outfile")
    ap.add_argument("--filter", default=None, help="only include names containing this substring")
    ap.add_argument("--check", action="store_true", help="re-read the result and verify")
    a = ap.parse_args(argv)

    entries = collect(a.srcdir, a.filter)
    if not entries:
        print("no input files", file=sys.stderr)
        return 1
    blob = build(entries)
    with open(a.outfile, "wb") as fh:
        fh.write(blob)
    print(f"wrote {a.outfile}: {len(entries)} files, {len(blob)} bytes", file=sys.stderr)

    if a.check:
        from rcf import Rcf
        with Rcf(a.outfile) as arc:
            assert arc.count == len(entries), (arc.count, len(entries))
            assert not arc.unnamed_entries, arc.unnamed_entries[:3]
            assert not arc.unmatched_names, arc.unmatched_names[:3]
            for name, payload in entries:
                got = arc.read(arc.find(name))
                assert got == payload, f"payload mismatch for {name}"
        print("verify: ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
