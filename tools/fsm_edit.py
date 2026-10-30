#!/usr/bin/env python3
"""A rebuilding editor for mission FSM files: leaf <-> model -> leaf.

Criterion 1 of the toolchain goal. The decode already knows every field of a mission file; this tool
turns a file into an editable text model and back, and **every size in the output is recomputed from
the model** rather than copied. That is what makes it safe to change things: if re-encoding the
unedited model reproduces the file byte-for-byte, the model is a complete description of it, and any
difference after an edit is necessarily the edit.

Model (JSON, stable and diffable):

    {"format": "proto2-fsm", "version": 1,
     "type": 0x20000702,
     "block": {"version":.., "name":.., "context":.., "path":.., "sibling":.., "itemCount":..,
               "branches": [ element, ... ]}}

    element   := {"tag":.., "name":.., "siblings":.., "extra":{..}|null,
                  "containers":[container...], "elements":[element...]}
    container := {"tag":.., "count":.., "items":[item...]}
    item      := {"tag":.., "anchor":.., "fields":{..}}

Commands:

    python3 tools/fsm_edit.py verify                 # round-trip every mission, report deviations
    python3 tools/fsm_edit.py export story_a1 out.json
    python3 tools/fsm_edit.py import out.json out.p3d
    python3 tools/fsm_edit.py add-condition   <mission> <transition#> [--type false|hasTag <text>]
    python3 tools/fsm_edit.py retarget        <mission> <transition#> <state name>
    python3 tools/fsm_edit.py rename-state    <mission> <state#> <new name>

The three edit commands each print the byte ranges that changed, so the diff can be checked against
what was asked for. They write a new leaf next to the original (never into the game directory).
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_chunk as C
import fsm_graph as F
import fsm_items as I
import fsm_schema as S
import uid as U
from uid import uid
from rz import wrap as rz_wrap, unwrap as rz_unwrap
import fsm_graph as F

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "tmp", "corpus")
MOD = os.path.join(ROOT, "work", "mod")
CHUNK = 0x616FE21D386330FF
TRANSITION = 0xE3CB071760C9B1B5
REF_STATEMACHINE = 0xB40395DB1B316A0C

NAMES = {}

ZERO_FIELD_TAGS = {
    0xBBF017325B564F48: "__anchorRef",
    0x64EE5E0BA3C653F9: "false",
    0x00000000006F1B23: "0x6f1b23",
    0x0000006E3695C3B7: "0x6e3695c3b7",
}


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


# ---------------------------------------------------------------- parse
def parse_item(leaf, it, container):
    body = leaf[it.off + I.ITEM_HDR:it.off + I.ITEM_HDR + it.length]
    fields = S.decode_body(container, it.tag, body)
    # the file offset is carried so ordering questions (e.g. which record is compressed against
    # which) can be answered from the model rather than by re-parsing
    item = {"tag": it.tag, "anchor": _u32(leaf, it.off + 12), "off": it.off}
    if fields is None:
        item["raw"] = body.hex()
    else:
        item["fields"] = fields
    return item


def parse_container(leaf, off, size, count, name):
    items = I.parse_items(leaf, off + 16, off + 16 + size) or []
    return {"tag": _u64(leaf, off), "count": count,
            "items": [parse_item(leaf, it, name) for it in items]}


def parse_element(leaf, off, table, siblings=0):
    tag = _u64(leaf, off)
    size = _u32(leaf, off + 8)
    body = off + 12
    end = body + size
    # siblings is the number of element-framed children of the PARENT, counting this element - not
    # this element's own children. Passing its own count was the first bug here.
    node = {"tag": tag, "name": _u64(leaf, body), "siblings": siblings, "extra": None,
            "containers": [], "elements": []}
    q = body + 16
    if tag == TRANSITION:
        ln = _u32(leaf, q)
        pad = (ln + 3) & ~3
        node["extra"] = {"len": ln, "text": leaf[q + 4:q + 4 + ln].decode("latin-1"),
                         "word": _u32(leaf, q + 4 + pad)}
        q += 4 + pad + 4
    elif tag == REF_STATEMACHINE:
        ln = _u32(leaf, q)
        pad = (ln + 3) & ~3
        node["extra"] = {"len": ln, "text": leaf[q + 4:q + 4 + ln].decode("latin-1"),
                         "word0": _u32(leaf, q + 4 + pad), "word1": _u32(leaf, q + 8 + pad)}
        q += 4 + pad + 8
    kids, _ok, stop = C.walk_children(leaf, q, end)
    for k in kids:
        node["containers"].append(parse_container(leaf, k["off"] - 16, k["size"], k["count"],
                                                  table.get(k["tag"], hex(k["tag"]))))
    p = stop
    children = []
    while p + 12 <= end:
        t = _u64(leaf, p)
        if t == 0:
            break
        s = _u32(leaf, p + 8)
        if p + 12 + s > end:
            break
        children.append(p)
        p += 12 + s + 8
    for c in children:
        node["elements"].append(parse_element(leaf, c, table, len(children)))
    return node


def parse_file(path, table=None):
    """Parse a mission FILE into a model, keeping the bytes around the leaf.

    `fsm_chunk.load` returns the leaf chunk's payload, not the file: the real file nests
    0xff443350 -> 0x20000701 -> 0x20000702. Writing the payload back as if it were the file produces
    something the engine loads and then abandons, because the outer containers are missing.
    """
    raw = open(path, "rb").read()
    blob = rz_unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, off = F.payload(blob)
    model = parse(leaf, table)
    # every chunk that ENCLOSES the leaf, excluding the leaf itself
    ancestors = [c for c in _chunk_path(blob, off) if c != off]
    model["file"] = {"offset": off, "leaf_len": len(leaf),
                     "prefix": blob[:off].hex(), "suffix": blob[off + len(leaf):].hex(),
                     "ancestors": ancestors, "rz": raw[:2] == b"RZ"}
    return model


def _chunk_path(blob, leaf_off):
    """offsets of the chunks on the path root -> leaf"""
    path = []
    def find(off, end):
        while off + 12 <= end:
            typ, hdr, tot = struct.unpack_from("<3I", blob, off)
            if hdr < 12 or tot < 12 or off + tot > end:
                return False
            path.append(off)
            if off == leaf_off:
                return True
            if tot > hdr and find(off + hdr, off + tot):
                return True
            path.pop()
            off += tot
        return False
    find(0, len(blob))
    return path


def encode_file(model):
    """Whole-file bytes for the model, with the enclosing chunks' sizes corrected."""
    leaf = encode(model)
    fi = model["file"]
    prefix = bytes.fromhex(fi["prefix"])
    suffix = bytes.fromhex(fi["suffix"])
    patched = bytearray(prefix + leaf + suffix)
    delta = len(leaf) - fi["leaf_len"]
    if delta:
        for c in fi["ancestors"]:
            old_tot = struct.unpack_from("<I", prefix, c + 8)[0]
            for field in (4, 8, 12):
                v = struct.unpack_from("<I", patched, c + field)[0]
                if abs(v - old_tot) <= 16:
                    struct.pack_into("<I", patched, c + field, v + delta)
    return bytes(patched)


def parse(leaf, table=None):
    table = table or F.name_table()
    info = C.parse_chunk(leaf)
    if not info:
        raise SystemExit("chunk block does not parse")
    gs = F.graph_start(leaf)
    chunk_tag_off = gs + 8
    p = chunk_tag_off + 12
    version = _u32(leaf, p)
    name = _u64(leaf, p + 4)
    context = _u64(leaf, p + 12)
    ln = _u32(leaf, p + 20)
    path = leaf[p + 24:p + 24 + ln].decode("latin-1")
    q = p + 24 + ((ln + 3) & ~3)
    sibling, item_count = struct.unpack_from("<II", leaf, q)
    branches = [parse_element(leaf, b["off"] - 12, table, len(info["branches"]))
                for b in info["branches"]]
    return {"format": "proto2-fsm", "version": 1, "type": _u32(leaf, 0),
            "block": {"version": version, "name": name, "context": context, "path": path,
                      "sibling": sibling, "itemCount": item_count, "branches": branches}}


# ---------------------------------------------------------------- encode
def encode_item(item):
    if "fields" in item:
        body = S.encode_body(item["fields"])
    else:
        body = bytes.fromhex(item["raw"])
    pad = (4 - len(body) % 4) % 4
    return (struct.pack("<QII", item["tag"], len(body), item["anchor"] & 0xFFFFFFFF)
            + body + b"\x00" * pad + b"\x00" * 4)


def encode_container(cont):
    out = b"".join(encode_item(i) for i in cont["items"])
    content = out + b"\x00" * 4
    return (struct.pack("<QII", cont["tag"], len(content), len(cont["items"]))
            + content + b"\x00" * 4)


def encode_element(node):
    extra = b""
    if node["extra"]:
        e = node["extra"]
        text = e["text"].encode("latin-1")
        pad = (len(text) + 3) & ~3
        extra = struct.pack("<I", len(text)) + text + b"\x00" * (pad - len(text))
        if node["tag"] == TRANSITION:
            extra += struct.pack("<I", e["word"])
        else:
            extra += struct.pack("<II", e["word0"], e["word1"])
    body = (struct.pack("<QII", node["name"], 0, node["siblings"])
            + extra
            + b"".join(encode_container(c) for c in node["containers"])
            + b"".join(encode_element(e) for e in node["elements"]))
    return struct.pack("<QI", node["tag"], len(body)) + body + b"\x00" * 8


def encode(model):
    block = model["block"]
    path = block["path"].encode("latin-1")
    pad = (len(path) + 3) & ~3
    head = (struct.pack("<I", block["version"]) + struct.pack("<QQ", block["name"], block["context"])
            + struct.pack("<I", len(path)) + path + b"\x00" * (pad - len(path))
            + struct.pack("<II", block["sibling"], block["itemCount"]))
    body = head + b"".join(encode_element(b) for b in block["branches"])
    gs = 16 + 4          # header + "fig0"
    leaf = (struct.pack("<I", model["type"]) + b"\x00" * 8 + b"\x00" * 4)
    out = (struct.pack("<I", model["type"])
           + b"\x00" * 12                       # placeholder, sizes filled below
           + b"fig0" + struct.pack("<I", 5) + struct.pack("<I", 0)
           + struct.pack("<Q", CHUNK) + struct.pack("<I", len(body)) + body + b"\x00" * 8)
    total = len(out)
    out = (struct.pack("<IIII", model["type"], total, total, total - 16) + out[16:])
    return out


# ---------------------------------------------------------------- io
def find_mission(name, variant="active"):
    for dirpath, _dirs, fns in os.walk(CORPUS):
        if "fig.p3d.rz" not in fns:
            continue
        parts = [p for p in dirpath.split("/") if p]
        if parts[-2] == name and parts[-1] == variant:
            return os.path.join(dirpath, "fig.p3d.rz")
    return None


def cmd_verify(argv):
    if not os.path.isdir(CORPUS) or not any("fig.p3d.rz" in fns for _dp, _d, fns in os.walk(CORPUS)):
        raise SystemExit("no extracted corpus under tmp/corpus - run ./proto2 corpus to rebuild it")
    table = F.name_table()
    ok = bad = 0
    for dirpath, _dirs, fns in os.walk(CORPUS):
        if "fig.p3d.rz" not in fns:
            continue
        try:
            model = parse_file(os.path.join(dirpath, "fig.p3d.rz"), table)
            original = open(os.path.join(dirpath, "fig.p3d.rz"), "rb").read()
            blob = rz_unwrap(original) if original[:2] == b"RZ" else original
            out = encode_file(model)
            leaf = blob
        except SystemExit as exc:
            print(f"  {dirpath}: {exc}")
            bad += 1
            continue
        if out == blob:
            ok += 1
        else:
            bad += 1
            first = next((i for i in range(min(len(out), len(leaf))) if out[i] != leaf[i]), -1)
            print(f"  {dirpath.split('/')[-3:]}: differs at {first:#x} "
                  f"({len(out)} vs {len(leaf)} bytes)")
    print(f"FILE-level round-trip byte-identical: {ok}   deviations: {bad}")
    return 0 if bad == 0 else 1


def cmd_export(argv):
    name, out_path = argv[0], argv[1]
    path = find_mission(name)
    if not path:
        raise SystemExit(f"mission {name!r} not found")
    model = parse(C.load(path))
    with open(out_path, "w") as fh:
        json.dump(model, fh, indent=1)
    print(f"wrote {out_path}")


def cmd_import(argv):
    in_path, out_path = argv[0], argv[1]
    with open(in_path) as fh:
        model = json.load(fh)
    with open(out_path, "wb") as fh:
        fh.write(encode(model))
    print(f"wrote {out_path}")


def diff_range(a, b):
    """(prefix, suffix, changed_a, changed_b): the common head, the common tail, and the middle.

    A replacement shows as 0..n changed bytes at one offset; an insert shows as 0 changed bytes on one
    side. Reporting first/last-differing alone makes an insertion look enormous, because everything
    after the insertion point shifts.
    """
    n = min(len(a), len(b))
    prefix = 0
    while prefix < n and a[prefix] == b[prefix]:
        prefix += 1
    suffix = 0
    while suffix < n - prefix and a[len(a) - 1 - suffix] == b[len(b) - 1 - suffix]:
        suffix += 1
    return prefix, suffix, a[prefix:len(a) - suffix], b[prefix:len(b) - suffix]


def _hex(b, limit=48):
    h = b[:limit].hex()
    return h + (f"... (+{len(b) - limit} bytes)" if len(b) > limit else "")


def diff_runs(a, b):
    """contiguous runs of differing byte positions (equal-length inputs only)"""
    if len(a) != len(b):
        return None
    runs = []
    i = 0
    n = len(a)
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def report_diff(leaf, out):
    prefix, suffix, ca, cb = diff_range(leaf, out)
    if ca == cb:
        print("  no bytes changed")
    elif len(leaf) == len(out) and len(ca) > 16:
        # scattered changes (e.g. a rename plus its references) are better described as runs than as
        # one envelope, which would claim tens of thousands of changed bytes
        runs = diff_runs(leaf, out)
        total = sum(z - y for y, z in runs)
        print(f"  {len(runs)} changed region(s), {total} byte(s) total")
        for y, z in runs[:4]:
            print(f"    at {y:#x}: {_hex(leaf[y:z])} -> {_hex(out[y:z])}")
        if len(runs) > 4:
            print(f"    ... and {len(runs) - 4} more")
    elif len(ca) == len(cb):
        print(f"  replaced {len(ca)} byte(s) at {prefix:#x}: {_hex(ca)} -> {_hex(cb)}")
    elif not ca:
        print(f"  inserted {len(cb)} byte(s) at {prefix:#x}: {_hex(cb)}")
    elif not cb:
        print(f"  removed {len(ca)} byte(s) at {prefix:#x}: {_hex(ca)}")
    else:
        # a resize changes the leaf's own size fields in the header, so the diff legitimately starts
        # at offset 4; the interesting position is where the bulk of the file shifts
        shift = len(leaf) - suffix if len(leaf) > len(out) else len(out) - suffix
        delta = len(cb) - len(ca)
        print(f"  header size fields updated (bytes 4..16)")
        print(f"  {'inserted' if delta > 0 else 'removed'} {abs(delta)} byte(s) at {min(len(leaf), shift):#x}")


# ---------------------------------------------------------------- edits
def load_pair(name):
    """(whole-file bytes, model) for a mission."""
    path = find_mission(name)
    if not path:
        raise SystemExit(f"mission {name!r} not found")
    model = parse_file(path)
    raw = open(path, "rb").read()
    blob = rz_unwrap(raw) if raw[:2] == b"RZ" else raw
    return blob, model


def walk_elements(model):
    """every element in the tree, depth-first in file order"""
    out = []

    def walk(el):
        out.append(el)
        for child in el["elements"]:
            walk(child)

    for br in model["block"]["branches"]:
        walk(br)
    return out


def target_elements(model):
    """elements that can carry a state change: transitions, nodes and reference_statemachines"""
    kinds = (TRANSITION, REF_STATEMACHINE, U.uid("node"))
    return [(None, None, el) for el in walk_elements(model) if el["tag"] in kinds]


def conditions_of(el):
    for c in el["containers"]:
        if c["tag"] == U.uid("conditions"):
            return c
    return None


def describe_targets(model):
    """rows of (index, kind, target name/uid) for the elements that carry state changes"""
    rows = []
    for i, (_bi, _ei, el) in enumerate(target_elements(model)):
        found = []
        for c in el["containers"]:
            for it in c["items"]:
                f = it.get("fields")
                if f and f.get("type") == "state-change":
                    found.append(f["stateName"])
        rows.append((i, el["tag"], found))
    return rows


def cmd_list_targets(argv):
    _leaf, model = load_pair(argv[0])
    names = NAMES
    for i, tag, found in describe_targets(model):
        kind = {TRANSITION: "transition", REF_STATEMACHINE: "reference_statemachine"}.get(
            tag, "node" if tag == U.uid("node") else hex(tag))
        tgt = ", ".join(names.get(f, f"{f:#018x}") for f in found) or "(no state-change)"
        print(f"  [{i:>3}] {kind:<22} -> {tgt}")
    return 0


def cmd_add_condition(argv):
    name, idx = argv[0], int(argv[1])
    kind = "false"
    if "--type" in argv:
        kind = argv[argv.index("--type") + 1]
    leaf, model = load_pair(name)
    targets = target_elements(model)
    if not (0 <= idx < len(targets)):
        raise SystemExit(f"transition index {idx} out of range 0..{len(targets)-1}")
    _bi, _ei, el = targets[idx]
    cond = conditions_of(el)
    if cond is None:
        raise SystemExit("that element has no conditions container; refusing to invent one")
    if kind == "false":
        tag = 0x64EE5E0BA3C653F9
        fields = {"type": "zero-field"}
    else:
        raise SystemExit(f"unsupported condition type {kind!r}; refusing rather than guessing")
    cond["items"].append({"tag": tag, "anchor": 0, "fields": fields})
    cond["count"] = len(cond["items"])
    out = encode_file(model)
    r = diff_range(leaf, out)
    print(f"added a zero-field condition to transition #{idx}")
    print(f"  conditions now {cond['count']} items")
    report_diff(leaf, out)
    return out, leaf


def cmd_retarget(argv):
    name, idx, state = argv[0], int(argv[1]), argv[2]
    leaf, model = load_pair(name)
    targets = target_elements(model)
    if not (0 <= idx < len(targets)):
        raise SystemExit(f"transition index {idx} out of range 0..{len(targets)-1}")
    _bi, _ei, el = targets[idx]
    cond = conditions_of(el)
    if cond is None:
        raise SystemExit("that element has no conditions container")
    new_uid = uid(state)
    changed = 0
    for it in cond["items"]:
        f = it.get("fields")
        if f and f.get("type") == "state-change":
            old = f["stateName"]
            f["stateName"] = new_uid
            changed += 1
            print(f"  stateName {old:#018x} -> {new_uid:#018x} ({state})")
    if not changed:
        available = [i for i, _t, f in describe_targets(model) if f]
        raise SystemExit(f"no state-change condition on element #{idx}; "
                         f"elements that have one: {available}")
    out = encode_file(model)
    print(f"retargeted transition #{idx} to {state!r}")
    report_diff(leaf, out)
    return out, leaf


def cmd_rename_state(argv):
    name, idx, new_name = argv[0], int(argv[1]), argv[2]
    leaf, model = load_pair(name)
    states = [el for el in walk_elements(model) if el["tag"] == U.uid("state")]
    if not (0 <= idx < len(states)):
        raise SystemExit(f"state index {idx} out of range 0..{len(states)-1}")
    old_uid = states[idx]["name"]
    new_uid = uid(new_name)
    states[idx]["name"] = new_uid
    # every reference to the old name moves with it, or the rename would silently orphan them
    refs = 0
    for el in walk_elements(model):
        for c in el["containers"]:
            for it in c["items"]:
                f = it.get("fields")
                if f and f.get("type") == "state-change" and f["stateName"] == old_uid:
                    f["stateName"] = new_uid
                    refs += 1
    out = encode_file(model)
    print(f"renamed state #{idx}: {old_uid:#018x} -> {new_uid:#018x} ({new_name})")
    print(f"  references retargeted: {refs}")
    report_diff(leaf, out)
    return out, leaf


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd = argv.pop(0)
    if cmd == "targets":
        return cmd_list_targets(argv)
    if cmd == "verify":
        return cmd_verify(argv)
    if cmd == "export":
        return cmd_export(argv)
    if cmd == "import":
        return cmd_import(argv)
    if cmd in ("add-condition", "retarget", "rename-state"):
        fn = {"add-condition": cmd_add_condition, "retarget": cmd_retarget,
              "rename-state": cmd_rename_state}[cmd]
        out, leaf = fn(argv)
        name = argv[0]
        # the edited FILE must carry a leaf that re-parses and re-encodes identically, and the
        # enclosing chunk sizes must still be consistent, or the writer refuses
        try:
            leaf2, off2 = F.payload(out)
            if encode(parse(leaf2)) != leaf2:
                raise SystemExit("edited leaf does not round-trip; refusing to write it")
            if off2 <= 0:
                raise SystemExit("edited file lost its enclosing chunks; refusing to write it")
        except SystemExit:
            raise
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(f"edited output does not parse ({exc}); refusing to write it")
        dst = os.path.join(ROOT, "tmp", f"{name}.edited.p3d")
        with open(dst, "wb") as fh:
            fh.write(out)
        print(f"  wrote {dst} ({len(out)} bytes)")
        if "--stage" in argv:
            variant = argv[argv.index("--variant") + 1] if "--variant" in argv else "active"
            dest = os.path.join(MOD, "art", "missions", name, variant, "fig.p3d.rz")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # the game reads <path>.rz, so the installed file must carry the RZ wrapper
            blob = rz_wrap(out)
            with open(dest, "wb") as fh:
                fh.write(blob)
            print(f"  staged {dest} ({len(blob)} bytes RZ)")
            print("  next: ./proto2 install   (then launch; undo the staging with "
                  "'rm' on that file before install, or ./proto2 fsm --revert after)")
        print(f"  original {len(leaf)} bytes; rebuild of the unedited model: "
              f"{'identical' if encode_file(load_pair(name)[1]) == leaf else 'DIFFERS'}")
        return 0
    print(f"unknown command {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
