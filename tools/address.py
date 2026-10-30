#!/usr/bin/env python3
"""Read and edit the world placement table (`Address` meta-objects).

This is where a placed object's *world* position actually lives.

`MatrixAttribute` (see tools/transform.py) is **local**: 101 of 103 observed
values are identity and the other two are small offsets like `(-4.09, 0, -1.39)`.
So editing it does not move anything across the map.

The world transform is in the `Address` class of meta-object, whose META payload
is fully decoded and was verified arithmetically:

    u32  payload_len        == body length - 4
    "META"
    u32  address_id
    u8   name_len, name     "Address_8113829"     (u8 length, no padding)
    u32  sub_len, sub       "InfectedFlood", "1", "2"   (u32 length)
    u32  sub_id
    f32  position[3]
    f32  quaternion[4]
    f32  zero[3]            (0,0,0 in all 242 observed)

`12 + (1+15) + (4+13) + 4 + 12 + 16 + 12 = 89` for the sample payload, exact.

Verification over all 242 `Address` objects in
`art/missions/story_intro2/active/tod.p3d`:

* **242/242 quaternions have |q| == 1.000000** to within 1e-6. A random decode
  would not produce unit quaternions, so this is strong evidence the field split
  is right.
* the trailing 3 floats are `(0,0,0)` in all 242.
* positions span `x` from -201.6 to 1142.9, i.e. world coordinates.

Usage:
  python3 tools/address.py <file.p3d|.p3d.rz>                    # list
  python3 tools/address.py <file> --filter InfectedFlood
  python3 tools/address.py <file> --set 'Address_8113829/Infected1_1:100,46,-1800' [-o out]
  python3 tools/address.py <file> --set 'Address_8113829:100,46,-1800' [-o out]  # if unique

An address name is a *namespace*, not a unique key: `Address_8113829` has 37
sub-records in story_intro2 (`Infected1_1`, `BlackWatchSpawn`, `BombSpawn`,
`FramerCam`..`FramerCam4`, `Checkpoints`, `Go_To1/2`, `PunchTutorial`, ...). Use
`NAME/SUB` to target one, otherwise a bare name is refused when ambiguous.

`--set` rewrites only the position; the rotation is preserved. It requires the
name to be unique, verifies by read-back, and refuses to write on mismatch.
RZ-compressed inputs can be read but not written (no re-encoder wired in).
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
TAIL_FLOATS = 10          # position 3 + quaternion 4 + zero 3


def _printable(b):
    return bool(b) and all(0x20 <= c < 0x7F for c in b)


def decode(p):
    """Decode an Address payload, or return None if it is not one."""
    if len(p) < 8 or p[4:8] != b"META":
        return None
    if struct.unpack_from("<I", p, 0)[0] != len(p) - 4:
        return None
    o = 8
    address_id = struct.unpack_from("<I", p, o)[0]
    o += 4
    n = p[o]
    if not (1 <= n <= 80) or not _printable(p[o + 1:o + 1 + n]):
        return None
    name = p[o + 1:o + 1 + n].decode("latin-1")
    o += 1 + n
    if o + 4 > len(p):
        return None
    n2 = struct.unpack_from("<I", p, o)[0]
    if not (1 <= n2 <= 80) or o + 4 + n2 > len(p) or not _printable(p[o + 4:o + 4 + n2]):
        return None
    sub = p[o + 4:o + 4 + n2].decode("latin-1")
    o += 4 + n2
    sub_id = struct.unpack_from("<I", p, o)[0]
    o += 4
    if len(p) - o != TAIL_FLOATS * 4:
        return None
    v = struct.unpack_from("<10f", p, o)
    return dict(address_id=address_id, name=name, sub=sub, sub_id=sub_id,
                pos=v[0:3], quat=v[3:7], tail=v[7:10], value_offset=o)


def collect(buf):
    """[(decode_result, payload)] in file order."""
    out = []
    for c in walk(parse(buf)):
        if c.type != META_OBJECT:
            continue
        for k in c.children:
            if k.type != META_DATA:
                continue
            payload = k.data[12:]
            d = decode(payload)
            if d is not None:
                out.append((d, payload))
    return out



def encode(d):
    """Inverse of decode(): one Address payload."""
    name = d["name"].encode("latin-1")
    sub = d["sub"].encode("latin-1")
    body = bytearray()
    body += b"META"
    body += struct.pack("<I", d["address_id"])
    body += struct.pack("<B", len(name)) + name
    body += struct.pack("<I", len(sub)) + sub
    body += struct.pack("<I", d["sub_id"])
    body += struct.pack("<10f", *(list(d["pos"]) + list(d["quat"]) + list(d["tail"])))
    return struct.pack("<I", len(body)) + bytes(body)


def serialize(chunk):
    """Chunk -> bytes, exactly as parsed: header data followed by the children."""
    return bytes(chunk.data) + b"".join(serialize(c) for c in chunk.children)


def _set_total(chunk, extra):
    """Add `extra` bytes to this chunk's total_size field (its own header, at offset 8)."""
    data = bytearray(chunk.data)
    total = struct.unpack_from("<I", data, 8)[0]
    struct.pack_into("<I", data, 8, total + extra)
    chunk.data = bytes(data)
    chunk.total_size = total + extra


def add_member(buf, name, sub, pos, quat=(0.0, 0.0, 0.0, 1.0)):
    """Append one Address member to the group that owns `name`, and return (new_bytes, info).

    `name` may be a group name (Address_1234567) or a member's sub name - mission Lua refers to placements by sub
    ("|BW_BaseGOTO"), which is what a spawner's rootPath points at. The untouched tree is serialized first and must
    reproduce the input byte for byte; anything else refuses rather than write a corrupt placement table.
    """
    from p3d import Chunk
    root = parse(buf)
    if serialize(root) != bytes(buf):
        raise ValueError("refusing: the parsed tree does not serialize back to the original bytes")

    def dec(ch):
        return decode(ch.data[12:]) if ch.type == META_DATA else None

    def find_path(node, want, trail=()):
        if node is want:
            return trail
        for ch in node.children:
            got = find_path(ch, want, trail + (node,))
            if got is not None:
                return got
        return None

    target = proto = None
    for c in walk(root):
        if c.type != META_OBJECT:
            continue
        for k in c.children:
            d = dec(k)
            if not d:
                continue
            if d["name"] == name:
                target, proto = c, k
            elif d["sub"] == name and proto is None:
                target, proto = c, k
        if target is not None:
            break
    if target is None:
        raise ValueError(f"no Address group or member named {name!r} in this placement table")

    path = find_path(root, target)
    if path is None:
        raise ValueError("refusing: could not locate the group in the tree")

    d = dict(dec(proto))
    d.update(sub=sub, pos=tuple(pos), quat=tuple(quat), tail=(0.0, 0.0, 0.0))
    payload = encode(d)
    total = 12 + len(payload)
    if total % 4:
        raise ValueError("refusing: the new member would not be 4-byte aligned")
    head = bytearray(proto.data[:12])
    struct.pack_into("<I", head, 4, total)          # header_size covers the inline payload
    struct.pack_into("<I", head, 8, total)          # total_size == header_size for a leaf member
    child = Chunk(META_DATA, total, total, 0)
    child.data = bytes(head) + payload
    child.children = []
    target.children.append(child)
    for anc in list(path) + [target]:
        _set_total(anc, total)
    return serialize(root), dict(name=d["name"], sub=sub, size=total,
                                 group_members=len(target.children))


def _archive_blob(mission, variant):
    """(inner_bytes, where) for a mission's tod.p3d.rz, preferring staging over the archive."""
    import rcf
    import rz as RZ
    rel = f"art/missions/{mission}/{variant}/tod.p3d.rz"
    staged = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "work", "mod", rel)
    if os.path.exists(staged):
        blob = open(staged, "rb").read()
        return (RZ.unwrap(blob) if blob[:2] == b"RZ" else blob), "staging"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config as CFG
    game = CFG.game_dir() if hasattr(CFG, "game_dir") else CFG.DEFAULT_GAME
    for arc in ("art.rcf", "boot.rcf"):
        path = os.path.join(game, arc)
        if not os.path.exists(path):
            continue
        r = rcf.Rcf(path)
        for e in r.entries:
            n = getattr(e, "name", "") or ""
            n = n.decode("latin-1") if isinstance(n, bytes) else n
            if n.replace("/", "\\").lower() == rel.replace("/", "\\").lower():
                blob = r.read(e)
                r.close()
                return (RZ.unwrap(blob) if blob[:2] == b"RZ" else blob), arc
        r.close()
    raise SystemExit(f"no {rel} in staging or the archives")


def _cmd_add(a):
    if not (a.name and a.sub and a.at):
        raise SystemExit("--mission needs --name, --sub and --at x,y,z")
    import rz as RZ
    inner, where = _archive_blob(a.mission, a.variant)
    pos = tuple(float(x) for x in a.at.split(","))
    if len(pos) != 3:
        raise SystemExit("--at wants x,y,z")
    before = collect(inner)
    # refuse a duplicate: the same sub at the same position is almost always a re-run, not an intention
    for d, _ in before:
        if d["sub"] == a.sub and all(abs(x - y) < 0.01 for x, y in zip(d["pos"], pos)):
            raise SystemExit(f"refusing: {a.sub!r} already exists at that position in {d['name']}")

    out, info = add_member(inner, a.name, a.sub, pos)
    after = collect(out)
    def _same(v):
        return all(abs(x - y) < 0.01 for x, y in zip(v, pos))

    fresh = [d for d, _ in after if d["sub"] == a.sub and _same(d["pos"])]
    import collections as _c
    def _key(d):
        return (d["name"], d["sub"], tuple(round(v, 2) for v in d["pos"]))
    a_cnt = _c.Counter(_key(d) for d, _ in before)
    b_cnt = _c.Counter(_key(d) for d, _ in after)
    b_cnt[_key(fresh[0])] -= 1
    if +b_cnt != a_cnt:
        raise SystemExit("refusing: existing placements changed - not writing")

    if not fresh:
        raise SystemExit("refusing: the new placement is not readable back from the result")
    print(f"{a.mission}/{a.variant} ({where}): {len(before)} -> {len(after)} placements, "
          f"+{info['size']} bytes, new entry {info['name']} | {a.sub} at {pos}")
    if a.dry_run:
        return 0
    staged = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "work", "mod", "art", "missions", a.mission, a.variant, "tod.p3d.rz")
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    open(staged, "wb").write(RZ.wrap(out))
    print(f"staged {os.path.relpath(staged)} - run ./proto2 install")
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--filter", default=None, help="substring of name or sub")
    ap.add_argument("--set", default=None, metavar="NAME:x,y,z")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--mission", default=None, help="add a member to a mission's tod.p3d.rz from the archive")
    ap.add_argument("--variant", default="active")
    ap.add_argument("--name", default=None, help="group name (Address_1234567) or a member's sub name")
    ap.add_argument("--sub", default=None, help="name of the new address")
    ap.add_argument("--at", default=None, metavar="x,y,z")
    ap.add_argument("--stage", action="store_true", help="write to work/mod instead of --out")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.mission:
        return _cmd_add(a)

    if a.path is None:
        print("error: give a tod.p3d path, --mission M (add an address), or --revert",
              file=sys.stderr)
        return 2

    blob = open(a.path, "rb").read()
    wrapped = blob[:2] == b"RZ"
    if wrapped:
        blob = unwrap(blob)

    recs = collect(blob)

    if a.set:
        key, _, xyz = a.set.rpartition(":")
        try:
            pos = [float(t) for t in xyz.split(",")]
            if len(pos) != 3:
                raise ValueError
        except ValueError:
            print(f"error: --set needs NAME:x,y,z, got {a.set!r}", file=sys.stderr)
            return 2
        # NAME/SUB targets one record exactly; a bare NAME must be unique.
        if "/" in key:
            want_name, _, want_sub = key.rpartition("/")
            hits = [r for r in recs
                    if r[0]["name"].lower() == want_name.lower()
                    and r[0]["sub"].lower() == want_sub.lower()]
        else:
            hits = [r for r in recs if key.lower() in r[0]["name"].lower()]
        if len(hits) != 1:
            print(f"error: {key!r} matched {len(hits)} record(s); refusing to guess",
                  file=sys.stderr)
            for d, _ in hits:
                print(f"   {d['name']} sub={d['sub']} pos={tuple(round(c,2) for c in d['pos'])}",
                      file=sys.stderr)
            return 2
        if wrapped:
            print("error: input was RZ-compressed; re-encoding is not implemented",
                  file=sys.stderr)
            return 3
        d, payload = hits[0]
        # locate this payload in the file buffer; verify by read-back
        base = blob.find(payload)
        if base < 0:
            print("error: could not locate payload in file", file=sys.stderr)
            return 4
        abs_off = base + d["value_offset"]
        edit = bytearray(blob)
        struct.pack_into("<3f", edit, abs_off, *pos)
        blob = bytes(edit)
        back = struct.unpack_from("<3f", blob, abs_off)
        if any(abs(back[i] - pos[i]) > 1e-3 for i in range(3)):
            print(f"error: read-back mismatch {back} != {pos}", file=sys.stderr)
            return 4
        print(f"set {d['name']} (sub={d['sub']})\n"
              f"  {tuple(round(c, 3) for c in d['pos'])} -> {tuple(pos)}")
        dest = a.out or a.path
        open(dest, "wb").write(blob)
        print(f"wrote {dest}")
        return 0

    shown = 0
    for d, _ in recs:
        if a.filter and (a.filter.lower() not in d["name"].lower()
                         and a.filter.lower() not in d["sub"].lower()):
            continue
        p, q = d["pos"], d["quat"]
        print(f"{d['name']:>18} sub={d['sub']:<16} "
              f"pos=({p[0]:9.3f},{p[1]:8.3f},{p[2]:9.3f}) "
              f"quat=({q[0]:.4f},{q[1]:.4f},{q[2]:.4f},{q[3]:.4f})")
        shown += 1
    print(f"-- {shown} of {len(recs)} Address record(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
