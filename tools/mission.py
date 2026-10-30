#!/usr/bin/env python3
"""Inspect and edit mission definitions in `missiondefinit.lua`.

Each mission is a Lua table literal in its own top-level proto: `const[0]` is
the mission name, and the remaining constants interleave field names with their
values (string values appear as constants; numbers and booleans are emitted as
instructions and are not editable here).

Field names are learned empirically: an identifier-shaped constant counts as a
field name when it appears in a large fraction of mission protos. That keeps
false positives like `TOD_BUNKER_INTRO2` or `evolved_m1` out of the vocabulary.

Usage:
  python3 tools/mission.py <archive-or-lua> list [--filter TEXT]
  python3 tools/mission.py <file> fields <mission>
  python3 tools/mission.py <file> set <mission> <field> <value> [--out PATH]
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lua51 import load, dump, resolve_path  # noqa: E402

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,40}$")
MISSION_NAME = re.compile(r"^(gz|rz|yz|story|test|structure|location|meta|ow|ms)_[A-Za-z0-9_]+$")


def mission_protos(root):
    """Yield (path, proto) for every top-level proto whose const[0] is a mission."""
    for i, p in enumerate(root.protos):
        if not p.constants:
            continue
        c = p.constants[0]
        if isinstance(c, bytes):
            name = c.decode("latin-1")
            if MISSION_NAME.match(name):
                yield str(i), name, p


def field_vocabulary(root):
    """Identifier constants that behave like table keys rather than values.

    Measured on the shipped file: real field names occur in 32-190 of the 190
    mission protos (`timeOfDay` 78, `isStoryMission` 32, `ambience` 32), while
    values occur in at most a handful (`evolved_m1` 2, `riley` 3,
    `TOD_BUNKER_INTRO2` 1). Requiring at least four protos separates them
    cleanly; rejecting names containing `_` additionally excludes asset and
    package identifiers (`INFECTED1_1`, `TOD_*`, `art/...`) and the `mm_`/`mms_`
    framework function names.
    """
    protos = list(mission_protos(root))
    total = max(1, len(protos))
    freq = {}
    for _path, _name, p in protos:
        seen = {c for c in p.constants if isinstance(c, bytes)}
        for s in seen:
            freq[s] = freq.get(s, 0) + 1
    threshold = max(4, int(total * 0.02))
    out = set()
    for s, n in freq.items():
        text = s.decode("latin-1")
        if IDENT.match(text) and "_" not in text and n >= threshold:
            out.add(text)          # vocabulary holds text, not bytes

    # One expansion pass: a lowercase identifier that immediately follows a
    # known field is a nested-table field (initialTransitionData ->
    # suppressFadeFromBlack, isSaved -> isIntroMission). Asset and package
    # values are excluded because they contain '_' or are ALL_CAPS.
    for _path, _name, p in protos:
        for i, c in enumerate(p.constants[:-1]):
            if not isinstance(c, bytes):
                continue
            if c.decode("latin-1") not in out:
                continue
            nxt = p.constants[i + 1]
            if not isinstance(nxt, bytes):
                continue
            t = nxt.decode("latin-1")
            if IDENT.match(t) and "_" not in t and t[:1].islower():
                out.add(t)
    return out


def fields_of(proto, vocab):
    """Resolve field -> (index, value) for string-valued fields."""
    out = {}
    for i, c in enumerate(proto.constants):
        if not isinstance(c, bytes):
            continue
        name = c.decode("latin-1")
        if name not in vocab:
            continue
        if i + 1 >= len(proto.constants):
            continue
        val = proto.constants[i + 1]
        if isinstance(val, bytes):
            v = val.decode("latin-1")
            if v in vocab:              # next token is another field, not a value
                continue
            if v.startswith(("mm_", "mms_")):   # framework call, not a value
                continue
            # A list field's first element is all we can address this way; mark it.
            nxt = proto.constants[i + 2] if i + 2 < len(proto.constants) else None
            is_list = isinstance(nxt, bytes) and nxt.decode("latin-1") not in vocab
            out[name] = (i + 1, v + ("  [list: sets element 0]" if is_list else ""))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="missiondefinit.lua (extracted) or an .rcf archive")
    ap.add_argument("cmd", choices=["list", "fields", "set"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--filter", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    if a.path.endswith(".rcf"):
        from rcf import Rcf
        with Rcf(a.path) as arc:
            blob = arc.read(arc.find("scripts\\missiondefinit.lua"))
    else:
        blob = open(a.path, "rb").read()

    root = load(blob)
    vocab = field_vocabulary(root)

    if a.cmd == "list":
        for path, name, proto in mission_protos(root):
            if a.filter and a.filter.lower() not in name.lower():
                continue
            f = fields_of(proto, vocab)
            tod = f.get("timeOfDay", (None, "-"))[1]
            iso = "story" if "isStoryMission" in f else "     "
            print(f"  proto[{path:>3}]  {name:26} {iso}  tod={tod}")
        return 0

    target = a.args[0] if a.args else None
    if not target:
        print("need a mission name", file=sys.stderr)
        return 1
    found = next(((p, n, pr) for p, n, pr in mission_protos(root) if n == target), None)
    if found is None:
        print(f"mission not found: {target}", file=sys.stderr)
        return 1
    path, name, proto = found

    if a.cmd == "fields":
        print(f"{name}  (proto[{path}], {len(proto.constants)} constants)")
        for k, (idx, v) in sorted(fields_of(proto, vocab).items()):
            print(f"  {k:28} const[{idx:>3}] = {v!r}")
        return 0

    # set
    if len(a.args) < 3:
        print("set needs FIELD VALUE", file=sys.stderr)
        return 1
    field, value = a.args[1], a.args[2]
    f = fields_of(proto, vocab)
    if field not in f:
        print(f"no string-valued field {field!r} in {name}; try 'fields'", file=sys.stderr)
        return 1
    idx, old = f[field]
    proto.constants[idx] = value.encode("latin-1")
    out_blob = dump(root)
    again = load(out_blob)
    got = fields_of(resolve_path(again, path), field_vocabulary(again)).get(field)
    if got is None or got[1] != value:
        print("verification failed: edit did not survive re-serialisation", file=sys.stderr)
        return 1
    out = a.out or a.path
    open(out, "wb").write(out_blob)
    print(f"{name}.{field}: {old!r} -> {value!r}  (proto[{path}] const[{idx}])")
    print(f"wrote {out} ({len(blob)} -> {len(out_blob)} bytes, verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
