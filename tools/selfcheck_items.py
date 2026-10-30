#!/usr/bin/env python3
"""Round-trip proof for the item writer - the evidence behind docs/19 HANDOFF 3.

For every editable container in the corpus (body tiles, count == items, at least two
items), duplicate one item and then delete the copy. The result must be byte-identical
to the input file. A writer that is only "close" fails here.

Writes only under tmp/. Run: python3 tools/selfcheck_items.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rz import unwrap                     # noqa: E402
import fsm_graph as F                     # noqa: E402
import fsm_items as I                     # noqa: E402
import fsm_write as W                     # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(HERE, "tmp", "corpus")
WORK = os.path.join(HERE, "tmp", "roundtrip")


def files():
    for root, _d, names in os.walk(CORPUS):
        for n in names:
            if n == "fig.p3d.rz":
                yield os.path.join(root, n)


def main():
    os.makedirs(WORK, exist_ok=True)
    table = F.name_table()
    tried = ok = skipped = bad = 0
    for path in files():
        raw = open(path, "rb").read()
        blob = unwrap(raw) if raw[:2] == b"RZ" else raw
        leaf, _ = F.payload(blob)
        nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
        for what, off, size, count, _kids in I.containers(leaf, nodes):
            items = I.parse_items(leaf, off + 16, off + 16 + size)
            if items is None or len(items) != count or len(items) < 2:
                skipped += 1
                continue
            tried += 1
            a = os.path.join(WORK, "a.p3d")
            b = os.path.join(WORK, "b.p3d")
            if W.item_edit(path, a, off, 0, True) != 0:
                print(f"ADD FAILED {path} {what} {off:#x}")
                bad += 1
                continue
            ra = open(a, "rb").read()
            ba = unwrap(ra) if ra[:2] == b"RZ" else ra
            la, _ = F.payload(ba)
            na = F.parse_region(la, F.graph_start(la), len(la), table)
            # the copy sits right after the item we duplicated
            items_now = I.parse_items(la, off + 16, off + 16 + size + items[0].total)
            if items_now is None or len(items_now) != len(items) + 1:
                print(f"REPARSE FAILED {path} {what} {off:#x}")
                bad += 1
                continue
            if W.item_edit(a, b, off, 1, False) != 0:
                print(f"DEL FAILED {path} {what} {off:#x}")
                bad += 1
                continue
            if open(b, "rb").read() != raw:
                print(f"ROUND-TRIP DIFFERS {path} {what} {off:#x}")
                bad += 1
            else:
                ok += 1
    print(f"\nround-trip byte-identical {ok}/{tried} containers "
          f"({skipped} skipped as not editable or single-item)")
    print("RESULT:", "PASS" if bad == 0 and tried > 0 else f"FAIL ({bad})")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
