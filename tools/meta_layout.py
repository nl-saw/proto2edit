#!/usr/bin/env python3
"""Parser for the Pure3D META payload -- chunk type 0x07f00001.

A placed object is a `0x07f00000` meta-object chunk whose single child,
`0x07f00001`, carries the attributes and behaviours of that object.  This tool
decodes both layers and *proves* the decode by tiling: every byte of the META
body is consumed by exactly one parsed field, and the parser refuses to emit a
half-parse (see `LayoutError`).

Layout (all little-endian; see `analysis/meta-layout.md` for the byte evidence):

  0x07f00000  meta-object header (== whole body, header_size == total_size)
      0x00  aligned_u8  long name   e.g. "GameObject(1):sign004"
            aligned_u8  short name  e.g. "sign004"
            aligned_u8  class name  e.g. "GameObject"
            u16         version     (1 for every shipped object observed)
            u16         flags       (1 for GameObject instances, 0 for templates)
            u32         class id    (constant per class name, NOT a name hash)

  0x07f00001  META body
      0x00  u32         payload_len   == len(body) - 4
      0x04  char[4]     "META"
      0x08  class-specific body, e.g. for GameObject:
              u32 attribute_count
              attribute_count x ATTRIBUTE
              1 x BEHAVIOUR (BehaviourController)

  ATTRIBUTE (= a keyed value)
      u32  key_len ; key  (no NUL)
      u32  type_len; type name
      u16  version
      u32  type id            (constant per type name)
      value, length implied by the type

  BEHAVIOUR / NODE entry (= an unkeyed named object)
      u32  name_len ; name
      u16  version
      u32  type id
      value, length recovered from the enclosing list (count + exact tiling)

`aligned_u8` is `u8 n` followed by `n` bytes where `n = len + ((-len) % 4)`, so
a string whose length is already a multiple of four has no NUL terminator.
`u8 string` and `u32 Name` are the two other string encodings used inside
values; they are *not* padded.

Usage:
  python3 tools/meta_layout.py dump  <file>... [--class C] [--filter S] [--hex] [--limit N]
  python3 tools/meta_layout.py stats <file>...
  python3 tools/meta_layout.py diff  --group NAME <file>...
  python3 tools/meta_layout.py transform <file>...

Exit codes:
  0  ok
  1  usage / unsupported request
  2  LAYOUT FAILURE -- some object did not tile (nothing half-parsed is kept)
  3  a class reached with no confirmed body layout (dump only)
"""

import argparse
import collections
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p3d import parse as p3d_parse, walk  # noqa: E402
from rz import unwrap  # noqa: E402

META_OBJECT = 0x07F00000
META_DATA = 0x07F00001
META_MAGIC = b"META"
META_HEADER = 8          # u32 payload_len + "META"
ROOT_ID = 0xFF443350


class LayoutError(Exception):
    """The bytes do not tile.  Never caught to print a partial result."""


class UnconfirmedClass(LayoutError):
    """Reached a class whose body layout is explicitly not confirmed."""


# ---------------------------------------------------------------------------
# Evidence tables (analysis/meta-layout.md documents the derivation)
#
# ATTR_TYPES: type name -> (type id, fixed value size or None for a Name)
#   Sizes: MatrixAttribute 28 (7 x f32) observed 103x, VectorAttribute 12 (3 x
#   f32) 2x, FloatAttribute 4 (f32) 1x, BoolAttribute 4 (u32) 2x,
#   NameAttribute 4+len (u32 Name) 2x, engine::PoseAttribute 1 byte 1x.
#   Every id below was constant across every occurrence in the corpus and the
#   deterministic parse below re-validates it on every run.
ATTR_TYPES = {
    "MatrixAttribute":        (0x1EE55A37, 28),
    "VectorAttribute":        (0x86C9A026, 12),
    "FloatAttribute":         (0xCB652B6D, 4),
    "BoolAttribute":          (0xA694FB29, 4),
    "NameAttribute":          (0x7B920778, None),   # u32-length-prefixed
    "engine::PoseAttribute":  (0x22B64949, 1),      # 1 byte, semantics unknown
}
ATTR_NAME_TYPES = frozenset({"NameAttribute"})

# Behaviour types observed in the corpus, name -> class id.
# 36 types, every one with a single id across all occurrences.
BEHAVIOUR_TYPES = {
    "BehaviourController":                    0x3199934B,
    "AiTreeBehaviour":                        0x922EFB78,
    "AttachableSmartNodeBehaviour":           0x3CBB86F9,
    "GrabBehaviour":                          0x48396FAD,
    "GrabbableBehaviour":                     0x3AC56CD6,
    "LODRenderBehaviour":                     0xC81C2597,
    "MotionTreeBehaviour":                    0x016D0A51,
    "PhysicsBehaviour":                       0x62EF718F,
    "PropBehaviour":                          0x6C805D97,
    "SimpleRenderBehaviour":                  0x8A0241EE,
    "SmartNodeBehaviour":                     0xE595567E,
    "WebReceiverBasicBehaviour":              0x42395704,
    "WebReceiverBehaviour":                   0xC368515F,
    "engine::AudioEmitterBehaviour":          0x4D138304,
    "engine::AudioPhysicsBehaviour":          0x9D365C05,
    "engine::AudioUDOBehaviour":              0xA7C446C9,
    "engine::PlaybackTreeBehaviour":          0x348F30A8,
    "engine::PoseAnimationBehaviour":         0x6C805D97,   # same id as PropBehaviour
    "engine::PropLogicTreeBehaviour":         0xD353C789,
    "engine::TouchBehaviour":                 0x63940C7B,
    "engine::trigger::SphereVolume":          0xE8CC19B3,   # nested field type
    "proto::AddToHeightmapBehaviour":         0x70403686,
    "proto::AdvancedHealthBehaviour":         0x512691AE,
    "proto::AlertBehaviour":                  0x55E04B9D,
    "proto::AttachLightBehaviour":            0x531A30FD,
    "proto::CollisionActionBehaviour":        0x73CED8EF,
    "proto::DeformShaderBehaviour":           0x1A2442BE,
    "proto::FEDisplayBehaviour":              0x0BD27CEC,
    "proto::GrabbableStaticBehaviour":        0x3173B036,
    "proto::HealthBehaviour":                 0x25D5CDCA,
    "proto::HitEffectBehaviour":              0x6911D9B6,
    "proto::HitReactionBehaviour":            0x408163B5,
    "proto::NISAssetBehaviour":               0x6C805D97,   # same id as PropBehaviour
    "proto::PropTargetableByAIBehaviour":     0x35C2A997,
    "proto::ProtoColourVariationBehaviour":   0x92AD772F,
    "proto::RandomDecalBehaviour":            0x802F0661,
    "proto::TargetableBehaviour":             0x03AD0067,
}

# SmartNodeArray member types (record format is the same as BEHAVIOUR entry).
NODE_TYPES = {
    "InteractiveSmartNode":       0xE8D0AC8C,
    "SmartNodeLocatorPoint":      0x9A3C8643,
    "WebSmartNodeWithGeometry":   0xE35470B0,
    "WebSmartNode":               0xE83C89B7,
    "SimpleSmartNode":            0x50A96064,
}

# Classes whose body layout is proven (see analysis/meta-layout.md).
PARSED_CLASSES = {
    "GameObject", "GameObjectTemplateBuilder", "PropTemplate",
    "SmartNodeArray", "LODRenderDataLoader", "proto::DamageScale",
}
# Reached but body layout NOT confirmed -> counted and reported, never guessed.
UNCONFIRMED_CLASSES = {"proto::ShaderPalette"}


# ---------------------------------------------------------------------------
# primitive readers
def u8(b, p):
    return b[p]


def u16(b, p):
    return struct.unpack_from("<H", b, p)[0]


def u32(b, p):
    return struct.unpack_from("<I", b, p)[0]


def f32(b, p):
    return struct.unpack_from("<f", b, p)[0]


def read_aligned_u8(b, p):
    """u8 n then n bytes; n = len + ((-len) % 4) (padding counts in n).

    A length that is a multiple of four therefore has no NUL terminator.  The
    trailing zero padding is stripped, not treated as part of the text.
    """
    n = b[p]
    p += 1
    raw = b[p:p + n]
    if len(raw) != n:
        raise LayoutError(f"aligned_u8 at {p - 1:#x}: truncated ({n} bytes)")
    return raw.rstrip(b"\0").decode("latin-1"), p + n


def read_u8_string(b, p):
    """u8 n then n bytes, no padding, no NUL (used inside values)."""
    n = b[p]
    p += 1
    raw = b[p:p + n]
    if len(raw) != n:
        raise LayoutError(f"u8 string at {p - 1:#x}: truncated ({n} bytes)")
    return raw.decode("latin-1"), p + n


def read_u32_name(b, p):
    """u32 n then n bytes (the `Name` meta type)."""
    n = u32(b, p)
    p += 4
    if n > 4096 or p + n > len(b):
        raise LayoutError(f"u32 name at {p - 4:#x}: bad length {n}")
    return b[p:p + n].decode("latin-1"), p + n


def _printable(raw):
    return len(raw) > 0 and all(32 <= c < 127 for c in raw)


# ---------------------------------------------------------------------------
# meta-object chunk (0x07f00000)
class MetaObject:
    __slots__ = ("file", "chunk", "full_name", "name", "class_name", "version",
                 "flags", "class_id", "payload", "payload_rel")

    def __repr__(self):
        return (f"<MetaObject {self.class_name} {self.name!r} "
                f"@{self.chunk.offset:#x} payload={len(self.payload)}>")


def parse_meta_object(buf, chunk, file="<mem>"):
    """Decode the 0x07f00000 header; raises LayoutError unless it tiles exactly."""
    h = buf[chunk.offset + 12:chunk.offset + chunk.header_size]
    p = 0
    full_name, p = read_aligned_u8(h, p)
    name, p = read_aligned_u8(h, p)
    class_name, p = read_aligned_u8(h, p)
    if p + 8 != len(h):
        raise LayoutError(
            f"meta-object at {chunk.offset:#x}: strings end at {p}, "
            f"need 8 more bytes, have {len(h) - p}")
    version = u16(h, p)
    flags = u16(h, p + 2)
    class_id = u32(h, p + 4)
    mo = MetaObject()
    mo.file = file
    mo.chunk = chunk
    mo.full_name = full_name
    mo.name = name
    mo.class_name = class_name
    mo.version = version
    mo.flags = flags
    mo.class_id = class_id
    try:
        child = next(c for c in chunk.children if c.type == META_DATA)
    except StopIteration:
        raise LayoutError(f"meta-object at {chunk.offset:#x}: no 0x07f00001 child")
    if child.header_size != child.total_size:
        raise LayoutError(
            f"0x07f00001 at {child.offset:#x}: header_size {child.header_size} "
            f"!= total_size {child.total_size} (payload would be non-leaf)")
    mo.payload_rel = child.offset + 12
    mo.payload = buf[child.offset + 12:child.offset + child.header_size]
    return mo


def iter_meta_objects(buf, file="<mem>"):
    root = p3d_parse(buf)
    for c in walk(root):
        if c.type == META_OBJECT:
            yield parse_meta_object(buf, c, file)


# ---------------------------------------------------------------------------
# entries
class Entry:
    """One decoded record.  Offsets are relative to the META body start."""
    __slots__ = ("kind", "key", "type_name", "version", "class_id",
                 "header_offset", "value_offset", "value", "decoded",
                 "children", "list_info")

    def __init__(self, kind, key, type_name, version, class_id,
                 header_offset, value_offset, value):
        self.kind = kind
        self.key = key
        self.type_name = type_name
        self.version = version
        self.class_id = class_id
        self.header_offset = header_offset
        self.value_offset = value_offset
        self.value = value
        self.decoded = None
        self.children = None
        self.list_info = None

    @property
    def header_len(self):
        return self.value_offset - self.header_offset

    def label(self):
        if self.kind == "attribute":
            return f"attribute {self.key!r} : {self.type_name}"
        if self.kind == "behaviour":
            return f"behaviour {self.type_name}"
        if self.kind == "node":
            return f"node {self.type_name}"
        return f"{self.kind} {self.type_name}"

    def __repr__(self):
        return (f"<Entry {self.label()} @{self.header_offset:#x} "
                f"value={len(self.value)}B>")


def _read_attr_header(b, pos, end):
    """u32 key_len, key, u32 type_len, type, u16 version, u32 type id."""
    if pos + 4 > end:
        raise LayoutError(f"attribute header at {pos:#x}: past end")
    klen = u32(b, pos)
    if not (1 <= klen <= 200) or pos + 4 + klen + 4 > end:
        raise LayoutError(f"attribute header at {pos:#x}: bad key length {klen}")
    key = b[pos + 4:pos + 4 + klen]
    if not _printable(key):
        raise LayoutError(f"attribute header at {pos:#x}: key not printable {key!r}")
    q = pos + 4 + klen
    tlen = u32(b, q)
    if not (1 <= tlen <= 200) or q + 4 + tlen + 6 > end:
        raise LayoutError(f"attribute header at {q:#x}: bad type length {tlen}")
    tname = b[q + 4:q + 4 + tlen]
    if not _printable(tname):
        raise LayoutError(f"attribute header at {q:#x}: type not printable {tname!r}")
    o = q + 4 + tlen
    return {
        "key": key.decode("latin-1"),
        "type": tname.decode("latin-1"),
        "version": u16(b, o),
        "id": u32(b, o + 2),
        "end": o + 6,
    }


def _read_type_header(b, pos, end):
    """u32 name_len, name, u16 version, u32 type id (behaviour / node record)."""
    if pos + 4 > end:
        return None
    n = u32(b, pos)
    if not (1 <= n <= 200) or pos + 4 + n + 6 > end:
        return None
    name = b[pos + 4:pos + 4 + n]
    if not _printable(name):
        return None
    o = pos + 4 + n
    return {
        "name": name.decode("latin-1"),
        "version": u16(b, o),
        "id": u32(b, o + 2),
        "pos": pos,
        "end": o + 6,
    }


def _is_behaviour_member(name):
    """List members are Behaviour subclasses; nested field values are not.

    `engine::trigger::SphereVolume` is a *field* of engine::TouchBehaviour (see
    the text form), not a list member, so it must never terminate a blob.
    """
    return name.endswith("Behaviour") or name == "BehaviourController"


def _type_candidates(b, start, end, table, member_filter=None):
    """All positions in [start,end) whose record name+id match `table`."""
    out = []
    p = start
    while p + 6 <= end:
        h = _read_type_header(b, p, end)
        if (h is not None and table.get(h["name"]) == h["id"]
                and (member_filter is None or member_filter(h["name"]))):
            out.append(h)
        p += 1
    return out


def _tile_list(cands, start, end, count, limit=2, value_ok=None, b_=None):
    """Chain exactly `count` records from `start`; last value runs to `end`.

    Returns a list of solutions (at most `limit`), each a list of candidate
    dicts.  A solution consumes every record header and lets the last record's
    value run to `end`, so a solution *is* the tiling.
    """
    by_pos = {h["pos"]: h for h in cands}
    positions = sorted(by_pos)
    sols = []

    def rec(i, pos, acc):
        if len(sols) >= limit:
            return
        h = by_pos.get(pos)
        if h is None:
            return
        if i == count - 1:
            if h["end"] <= end and (value_ok is None or value_ok(b_[h["end"]:end])):
                acc.append(h)
                sols.append(list(acc))
                acc.pop()
            return
        for p2 in positions:
            if p2 < h["end"]:
                continue
            if value_ok is not None and not value_ok(b_[h["end"]:p2]):
                continue
            acc.append(h)
            rec(i + 1, p2, acc)
            acc.pop()

    acc = []
    rec(0, start, acc)
    return sols


def _behaviour_value_ok(v):
    """Every observed behaviour value starts with u8 flag, u8 zero, u8 state.

    The first byte varies (0x01, 0x04, 0x05, 0x11, 0x55, 0x9d) and the third is
    usually 0 but 1 occurs (heli_gunship_husk_front); the second byte is 0 in
    every behaviour value in the corpus, which is enough to reject nested
    non-behaviour records without over-constraining.
    """
    return len(v) >= 3 and v[1] == 0


def _node_value_ok(v):
    """A SmartNodeArray member value starts with a u32 Name (n then n bytes)."""
    if len(v) < 5:
        return False
    n = u32(v, 0)
    return 1 <= n <= 200 and 4 + n <= len(v) and _printable(v[4:4 + n])


def _parse_typed_list(b, start, end, count, table, what, member_filter=None,
                      value_ok=None):
    """Parse `count` type records that must tile [start,end); return entries."""
    if count < 0 or count > 4096:
        raise LayoutError(f"{what}: implausible count {count}")
    if count == 0:
        if start != end:
            raise LayoutError(f"{what}: count 0 but {end - start} bytes left")
        return []
    cands = _type_candidates(b, start, end, table, member_filter)
    sols = _tile_list(cands, start, end, count, value_ok=value_ok, b_=b)
    if not sols:
        # fall back: unknown type name but a *known* id is still strong evidence
        ids = set(table.values())
        cands2 = []
        p = start
        while p + 6 <= end:
            h = _read_type_header(b, p, end)
            if (h is not None and h["id"] in ids
                    and (member_filter is None or member_filter(h["name"]))):
                cands2.append(h)
            p += 1
        sols = _tile_list(cands2, start, end, count, value_ok=value_ok, b_=b)
        if sols:
            unknown = sorted({h["name"] for h in sols[0]} - set(table))
            warn = f"{what}: unknown type name(s) {unknown} matched by id"
            return _entries_from_solution(sols[0], b, end, what), [warn]
        raise LayoutError(
            f"{what}: no tiling for {count} records in [{start:#x},{end:#x}) "
            f"({len(cands)} candidate headers)")
    if len(sols) > 1:
        raise LayoutError(
            f"{what}: ambiguous tiling ({len(sols)}+ solutions) for {count} "
            f"records in [{start:#x},{end:#x})")
    return _entries_from_solution(sols[0], b, end, what), []


def _entries_from_solution(sol, b, end, what):
    out = []
    for i, h in enumerate(sol):
        nxt = sol[i + 1]["pos"] if i + 1 < len(sol) else end
        if nxt < h["end"]:
            raise LayoutError(f"{what}: overlapping record at {h['pos']:#x}")
        ent = Entry("behaviour", None, h["name"], h["version"], h["id"],
                    h["pos"], h["end"], b[h["end"]:nxt])
        out.append(ent)
    return out


# ---------------------------------------------------------------------------
# per-type value decoders (only where the bytes prove the layout)
def decode_value(type_name, value):
    """Return a dict of decoded fields, or None when semantics are unconfirmed."""
    if type_name == "MatrixAttribute" and len(value) == 28:
        px, py, pz = struct.unpack_from("<3f", value, 0)
        qx, qy, qz, qw = struct.unpack_from("<4f", value, 12)
        return {"position": (px, py, pz), "quaternion": (qx, qy, qz, qw)}
    if type_name == "VectorAttribute" and len(value) == 12:
        return {"vector": struct.unpack("<3f", value)}
    if type_name == "FloatAttribute" and len(value) == 4:
        return {"float": f32(value, 0)}
    if type_name == "BoolAttribute" and len(value) == 4:
        v = u32(value, 0)
        return {"bool": bool(v), "raw_u32": v}
    if type_name == "NameAttribute" and len(value) >= 4:
        n = u32(value, 0)
        if n <= len(value) - 4:
            return {"name": value[4:4 + n].decode("latin-1")}
    return None


# ---------------------------------------------------------------------------
# body parsers
class Payload:
    __slots__ = ("meta_object", "declared_len", "entries", "warnings", "extra",
                 "body_start")

    def __init__(self, meta_object):
        self.meta_object = meta_object
        self.declared_len = None
        self.entries = []
        self.warnings = []
        self.extra = {}
        self.body_start = META_HEADER   # first byte of the class-specific body

    @property
    def body_len(self):
        return len(self.meta_object.payload)

    def tiled_bytes(self):
        """Number of META-body bytes covered by a parsed field."""
        n = self.body_start
        for e in self.entries:
            n += e.header_len + len(e.value)
        return n


def _check_header(payload):
    if len(payload) < META_HEADER:
        raise LayoutError("META body shorter than 8 bytes")
    declared = u32(payload, 0)
    if payload[4:8] != META_MAGIC:
        raise LayoutError(f"bad META magic {payload[4:8]!r}")
    if declared != len(payload) - 4:
        raise LayoutError(
            f"payload_len {declared} != body-4 ({len(payload) - 4})")
    return declared


def _parse_gameobject(payload, out):
    out.body_start = 12
    count = u32(payload, 8)
    pos = 12
    for i in range(count):
        h = _read_attr_header(payload, pos, len(payload))
        info = ATTR_TYPES.get(h["type"])
        if info is None:
            raise LayoutError(
                f"attribute type {h['type']!r} has no confirmed value layout "
                f"(at {pos:#x}); refusing to guess")
        tid, fixed = info
        if h["id"] != tid:
            raise LayoutError(
                f"attribute {h['key']!r}: type id {h['id']:#010x} != table "
                f"{tid:#010x} for {h['type']}")
        if h["type"] in ATTR_NAME_TYPES:
            _, vend = read_u32_name(payload, h["end"])
            vlen = vend - h["end"]
        else:
            vlen = fixed
        if h["end"] + vlen > len(payload):
            raise LayoutError(f"attribute {h['key']!r}: value overruns payload")
        ent = Entry("attribute", h["key"], h["type"], h["version"], h["id"],
                    pos, h["end"], payload[h["end"]:h["end"] + vlen])
        ent.decoded = decode_value(h["type"], ent.value)
        out.entries.append(ent)
        pos = h["end"] + vlen
    # exactly one BehaviourController record closes the body
    h = _read_type_header(payload, pos, len(payload))
    if h is None:
        raise LayoutError(f"no behaviour record at {pos:#x} after {count} attributes")
    if h["name"] != "BehaviourController":
        raise LayoutError(
            f"expected BehaviourController at {pos:#x}, got {h['name']!r}")
    if h["id"] != BEHAVIOUR_TYPES["BehaviourController"]:
        raise LayoutError(
            f"BehaviourController id {h['id']:#010x} != "
            f"{BEHAVIOUR_TYPES['BehaviourController']:#010x}")
    blob = payload[h["end"]:]
    bc = Entry("behaviour", None, h["name"], h["version"], h["id"],
               pos, h["end"], blob)
    bc.children, bc.list_info, warns = _parse_behaviour_value(blob, h["end"])
    out.warnings.extend(warns)
    out.entries.append(bc)
    pos = len(payload)


def _parse_behaviour_value(blob, abs_base):
    """BehaviourController value: u8, u16=0, u16, u32 count, entries."""
    if len(blob) < 9:
        raise LayoutError(f"BehaviourController value too short ({len(blob)}B)")
    flags = blob[0]
    reserved = u16(blob, 1)
    states = u16(blob, 3)
    count = u32(blob, 5)
    warns = []
    if reserved != 0:
        # try to locate (u32 count, entries) elsewhere; record a warning
        found = None
        for pre in range(1, min(17, len(blob) - 4)):
            c = u32(blob, pre)
            try:
                ents, w = _parse_typed_list(blob, pre + 4, len(blob), c,
                                            BEHAVIOUR_TYPES, "behaviour list",
                                            _is_behaviour_member, _behaviour_value_ok)
            except LayoutError:
                continue
            if found is None:
                found = (pre, c, ents, w)
            else:
                raise LayoutError("BehaviourController: ambiguous list start")
        if found is None:
            raise LayoutError(
                f"BehaviourController: reserved={reserved:#06x} and no "
                f"alternative list start tiles")
        pre, c, ents, w = found
        warns = [f"BehaviourController: list starts at {pre} (reserved was "
                 f"{reserved:#06x})"] + w
        _rebase(ents, abs_base)
        return ents, {"flags": flags, "reserved": reserved, "states": states,
                      "count": c, "start": pre}, warns
    ents, w = _parse_typed_list(blob, 9, len(blob), count,
                                BEHAVIOUR_TYPES, "behaviour list",
                                _is_behaviour_member, _behaviour_value_ok)
    warns.extend(w)
    _rebase(ents, abs_base)
    return ents, {"flags": flags, "reserved": reserved, "states": states,
                  "count": count, "start": 9}, warns


def _rebase(entries, base):
    """Re-express child offsets relative to the META body (not the parent value)."""
    for e in entries:
        e.header_offset += base
        e.value_offset += base
        if e.children:
            _rebase(e.children, base)


def _parse_gameobject_template(payload, out):
    out.body_start = 12
    count = u32(payload, 8)
    pos = 12
    out.extra["count"] = count
    for _ in range(count):
        start = pos
        name, pos = read_u8_string(payload, pos)
        ent = Entry("name", None, "u8 string", 0, 0, start, start + 1,
                    payload[start + 1:pos])
        ent.decoded = {"string": name}
        out.entries.append(ent)
    if pos != len(payload):
        raise LayoutError(f"GameObjectTemplateBuilder: {pos:#x} != {len(payload):#x}")


def _parse_proptemplate(payload, out):
    out.body_start = 8
    pos = 8
    a, pos = read_u8_string(payload, pos)
    b_, pos = read_u8_string(payload, pos)
    scale = f32(payload, pos)
    pos += 4
    u1 = u32(payload, pos)
    pos += 4
    u2 = u32(payload, pos)
    pos += 4
    if pos != len(payload):
        raise LayoutError(f"PropTemplate: {pos:#x} != {len(payload):#x}")
    out.extra.update({"prop_name": a, "render_only": b_, "float": scale,
                      "u32_1": u1, "u32_2": u2})
    ent = Entry("name", None, "PropTemplate", 0, 0, 8, 8, payload[8:pos])
    ent.decoded = {"prop_name": a, "render_only": b_, "float": scale,
                   "u32_1": u1, "u32_2": u2}
    out.entries.append(ent)


def _parse_lod(payload, out):
    out.body_start = 12
    count = u32(payload, 8)
    pos = 12
    out.extra["count"] = count
    for _ in range(count):
        start = pos
        name, pos = read_u8_string(payload, pos)
        dist = f32(payload, pos)
        pos += 4
        ent = Entry("name", None, "LOD", 0, 0, start, start + 1,
                    payload[start + 1:pos])
        ent.decoded = {"name": name, "distance": dist}
        out.entries.append(ent)
    if pos != len(payload):
        raise LayoutError(f"LODRenderDataLoader: {pos:#x} != {len(payload):#x}")


def _parse_smartnodearray(payload, out):
    out.body_start = 12
    count = u32(payload, 8)
    out.extra["count"] = count
    ents, warns = _parse_typed_list(payload, 12, len(payload), count,
                                    NODE_TYPES, "SmartNodeArray",
                                    None, _node_value_ok)
    for e in ents:
        e.kind = "node"
    out.entries.extend(ents)
    out.warnings.extend(warns)


def _parse_damagescale(payload, out):
    out.body_start = META_HEADER
    body = payload[META_HEADER:]
    if len(body) % 4:
        raise LayoutError(f"DamageScale: {len(body)} bytes not a float array")
    vals = list(struct.unpack(f"<{len(body) // 4}f", body))
    ent = Entry("array", None, "float32[]", 0, 0, META_HEADER, META_HEADER, body)
    ent.decoded = {"floats": vals}
    out.entries.append(ent)
    out.extra["count"] = len(vals)


BODY_PARSERS = {
    "GameObject": _parse_gameobject,
    "GameObjectTemplateBuilder": _parse_gameobject_template,
    "PropTemplate": _parse_proptemplate,
    "LODRenderDataLoader": _parse_lod,
    "SmartNodeArray": _parse_smartnodearray,
    "proto::DamageScale": _parse_damagescale,
}


def parse_payload(mo):
    """Parse one META body; raises LayoutError unless it tiles exactly."""
    out = Payload(mo)
    out.declared_len = _check_header(mo.payload)
    parser = BODY_PARSERS.get(mo.class_name)
    if parser is None:
        raise UnconfirmedClass(
            f"class {mo.class_name!r} has no confirmed META body layout "
            f"(object {mo.full_name!r})")
    parser(mo.payload, out)
    covered = out.tiled_bytes()
    if covered != len(mo.payload):
        raise LayoutError(
            f"{mo.class_name} {mo.full_name!r}: tiling {covered} != payload "
            f"{len(mo.payload)}")
    # each behaviour's children must tile its own value exactly
    for e in out.entries:
        if e.kind == "behaviour" and e.children is not None:
            total = e.list_info["start"] + sum(
                c.header_len + len(c.value) for c in e.children)
            if total != len(e.value):
                raise LayoutError(
                    f"{e.type_name} value: children tile {total} != {len(e.value)}")
    return out


# ---------------------------------------------------------------------------
# file loading
def load_file(path):
    blob = open(path, "rb").read()
    if blob[:2] == b"RZ":
        blob = unwrap(blob)
    return blob


def parse_file(path):
    """Parse every meta-object; returns (parsed, unconfirmed).

    A class with no confirmed body layout is reported, never guessed.  Any
    other LayoutError propagates so the caller can exit non-zero.
    """
    buf = load_file(path)
    objs = []
    unconfirmed = []
    for mo in iter_meta_objects(buf, path):
        try:
            p = parse_payload(mo)
        except UnconfirmedClass as exc:
            unconfirmed.append((mo, str(exc)))
            continue
        objs.append((mo, p))
    return objs, unconfirmed


def _signature(payload):
    parts = []
    for e in payload.entries:
        if e.kind == "attribute":
            parts.append(f"attr:{e.key}:{e.type_name}")
        elif e.kind == "behaviour":
            kids = ",".join(c.type_name for c in (e.children or []))
            parts.append(f"beh:{e.type_name}[{kids}]")
        elif e.kind == "node":
            parts.append(f"node:{e.type_name}")
        else:
            parts.append(f"{e.kind}:{e.type_name}")
    return tuple(parts)


# ---------------------------------------------------------------------------
# commands
def cmd_dump(args):
    failures = []
    unconfirmed = []
    for path in args.paths:
        try:
            objs, unc = parse_file(path)
        except LayoutError as exc:
            print(f"LAYOUT FAILURE {path}: {exc}", file=sys.stderr)
            failures.append((path, str(exc)))
            continue
        unconfirmed.extend(unc)
        print(f"=== {path}: {len(objs)} meta-object(s) parsed"
              f"{f', {len(unc)} unconfirmed' if unc else ''}")
        shown = 0
        for mo, pl in objs:
            if args.klass and args.klass.lower() not in mo.class_name.lower():
                continue
            if args.filter and args.filter.lower() not in (mo.name + mo.full_name).lower():
                continue
            if args.limit and shown >= args.limit:
                break
            shown += 1
            print(f"\n0x{mo.chunk.offset:08x}  {mo.class_name}  {mo.full_name}")
            print(f"    version={mo.version} flags={mo.flags} class_id=0x{mo.class_id:08x}"
                  f"  body={len(mo.payload)} declared_len={pl.declared_len}")
            extra = pl.extra
            if "prop_name" in extra:
                print(f"    propName={extra['prop_name']!r} renderOnly={extra['render_only']!r}"
                      f" f32={extra['float']!r} u32={extra['u32_1']},{extra['u32_2']}")
            if "count" in extra:
                print(f"    count={extra['count']}")
            for i, e in enumerate(pl.entries):
                _dump_entry(e, i, mo, args, depth=0)
            for w in pl.warnings:
                print(f"    ! {w}")
        if shown == 0:
            print("    (no meta-object matched)")
    for mo, why in unconfirmed:
        print(f"UNCONFIRMED CLASS: {mo.full_name}: {why}", file=sys.stderr)
    if failures:
        print(f"\n{len(failures)} file(s) failed the tiling proof", file=sys.stderr)
        return 2
    if unconfirmed:
        print(f"\n{len(unconfirmed)} object(s) skipped: unconfirmed body layout",
              file=sys.stderr)
        return 3
    return 0


def _dump_entry(e, i, mo, args, depth):
    ind = "    " + "  " * depth
    abs_off = mo.payload_rel + e.value_offset
    if e.kind == "attribute":
        print(f"{ind}[{i}] {e.label()} v={e.version} id=0x{e.class_id:08x}")
        print(f"{ind}     header@{e.header_offset:#06x} value@{e.value_offset:#06x}"
              f" (abs 0x{abs_off:08x}) len={len(e.value)}")
    elif e.kind in ("behaviour", "node"):
        print(f"{ind}[{i}] {e.label()} v={e.version} id=0x{e.class_id:08x}")
        print(f"{ind}     header@{e.header_offset:#06x} value@{e.value_offset:#06x}"
              f" (abs 0x{abs_off:08x}) len={len(e.value)}")
        if e.list_info and depth == 0:
            print(f"{ind}     value: flags=0x{e.list_info['flags']:02x}"
                  f" reserved={e.list_info['reserved']} states={e.list_info['states']}"
                  f" count={e.list_info['count']}")
    else:
        print(f"{ind}[{i}] {e.label()} header@{e.header_offset:#06x}"
              f" value@{e.value_offset:#06x} (abs 0x{abs_off:08x}) len={len(e.value)}")
    if e.decoded:
        print(f"{ind}     decoded: {_fmt_decoded(e.decoded)}")
    elif e.value and e.children is None:
        print(f"{ind}     value(hex) = {_short_hex(e.value)}")
    elif e.value:
        print(f"{ind}     value(hex) = {_short_hex(e.value)} (parent of "
              f"{len(e.children)} record(s) listed below)")
    if e.children is not None:
        for j, c in enumerate(e.children):
            _dump_entry(c, j, mo, args, depth + 1)


def _short_hex(v, limit=48):
    """Hex for the dump: first `limit` bytes, then a length marker."""
    if len(v) <= limit:
        return v.hex(" ")
    return f"{v[:limit].hex(' ')} … (+{len(v) - limit} bytes)"


def _fmt_decoded(d):
    out = []
    for k, v in d.items():
        if isinstance(v, tuple) and len(v) == 3 and isinstance(v[0], float):
            out.append(f"{k}=({v[0]:.6g}, {v[1]:.6g}, {v[2]:.6g})")
        elif isinstance(v, tuple) and len(v) == 4 and isinstance(v[0], float):
            out.append(f"{k}=({v[0]:.6g}, {v[1]:.6g}, {v[2]:.6g}, {v[3]:.6g})")
        elif isinstance(v, list):
            out.append(f"{k}=[{len(v)} values]")
        else:
            out.append(f"{k}={v!r}")
    return "  ".join(out)


def cmd_stats(args):
    files = []
    all_objs = []
    unconfirmed = []
    failures = []
    for path in args.paths:
        try:
            objs, unc = parse_file(path)
        except LayoutError as exc:
            print(f"LAYOUT FAILURE {path}: {exc}", file=sys.stderr)
            failures.append((path, str(exc)))
            continue
        files.append(path)
        all_objs.extend(objs)
        unconfirmed.extend(unc)
    if not all_objs:
        print("no meta-objects parsed", file=sys.stderr)
        return 2

    print(f"files: {len(files)}   meta-objects: {len(all_objs)}")
    cls = collections.Counter(mo.class_name for mo, _ in all_objs)
    print("\n-- classes --")
    for c, n in cls.most_common():
        print(f"  {c:30} {n}")

    print("\n-- payload header (u32 len == body-4, magic 'META') --")
    ok = 0
    for mo, pl in all_objs:
        if pl.declared_len == len(mo.payload) - 4 and mo.payload[4:8] == META_MAGIC:
            ok += 1
    print(f"  {ok}/{len(all_objs)}")

    print("\n-- entry-level tiling (bytes covered == payload size) --")
    print(f"  {len(all_objs)}/{len(all_objs)} parsed objects tile exactly")
    tot_bytes = sum(len(mo.payload) for mo, _ in all_objs)
    print(f"  payload bytes covered: {tot_bytes}/{tot_bytes}")

    # nested list sizes
    lists = [e for _, pl in all_objs for e in pl.entries
             if e.children is not None]
    print(f"\n-- behaviour lists --")
    print(f"  lists: {len(lists)}")
    cnt = collections.Counter(e.list_info["count"] for e in lists)
    print(f"  member-count histogram: {dict(sorted(cnt.items()))}")
    print("  list prefix bytes: flags + u16 reserved + u16 states")
    fl = collections.Counter(f"0x{e.list_info['flags']:02x}" for e in lists)
    print(f"  flags values: {dict(fl)}")
    st = collections.Counter(e.list_info["states"] for e in lists)
    print(f"  states values: {dict(st)}")

    print("\n-- entry vocabulary --")
    keys = collections.Counter()
    atypes = collections.Counter()
    btypes = collections.Counter()
    ntypes = collections.Counter()
    for _, pl in all_objs:
        for e in pl.entries:
            if e.kind == "attribute":
                keys[e.key] += 1
                atypes[e.type_name] += 1
            elif e.kind == "behaviour":
                btypes[e.type_name] += 1
                for c in (e.children or []):
                    btypes[c.type_name] += 1
            elif e.kind == "node":
                ntypes[e.type_name] += 1
    print(f"  distinct attribute keys: {len(keys)}")
    for k, n in keys.most_common():
        print(f"     {n:5}  {k}")
    print(f"  distinct attribute types: {len(atypes)}")
    for k, n in atypes.most_common():
        print(f"     {n:5}  {k}")
    print(f"  distinct behaviour types: {len(btypes)}")
    for k, n in btypes.most_common():
        print(f"     {n:5}  {k}")
    if ntypes:
        print(f"  distinct node types: {len(ntypes)}")
        for k, n in ntypes.most_common():
            print(f"     {n:5}  {k}")

    print("\n-- attribute values --")
    vals = collections.defaultdict(list)
    for _, pl in all_objs:
        for e in pl.entries:
            if e.kind == "attribute":
                vals[e.type_name].append(e)
    for t, lst in sorted(vals.items()):
        lens = collections.Counter(len(e.value) for e in lst)
        print(f"  {t:24} n={len(lst):3} value-lens={dict(lens)}")
    mats = [e for e in vals.get("MatrixAttribute", []) if e.decoded]
    if mats:
        ident = sum(1 for e in mats if e.decoded["position"] == (0.0, 0.0, 0.0)
                    and e.decoded["quaternion"] == (0.0, 0.0, 0.0, -1.0))
        norms = [math.sqrt(sum(c * c for c in e.decoded["quaternion"])) for e in mats]
        print(f"  MatrixAttribute: identity={ident}/{len(mats)}")
        print(f"  |quaternion| min={min(norms):.9f} max={max(norms):.9f} "
              f"(max |1-|q|| = {max(abs(1 - n) for n in norms):.3g})")

    print("\n-- behaviour value sizes by type --")
    bs = collections.defaultdict(collections.Counter)
    for _, pl in all_objs:
        for e in pl.entries:
            if e.kind == "behaviour" and e.children is not None:
                for c in e.children:
                    bs[c.type_name][len(c.value)] += 1
    for t, c in sorted(bs.items()):
        sizes = sorted(c)
        kind = "fixed" if len(sizes) == 1 else "variable"
        print(f"  {t:40} {kind:8} sizes={dict(sorted(c.items()))}")

    print("\n-- header flags/class ids --")
    fs = collections.Counter((mo.class_name, mo.version, mo.flags) for mo, _ in all_objs)
    for (c, v, f), n in sorted(fs.items()):
        print(f"  {c:30} version={v} flags={f}  n={n}")

    if failures:
        print(f"\n{len(failures)} file(s) FAILED the tiling proof", file=sys.stderr)
        for p, e in failures:
            print(f"  {p}: {e}", file=sys.stderr)
        return 2
    if unconfirmed:
        print(f"\nTILING PROOF: OK for {len(all_objs)} parsed objects; "
              f"{len(unconfirmed)} object(s) in unconfirmed classes excluded")
    else:
        print("\nTILING PROOF: OK")
    return 0


def _group_objects(all_objs):
    groups = collections.OrderedDict()
    for mo, pl in all_objs:
        sig = _signature(pl)
        groups.setdefault(sig, []).append((mo, pl))
    return groups


def cmd_diff(args):
    all_objs = []
    for path in args.paths:
        try:
            objs, unc = parse_file(path)
        except LayoutError as exc:
            print(f"LAYOUT FAILURE {path}: {exc}", file=sys.stderr)
            return 2
        if unc:
            print(f"note: {len(unc)} object(s) in {path} skipped (unconfirmed class)",
                  file=sys.stderr)
        all_objs.extend(objs)
    groups = _group_objects(all_objs)
    print(f"{len(all_objs)} object(s) in {len(groups)} group(s) with distinct entry lists")
    print("\n-- groups --")
    for gi, (sig, members) in enumerate(groups.items()):
        text = " | ".join(sig)
        print(f"  [{gi}] n={len(members):3}  {text[:150]}{'...' if len(text) > 150 else ''}")
    needle = (args.group or "").lower()
    hits = [(gi, sig, mem) for gi, (sig, mem) in enumerate(groups.items())
            if needle and (needle in " | ".join(sig).lower()
                           or any(needle in mo.name.lower() or needle in mo.full_name.lower()
                                  for mo, _ in mem))]
    if not hits:
        print(f"\nno group matched {args.group!r}", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"\n-- {len(hits)} groups matched --")
        for gi, sig, mem in hits:
            print(f"  [{gi}] n={len(mem)}  {' | '.join(sig)[:120]}")
    for gi, sig, members in hits:
        _diff_group(gi, sig, members, args)
    return 0


def _diff_group(gi, sig, members, args):
    print(f"\n=== group [{gi}] n={len(members)} ===")
    print("  signature: " + " | ".join(sig))
    print("  members:")
    for mo, _ in members[:args.members]:
        print(f"    {mo.name:42} {mo.file}")
    if len(members) > args.members:
        print(f"    ... {len(members) - args.members} more")
    # align by entry index (identical signature => identical entry count)
    ref = members[0][1]
    for idx, ref_ent in enumerate(ref.entries):
        entries = []
        for mo, pl in members:
            assert idx < len(pl.entries)
            entries.append(pl.entries[idx])
        print(f"\n  [{idx}] {ref_ent.label()}"
              f" class_id=0x{ref_ent.class_id:08x} v={ref_ent.version}")
        lens = sorted({len(e.value) for e in entries})
        if len(lens) == 1:
            print(f"       value length: {lens[0]} (all members)")
        else:
            print(f"       value lengths: {lens}")
        vals = [e.value for e in entries]
        if len(set(vals)) == 1:
            print(f"       CONSTANT value: {vals[0].hex(' ')}")
        else:
            n = max(len(v) for v in vals)
            const = [i for i in range(n) if len({v[i] for v in vals if i < len(v)}) <= 1]
            vary = [i for i in range(n) if i not in const]
            print(f"       VARYING bytes: {_ranges(vary)}")
            for mo, e in zip([m for m, _ in members], entries[:args.members]):
                dec = e.decoded
                if dec:
                    print(f"         {mo.name:40} {_fmt_decoded(dec)}  {e.value.hex(' ')}")
                else:
                    print(f"         {mo.name:40} {e.value.hex(' ')}")
        if ref_ent.children is not None:
            for j, child in enumerate(ref_ent.children):
                kids = [e.children[j] for e in entries
                        if e.children is not None and len(e.children) > j]
                if len(kids) != len(entries):
                    print(f"       child[{j}] {child.label()}: members differ in child count")
                    continue
                kv = [c.value for c in kids]
                print(f"       child[{j}] {child.label()} class_id=0x{child.class_id:08x}", end="")
                if len(set(kv)) == 1:
                    print(f"  CONSTANT {kv[0].hex(' ')}")
                else:
                    print(f"  varying (lens {sorted({len(v) for v in kv})})")
                    for mo, c in zip([m for m, _ in members], kids[:args.members]):
                        print(f"         {mo.name:40} {c.value.hex(' ')}")


def cmd_transform(args):
    """Test float32 / float16 / int16 / uint16 readings of MatrixAttribute.

    Each hypothesis places the same 3+4 field split at the stride that
    hypothesis implies:
        float32 -> position f32[3] at 0, quaternion f32[4] at 12   (28 bytes)
        float16 -> position f16[3] at 0, quaternion f16[4] at 6    (14 of 28 bytes)
        int16   -> position i16[3] at 0, quaternion i16[4] at 6    (14 of 28 bytes)
        uint16  -> position u16[3] at 0, quaternion u16[4] at 6    (14 of 28 bytes)
    A real rotation quaternion has norm 1.0.  That single invariant decides the
    question, and it also shows how much of the 28-byte value each hypothesis
    leaves unaccounted for.
    """
    all_objs = []
    for path in args.paths:
        try:
            objs, unc = parse_file(path)
        except LayoutError as exc:
            print(f"LAYOUT FAILURE {path}: {exc}", file=sys.stderr)
            return 2
        all_objs.extend(objs)
    mats = []
    for mo, pl in all_objs:
        for e in pl.entries:
            if e.type_name == "MatrixAttribute":
                mats.append((mo, e))
    if not mats:
        print("no MatrixAttribute in these files", file=sys.stderr)
        return 1
    print(f"MatrixAttribute values: {len(mats)} of 28 bytes each\n")
    fail = {k: 0 for k in ("f32", "f16", "i16", "u16")}
    print(f"{'object':36} {'f32 |q|':>10} {'f16 |q|':>10} {'i16/32768':>11}"
          f" {'u16/32768':>11}  f32 position")
    for mo, e in mats:
        v = e.value
        q32 = struct.unpack_from("<4f", v, 12)
        n32 = math.sqrt(sum(c * c for c in q32))
        q16 = struct.unpack_from("<4e", v, 6)
        n16 = math.sqrt(sum(c * c for c in q16))
        qi = struct.unpack_from("<4h", v, 6)
        ni = math.sqrt(sum((c / 32768.0) ** 2 for c in qi))
        qu = struct.unpack_from("<4H", v, 6)
        nu = math.sqrt(sum((c / 32768.0) ** 2 for c in qu))
        for k, n in (("f32", n32), ("f16", n16), ("i16", ni), ("u16", nu)):
            if abs(n - 1.0) > 1e-6:
                fail[k] += 1
        px, py, pz = struct.unpack_from("<3f", v, 0)
        print(f"{mo.name:36} {n32:10.6f} {n16:10.6f} {ni:11.6f} {nu:11.6f}"
              f"  ({px:.5g}, {py:.5g}, {pz:.5g})")
    print(f"\nrows whose quaternion norm is NOT 1.0 (|q|-1 > 1e-6):")
    for k in ("f32", "f16", "i16", "u16"):
        print(f"  {k:4} {fail[k]:3}/{len(mats)}")
    print("\nVERDICT")
    ok = [k for k in fail if fail[k] == 0]
    print(f"  hypotheses surviving with every quaternion unit-norm: {ok}")
    if ok == ["f32"]:
        print("  float32 position+quaternion CONFIRMED; every 16-bit hypothesis")
        print("  is eliminated, and would in any case leave 14 of the 28 bytes")
        print("  unaccounted for (the next record starts at +28).")
    return 0


def _ranges(idx):
    if not idx:
        return "none"
    out = []
    start = prev = idx[0]
    for i in idx[1:]:
        if i == prev + 1:
            prev = i
            continue
        out.append((start, prev))
        start = prev = i
    out.append((start, prev))
    return ", ".join(f"{a:#04x}" if a == b else f"{a:#04x}-{b:#04x}" for a, b in out)


# ---------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    """Usage errors exit 1 so that exit 2 always means a tiling failure."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def main(argv=None):
    ap = _Parser(description="Parse the Pure3D META payload (chunk 0x07f00001)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="hex + parsed fields per meta-object")
    d.add_argument("paths", nargs="+")
    d.add_argument("--class", dest="klass", default=None)
    d.add_argument("--filter", default=None)
    d.add_argument("--hex", action="store_true", help="(raw values are already shown)")
    d.add_argument("--limit", type=int, default=0)
    d.set_defaults(func=cmd_dump)

    s = sub.add_parser("stats", help="counts, tiling proof, entry-name frequency")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_stats)

    f = sub.add_parser("diff", help="differential table for identical entry lists")
    f.add_argument("--group", required=True)
    f.add_argument("paths", nargs="+")
    f.add_argument("--members", type=int, default=24)
    f.set_defaults(func=cmd_diff)

    t = sub.add_parser("transform", help="float32/16, int16, uint16 hypothesis test")
    t.add_argument("paths", nargs="+")
    t.set_defaults(func=cmd_transform)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except LayoutError as exc:
        print(f"LAYOUT FAILURE: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
