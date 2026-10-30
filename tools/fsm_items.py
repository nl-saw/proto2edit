#!/usr/bin/env python3
"""Parse a container body into ITEMS and check them against the container's `count`.

THE ITEM GRAMMAR, as measured (docs/19 has every number):

    item    := u64 typeId | u32 len | 8 bytes | payload[len] | pad to 4
    body    := item* u32 0
    total   := 20 + len + ((-len) & 3)

The 20-byte header is not assumed. It was DERIVED with no free parameter from 6813
container bodies whose `count` is 1 - one item plus the closing zero u32 - so that
header = size - 4 - len - pad. Every named type came out at exactly 20 and no type
mixed values. The `record` type (0x6c357b9d3e) is just one typeId under this grammar;
its payload has its own fixed layout, handled in fsm_write.

Every body in the corpus that holds items ends with a zero u32 (8273 of 8273). The
16 bytes between `len` and the payload are not uniform across types, so they are
preserved as opaque bytes, never reconstructed - which is what makes a no-op rebuild
byte-identical.

    python3 tools/fsm_items.py <file> [...]      # report coverage
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rz import unwrap          # noqa: E402
import fsm_graph as F          # noqa: E402

ITEM_HDR = F.ITEM_HDR          # 20
ITEM_MAX_LEN = F.ITEM_MAX_LEN
RECORD_TYPE = 0x0000006C357B9D3E


class Item:
    """One item: where it is, how long it is, and the bytes that are opaque."""

    __slots__ = ("tag", "length", "off", "total")

    def __init__(self, tag, length, off, total):
        self.tag = tag
        self.length = length
        self.off = off
        self.total = total

    def __repr__(self):
        return f"Item(tag={self.tag:#x}, len={self.length}, off={self.off:#x})"


def parse_items(leaf, start, end):
    """Items in [start,end) plus the closing zero u32, or None if it does not tile."""
    items = []
    off = start
    while off + ITEM_HDR <= end:
        tag, ln = struct.unpack_from("<QI", leaf, off)
        if ln >= ITEM_MAX_LEN:
            return None
        total = ITEM_HDR + ln + ((-ln) % 4) + F.ITEM_TRAILER
        if off + total > end:
            return None
        items.append(Item(tag, ln, off, total))
        off += total
    if off == end:
        return items
    if off + 4 == end and struct.unpack_from("<I", leaf, off)[0] == 0:
        return items
    return None


def body_bytes(leaf, items, insert_after=None, drop=None):
    """Re-emit a body from its items, optionally inserting after one or dropping one."""
    out = bytearray()
    for it in items:
        if drop is not None and it.off == drop:
            continue
        out += leaf[it.off:it.off + it.total]
        if insert_after is not None and it.off == insert_after[0]:
            out += insert_after[1]
    out += b"\x00\x00\x00\x00"
    return bytes(out)



def body_shape(leaf, start, end):
    """The body's item multiset as a hashable tuple, or None if it does not tile."""
    items = parse_items(leaf, start, end)
    if items is None:
        return None
    return tuple(sorted(bytes(leaf[i.off:i.off + i.total]) for i in items))


def attested_shapes(corpus_root):
    """{(container name, item multiset)} for every body the shipped game contains.

    The engine's loader does not reject a container whose item multiset never occurs in
    shipped data - nothing in the read path compares shapes - but such a container is,
    by definition, a shape the game has never been asked to load. After one crash on a
    shape that occurs zero times in 8244 shipped containers (`false` duplicated), the
    writer treats "attested" as a hard requirement rather than an assumption.
    """
    import os
    from rz import unwrap
    import fsm_graph as F
    table = F.name_table()
    seen = set()
    for root, _d, names in os.walk(corpus_root):
        if "fig.p3d.rz" not in names:
            continue
        raw = open(os.path.join(root, "fig.p3d.rz"), "rb").read()
        blob = unwrap(raw) if raw[:2] == b"RZ" else raw
        try:
            leaf, _ = F.payload(blob)
        except ValueError:
            continue
        nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
        for what, off, size, _count, _kids in containers(leaf, nodes):
            shape = body_shape(leaf, off + 16, off + 16 + size)
            if shape is not None:
                seen.add((what, shape))
    return seen

def containers(leaf, nodes):
    """Every container node, flattened, in document order."""
    out = []
    stack = list(reversed(nodes))
    while stack:
        kind, what, off, size, count, kids = stack.pop()
        if kind == "container":
            out.append((what, off, size, count, kids))
            stack.extend(reversed(kids or ()))
    return out


def analyse(path, table=None, verbose=True):
    table = table or F.name_table()
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, _ = F.payload(blob)
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)

    stats = {"bodies": 0, "tile": 0, "fail": 0, "agree": 0, "disagree": 0,
             "items": 0, "records": 0}
    fails = {}
    for what, off, size, count, _kids in containers(leaf, nodes):
        stats["bodies"] += 1
        items = parse_items(leaf, off + 16, off + 16 + size)
        if items is None:
            stats["fail"] += 1
            fails[what] = fails.get(what, 0) + 1
            continue
        stats["tile"] += 1
        stats["items"] += len(items)
        stats["records"] += sum(1 for i in items if i.tag == RECORD_TYPE)
        if len(items) == count:
            stats["agree"] += 1
        else:
            stats["disagree"] += 1
            fails[what] = fails.get(what, 0) + 1
    if verbose:
        b = stats["bodies"]
        print(f"{path}")
        print(f"  bodies {b}  tile {stats['tile']} ({100*stats['tile']/max(b,1):.1f}%)  "
              f"fail {stats['fail']}  count agrees {stats['agree']} "
              f"({100*stats['agree']/max(stats['tile'],1):.2f}% of tiled)")
        print(f"  items {stats['items']} ({stats['records']} records)")
        if fails:
            print("  explained least: " + ", ".join(
                f"{k}={v}" for k, v in sorted(fails.items(), key=lambda kv: -kv[1])[:8]))
    return stats


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    table = F.name_table()
    total = {"bodies": 0, "tile": 0, "fail": 0, "agree": 0, "disagree": 0,
             "items": 0, "records": 0}
    for p in argv:
        s = analyse(p, table)
        for k in total:
            total[k] += s[k]
    if len(argv) > 1:
        b = total["bodies"]
        print(f"\nTOTAL bodies {b}  tile {total['tile']} ({100*total['tile']/max(b,1):.1f}%)  "
              f"items {total['items']}  count agrees {100*total['agree']/max(total['tile'],1):.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
