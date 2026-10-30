#!/usr/bin/env python3
"""Radical Entertainment "ATG CORE CEMENT LIBRARY" (.rcf) archive reader.

Format (little-endian), verified byte-for-byte against every shipped
Prototype 2 archive and cross-checked against the community specification:

  Archive header (0x3C bytes)
    0x00  char[24]  "ATG CORE CEMENT LIBRARY" then 8 zero bytes
    0x20  u8        major version (2)
    0x21  u8        minor version (1)
    0x22  u8        endian flag (0 = little, 1 = big)
    0x23  u8        unknown (1)
    0x24  u32       index offset (always 0x3C)
    0x28  u32       index size (entry_count * 12)
    0x2C  u32       metadata (name) offset, 2048-aligned
    0x30  u32       metadata size
    0x34  u32       reserved (0)
    0x38  u32       entry count

  Index entry (12 bytes), array sorted ascending by file hash:
    0x00  u32       hash of the file name (see hash_filename)
    0x04  u32       absolute file offset, aligned to `alignment`
    0x08  u32       file length in bytes

  Metadata table (at metadata offset)
    0x00  u32       default alignment (2048)
    0x04  u32       reserved (0)
    then `entry_count` records.  A record is NOT index-aligned with the
    sorted index: the association is the hash, which is why this reader
    re-derives every name's hash instead of trusting table order.
    0x00  u32       type hash (0 in shipped Prototype 2 archives)
    0x04  u32       per-file alignment (2048)
    0x08  u32       reserved (0)
    0x0C  u32       name length including the trailing NUL
    0x10  char[]    name, name_length bytes including NUL
    +3 bytes        reserved zero padding before the next record

  File payloads are stored at their aligned offsets; short files are padded
  with zeros up to the next alignment boundary, which is why a payload's
  declared length generally differs from the gap to the next offset.
"""

import argparse
import json
import os
import struct
import sys
import zlib

MAGIC = b"ATG CORE CEMENT LIBRARY"
HEADER_SIZE = 0x3C
MASK32 = 0xFFFFFFFF


class RcfError(Exception):
    pass


def hash_filename(name, seed=0):
    """Radical's Cement path hash.

    Verified against the shipped archives: for each name in a metadata table,
    hash_filename(name) equals exactly one hash in the sorted index.  As a
    control, the nine `art\\hud\\*.gfx` names in boot.rcf hash onto the nine
    CFX payloads present in that archive, with no collisions.
    """
    if name.startswith("\\"):
        name = name[1:]
    for b in name.encode("latin-1"):
        seed = (seed * 31) & MASK32
        seed = (seed + (b + 0x20 if b < 0x61 else b)) & MASK32
    return seed


class Entry:
    __slots__ = ("index", "hash", "offset", "size", "name", "alignment", "type_hash")

    def __init__(self, index, hash_, offset, size):
        self.index = index
        self.hash = hash_
        self.offset = offset
        self.size = size
        self.name = None
        self.alignment = None
        self.type_hash = None

    @property
    def path(self):
        return (self.name or f"<unnamed 0x{self.hash:08x}>").replace("\\", "/")

    def __repr__(self):
        return f"<Entry {self.name!r} hash=0x{self.hash:08x} off=0x{self.offset:x} size={self.size}>"


class Rcf:
    def __init__(self, path):
        self.path = path
        self._fh = open(path, "rb")
        self._fh.seek(0, os.SEEK_END)
        self.file_size = self._fh.tell()
        self._fh.seek(0)
        head = self._fh.read(HEADER_SIZE)
        if len(head) < HEADER_SIZE:
            raise RcfError("file too small for an RCF header")
        if not head.startswith(MAGIC):
            raise RcfError(f"bad magic: {head[:24]!r}")
        self.major, self.minor, self.endian_flag, self.unknown1 = head[0x20], head[0x21], head[0x22], head[0x23]
        if self.endian_flag != 0:
            raise RcfError("big-endian cement archives are not supported by this reader")
        (self.dir_offset, self.dir_length, self.name_offset, self.name_length,
         self.reserved, self.count) = struct.unpack_from("<6I", head, 0x24)
        if self.dir_offset < HEADER_SIZE or self.dir_offset + self.dir_length > self.file_size:
            raise RcfError("directory out of range")
        if self.name_offset < HEADER_SIZE or self.name_offset + self.name_length > self.file_size:
            raise RcfError("metadata table out of range")
        self.entries = []
        self.unmatched_names = []
        self.unnamed_entries = []
        self._read_tables()

    # -- tables ---------------------------------------------------------
    def _read_tables(self):
        self._fh.seek(self.dir_offset)
        dir_raw = self._fh.read(self.dir_length)
        self._fh.seek(self.name_offset)
        meta_raw = self._fh.read(self.name_length)
        if len(dir_raw) < self.count * 12:
            raise RcfError("truncated index")
        for i in range(self.count):
            h, off, size = struct.unpack_from("<3I", dir_raw, i * 12)
            self.entries.append(Entry(i, h, off, size))

        by_hash = {}
        for e in self.entries:
            by_hash.setdefault(e.hash, e)
        p = 8
        for _ in range(self.count):
            if p + 16 > len(meta_raw):
                raise RcfError("truncated metadata table")
            type_hash, alignment, _reserved, nlen = struct.unpack_from("<4I", meta_raw, p)
            if nlen == 0 or nlen > 4096 or p + 16 + nlen > len(meta_raw):
                raise RcfError(f"implausible name length {nlen} at metadata offset 0x{p:x}")
            name = meta_raw[p + 16: p + 16 + nlen].split(b"\0", 1)[0].decode("latin-1")
            e = by_hash.get(hash_filename(name))
            if e is None:
                self.unmatched_names.append(name)
            elif e.name is None:
                e.name = name
                e.alignment = alignment
                e.type_hash = type_hash
            else:
                self.unmatched_names.append(name)
            p += 16 + nlen + 3

        self.unnamed_entries = [e for e in self.entries if e.name is None]
        self.entries.sort(key=lambda e: (e.name or ""))

    # -- access ---------------------------------------------------------
    def read(self, entry):
        self._fh.seek(entry.offset)
        data = self._fh.read(entry.size)
        if len(data) != entry.size:
            raise RcfError(f"short read for {entry.name!r}")
        return data

    def find(self, name):
        want = hash_filename(name)
        for e in self.entries:
            if e.hash == want:
                return e
        raise KeyError(name)

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# -- child payload decoding ------------------------------------------------
RZ_MAGIC = b"RZ\0\0\0\0\0\0"


def unwrap_rz(data):
    """Decode the 'RZ' container used for `.rz` payloads.

    Layout: 2-byte magic "RZ", 6 reserved bytes, u64 little-endian
    uncompressed length, then one raw zlib stream.
    """
    if len(data) < 16 or not data.startswith(RZ_MAGIC):
        return data, {}
    (usize,) = struct.unpack_from("<Q", data, 8)
    try:
        out = zlib.decompress(data[16:])
    except zlib.error as exc:
        return data, {"rz_error": str(exc)}
    return out, {"rz": True, "declared_size": usize, "actual_size": len(out)}


def sniff(data):
    if data[:4] == b"P3D\xff" or data[:3] == b"P3D":
        return "p3d"
    if data[:4] == b"\x1bLua":
        return "lua"
    if data[:3] == b"CFX":
        return "gfx"
    if data[:3] == b"BIK":
        return "bik"
    if data[:4] == b"RADP":
        return "radp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xef\xbb\xbf":
        return "text"
    return "bin"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read/extract Prototype / Cement .rcf archives")
    ap.add_argument("archive")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list entries")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--filter", default=None, help="substring filter on the name")

    p_x = sub.add_parser("extract", help="extract entries")
    p_x.add_argument("outdir")
    p_x.add_argument("--filter", default=None)
    p_x.add_argument("--unwrap-rz", action="store_true", help="decompress .rz payloads")
    p_x.add_argument("--rename-ext", action="store_true", help="append a content-derived extension")
    p_x.add_argument("--force", action="store_true")

    p_v = sub.add_parser("verify", help="report name/hash join integrity")

    args = ap.parse_args(argv)
    with Rcf(args.archive) as arc:
        if args.cmd == "list":
            rows = [e for e in arc.entries if _match(e, args.filter)]
            if args.json:
                print(json.dumps({
                    "archive": os.path.abspath(args.archive),
                    "version": f"{arc.major}.{arc.minor}",
                    "count": arc.count,
                    "entries": [{"index": e.index, "name": e.name, "hash": f"0x{e.hash:08x}",
                                 "offset": e.offset, "size": e.size, "alignment": e.alignment}
                                for e in rows],
                }, indent=1))
            else:
                for e in rows:
                    print(f"{e.size:>10}  0x{e.hash:08x}  {e.name}")
                print(f"-- {len(rows)}/{arc.count} entries, {os.path.basename(args.archive)} v{arc.major}.{arc.minor}")
            return 0

        if args.cmd == "verify":
            print(f"archive         {args.archive}")
            print(f"version         {arc.major}.{arc.minor} endian_flag={arc.endian_flag}")
            print(f"entries         {arc.count}")
            print(f"named           {arc.count - len(arc.unnamed_entries)}")
            print(f"unmatched names {len(arc.unmatched_names)}")
            print(f"unnamed entries {len(arc.unnamed_entries)}")
            bad = 0
            for e in arc.entries:
                if e.offset + e.size > arc.file_size:
                    print(f"  OVERRUN {e.name}")
                    bad += 1
                if e.alignment and e.offset % e.alignment:
                    print(f"  MISALIGNED {e.name} off=0x{e.offset:x} align={e.alignment}")
                    bad += 1
            print(f"integrity problems {bad}")
            return 0

        n = 0
        for e in arc.entries:
            if not _match(e, args.filter):
                continue
            data = arc.read(e)
            info = {}
            if args.unwrap_rz:
                data, info = unwrap_rz(data)
            rel = (e.name or f"unnamed_{e.hash:08x}").replace("\\", "/")
            if args.rename_ext:
                ext = sniff(data)
                base, cur = os.path.splitext(rel)
                if cur.lower() in (".p3d", ".rz", "") and ext != "bin":
                    rel = base + "." + ext
            out = os.path.join(args.outdir, rel)
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            if os.path.exists(out) and not args.force:
                print(f"skip (exists): {rel}", file=sys.stderr)
                continue
            with open(out, "wb") as fh:
                fh.write(data)
            n += 1
            extra = f" [{info}]" if info else ""
            print(f"{len(data):>10}  {sniff(data):5}  {rel}{extra}")
        print(f"-- wrote {n} file(s)", file=sys.stderr)
        return 0


def _match(entry, pattern):
    if pattern is None:
        return True
    if entry.name is None:
        return False
    return pattern.lower() in entry.name.lower()


if __name__ == "__main__":
    sys.exit(main())
