#!/usr/bin/env python3
"""List the placed objects inside a mission's Pure3D asset.

Placed props, spawners and enemies are *meta-objects*: chunk `0x07f00000` holds
the object's three aligned-u8 names and a class id, and its child chunk
`0x07f00001` holds a `META` block with the object's attributes (transform,
health, ...) and its behaviour list.

The entry-name parse that used to live here was wrong: after `"META"` there is
**no** `u32 reserved` and no `u32 count`.  The body is class-specific, and for
`GameObject` it is `u32 attribute_count` followed by keyed attribute records and
one `BehaviourController` record; attribute/behaviour names are `u32`-length
prefixed, not NUL-terminated.  The verified parser is `tools/meta_layout.py`
(which also proves the parse by tiling every byte of the payload); this tool is a
thin lister on top of it.

Usage:
  python3 tools/entities.py <file.p3d|.p3d.rz> [--filter TEXT] [--types] [--all]
  python3 tools/entities.py <file> --classes      # class histogram only
"""

import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from meta_layout import (  # noqa: E402
    LayoutError, UnconfirmedClass, iter_meta_objects, parse_payload,
)


def objects(buf, file="<mem>"):
    """Yield (name, payload_size, entry_names, meta_object) for each meta-object.

    `entry_names` is the attribute keys followed by the behaviour type names;
    objects whose body layout is not confirmed yield an empty list.
    """
    for mo in iter_meta_objects(buf, file):
        names = []
        try:
            payload = parse_payload(mo)
        except UnconfirmedClass:
            payload = None
        if payload is not None:
            for e in payload.entries:
                if e.kind == "attribute":
                    names.append(f"{e.key}={e.type_name}")
                elif e.children is not None:
                    names.append(f"{e.type_name}[{','.join(c.type_name for c in e.children)}]")
                elif e.decoded and "string" in e.decoded:
                    names.append(repr(e.decoded["string"]))
                else:
                    names.append(e.type_name)
        yield mo.name, len(mo.payload), names, mo


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="List the placed objects (0x07f00000 meta-objects) of a P3D")
    ap.add_argument("path")
    ap.add_argument("--filter", default=None,
                    help="substring filter on the object's long/short name")
    ap.add_argument("--types", action="store_true",
                    help="print the attribute keys and behaviour types")
    ap.add_argument("--classes", action="store_true",
                    help="print only the meta-object class histogram")
    ap.add_argument("--all", action="store_true",
                    help="print every type name once per object, not just the first 8")
    a = ap.parse_args(argv)

    blob = open(a.path, "rb").read()
    if blob[:2] == b"RZ":
        from rz import unwrap
        blob = unwrap(blob)

    n = 0
    classes = collections.Counter()
    failures = []
    for name, size, names, mo in objects(blob, a.path):
        classes[mo.class_name] += 1
        if a.classes:
            continue
        if a.filter and a.filter.lower() not in (name + mo.full_name).lower():
            continue
        n += 1
        print(f"{size:>7}  {mo.class_name:26}  {name}")
        if a.types:
            shown = names if a.all else names[:8]
            more = "" if a.all or len(names) <= 8 else \
                f" … (+{len(names) - 8} more)"
            print(f"         {', '.join(shown)}{more}")

    if a.classes:
        for cls, k in classes.most_common():
            print(f"{k:5}  {cls}")
    print(f"-- {sum(classes.values())} meta-object(s), "
          f"{len(classes)} class(es)", file=sys.stderr)
    if failures:
        for f in failures:
            print(f"layout failure: {f}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LayoutError as exc:
        print(f"LAYOUT FAILURE: {exc}", file=sys.stderr)
        sys.exit(2)
