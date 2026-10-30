#!/usr/bin/env python3
"""Read and edit Pure3D text bibles (chunk 0x18202 holder + 0x18201 storage).

Layout, recovered from `engine::TextBible::Loader` and confirmed against the
shipped bibles (gibbed's `TextBibleHolder` documents the same fields):

  0x18202  TextBibleHolder
      aligned_u8  language          ("english")
      u32         version
      u32         count
      aligned_u8  keys[count]       ("MISSION_NAME_story_a1", ...)
      u32         string_starts[count]
      u32         string_stops[count]   (documented as next_start + 1 in shipped data)

  0x18201  TextBibleStorage          (child)
      aligned_u8  name              ("Text Bible Data")
      u32         unknown
      u32         data_length
      bytes       data[data_length]  <- the values

`aligned_u8` is: u8 n, then n bytes = the string, a NUL, and zero padding such
that n is the string length rounded up to a multiple of 4 plus 4 (see gibbed's
`WriteStringAlignedU8`).

A value is `data[start_i : start_i+1]`. Editing in place with an equal-length string
leaves every offset, chunk size and ancestor `total_size` untouched, which is
why `set` refuses to change the length.
"""

import argparse
import struct
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from p3d import parse, walk  # noqa: E402

HOLDER = 0x00018202
STORAGE = 0x00018201


def encode_aligned_u8(value):
    """Inverse of `read_aligned_u8`, matching the SHIPPED data.

    The length byte counts the string plus enough zero padding to reach a
    multiple of four, i.e. `n = len + ((-len) % 4)`. The first padding byte
    doubles as the NUL terminator, so a string whose length is already a
    multiple of four is stored with no terminator at all — which is what the
    archives contain (e.g. `PLAYER_CHARACTER`, 16 bytes, length byte 0x10).

    Note gibbed's `WriteStringAlignedU8` instead computes `4 - (len % 4)`,
    which adds four bytes in that case and does not reproduce the shipped data.
    """
    raw = value.encode("latin-1") if isinstance(value, str) else bytes(value)
    pad = (-len(raw)) % 4
    n = len(raw) + pad
    if n > 255:
        raise ValueError(f"string too long for aligned u8: {len(raw)} bytes")
    body = raw + b"\0" * pad
    assert len(body) == n
    return bytes([n]) + body


def _chunk_bytes(kind, extra, children=b""):
    header_size = 12 + len(extra)
    total = header_size + len(children)
    return struct.pack("<3I", kind, header_size, total) + extra + children


def read_aligned_u8(blob, pos):
    n = blob[pos]
    pos += 1
    if n == 0:
        return "", pos
    raw = blob[pos:pos + n]
    return raw.split(b"\0", 1)[0].decode("latin-1"), pos + n


class Holder:
    def __init__(self, chunk, storage_chunk):
        blob = chunk.data[12:]
        self.language, p = read_aligned_u8(blob, 0)
        (self.version,) = struct.unpack_from("<I", blob, p)
        p += 4
        (self.count,) = struct.unpack_from("<I", blob, p)
        p += 4
        self.keys = []
        self.key_offsets = []
        for _ in range(self.count):
            self.key_offsets.append(p)
            k, p = read_aligned_u8(blob, p)
            self.keys.append(k)
        self.starts = list(struct.unpack_from(f"<{self.count}I", blob, p))
        p += 4 * self.count
        self.stops = list(struct.unpack_from(f"<{self.count}I", blob, p))

        data_blob = storage_chunk.data[12:]
        self.storage_name, q = read_aligned_u8(data_blob, 0)
        (self.storage_unknown,) = struct.unpack_from("<I", data_blob, q)
        q += 4
        (self.data_len,) = struct.unpack_from("<I", data_blob, q)
        q += 4
        self.data_offset = storage_chunk.offset + storage_chunk.header_size + 0 if False else None
        self.data = bytearray(data_blob[q:q + self.data_len])
        self.values = [bytes(self.data[self.starts[i]:
                                      (self.starts[i + 1] if i + 1 < self.count else len(self.data))])
                       for i in range(self.count)]
        # absolute file offset of data[0]
        self.data_abs = storage_chunk.offset + 12 + q

    def rebuild(self):
        """Re-serialise this holder and its storage child.

        Recomputed: every chunk's `header_size`/`total_size`, the storage data
        length, and the value offsets. Callers must re-emit the whole document
        because ancestor sizes change too.
        """
        starts, stops, data = [], [], bytearray()
        for i in range(self.count):
            starts.append(len(data))
            data += self.values[i]
            # stops is next_start + 1 in shipped data; the last one points one
            # past the blob.
            stops.append(len(data) + 1)
        self.starts, self.stops = starts, list(stops)

        holder_extra = bytearray()
        holder_extra += encode_aligned_u8(self.language)
        holder_extra += struct.pack("<II", self.version, self.count)
        for k in self.keys:
            holder_extra += encode_aligned_u8(k)
        holder_extra += struct.pack(f"<{self.count}I", *self.starts)
        holder_extra += struct.pack(f"<{self.count}I", *self.stops)

        storage_extra = bytearray()
        storage_extra += encode_aligned_u8(self.storage_name)
        storage_extra += struct.pack("<I", self.storage_unknown)
        storage_extra += struct.pack("<I", len(data))
        storage_extra += bytes(data)

        storage = _chunk_bytes(STORAGE, bytes(storage_extra))
        return _chunk_bytes(HOLDER, bytes(holder_extra), storage)

    def index(self, key):
        try:
            return self.keys.index(key)
        except ValueError:
            return -1

    def value(self, i):
        """Value i is data[starts[i] : starts[i+1]].

        Verified on every key we checked: `stops[i] == starts[i+1] + 1`, so the
        stop array is exclusive-plus-one and must not be used as the end bound.
        """
        start = self.starts[i]
        stop = self.starts[i + 1] if i + 1 < self.count else len(self.data)
        if stop < start:
            stop = start
        return bytes(self.data[start:stop]).decode("latin-1")


def rebuild_document(buf, holders_list):
    """Re-emit a whole bible document from parsed holders, fixing all sizes."""
    body = b"".join(h.rebuild() for h in holders_list)
    return struct.pack("<3I", 0xFF443350, 12, 12 + len(body)) + body


def holders(buf):
    root = parse(buf)
    out = []
    for c in walk(root):
        if c.type == HOLDER:
            storage = next((k for k in c.children if k.type == STORAGE), None)
            if storage is not None:
                out.append(Holder(c, storage))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="List or edit a Pure3D text bible")
    ap.add_argument("path")
    ap.add_argument("cmd", choices=["list", "get", "set", "add", "languages"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--value", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--filter", default=None)
    ap.add_argument("--language", default="english")
    a = ap.parse_args(argv)

    buf = bytearray(open(a.path, "rb").read())
    hs = holders(buf)
    if not hs:
        print("no text bible holders found", file=sys.stderr)
        return 1

    if a.cmd == "languages":
        for h in hs:
            print(f"  {h.language!r} items={h.count} version={h.version} data={h.data_len}")
        return 0

    h = next((x for x in hs if x.language.lower() == a.language.lower()), hs[0])

    if a.cmd == "list":
        for i, k in enumerate(h.keys):
            if a.filter and a.filter.lower() not in k.lower():
                continue
            print(f"{i:5}  {k:44}  {h.value(i)!r}")
        return 0

    if a.cmd == "get":
        i = h.index(a.arg)
        if i < 0:
            print(f"key not found: {a.arg}", file=sys.stderr)
            return 1
        print(f"{a.arg} = {h.value(i)!r}  (index {i}, {len(h.value(i))} bytes)")
        return 0

    if a.cmd == "add":
        key, value = a.arg, a.value
        if not key or value is None:
            print("add needs a key and --value", file=sys.stderr)
            return 1
        if h.index(key) >= 0:
            print(f"key already exists: {key}", file=sys.stderr)
            return 1
        from p3d import parse as _parse
        root = _parse(bytes(buf))
        if len(root.children) != len(hs):
            print("refusing: document root has children that are not text bibles", file=sys.stderr)
            return 1
        before_keys = list(h.keys)
        before_values = list(h.values)
        h.keys.append(key)
        h.values.append(value.encode("latin-1"))
        h.count += 1
        out_bytes = rebuild_document(bytes(buf), hs)
        check = holders(out_bytes)
        ch = next(x for x in check if x.language.lower() == h.language.lower())
        ok = (ch.count == h.count and key in ch.keys and ch.value(ch.index(key)) == value
              and ch.keys[:len(before_keys)] == before_keys)
        for k in range(len(before_keys)):
            if ch.value(k).encode("latin-1") != before_values[k]:
                ok = False
                print(f"value {before_keys[k]} changed during rebuild: "
                      f"{before_values[k]!r} -> {ch.value(k)!r}", file=sys.stderr)
        if not ok:
            print("refusing: rebuild did not verify", file=sys.stderr)
            return 1
        out = a.out or a.path
        open(out, "wb").write(out_bytes)
        print(f"added {key} = {value!r} ({len(value)} bytes)")
        print(f"wrote {out} ({len(buf)} -> {len(out_bytes)} bytes, {ch.count} keys, verified; "
              f"every existing key unchanged)")
        return 0

    # set — any length: the document is re-serialised with recomputed offsets
    i = h.index(a.arg)
    if i < 0:
        print(f"key not found: {a.arg}", file=sys.stderr)
        return 1
    old = h.value(i)
    new = a.value
    if new is None:
        print("set needs --value", file=sys.stderr)
        return 1

    from p3d import parse as _parse
    root = _parse(bytes(buf))
    if len(root.children) != len(hs):
        print("refusing: document root has children that are not text bibles", file=sys.stderr)
        return 1

    # Snapshot before rebuild(): rebuild() rewrites h.starts/h.stops in place.
    original_values = list(h.values)
    keys_snapshot = list(h.keys)
    h.values[i] = new.encode("latin-1")
    out_bytes = rebuild_document(bytes(buf), hs)

    # verify: reparse, check the edit and that every other value survived
    check = holders(out_bytes)
    ch = next(x for x in check if x.language.lower() == h.language.lower())
    assert ch.value(i) == new, "edit did not survive the rebuild"
    assert ch.keys == keys_snapshot, "key table changed during rebuild"
    for k in range(len(keys_snapshot)):
        if k == i:
            continue
        got = ch.value(k).encode("latin-1")
        assert got == original_values[k], (
            f"value {k} ({keys_snapshot[k]}) changed: {original_values[k]!r} -> {got!r}")

    out = a.out or a.path
    open(out, "wb").write(out_bytes)
    print(f"set {a.arg}: {old!r} -> {new!r} ({len(old)} -> {len(new)} bytes)")
    print(f"wrote {out} ({len(buf)} -> {len(out_bytes)} bytes, {h.count} keys, verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
