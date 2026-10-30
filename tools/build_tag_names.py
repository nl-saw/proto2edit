#!/usr/bin/env python3
"""Write artifacts/tag-names.json: corpus container tags -> the engine string that hashes to them.

`fsm_graph.name_table()` used to read `tmp/engine_all_strings.txt`, a scratch dump that disappears whenever tmp/ is
cleaned - which silently turned every record role into `None`. This rebuilds the same mapping from the shipped
engine binary and the extracted corpus, and writes it where it is committed.

    ./proto2 corpus        # rebuilds tmp/corpus, then calls this automatically
    python3 tools/build_tag_names.py    # just refresh the table
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import script_edit as SE   # noqa: E402
import fsm_edit as E       # noqa: E402
from uid import uid        # noqa: E402
import config  # noqa: E402

GAME = config.game_dir()
OUT = os.path.join(ROOT, "artifacts", "tag-names.json")
PRINTABLE = re.compile(rb"[\x20-\x7e]{3,64}")


def engine_names(dll):
    data = open(dll, "rb").read()
    names = {}
    for m in PRINTABLE.finditer(data):
        text = m.group().decode("latin-1")
        names.setdefault(uid(text), text)
    return names, hashlib.sha256(data).hexdigest()


def fsm_element_tags():
    """Tags of the FSM element graph itself: `state`, `transition`, `branch`, ...

    `corpus_tags()` only sees record containers and item types, which is why the graph view was empty for every
    mission: `mission_graph` looks for elements whose tag resolves to "state"/"transition", and neither was in the
    table. These names hash to the ids the element walk actually produces (verified: uid("state") appears 8 times in
    one mission, uid("transition") 9).
    """
    import fsm_chunk as C
    import fsm_graph as F
    import mission_graph as MG
    tags = set()
    seen = set()
    for dirpath, _d, names in os.walk(SE.CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        parts = [p for p in dirpath.split(os.sep) if p]
        if (parts[-2], parts[-1]) in seen:
            continue
        seen.add((parts[-2], parts[-1]))
        path = os.path.join(dirpath, "fig.p3d.rz")
        try:
            leaf = C.load(path)
            info = C.parse_chunk(leaf)
        except Exception:
            continue
        if not info:
            continue
        for b in info["branches"]:
            for node in MG.elements(leaf, b["off"] - 12, b["off"] + b["size"] + 8, {}):
                # elements() reports the name when the table knows it and a "0x..." string otherwise; we want the
                # raw ids here, which is what the table is built from
                tag = node["tag"]
                tags.add(int(tag, 16) if isinstance(tag, str) else tag)
    return tags


def corpus_tags():
    tags = set()
    seen = set()
    for dirpath, _d, names in os.walk(SE.CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        parts = [p for p in dirpath.split(os.sep) if p]
        if (parts[-2], parts[-1]) in seen:
            continue
        seen.add((parts[-2], parts[-1]))
        got = SE.open_side(parts[-2], parts[-1], "shipped")
        if got is None:
            continue
        for el in E.walk_elements(got[3]):
            for c in el["containers"]:
                tags.add(c["tag"])              # element-level container tags (roles)
                for item in c["items"]:
                    tags.add(item["tag"])       # item type tags (conditionGroup, record, ...)
    return tags


def main(argv=None):
    dll = os.path.join(GAME, "prototype2engine.dll")
    if not os.path.exists(dll):
        raise SystemExit(f"engine binary not found: {dll} (set PROTO2_GAME)")
    names, digest = engine_names(dll)
    tags = corpus_tags()
    tags |= fsm_element_tags()
    table = {f"0x{tag:016x}": names[tag] for tag in tags if tag in names}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"engine_sha256": digest, "tags": table}, open(OUT, "w"), indent=1, sort_keys=True)
    print(f"tag names: {len(table)} of {len(tags)} corpus tags named from {len(names)} engine strings")
    print(f"  wrote {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
