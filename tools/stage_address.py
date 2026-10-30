#!/usr/bin/env python3
"""Stage an edited world-placement table (`tod.p3d`) as a loose mod file.

This is the end-to-end path from "I want to move a mission's enemy spawn" to
"the game will load my file":

    archive entry  art/missions/<mission>/<variant>/tod.p3d.rz
      -> unwrap RZ
      -> edit Address records (tools/address.py does the decoding)
      -> re-wrap RZ
      -> write work/mod/art/missions/<mission>/<variant>/tod.p3d.rz

`./proto2 install` then copies `work/mod` over the game directory as loose files,
which `PreferLooseFiles = true` makes take priority over the archives.

RZ note: re-wrapping is **functionally** lossless (`unwrap(wrap(x)) == x`, verified)
but not byte-identical to the shipped stream, because the original used a
different zlib level. That is fine for a mod - the bytes differ, the content does
not.

Usage:
  python3 tools/stage_address.py --mission story_intro2 --list
  python3 tools/stage_address.py --mission story_intro2 --filter Infected
  python3 tools/stage_address.py --mission story_intro2 \
      --set 'Address_8113829/Infected1_1:2000,50,-2000'
  python3 tools/stage_address.py --mission story_intro2 --variants   # active/open only
  python3 tools/stage_address.py --revert                            # drop staged files
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import address as A  # noqa: E402
from rcf import Rcf  # noqa: E402
from rz import unwrap, wrap  # noqa: E402
from p3d import parse, walk  # noqa: E402
import config  # noqa: E402

GAME = config.game_dir()
MOD = os.path.join(ROOT, "work", "mod")
ARCHIVES = ["art.rcf", "boot.rcf"]


def find_entry(mission, variant):
    rel = f"art/missions/{mission}/{variant}/tod.p3d.rz".lower()
    for arc in ARCHIVES:
        path = os.path.join(GAME, arc)
        if not os.path.exists(path):
            continue
        rcf = Rcf(path)
        try:
            for e in rcf.entries:
                if e.path.lower() == rel:
                    rcf._fh.seek(e.offset)
                    return path, e, rcf._fh.read(e.size)
        finally:
            rcf._fh.close()
    return None, None, None


def stage(mission, variant, blob, verbose=True):
    dest = os.path.join(MOD, "art", "missions", mission, variant, "tod.p3d.rz")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(blob)
    if verbose:
        print(f"staged {dest}  ({len(blob)} bytes)")
    return dest


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mission", default=None)
    ap.add_argument("--variant", default="active")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--filter", default=None)
    ap.add_argument("--set", default=None, action="append",
                    metavar="NAME[/SUB]:x,y,z",
                    help="repeatable; each is applied to the same staged file")
    ap.add_argument("--shift-matching", default=None, action="append",
                    metavar="REGEX:dx,dy,dz",
                    help="repeatable; shift every record whose sub matches REGEX")
    ap.add_argument("--place-matching", default=None, action="append",
                    metavar="REGEX:x,y,z",
                    help="repeatable; set every record whose sub matches REGEX "
                         "to the same absolute position")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args(argv)

    if a.revert:
        import shutil
        base = os.path.join(MOD, "art", "missions")
        n = 0
        for dirpath, _, names in os.walk(base):
            for nm in names:
                if nm.startswith("tod.p3d"):
                    os.remove(os.path.join(dirpath, nm))
                    n += 1
        print(f"removed {n} staged tod.p3d file(s)")
        return 0

    if not a.mission:
        print("error: --mission is required (or --revert)", file=sys.stderr)
        return 1

    arc, entry, blob = find_entry(a.mission, a.variant)
    if blob is None:
        print(f"error: no art/missions/{a.mission}/{a.variant}/tod.p3d.rz "
              f"in {ARCHIVES}", file=sys.stderr)
        return 1
    if a.list or a.filter:
        print(f"{entry.path}  ({len(blob)} bytes in {arc})", file=sys.stderr)

    wrapped = blob[:2] == b"RZ"
    inner = unwrap(blob) if wrapped else blob

    if not a.set and not a.shift_matching and not a.place_matching:
        # read-only listing: reuse address.py's decoder over a temp buffer
        recs = A.collect(inner)
        shown = 0
        for d, _ in recs:
            if a.filter and (a.filter.lower() not in d["name"].lower()
                             and a.filter.lower() not in d["sub"].lower()):
                continue
            p, q = d["pos"], d["quat"]
            print(f"{d['name']:>18} sub={d['sub']:<22} "
                  f"pos=({p[0]:9.3f},{p[1]:8.3f},{p[2]:9.3f})")
            shown += 1
        print(f"-- {shown} of {len(recs)} record(s)", file=sys.stderr)
        return 0

    edits = []
    for spec in (a.set or []):
        key, _, xyz = spec.rpartition(":")
        try:
            pos = [float(t) for t in xyz.split(",")]
            if len(pos) != 3:
                raise ValueError
        except ValueError:
            print(f"error: --set needs NAME[/SUB]:x,y,z, got {spec!r}", file=sys.stderr)
            return 2
        recs = A.collect(inner)
        if "/" in key:
            want_name, _, want_sub = key.rpartition("/")
            hits = [r for r in recs if r[0]["name"].lower() == want_name.lower()
                    and r[0]["sub"].lower() == want_sub.lower()]
        else:
            hits = [r for r in recs if key.lower() in r[0]["name"].lower()]
        if len(hits) != 1:
            print(f"error: {key!r} matched {len(hits)} record(s); refusing to guess",
                  file=sys.stderr)
            for d, _ in hits[:40]:
                print(f"   {d['name']} sub={d['sub']}", file=sys.stderr)
            if len(hits) > 40:
                print(f"   ... {len(hits) - 40} more", file=sys.stderr)
            return 2
        d0, pay0 = hits[0]
        off = inner.find(pay0)
        if off < 0:
            print(f"error: could not locate payload for {key}", file=sys.stderr)
            return 4
        edits.append((off + d0["value_offset"], list(pos), key, d0["pos"]))

    for spec in (a.shift_matching or []):
        rx, _, delta = spec.rpartition(":")
        try:
            dv = [float(t) for t in delta.split(",")]
            if len(dv) != 3:
                raise ValueError
        except ValueError:
            print(f"error: --shift-matching needs REGEX:dx,dy,dz, got {spec!r}", file=sys.stderr)
            return 2
        pat = re.compile(rx, re.I)
        hits = [(r[0], r[1]) for r in A.collect(inner) if pat.search(r[0]["sub"])]
        if not hits:
            print(f"error: --shift-matching {rx!r} matched no record", file=sys.stderr)
            return 2
        for d, payload in hits:
            off = inner.find(payload)
            if off < 0:
                print(f"error: could not locate payload for {d['sub']}", file=sys.stderr)
                return 4
            edits.append((off + d["value_offset"],
                          [d["pos"][i] + dv[i] for i in range(3)],
                          f"{d['name']}/{d['sub']}", d["pos"]))
        print(f"shift-matching {rx!r}: {len(hits)} record(s) by {dv}", file=sys.stderr)

    for spec in (a.place_matching or []):
        rx, _, xyz = spec.rpartition(":")
        try:
            pos = [float(t) for t in xyz.split(",")]
            if len(pos) != 3:
                raise ValueError
        except ValueError:
            print(f"error: --place-matching needs REGEX:x,y,z, got {spec!r}", file=sys.stderr)
            return 2
        pat = re.compile(rx, re.I)
        hits = [(r[0], r[1]) for r in A.collect(inner) if pat.search(r[0]["sub"])]
        if not hits:
            print(f"error: --place-matching {rx!r} matched no record", file=sys.stderr)
            return 2
        for d, payload in hits:
            off = inner.find(payload)
            if off < 0:
                print(f"error: could not locate payload for {d['sub']}", file=sys.stderr)
                return 4
            edits.append((off + d["value_offset"], list(pos),
                          f"{d['name']}/{d['sub']}", d["pos"]))
        print(f"place-matching {rx!r}: {len(hits)} record(s) -> {tuple(pos)}", file=sys.stderr)

    import struct
    # Every edit carries an absolute file offset resolved BEFORE any modification,
    # so duplicate edits on the same record are safe and the last one wins.
    edit = bytearray(inner)
    for abs_off, pos, key, orig in edits:
        struct.pack_into("<3f", edit, abs_off, *pos)
    inner = bytes(edit)
    last = {}
    for abs_off, pos, key, orig in edits:
        last[abs_off] = (pos, key)          # a later edit supersedes an earlier one
    for abs_off, (pos, key) in last.items():
        back = struct.unpack_from("<3f", inner, abs_off)
        if any(abs(back[i] - pos[i]) > 1e-3 for i in range(3)):
            print(f"error: read-back mismatch {back} != {pos} for {key}", file=sys.stderr)
            return 4

    out = wrap(inner) if wrapped else inner
    if wrapped and unwrap(out) != inner:
        print("error: RZ re-wrap is not lossless; refusing to stage", file=sys.stderr)
        return 5
    dest = stage(a.mission, a.variant, out)
    # verify the staged file parses and every requested edit is present
    staged = open(dest, "rb").read()
    check = unwrap(staged) if staged[:2] == b"RZ" else staged
    got_all = {(dd["name"], dd["sub"]): dd["pos"] for dd, _ in A.collect(check)}
    final = {}
    for abs_off, pos, key, orig in edits:
        final[key] = (orig, pos)
    bad = 0
    for key, (orig, pos) in final.items():
        nm, _, sub = key.partition("/")
        got = got_all.get((nm, sub))
        if got is None:
            print(f"error: staged file lost {key}", file=sys.stderr)
            return 6
        ok = all(abs(got[i] - pos[i]) < 1e-2 for i in range(3))
        print(f"{key}\n"
              f"  {tuple(round(c, 3) for c in orig)} -> "
              f"{tuple(round(c, 3) for c in got)}   verified={ok}")
        if not ok:
            bad += 1
    print(f"-- {len(final)} record(s) edited", file=sys.stderr)
    return 0 if bad == 0 else 6


if __name__ == "__main__":
    sys.exit(main())
