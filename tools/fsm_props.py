#!/usr/bin/env python3
"""Extract the engine's property-name vocabulary from its registration code.

Every class registers its serialised properties by calling `FUN_10676ab0` with a
list of (name, property type, reader). Each name is pushed as a string literal
immediately before the call, so the whole vocabulary can be recovered statically
without running the game.

The names are what appear in the FSM graph as `uid(name)` container ids
(`initialTracks`, `conditions`, `execute`...), so this is the dictionary needed to
read the graph structurally.

    python3 tools/fsm_props.py --dll <prototype2engine.dll>
    python3 tools/fsm_props.py --dll ... --check chunk,conditions,music

`--check` cross-references names observed in a mission graph against the swept
vocabulary and reports which are accounted for - the check that keeps this honest
rather than aspirational.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from peinfo import PE  # noqa: E402
from uid import uid    # noqa: E402

REGISTER_HELPER = 0x10676AB0
SCENARIO_LO = 0x102D0000
SCENARIO_HI = 0x102D6000


def registration_sites(pe, lo_va=None, hi_va=None):
    """Addresses of every `call FUN_10676ab0` in .text (optionally ranged)."""
    d = pe.data
    base = pe.image_base
    out = []
    for sec in pe.sections:
        if sec["name"] != ".text":
            continue
        data = d[sec["raw_ptr"]:sec["raw_ptr"] + sec["raw_size"]]
        for i in range(len(data) - 5):
            if data[i] != 0xE8:
                continue
            rel = struct.unpack_from("<i", data, i + 1)[0]
            va = base + sec["vaddr"] + i
            if (va + 5 + rel) & 0xFFFFFFFF != REGISTER_HELPER:
                continue
            if lo_va is not None and not (lo_va <= va < hi_va):
                continue
            out.append(va)
    return out


def sweep(pe, window=0x200, lo_va=None, hi_va=None):
    """{name: [call site va]} for strings pushed before registration calls."""
    d = pe.data
    n = len(d)
    base = pe.image_base

    def off_of(v):
        return pe.rva_to_offset(v - base) if v >= base else None

    def cstr(v):
        o = off_of(v)
        if o is None or not (0 <= o < n - 2):
            return None
        end = d.find(b"\x00", o, min(o + 48, n))
        if end <= o:
            return None
        s = d[o:end]
        if 2 <= len(s) <= 32 and all(32 <= b < 127 for b in s):
            return s.decode("latin-1")
        return None

    sites = []
    for sec in pe.sections:
        if sec["name"] != ".text":
            continue
        data = d[sec["raw_ptr"]:sec["raw_ptr"] + sec["raw_size"]]
        for i in range(len(data) - 5):
            if data[i] != 0xE8:
                continue
            rel = struct.unpack_from("<i", data, i + 1)[0]
            va = base + sec["vaddr"] + i
            if (va + 5 + rel) & 0xFFFFFFFF != REGISTER_HELPER:
                continue
            if lo_va is not None and not (lo_va <= va < hi_va):
                continue
            sites.append(va)

    vocab = {}
    for site in sites:
        so = off_of(site)
        if so is None:
            continue
        j = max(0, so - window)
        while j + 5 <= min(so, n - 5):
            if d[j] == 0x68:
                name = cstr(struct.unpack_from("<I", d, j + 1)[0])
                if name:
                    vocab.setdefault(name, []).append(site)
            j += 1
    return vocab, sites


PROLOGUES = (b"\x55\x8b\xec", b"\x56\x8b\xf1", b"\x53\x8b\xd9",
             b"\x8b\xff\x55\x8b\xec")


def func_start(pe, va, limit=0x900):
    """Nearest preceding function prologue, so properties group per class.

    Properties must be attributed per registration function: the same name can
    map to a different reader for different classes (`initialTracks` is a
    TrackGroup of StateMachine in one table and of ReferenceStateMachine in
    another).
    """
    base = pe.image_base
    o = pe.rva_to_offset(va - base)
    if o is None:
        return None
    d = pe.data
    for back in range(0, limit):
        p = o - back
        if p < 0:
            break
        for pr in PROLOGUES:
            if d[p:p + len(pr)] == pr:
                return va - back
    return None


def by_function(pe, lo_va, hi_va):
    """[(func_start, [property names])] for registrations in the address range."""
    d = pe.data
    n = len(d)
    base = pe.image_base
    sites = [v for v in registration_sites(pe, lo_va, hi_va)]

    def off_of(v):
        return pe.rva_to_offset(v - base) if v >= base else None

    def cstr(v):
        o = off_of(v)
        if o is None or not (0 <= o < n - 2):
            return None
        end = d.find(b"\x00", o, min(o + 40, n))
        if end <= o:
            return None
        s = d[o:end]
        if 2 <= len(s) <= 30 and all(32 <= b < 127 for b in s):
            return s.decode("latin-1")
        return None

    groups = {}
    for site in sites:
        fs = func_start(pe, site)
        if fs is None:
            continue
        a, b = off_of(fs), off_of(site)
        names = []
        j = max(0, a)
        while j + 5 <= min(b, n - 5):
            if d[j] == 0x68:
                t = cstr(struct.unpack_from("<I", d, j + 1)[0])
                if t and t not in names:
                    names.append(t)
            j += 1
        groups.setdefault(fs, []).extend(x for x in names if x not in groups.get(fs, []))
    return sorted(groups.items())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dll", required=True)
    ap.add_argument("--scenario-only", action="store_true",
                    help="restrict to the proto::fightitems::scenario cluster")
    ap.add_argument("--check", default=None,
                    help="comma-separated names to look up (e.g. observed graph containers)")
    ap.add_argument("--ids", action="store_true", help="show each name's uid hash")
    ap.add_argument("--by-function", action="store_true",
                    help="group scenario-cluster properties per registration function")
    a = ap.parse_args(argv)

    pe = PE(a.dll)
    lo, hi = (SCENARIO_LO, SCENARIO_HI) if a.scenario_only else (None, None)
    vocab, sites = sweep(pe, lo_va=lo, hi_va=hi)
    print(f"{len(sites)} registration call site(s), {len(vocab)} distinct name(s)")

    if a.by_function:
        for fs, names in by_function(pe, SCENARIO_LO, SCENARIO_HI):
            if names:
                print(f"  {fs:#010x}: {', '.join(names[:12])}")
        return 0

    if a.check:
        wanted = [w.strip() for w in a.check.split(",") if w.strip()]
        hit = [w for w in wanted if w in vocab]
        miss = [w for w in wanted if w not in vocab]
        print(f"\naccounted for ({len(hit)}/{len(wanted)}): {', '.join(hit) or '-'}")
        if miss:
            print(f"NOT in the swept vocabulary: {', '.join(miss)}")
            print("  (a name can be registered through a different helper, or be")
            print("   structural rather than a property - 'chunk' is the graph root)")
        return 0

    for name in sorted(vocab):
        if a.ids:
            print(f"  {uid(name):#018x}  {name}")
        else:
            print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
