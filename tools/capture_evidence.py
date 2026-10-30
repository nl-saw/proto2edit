#!/usr/bin/env python3
"""Evidence that a staged (reconstructed) record actually ran in a playthrough.

A capture records the *output* of every inflate the engine performed, so a Lua chunk the game loaded is in
there byte-for-byte. This tool compares those chunks against the records we staged: for every record whose
staged payload differs from the shipped one, does the staged chunk's signature appear in the capture?

That turns "the owner played it" into "these N records were loaded by the engine, and here they are". It
does not prove behaviour - only that the code we wrote was the code that ran.

    ./proto2 evidence                     # newest capture in artifacts/inflate/
    ./proto2 evidence --capture FILE      # one capture
    ./proto2 evidence --json out.json     # machine-readable report

Signature = lua51.signature: code words, constants, numparams/is_vararg. Two chunks with the same signature
are the same program even if the binary differs in debug data, which is exactly what staging changes.
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_luablob as FL                          # noqa: E402
import harvest_inflate as HI                      # noqa: E402
import lua51 as L                                 # noqa: E402
import script_edit as SE                          # noqa: E402
import recovery_report as R                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INFLATE = os.path.join(ROOT, "artifacts", "inflate")


def newest_capture():
    files = sorted(glob.glob(os.path.join(INFLATE, "session-*.in")))
    if not files:
        raise SystemExit("no captures in artifacts/inflate/ - run ./proto2 harvest after a playthrough")
    return files[-1]


def loaded_bytes(payload):
    """The bytes the engine hands to luaL_loadbuffer for a record payload (text, or packed/deflated)."""
    packed = FL.strip_prefix(payload)
    if FL.is_packed(packed):
        return FL.engine_dict_bytes(packed) or b""
    return bytes(packed)


def capture_outputs(path):
    """Every distinct inflate output the engine produced in this session."""
    outs = {}
    for rec in HI.records(path):
        if rec is None:                 # resync marker: a torn record was skipped
            continue
        out = rec[1]
        if out:
            outs[out] = outs.get(out, 0) + 1
    return outs


def staged_records():
    """(mission, variant, role, loaded bytes) for every record we changed relative to the shipped file."""
    out = []
    for mission, variant, _model in R.missions():
        staged = SE.open_side(mission, variant, "staged")
        if staged is None:
            continue
        shipped = SE.open_side(mission, variant, "shipped")
        if shipped is None:
            continue
        srows = SE.records_with_roles(staged[3])
        hrows = SE.records_with_roles(shipped[3])
        for (it, payload, role), (_hit, hpayload, _hrole) in zip(srows, hrows):
            if bytes(payload) == bytes(hpayload):
                continue
            blob = loaded_bytes(payload)
            if not blob:
                continue
            hand = SE.cached_verdict(hpayload) != "equivalent"
            out.append((mission, variant, role, blob, hand))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="did our staged records appear in a playthrough capture?")
    ap.add_argument("--capture", default=None, help="capture file (default: newest in artifacts/inflate)")
    ap.add_argument("--json", default=None, help="write the report here")
    ap.add_argument("--quiet", action="store_true", help="only the summary lines")
    ap.add_argument("--verbose", action="store_true", help="list every record that was not seen")
    a = ap.parse_args(argv)

    path = a.capture or newest_capture()
    outs = capture_outputs(path)
    rows = staged_records()
    hits = 0
    per_mission = {}
    misses = []
    hand_staged = sum(1 for r in rows if r[4])
    hand_seen = 0
    for mission, variant, role, blob, hand in rows:
        ok = blob in outs
        hand_seen += 1 if (ok and hand) else 0
        hits += 1 if ok else 0
        bucket = per_mission.setdefault(mission, {"staged": 0, "seen": 0, "hand": 0, "hand_seen": 0})
        if hand:
            bucket["hand"] += 1
            bucket["hand_seen"] += 1 if ok else 0
        bucket["staged"] += 1
        bucket["seen"] += 1 if ok else 0
        if not ok:
            misses.append((mission, variant, role))

    print(f"capture            {os.path.relpath(path, ROOT)}")
    print(f"outputs in capture {len(outs)}")
    print(f"staged records     {len(rows)}")
    print(f"seen in capture    {hits}  ({100.0 * hits / len(rows):.1f}%)" if rows else "seen in capture    0")
    print(f"  of which hand-reconstructed: {hand_staged} staged, {hand_seen} seen")
    print(f"{'mission':24} {'staged':>7} {'seen':>6}")
    for mission, b in sorted(per_mission.items(), key=lambda kv: -kv[1]["staged"]):
        print(f"{mission:24} {b['staged']:>7} {b['seen']:>6}")
    if misses and not a.quiet:
        per_mission_miss = {}
        for mission, variant, role in misses:
            per_mission_miss.setdefault(mission, []).append((variant, role))
        print("\nnot seen, by mission (missions not loaded in this session, or records on paths never reached):")
        for mission, items in sorted(per_mission_miss.items(), key=lambda kv: -len(kv[1]))[:12]:
            staged_n = per_mission.get(mission, {}).get("staged", 0)
            print(f"  {mission:34} {len(items):>3} of {staged_n:>3}")
        if a.verbose:
            print("\n  detail:")
            for mission, variant, role in misses[:40]:
                print(f"    {mission}/{variant}  {role}")
            if len(misses) > 40:
                print(f"    ... {len(misses) - 40} more")

    if a.json:
        json.dump({"capture": os.path.basename(path),
                   "capture_outputs": len(outs),
                   "staged_records": len(rows),
                   "seen": hits,
                   "hand_staged": hand_staged,
                   "hand_seen": hand_seen,
                   "per_mission": per_mission,
                   "missed": [{"mission": m, "variant": v, "role": r} for m, v, r in misses]},
                  open(a.json, "w"), indent=1)
        print(f"\nwrote {os.path.relpath(a.json, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
