#!/usr/bin/env python3
"""Wrap/unwrap the `RZ` container used by `.p3d.rz` payloads.

Layout: "RZ", 6 zero bytes, u64 little-endian uncompressed length, then one raw
zlib stream. The engine requests these entries by their `.rz` name, so a loose
override must keep the `.rz` suffix and stay wrapped.
"""

import argparse
import struct
import sys
import zlib

MAGIC = b"RZ\0\0\0\0\0\0"


def wrap(data, level=9):
    return MAGIC + struct.pack("<Q", len(data)) + zlib.compress(data, level)


def unwrap(blob):
    if not blob.startswith(MAGIC):
        raise ValueError("not an RZ container")
    declared = struct.unpack_from("<Q", blob, 8)[0]
    out = zlib.decompress(blob[16:])
    if len(out) != declared:
        raise ValueError(f"declared {declared} bytes but decoded {len(out)}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wrap or unwrap an RZ payload")
    ap.add_argument("cmd", choices=["wrap", "unwrap", "check"])
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--level", type=int, default=9)
    a = ap.parse_args(argv)
    blob = open(a.src, "rb").read()
    if a.cmd == "check":
        out = unwrap(blob)
        print(f"ok: {len(blob)} stored -> {len(out)} bytes")
        return 0
    if a.cmd == "unwrap":
        out = unwrap(blob)
    else:
        out = wrap(blob, a.level)
    if not a.dst:
        raise SystemExit("need a destination path")
    open(a.dst, "wb").write(out)
    print(f"{a.cmd}: {len(blob)} -> {len(out)} bytes into {a.dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
