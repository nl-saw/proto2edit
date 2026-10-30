#!/usr/bin/env python3
"""Build a loose-file mod for the game's first mission (story_intro2).

Produces a tree that mirrors archive paths, ready to copy into the game folder
where the installed PrototypeFix `PreferLooseFiles` hook will pick it up:

  art/hud/fe_textbible.p3d.rz        mission title, front-end text bible
  art/hud/textbible_english.p3d      mission title, standalone English bible
  scripts/missiondefinit.lua         mission definition (time of day)

Usage:
  python3 tools/make_first_mission_mod.py --game "<Prototype 2 dir>" \
      --out work/mod --title "AWAKENING!!!" --tod TOD_GREENZONE_DAWN
"""

import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lua51 import load, dump, resolve_path          # noqa: E402
from rcf import Rcf                                  # noqa: E402
from rz import unwrap, wrap                          # noqa: E402
from textbible import holders, rebuild_document      # noqa: E402

MISSION = "story_intro2"
TITLE_KEY = f"MISSION_NAME_{MISSION}"


def patch_bible(buf, key, value, language="english"):
    """Replace one value at any length. Returns (new_bytes, old, index).

    The document is re-serialised: value offsets, the storage data length and
    every chunk's `header_size`/`total_size` are recomputed. The result is
    re-parsed and verified before being returned.
    """
    hs = holders(bytes(buf))
    h = next((x for x in hs if x.language.lower() == language.lower()), None)
    if h is None:
        raise SystemExit(f"no {language} holder; languages={[x.language for x in hs]}")
    i = h.index(key)
    if i < 0:
        raise SystemExit(f"key {key} not in {language} holder")
    old = h.value(i)
    original = list(h.values)
    h.values[i] = value.encode("latin-1")
    out = rebuild_document(bytes(buf), hs)
    check = holders(out)
    ch = next(x for x in check if x.language.lower() == language.lower())
    if ch.value(i) != value:
        raise SystemExit("rebuild verification failed: edit did not survive")
    for k in range(len(original)):
        if k != i and ch.value(k).encode("latin-1") != original[k]:
            raise SystemExit(f"rebuild verification failed: value {k} changed")
    return out, old, i


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, help="Prototype 2 install directory")
    ap.add_argument("--out", required=True, help="output directory for the loose tree")
    ap.add_argument("--title", required=True, help=f"new title for {MISSION}")
    ap.add_argument("--tod", default="TOD_GREENZONE_DAWN",
                    help="replacement time-of-day constant, or 'none' to leave it alone")
    ap.add_argument("--proto-path", default="103", help="missiondefinit proto path for the mission")
    ap.add_argument("--tod-index", type=int, default=42, help="constant index holding the TOD value")
    a = ap.parse_args(argv)

    out = a.out
    os.makedirs(out, exist_ok=True)
    log = []

    # --- 1. front-end text bible (boot.rcf, RZ wrapped) --------------------
    with Rcf(os.path.join(a.game, "boot.rcf")) as arc:
        entry = arc.find("art\\hud\\fe_textbible.p3d.rz")
        if entry is None:
            raise SystemExit("art\\hud\\fe_textbible.p3d.rz missing from boot.rcf")
        raw = unwrap(arc.read(entry))
    raw, old, idx = patch_bible(raw, TITLE_KEY, a.title)
    dest = os.path.join(out, "art", "hud", "fe_textbible.p3d.rz")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(wrap(bytes(raw)))
    log.append(f"title   {dest}: {old!r} -> {a.title!r} (index {idx})")

    # --- 2. standalone English bible (art.rcf, uncompressed) --------------
    with Rcf(os.path.join(a.game, "art.rcf")) as arc:
        entry = arc.find("art\\hud\\textbible_english.p3d")
        if entry is None:
            raise SystemExit("art\\hud\\textbible_english.p3d missing from art.rcf")
        raw2, old2, idx2 = patch_bible(bytearray(arc.read(entry)), TITLE_KEY, a.title)
    dest2 = os.path.join(out, "art", "hud", "textbible_english.p3d")
    os.makedirs(os.path.dirname(dest2), exist_ok=True)
    with open(dest2, "wb") as fh:
        fh.write(bytes(raw2))
    log.append(f"title   {dest2}: {old2!r} -> {a.title!r} (index {idx2})")

    if a.tod.lower() == "none":
        print("\n".join(log))
        print(f"\nloose tree ready at {out} (mission definitions untouched)")
        return 0

    # --- 3. mission definition (boot.rcf, Lua bytecode) --------------------
    with Rcf(os.path.join(a.game, "boot.rcf")) as arc:
        entry = arc.find("scripts\\missiondefinit.lua")
        if entry is None:
            raise SystemExit("scripts\\missiondefinit.lua missing from boot.rcf")
        chunk = load(arc.read(entry))
    node = resolve_path(chunk, a.proto_path)
    if node.constants[0] != MISSION.encode():
        raise SystemExit(
            f"proto {a.proto_path} is {node.constants[0]!r}, expected {MISSION!r}; "
            f"pass the right --proto-path")
    old_tod = node.constants[a.tod_index]
    node.constants[a.tod_index] = a.tod.encode("latin-1")
    dest3 = os.path.join(out, "scripts", "missiondefinit.lua")
    os.makedirs(os.path.dirname(dest3), exist_ok=True)
    blob = dump(chunk)
    with open(dest3, "wb") as fh:
        fh.write(blob)
    # prove it still parses and the edit survived
    again = load(blob)
    assert resolve_path(again, a.proto_path).constants[a.tod_index] == a.tod.encode("latin-1")
    assert resolve_path(again, a.proto_path).constants[0] == MISSION.encode()
    log.append(f"tod     {dest3}: {old_tod!r} -> {a.tod!r} (proto {a.proto_path}[{a.tod_index}])")

    print("\n".join(log))
    print(f"\nloose tree ready at {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
