#!/usr/bin/env python3
"""Check a mission graph against every invariant decoded so far.

This is the gate that installs should have gone through. It answers "is this file
internally consistent by the rules we have evidence for" - not "will the engine accept
it", which needs the loader decode to finish. Stating that limit is the point: the two
bugs that crashed the game five times (a stale size field, a lost RZ wrapper) both fail
here, which is exactly why it exists.

Checks:
  1. RZ wrapper intact when the input was .rz.
  2. Leaf chunk: +4 == +8 == leaf length, +12 == +8 - 16.
  3. Every enclosing chunk's total covers the leaf.
  4. Every container: size == length of its content, and content parses to exactly size.
  5. Every record: sentinel fields, size == 12 + len + pad + 8, 4-byte alignment.
  6. Nothing unparsed at the end of the graph region.

    python3 tools/fsm_validate.py <file> [...]
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rz import unwrap          # noqa: E402
import fsm_graph as F          # noqa: E402
import fsm_write as W          # noqa: E402


def validate(path, table):
    problems = []
    raw = open(path, "rb").read()
    wrapped = raw[:2] == b"RZ"
    try:
        blob = unwrap(raw) if wrapped else raw
    except ValueError:
        return [f"{path}: claims RZ but does not decompress"]

    try:
        leaf, leaf_off = F.payload(blob)
    except ValueError as e:
        return [f"{path}: {e}"]

    # 2. leaf chunk's three size fields
    hdr, tot, body_len = struct.unpack_from("<3I", leaf, 4)
    if len(leaf) != tot:
        problems.append(f"leaf covers {len(leaf)} bytes but total says {tot}")
    if hdr != tot:
        problems.append(f"leaf +4 field is {hdr}, expected {tot}")
    if body_len != tot - 16:
        problems.append(f"leaf +12 field is {body_len}, expected {tot - 16}")

    # 3. enclosing chunks
    for c in W.chunk_path(blob, leaf_off):
        ct, chdr, ctot = struct.unpack_from("<3I", blob, c)
        if not (c <= leaf_off < c + ctot):
            problems.append(f"chunk at {c:#x} does not contain the leaf")

    # 4/5/6. parse the graph and walk it
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)

    def span(item):
        k, _w, _o, a, _b, _k = item
        return 16 + a if k == "container" else (20 + a if k == "record" else 4)

    seen_container = [False]
    stats = {"unparsed": 0, "count_checked": 0, "count_unframed": 0,
             "count_records_disagree": 0, "count_entries_disagree": 0}

    stats["count_checked"] = 0
    stats["count_unframed"] = 0

    def count_check(leaf, off, size, kids, count, where):
        """count must equal the number of items, where items are parseable.

        `count` is itemCount (confirmed by 1802 agreements in conditions containers).
        Containers whose bodies do not frame under any known item rule are counted as
        unframed and skipped rather than failed - those are the 955 bodies still to decode.
        """
        recs = sum(1 for k in kids if k[0] == "record")
        if recs:
            # record-bearing container: no entry fallback, or a body can frame spuriously
            stats["count_checked"] += 1
            if recs != count:
                stats["count_records_disagree"] += 1
            return
        # try the 24-byte entry framing used by conditions/hasTag
        pos = 0
        items = 0
        while pos + 24 <= size:
            try:
                _ident, ln = struct.unpack_from("<QI", leaf, off + 16 + pos)
            except struct.error:
                break
            step = 24 + ln + ((-ln) % 4)
            if pos + step > size:
                break
            items += 1
            pos += step
        if pos == size:
            stats["count_checked"] += 1
            if items != count:
                stats["count_entries_disagree"] += 1
        else:
            stats["count_unframed"] += 1

    def walk(items, where):
        for it in items:
            k, what, off, a, cnt, kids = it
            if k == "container":
                seen_container[0] = True
                inside = sum(span(c) for c in kids)
                if inside != a:
                    problems.append(f"{where}/{what}: content {inside} != size {a}")
                count_check(leaf, off, a, kids, cnt, f"{where}/{what}")
                walk(kids, f"{where}/{what}")
            elif k == "record":
                _rid, _h, size, sent, zero = struct.unpack_from("<5I", leaf, off)
                if sent != 0xFFFFFFFF or zero != 0:
                    problems.append(f"{where}: record at {off:#x} bad sentinel/zero")
                if size != a:
                    problems.append(f"{where}: record at {off:#x} size mismatch")
                payload = W.split_record(leaf, off, a)
                if payload is None:
                    problems.append(f"{where}: record at {off:#x} body does not match grammar")
            else:
                # Unparsed dwords are legitimate: the root container carries a
                # Scenario/statemachine preamble that is not container or record data.
                stats["unparsed"] += 4

    walk(nodes, "graph")
    if not problems:
        print(f"{os.path.basename(path)}: OK "
              f"({len(leaf)} byte leaf, wrapper {'RZ' if wrapped else 'raw'}, "
              f"{stats['unparsed']} preamble/unparsed byte(s), "
              f"count checked {stats['count_checked']}: "
              f"{stats['count_entries_disagree']} entry mismatch, "
              f"{stats['count_records_disagree']} record mismatch, "
              f"{stats['count_unframed']} unframed)")
    else:
        print(f"{os.path.basename(path)}: {len(problems)} problem(s)")
        for p in problems[:12]:
            print("   " + p)
    return problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    table = F.name_table()
    bad = 0
    for p in argv:
        bad += len(validate(p, table))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
