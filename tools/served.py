#!/usr/bin/env python3
"""Did the last played session actually run our converted records?

`prototype_fix.log` is not a reliable witness: it stops mentioning mission files in some launch
configurations, and reading "no loose line" as "override not used" was wrong once already. The capture is
authoritative, because a converted record's payload is *source text* where the shipped file has bytecode - so if a
converted payload appears in the session's inflate stream, the override was served and used.

Output: per mission, how many converted payloads inflated in the newest capture, plus the log's loose-file lines
for context. Exit code 1 when a session ran no converted payload at all, so this can gate a claim.
"""
import argparse
import collections
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def newest_capture():
    d = os.path.join(ROOT, "artifacts", "inflate")
    if not os.path.isdir(d):
        return None
    ins = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".in")]
    return max(ins, key=os.path.getmtime) if ins else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", help="capture file (default: newest under artifacts/inflate)")
    a = ap.parse_args(argv)

    os.environ["PROTO2_NO_CACHE"] = "1"
    import hashlib
    import harvest_inflate as HI
    import recovery_report as R
    import script_edit as SE
    import fsm_luablob as L

    capture = a.capture or newest_capture()
    if not capture or not os.path.exists(capture):
        print("no capture to inspect - play a session, then ./proto2 harvest")
        return 1

    converted = {}
    for mission, variant, model in R.missions():
        staged = SE.open_side(mission, variant, "staged")
        shipped = SE.open_side(mission, variant, "shipped")
        if not staged or not shipped:
            continue
        for i, ((_a, ps, role), (_b, ph, _r)) in enumerate(
                zip(SE.records_with_roles(staged[3]), SE.records_with_roles(shipped[3]))):
            if L.stream_of(ps) == L.stream_of(ph):
                continue
            out = L.engine_dict_bytes(ps)
            if out:
                converted[hashlib.sha1(out).hexdigest()] = (mission, variant, i, role)

    tally = collections.Counter()
    for rec in HI.records(capture):
        if rec is None:
            continue
        out = rec[1]
        if not out:
            continue
        hit = converted.get(hashlib.sha1(out).hexdigest())
        if hit:
            tally[f"{hit[0]}/{hit[1]}"] += 1

    print(f"capture    {os.path.relpath(capture, ROOT)}  ({os.path.getsize(capture) / 1e6:.0f} MB)")
    total = sum(tally.values())
    print(f"converted payloads inflated: {total}")
    for k, v in tally.most_common(12):
        print(f"  {k:<34} {v}")
    if not total:
        print("=> this session ran no converted record: the overrides were not used, or the mission does not")
        print("   reach any converted record. Play a mission that is staged and likely to reach them.")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
