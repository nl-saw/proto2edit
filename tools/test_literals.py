#!/usr/bin/env python3
"""The value editors must accept exactly what the field list shows.

The Definition tab fills its list from mission_tree.Leaf.render() (Python reprs: True, False, None, 'name'), so an
edit box pre-filled from that text must parse back to the same constant. This pins both halves: raw_value() emits
the displayed spelling, and coerce_literal() accepts the displayed and the Lua spellings in any case.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mission_tree as MT  # noqa: E402
import mission_gui as G    # noqa: E402


def main():
    fails = []
    leaf_bool = MT.Leaf(0, "const", False)
    leaf_true = MT.Leaf(0, "const", True)
    leaf_nil = MT.Leaf(0, "const", None)
    for leaf, want in ((leaf_bool, "False"), (leaf_true, "True"), (leaf_nil, "None")):
        got = G.raw_value(leaf)
        if got != want:
            fails.append(f"raw_value({leaf.value!r}) = {got!r}, want {want!r}")

    cases = {
        "True": True, "true": True, "TRUE": True,
        "False": False, "false": False,
        "None": None, "nil": None, "NIL": None,
        "'story_a1'": b"story_a1", '"story_a1"': b"story_a1",
        "128.0": 128.0, "TOD_LAIR": b"TOD_LAIR",
    }
    for text, want in cases.items():
        got = MT.coerce_literal(text)
        if got != want or type(got) is not type(want):
            fails.append(f"coerce_literal({text!r}) = {got!r}, want {want!r}")

    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print(f"literal round-trip: {len(cases)} spellings + 3 raw_value cases agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
