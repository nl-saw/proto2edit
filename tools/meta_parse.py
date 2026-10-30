#!/usr/bin/env python3
"""Parse the meta-object attribute payload (chunk 0x07f00001) exactly.

The payload has three body variants, discriminated by the bytes right after the
`META` magic. All are verified to tile to the byte end of the payload.

  A. entry list - u32 at offset 8 is 0 or 1:
         u32 payload_len, "META", u32 version
         then entries from offset 12:
             u32 key_len, key
             [ u32 class_len, className ]    ONLY IF className != key
             u16 version
             u32 classId
             u8[blob] value                  blob size implied by the class
     `BehaviourController` is a container: its blob is
         u8 active, u32 unknown, u32 count, then `count` entries of the same shape.

  B. name pair - u32 at offset 8 is 2:
         u32 payload_len, "META", u32 version=2
         u8 len, long name ('#'-prefixed), u8 len, short name
     Carries no attributes at all.

  C. name pair + value - no version word; the first body byte is a u8 length:
         u32 payload_len, "META"
         u8 len, long name, u8 len, short name, u8[12] value
     The 12-byte trailer reads as one float plus two u32s in the samples
     (payload 15 has 0x40a00000 = 5.0).

The single most important detail, and the one that broke every earlier attempt:
the class-name string is elided when it is identical to the key.

Usage:
  python3 tools/meta_parse.py <file.p3d|.p3d.rz>            # attribute table
  python3 tools/meta_parse.py <file> --check                # tiling assertion only
  python3 tools/meta_parse.py <file> --object heller        # filter by object

Exit status is non-zero if any payload fails to tile, so this is usable in CI.
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

# Derived per-class value sizes. VERIFIED rows were obtained by hand-deriving
# every boundary of chunk 1 and then reproducing them with a solver; they must
# not be changed without redoing that. The rest are cross-payload majority
# votes and are hypotheses - an unknown class raises rather than guessing.
BLOB = {
    # verified against hand-derived boundaries
    "MatrixAttribute": 28,        # 7 float32: position.xyz + quaternion.xyzw
    "BehaviourController": 9,     # u8 active + u32 unknown + u32 count
    "SimpleRenderBehaviour": 44,
    "PropBehaviour": 3,
    # majority vote across payloads (single- or few-vote: treat as hypothesis)
    "engine::AudioEmitterBehaviour": 7,
    "proto::HitEffectBehaviour": 4,
    "proto::HitReactionBehaviour": 15,
    "proto::HealthBehaviour": 31,
    "engine::PropLogicTreeBehaviour": 3,
    "proto::FEDisplayBehaviour": 39,
    "proto::TargetableBehaviour": 123,
    "engine::PoseAnimationBehaviour": 3,
    "engine::TouchBehaviour": 3,
    "engine::trigger::SphereVolume": 20,
    "GrabbableBehaviour": 4,
    "MotionTreeBehaviour": 3,
    "engine::PlaybackTreeBehaviour": 3,
    "FloatAttribute": 4,
    "GrabBehaviour": 7,
    "proto::GrabbableStaticBehaviour": 4,
    "proto::AttachLightBehaviour": 24,
    "proto::AdvancedHealthBehaviour": 120,
    "PhysicsBehaviour": 46,
    "engine::AudioPhysicsBehaviour": 3,
}

CONTAINERS = {"BehaviourController"}


class ParseError(Exception):
    pass


def u8(p, o):
    if o >= len(p):
        raise ParseError(f"u8 past end at {o}")
    return p[o]


def u16(p, o):
    if o + 2 > len(p):
        raise ParseError(f"u16 past end at {o}")
    return struct.unpack_from("<H", p, o)[0]


def u32(p, o):
    if o + 4 > len(p):
        raise ParseError(f"u32 past end at {o}")
    return struct.unpack_from("<I", p, o)[0]


def printable(b):
    return bool(b) and all(0x20 <= c < 0x7F for c in b)


def s_u32(p, o):
    """u32-length string. Returns (text, new_offset)."""
    n = u32(p, o)
    if not (1 <= n <= 80) or o + 4 + n > len(p):
        raise ParseError(f"bad string length {n} at {o}")
    b = p[o + 4:o + 4 + n]
    if not printable(b):
        raise ParseError(f"non-printable string at {o}")
    return b.decode("latin-1"), o + 4 + n


def s_u8(p, o):
    """u8-length string with no alignment padding. Returns (text, new_offset)."""
    n = u8(p, o)
    if not (1 <= n <= 200) or o + 1 + n > len(p):
        raise ParseError(f"bad u8 string length {n} at {o}")
    b = p[o + 1:o + 1 + n]
    if not printable(b):
        raise ParseError(f"non-printable u8 string at {o}")
    return b.decode("latin-1"), o + 1 + n


CONTAINER_HEADER = 9   # u8 active + u32 unknown + u32 count


def one_entry(p, pos, has_class):
    """Parse one entry. `has_class` says whether the class-name string is stored.

    The class name is elided when it equals the key, but that is not decidable
    from the key alone, so callers backtrack over both choices.
    """
    key, pos = s_u32(p, pos)
    if has_class:
        cls, pos = s_u32(p, pos)
    else:
        cls = key
    ver = u16(p, pos)
    pos += 2
    cid = u32(p, pos)
    pos += 4
    body = pos
    e = dict(key=key, cls=cls, ver=ver, class_id=cid, body=body)

    if cls in CONTAINERS:
        active = u8(p, body)
        unk = u32(p, body + 1)
        cnt = u32(p, body + 5)
        if cnt > 64:
            raise ParseError(f"{cls} count {cnt} implausible")
        kids, after = parse_n(p, body + CONTAINER_HEADER, cnt)
        e.update(active=active, unknown=unk, children=kids,
                 size=(after - body), header=CONTAINER_HEADER)
        return e, after

    if cls not in BLOB:
        raise ParseError(f"unknown class {cls!r} at {body}")
    after = body + BLOB[cls]
    if after > len(p):
        raise ParseError(f"{cls} blob overruns payload")
    e["size"] = BLOB[cls]
    return e, after


def parse_n(p, pos, n):
    """Parse exactly `n` entries starting at pos. Returns (list, end) or raises."""
    out = []
    for _ in range(n):
        got = None
        for has in (True, False):
            try:
                e, pos2 = one_entry(p, pos, has)
            except ParseError:
                continue
            got = (e, pos2)
            break
        if got is None:
            raise ParseError(f"no valid entry {len(out) + 1}/{n} at {pos}")
        e, pos = got
        out.append(e)
    return out, pos


def parse_from(p, pos):
    """Parse entries until the payload ends exactly. Returns list or None."""
    if pos == len(p):
        return []
    for has in (True, False):
        try:
            e, pos2 = one_entry(p, pos, has)
        except ParseError:
            continue
        rest = parse_from(p, pos2)
        if rest is not None:
            return [e] + rest
    return None


def parse_entry_list(p):
    """Variant A. Returns the entry list; raises unless it tiles exactly."""
    ver = u32(p, 8)
    if ver not in (0, 1):
        raise ParseError(f"not an entry list (version {ver})")
    out = parse_from(p, 12)
    if out is None:
        raise ParseError("entries do not tile to the payload end")
    return out


def parse_name_pair(p, with_value):
    """Variants B and C."""
    ver = None
    pos = 8
    if not with_value:
        ver = u32(p, 8)
        if ver != 2:
            raise ParseError(f"not a version-2 name pair (version {ver})")
        pos = 12
    long_name, pos = s_u8(p, pos)
    short_name, pos = s_u8(p, pos)
    value = None
    if with_value:
        if pos + 12 != len(p):
            raise ParseError(f"name pair + value: {pos + 12} != {len(p)}")
        value = p[pos:pos + 12]
        pos += 12
    if pos != len(p):
        raise ParseError(f"name pair ends at {pos}, payload is {len(p)}")
    return dict(version=ver, long_name=long_name, short_name=short_name,
                value=value, entries=[])


def parse_payload(p):
    """Dispatch on the body after 'META'. Raises ParseError if nothing tiles."""
    if p[4:8] != b"META":
        raise ParseError("no META magic at offset 4")
    what = u32(p, 8)
    variants = []
    if what in (0, 1):
        variants = [("entries", lambda: dict(version=what,
                                             entries=parse_entry_list(p)))]
    elif what == 2:
        variants = [("names", lambda: parse_name_pair(p, with_value=False)),
                    ("names+value", lambda: parse_name_pair(p, with_value=True))]
    else:
        variants = [("names+value", lambda: parse_name_pair(p, with_value=True))]
    errors = []
    for name, fn in variants:
        try:
            r = fn()
            r["variant"] = name
            return r
        except ParseError as e:
            errors.append(f"{name}: {e}")
    raise ParseError("; ".join(errors))


def object_index(buf):
    """[(longName, payload)] for every meta-data chunk with its owning object."""
    out = []
    for c in walk(parse(buf)):
        if c.type != META_OBJECT:
            continue
        body = c.data[12:]
        o = 0
        names = []
        for _ in range(3):
            if o >= len(body):
                names = None
                break
            n = body[o]
            if n == 0:
                names.append("")
                o += 1
                continue
            names.append(body[o + 1:o + 1 + n].split(b"\0", 1)[0].decode("latin-1"))
            o += 1 + n
        if not names:
            continue
        for k in c.children:
            if k.type == META_DATA:
                out.append((names[0], k.data[12:]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--object", default=None)
    ap.add_argument("--check", action="store_true", help="only assert tiling")
    a = ap.parse_args(argv)

    blob = open(a.path, "rb").read()
    if blob[:2] == b"RZ":
        blob = unwrap(blob)

    pairs = object_index(blob)
    ok = fail = 0
    failures = []
    for name, payload in pairs:
        try:
            r = parse_payload(payload)
        except ParseError as e:
            fail += 1
            failures.append((name, len(payload), str(e)))
            continue
        ok += 1
        if a.check:
            continue
        if a.object and a.object.lower() not in name.lower():
            continue
        print(f"{name}   [{r['variant']}, {len(payload)}B]")
        if r.get("long_name"):
            print(f"   long={r['long_name']!r} short={r['short_name']!r}")
            if r.get("value"):
                f = struct.unpack_from("<f", r["value"], 0)[0]
                print(f"   value float={f:.4f} u32s={struct.unpack_from('<3I', r['value'], 0)}")
        for e in r["entries"]:
            extra = ""
            if e["cls"] == "MatrixAttribute":
                pos = struct.unpack_from("<7f", payload, e["body"])
                extra = (f"  pos=({pos[0]:.3f},{pos[1]:.3f},{pos[2]:.3f})"
                         f" quat=({pos[3]:.3f},{pos[4]:.3f},{pos[5]:.3f},{pos[6]:.3f})")
            print(f"   {e['key']:28} {e['cls']:32} blob={e['size']:3}{extra}")
            for s in e.get("children", []):
                print(f"      {s['key']:25} {s['cls']:30} blob={s['size']:3}")

    print(f"\n-- tiling: {ok} ok, {fail} failed of {len(pairs)} payload(s)",
          file=sys.stderr)
    for name, n, err in failures:
        print(f"   FAIL {name[:50]} ({n}B): {err}", file=sys.stderr)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
