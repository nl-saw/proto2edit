#!/usr/bin/env python3
"""Walk the mission FSM object graph (`fig.p3d` -> chunk 0x20000702).

Known so far, from story_intro1:

    payload  u32 body_len | "fig0" | u32 5 | records...
    record   u32 id | u32 0x6c | u32 size | u32 0xffffffff | u32 0 | body[size]
             total = size + 20, and the body opens with u32 0 | f32 -1.0

That holds for 7 consecutive records and for 62 of 211 occurrences of the common
id 0x357b9d3e, then the walk stops - there is at least one other record layout.
This tool walks what it can, resynchronises on a plausible header, and reports
coverage so the unexplained spans are explicit rather than guessed at.

    python3 tools/fsm_graph.py <fig.p3d|.p3d.rz> [--dump OFFSET]
"""

import functools
import json
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rz import unwrap  # noqa: E402
from uid import uid     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Preferred source: a committed table (tools/build_tag_names.py). The old scratch dump is still honoured when it
# exists, because it can name strings the corpus does not currently reference - but tmp/ is deletable, so it can
# never be the only source again.
TAG_NAMES = os.path.join(ROOT, "artifacts", "tag-names.json")
STRINGS_DUMP = os.path.join(ROOT, "tmp", "engine_all_strings.txt")


@functools.lru_cache(maxsize=4)
def name_table(path=STRINGS_DUMP, limit=64):
    """{64-bit hash: engine string} so record ids can be read as names.

    Property ids in the graph are uid() of their name ('exitTracks',
    'conditions', 'execute'...). Verified against real data: those three ids
    appear verbatim in the corpus and hash to exactly these strings.
    """
    table = {}
    if os.path.exists(TAG_NAMES):
        try:
            data = json.load(open(TAG_NAMES))
            for tag, name in data.get("tags", {}).items():
                table[int(tag, 16)] = name
        except Exception as exc:      # a broken table must not crash parsing, but it must not be silent either
            print(f"warning: {TAG_NAMES}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if not os.path.exists(path):
        return table
    with open(path, encoding="latin-1") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t", 1)
            if len(parts) != 2:
                continue
            text = parts[1]
            if 3 <= len(text) <= limit and all(32 <= ord(c) < 127 for c in text):
                table.setdefault(uid(text), text)
    return table

LEAF = 0x20000702
SENTINEL = 0xFFFFFFFF
COMMON_ID = 0x357B9D3E

# The generic item ("value"): u64 typeId | u32 len | 8 bytes | payload[len], padded.
# The 20-byte header was DERIVED, with no free parameter, from 6813 container bodies
# whose `count` is 1 - one item plus the closing zero u32 - so that
# header = size - 4 - len - pad. Every named type came out at 20 and none mixed.
# Kept OUT of classify(): a value can span a chunk's whole child blob, and accepting
# one there drops the container count from 9021 to 434 (measured).
ITEM_HDR = 16          # u64 tag | u32 len | u32 anchorIndex
ITEM_TRAILER = 4       # u32 0 AFTER the body (outside `len`)
ITEM_MAX_LEN = 0x40000


def payload(blob):
    """Inner bytes of the 0x20000702 chunk, and its offset in the file.

    The leaf is nested: root 0xff443350 -> 0x20000701 -> 0x20000702, so this
    recurses rather than scanning a flat sibling list.
    """
    def find(off, end):
        while off + 12 <= end:
            typ, hdr, tot = struct.unpack_from("<3I", blob, off)
            if hdr < 12 or tot < 12 or off + tot > end:
                return None
            if typ == LEAF:
                return off
            if tot > hdr:
                got = find(off + hdr, off + tot)
                if got is not None:
                    return got
            off += tot
        return None

    off = find(0, len(blob))
    if off is None:
        raise ValueError("no 0x20000702 chunk found")
    hdr, tot = struct.unpack_from("<2I", blob, off + 4)
    return blob[off:off + tot], off


def graph_start(leaf):
    """Offset of the first record, past u32 body_len + "fig0" + u32 version."""
    body_len, = struct.unpack_from("<I", leaf, 12)
    if leaf[16:20] != b"fig0":
        raise ValueError(f"expected 'fig0', found {leaf[16:20]!r}")
    return 20


def header_at(leaf, off):
    """(id, hdr, size) if a known-format record starts here, else None."""
    if off + 20 > len(leaf):
        return None
    rid, hdr, size, sent, zero = struct.unpack_from("<5I", leaf, off)
    if hdr != 0x6C or sent != SENTINEL or zero != 0:
        return None
    if size > len(leaf) - off - 20:
        return None
    return rid, hdr, size


def walk(leaf, start):
    """Yield (offset, id, size, body) for known-format records, resyncing."""
    off = start
    unexplained = []
    while off + 20 <= len(leaf):
        got = header_at(leaf, off)
        if got:
            rid, _hdr, size = got
            yield off, rid, size
            off += 20 + size
            continue
        # resync: scan forward for the next offset that looks like a record
        nxt = None
        for probe in range(off + 1, min(off + 4096, len(leaf) - 20)):
            if header_at(leaf, probe):
                nxt = probe
                break
        if nxt is None:
            unexplained.append((off, len(leaf)))
            break
        unexplained.append((off, nxt))
        off = nxt
    if off < len(leaf):
        unexplained.append((off, len(leaf)))
    return unexplained


RECORD_ID = 0x357B9D3E
HDR_6C = 0x6C


def classify(leaf, off, end, table):
    """What starts at `off`: ("container"|"record", payload) or None."""
    if off + 16 <= end:
        ident, size, count = struct.unpack_from("<QII", leaf, off)
        if (ident in table and 16 <= size <= end - off - 16
                and 0 <= count <= 4096):
            return "container", (table[ident], size, count)
    if off + 20 <= end:
        rid, hdr, size, sent, zero = struct.unpack_from("<5I", leaf, off)
        if (rid == RECORD_ID and hdr == HDR_6C and sent == SENTINEL
                and zero == 0 and off + 20 + size <= end):
            return "record", (size,)
    return None


def parse_region(leaf, start, end, table):
    """Parse [start,end) into items, recording anything unparsed.

    Containers nest (enterTracks holds execute), so this recurses. Every byte is
    accounted for: a byte that matches neither pattern is an "unknown" item, and
    the caller can decide whether the region tiles.
    """
    items = []
    off = start
    while off + 4 <= end:
        got = classify(leaf, off, end, table)
        if got and got[0] == "container":
            name, size, count = got[1]
            kids = parse_region(leaf, off + 16, off + 16 + size, table)
            items.append(("container", name, off, size, count, kids))
            off += 16 + size
            continue
        if got and got[0] == "record":
            size, = got[1]
            items.append(("record", RECORD_ID, off, size, None, None))
            off += 20 + size
            continue
        items.append(("unknown", None, off, 4, None, None))
        off += 4
    return items


def audit(leaf, start, end, table, depth=0, report=None):
    """Parse a region and report whether it tiles, recursing into containers."""
    if report is None:
        report = {"containers": 0, "records": 0, "bad": [], "unknown_bytes": 0}
    items = parse_region(leaf, start, end, table)
    covered = 0
    for kind, name, off, size, count, kids in items:
        if kind == "container":
            report["containers"] += 1
            covered += 16 + size
            before = (report["containers"], len(report["bad"]))
            audit(leaf, off + 16, off + 16 + size, table, depth + 1, report)
            kids_n = sum(1 for k in kids if k[0] in ("record", "container"))
            if kids_n != count:
                report["bad"].append((name, count, kids_n, depth))
        elif kind == "record":
            report["records"] += 1
            covered += 20 + size
        else:
            report["unknown_bytes"] += 4
    return report


def cmd_tree(path, max_depth=2):
    blob = open(path, "rb").read()
    if blob[:2] == b"RZ":
        blob = unwrap(blob)
    leaf, _ = payload(blob)
    table = name_table()
    start = graph_start(leaf)
    items = parse_region(leaf, start, len(leaf), table)

    def show(nodes, indent=0):
        pad = "  " * indent
        for kind, what, off, a, b, kids in nodes:
            if kind == "container":
                print(f"{pad}{what}  size={a} count={b} @ {off:#07x}")
                if indent < max_depth:
                    show(kids, indent + 1)
            elif kind == "record":
                print(f"{pad}record size={a} @ {off:#07x}")

    show(items)
    rep = audit(leaf, start, len(leaf), table)
    print(f"\n{rep['containers']} container(s), {rep['records']} record(s), "
          f"{rep['unknown_bytes']} unparsed byte(s)")
    print(f"containers whose child count != stated count: {len(rep['bad'])}")
    for name, want, got, depth in rep["bad"][:10]:
        print(f"  {name:<16} stated {want}, found {got} (depth {depth})")
    return 0


def cmd_report(path):
    blob = open(path, "rb").read()
    if blob[:2] == b"RZ":
        blob = unwrap(blob)
    leaf, leaf_off = payload(blob)
    start = graph_start(leaf)
    recs = list(walk(leaf, start))
    unexplained = []
    off = start
    for o, rid, size in recs:
        if o != off:
            unexplained.append((off, o))
        off = o + 20 + size
    if off < len(leaf):
        unexplained.append((off, len(leaf)))

    covered = sum(20 + s for _o, _i, s in recs)
    ids = {}
    for _o, rid, _s in recs:
        ids[rid] = ids.get(rid, 0) + 1

    print(f"{path}")
    print(f"  leaf {len(leaf)} bytes at file offset {leaf_off:#x}; "
          f"records start {start:#x}")
    print(f"  {len(recs)} record(s), {covered} bytes in records "
          f"({covered * 100 // max(1, len(leaf))}% of leaf)")
    print(f"  {len(ids)} distinct id(s):")
    for rid, n in sorted(ids.items(), key=lambda kv: -kv[1]):
        print(f"    {rid:#010x}  x{n}")
    table = name_table()
    print(f"  {len(unexplained)} unexplained span(s) "
          f"({len(table)} engine names hashed):")
    for a, b in unexplained[:12]:
        note = ""
        if b - a >= 16:
            ident, x, y = struct.unpack_from("<QII", leaf, b - 16)
            named = table.get(ident)
            if named:
                note = f"   last 16: {named!r} size={x} count={y}"
        print(f"    {a:#07x}..{b:#07x}  ({b - a} bytes){note}")
        print("      " + leaf[a:min(a + 32, b)].hex())
    return recs, unexplained


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    path = argv[0]
    if "--tree" in argv:
        return cmd_tree(path)
    if "--dump" in argv:
        i = argv.index("--dump")
        at = int(argv[i + 1], 0)
        blob = open(path, "rb").read()
        if blob[:2] == b"RZ":
            blob = unwrap(blob)
        leaf, _ = payload(blob)
        for row in range(0, 128, 16):
            chunk = leaf[at + row:at + row + 16]
            print(f"  {at + row:07x}  " + " ".join(f"{b:02x}" for b in chunk))
        return 0
    cmd_report(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
