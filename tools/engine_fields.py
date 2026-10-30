#!/usr/bin/env python3
"""Recover the engine's per-type FIELD NAMES from prototype2engine.dll.

The naming mechanism, established by measurement (docs/19 HANDOFF 49):

  * every property object is built as `alloc 0x18; ctor(<name>); vtable; handler@+0x10`,
    and `<name>` is the FIELD NAME. The marker is `movl $handler,0x10(%esi)` = C7 46 10 xx xx xx xx,
    with a `push $<string>` shortly before it.
  * a class registers a contiguous array of `count` such properties:
    `FUN_10676ab0(array, count, &classDescriptor)`. So a class's field list is the last `count`
    property names built before that call.
  * a type is bound to a class by its registration wrapper, which pushes `(class, init, create, ctx)`
    and calls `FUN_106ada00`. The wrapper's caller pushes the type name and calls the wrapper, so
    type -> class is a two-step read.

Output: a JSON map {type_name: {class, fields}} and a summary on stdout.

This is evidence extraction, not a guess: every field name comes from a string the engine itself
passes to a property constructor, and every type name from the string the engine pushes before the
registration wrapper.
"""
import json
import os
import struct
import sys

DATA_LO, DATA_HI = 0x10C1E000, 0x10E59C30   # .data (VirtualSize extends past SizeOfRawData)


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dll = os.path.join(here, "..", "SteamLibrary", "steamapps", "common",
                       "Prototype 2", "prototype2engine.dll")
    dll = os.environ.get("PROTO2_ENGINE", dll)
    if not os.path.isfile(dll):
        print(f"engine not found: {dll}")
        return 2
    d = open(dll, "rb").read()

    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    osz = struct.unpack_from("<H", d, pe + 20)[0]
    opt = pe + 24
    base = struct.unpack_from("<I", d, opt + 28)[0]
    secs = []
    for i in range(nsec):
        o = opt + osz + i * 40
        vs, va, rs, ra = struct.unpack_from("<IIII", d, o + 8)
        secs.append((base + va, vs, ra))

    def cstr(va):
        for sva, vs, ra in secs:
            if sva <= va < sva + max(vs, 1):
                fo = ra + (va - sva)
                if fo >= len(d):
                    return None
                e = d.find(b"\0", fo)
                s = d[fo:e]
                if 2 <= len(s) <= 64 and all(32 <= c < 127 for c in s):
                    return s.decode("latin-1")
                return None
        return None

    def calls_to(target):
        out = []
        for k in range(len(d) - 5):
            if d[k] == 0xE8 and (k + 5) + struct.unpack_from("<i", d, k + 1)[0] + 0x10000C00 == target:
                out.append(k)
        return out

    # 1. property objects: (offset, name)
    props = []
    i = 0
    while True:
        i = d.find(b"\xc7\x46\x10", i)
        if i < 0:
            break
        name = None
        for j in range(max(0, i - 72), i):
            if d[j] == 0x68:
                s = cstr(struct.unpack_from("<I", d, j + 1)[0])
                if s:
                    name = s
        if name:
            props.append((i, name))
        i += 3

    # 2. class -> field list
    class_fields = {}
    prev = 0
    for rc in calls_to(0x10676AB0):
        cnt = None
        for j in range(max(0, rc - 12), rc):
            if d[j] == 0x6A:
                cnt = d[j + 1]
        cls = None
        for j in range(max(0, rc - 60), rc - 6):
            if d[j] == 0x68:
                va = struct.unpack_from("<I", d, j + 1)[0]
                if DATA_LO <= va < DATA_HI:
                    cls = va
        if cnt and 0 < cnt <= 60 and cls is not None:
            win = [n for off, n in props if prev < off < rc]
            if len(win) >= cnt:
                class_fields.setdefault(cls, win[-cnt:])
        prev = rc

    # 3. type -> class, via the registration wrapper
    site_class = {}
    for c in calls_to(0x106ADA00):
        cls = None
        for j in range(max(0, c - 120), c):
            if d[j] == 0x68:
                va = struct.unpack_from("<I", d, j + 1)[0]
                if DATA_LO <= va < DATA_HI:
                    cls = va
        if cls:
            site_class[c] = cls
    mapping = {}
    for i in range(len(d) - 5):
        if d[i] != 0xE8:
            continue
        tgt = (i + 5) + struct.unpack_from("<i", d, i + 1)[0] + 0x10000C00
        fo = tgt - 0x10000C00
        for c, cls in site_class.items():
            if fo <= c <= fo + 200:
                name = None
                for j in range(max(0, i - 16), i):
                    if d[j] == 0x68:
                        s = cstr(struct.unpack_from("<I", d, j + 1)[0])
                        if s:
                            name = s
                if name:
                    mapping.setdefault(name, cls)
                break

    # second registration wrapper: FUN_106ADAC0, whose callers push the type name and whose
    # enclosing function pushes (class, init, create). This path covers the audio family
    # (music, excludeVolume, dialogue, ...) which the FUN_106ADA00 path does not.
    def func_start(c):
        j = c
        while j > 0 and d[j] != 0xCC:
            j -= 1
        while j < len(d) and d[j] == 0xCC:
            j += 1
        return j

    site2 = {}
    for c in calls_to(0x106ADAC0):
        cls = None
        for j in range(max(0, c - 160), c):
            if d[j] == 0x68:
                va = struct.unpack_from("<I", d, j + 1)[0]
                if DATA_LO <= va < DATA_HI:
                    cls = va
        if cls:
            site2.setdefault(func_start(c), cls)
    for i in range(len(d) - 5):
        if d[i] != 0xE8:
            continue
        fo = ((i + 5) + struct.unpack_from("<i", d, i + 1)[0] + 0x10000C00) - 0x10000C00
        if fo not in site2:
            continue
        name = None
        for j in range(max(0, i - 20), i):
            if d[j] == 0x68:
                sname = cstr(struct.unpack_from("<I", d, j + 1)[0])
                if sname:
                    name = sname
        if name:
            mapping.setdefault(name, site2[fo])

    out = {}
    for nm, cls in mapping.items():
        entry = {"class": hex(cls)}
        if cls in class_fields:
            entry["fields"] = class_fields[cls]
        out[nm] = entry

    dest = os.path.join(here, "artifacts", "engine-class-fields.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
        fh.write("\n")

    with_fields = sum(1 for v in out.values() if "fields" in v)
    print(f"types named            : {len(out)}")
    print(f"  with a field list    : {with_fields}")
    print(f"  property objects seen: {len(props)}")
    print(f"  class field lists    : {len(class_fields)}")
    print(f"wrote {dest}")
    for nm in ("event_String", "hasTag", "conditionGroup", "alertStatus", "isPlayerCharacter"):
        e = out.get(nm)
        print(f"  {nm:<18} {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
