#!/usr/bin/env python3
"""Decode and edit a mission definition's nested table straight from bytecode.

Each mission is one top-level proto that builds a plain Lua table with
straight-line code: NEWTABLE, then key/value pairs via SETTABLE with RK
operands, SETLIST for arrays, and GETGLOBAL for named constants such as
`TOD_BUNKER_INTRO2` or `INFECTED1_1`.

The useful consequence: **every leaf is a constant-table entry**. A literal
value points at a constant slot; a named global points at a constant slot
holding the global's name. So editing any field is replacing one constant, and
the instruction stream never has to change.

Paths are dotted, with `[n]` for array elements:

    timeOfDay
    activationData.activeList[0]
    activationData.ambientActiveList[3]
    missionInfo.isStoryMission
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lua51 import load, dump, decode, OPCODES, resolve_path  # noqa: E402
from mission import mission_protos, field_vocabulary  # noqa: E402

CONST = 256
EXTERNAL_TABLES = {"mm_MissionFlowConfigs"}


class Leaf:
    """A value read through one instruction operand.

    `slot` is the constant index, `ref` is `(pc, operand)` naming the single
    instruction field that points at it. Editing rewrites that operand to a new
    constant instead of mutating the shared slot, because the same constant is
    often reused by several fields (five booleans share `const 12 = true` in
    story_intro2).
    """

    __slots__ = ("slot", "kind", "value", "ref")

    def __init__(self, slot, kind, value, ref=None):
        self.slot = slot          # index into proto.constants
        self.kind = kind          # 'const' or 'global'
        self.value = value        # rendered python value / name
        self.ref = ref            # (pc, 'bx' | 'b' | 'c') or None

    def render(self):
        if self.kind == "global":
            return f"<global {self.value}>"
        return repr(self.value)


class Node:
    __slots__ = ("kind", "items", "global_name")

    def __init__(self, kind):
        self.kind = kind          # 'table' or 'array'
        self.items = []           # (key, value) or index order
        self.global_name = None

    def set(self, key, value):
        self.items.append([key, value])

    def as_dict(self):
        return dict((k, v) for k, v in self.items)


class Unsupported(Exception):
    pass


def rk(f, x, regs, pc=None, operand=None):
    if x >= CONST:
        idx = x - CONST
        if idx >= len(f.constants):
            raise Unsupported(f"constant {idx} out of range")
        c = f.constants[idx]
        ref = (pc, operand) if pc is not None and operand else None
        if isinstance(c, bytes):
            return Leaf(idx, "const", c.decode("latin-1"), ref)
        return Leaf(idx, "const", c, ref)
    v = regs[x]
    if v is None:
        raise Unsupported(f"read of uninitialised register R{x}")
    return v


def decode_proto(f):
    """Symbolically execute one mission proto and return its config table."""
    regs = [None] * 256
    root = None
    flow = None
    for pc, word in enumerate(f.code):
        op, a, b, c, bx, _sbx = decode(word)
        name = OPCODES[op]
        if name == "LOADK":
            c = f.constants[bx]
            val = c.decode("latin-1") if isinstance(c, bytes) else c
            regs[a] = Leaf(bx, "const", val, (pc, "bx"))
        elif name == "LOADNIL":
            for r in range(a, a + b + 1):
                regs[r] = None
        elif name in ("NEWTABLE",):
            regs[a] = Node("table")
        elif name == "SETTABLE":
            t = regs[a]
            if not isinstance(t, Node):
                raise Unsupported(f"SETTABLE into {type(t).__name__} at {pc}")
            key = rk(f, b, regs, pc, "b")
            val = rk(f, c, regs, pc, "c")
            k = key.value if isinstance(key, Leaf) else key
            if isinstance(k, float) and k.is_integer():
                k = int(k)
            if k == "<computed>":
                k = "config"
            t.set(k, val)
            if a == 2:
                root = t
            if getattr(t, "global_name", None) == "mm_MissionFlowConfigs" and isinstance(val, Node):
                flow = val
        elif name == "SETLIST":
            t = regs[a]
            if not isinstance(t, Node):
                raise Unsupported(f"SETLIST on non-table at {pc}")
            base = (c - 1) * 50
            for i in range(1, b + 1):
                v = regs[a + i]
                if v is None:
                    raise Unsupported(f"SETLIST gap at {pc}")
                t.set(base + i, v)
        elif name == "GETGLOBAL":
            gname = f.constants[bx].decode("latin-1")
            if gname in EXTERNAL_TABLES:
                # A global used as a table (mm_MissionFlowConfigs[name] = ...).
                node = Node("table")
                node.global_name = gname
                regs[a] = node
            else:
                regs[a] = Leaf(bx, "global", gname, (pc, "bx"))
        elif name == "GETTABLE":
            t = regs[b]
            key = rk(f, c, regs)
            if isinstance(t, Leaf) and t.kind == "global" and isinstance(key, Leaf):
                # e.g. MissionType.Priority -> editable through either name
                regs[a] = Leaf(t.slot, "global", f"{t.value}.{key.value}")
            else:
                regs[a] = Leaf(-1, "global", "<computed>")
        elif name == "MOVE":
            regs[a] = regs[b]
        elif name in ("CALL", "GETGLOBAL_CALL"):
            pass
        elif name in ("CLOSURE", "CLOSE", "GETUPVAL", "SETUPVAL", "RETURN", "JMP",
                      "EQ", "LT", "LE", "TEST", "TESTSET", "CONCAT", "VARARG"):
            continue
        else:
            # arithmetic, SELF, FOR*, UNM/NOT/LEN: not used by these tables
            regs[a] = None
    if root is None:
        raise Unsupported("no SETTABLE into R2; not a mission table")

    if flow is None:
        for r in regs:
            if isinstance(r, Node) and r.global_name == "mm_MissionFlowConfigs":
                for _k, v in r.items:
                    if isinstance(v, Node):
                        flow = v
    return root, flow


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def coerce_literal(text):
    """Parse an edited value back into a constant, preserving the displayed spelling.

    The field list shows constants as Python reprs ("True", "False", "None", 'name'), while the bytecode dump uses
    Lua spellings; a value copied from either place must round-trip. Matching is case-insensitive for the keywords,
    single and double quotes are both accepted, and anything else falls through to a number or a byte string.
    """
    t = text.strip()
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("nil", "none"):
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1].encode("latin-1")
    try:
        return float(t)
    except ValueError:
        return t.encode("latin-1")


def _unquote(text):
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _coerce(text):
    return coerce_literal(text)


OP_SET_B = 23
OP_SET_C = 14
OP_BX = 14


def _put_field(word, shift, bits, value):
    mask = ((1 << bits) - 1) << shift
    return (word & ~mask) | ((value & ((1 << bits) - 1)) << shift)


def rewrite_ref(proto, ref, new_index):
    """Point one instruction operand at a (possibly new) constant."""
    pc, operand = ref
    word = proto.code[pc]
    if operand == "bx":
        proto.code[pc] = _put_field(word, OP_BX, 18, new_index)
    elif operand == "b":
        if new_index > 255:
            raise Unsupported("constant index past 255 cannot be an RK operand")
        proto.code[pc] = _put_field(word, OP_SET_B, 9, CONST + new_index)
    elif operand == "c":
        if new_index > 255:
            raise Unsupported("constant index past 255 cannot be an RK operand")
        proto.code[pc] = _put_field(word, OP_SET_C, 9, CONST + new_index)
    else:
        raise Unsupported(f"unknown operand {operand!r}")


def intern_constant(proto, value):
    for i, c in enumerate(proto.constants):
        if type(c) is type(value) and c == value:
            return i
    proto.constants.append(value)
    return len(proto.constants) - 1


def flatten(node, prefix=""):
    """Yield (path, leaf) for every leaf in the tree."""
    if isinstance(node, Node):
        for k, v in node.items:
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, Node):
                yield from flatten(v, p)
            else:
                yield p, v
    else:
        yield prefix, node


def find_path(root, path):
    cur = root
    for part in path.replace("[", ".").replace("]", "").split("."):
        if part == "":
            continue
        if isinstance(cur, Node):
            d = cur.as_dict()
            key = part
            if key not in d and key.isdigit() and int(key) in d:
                key = int(key)
            if key not in d:
                raise KeyError(f"{part!r} not found")
            cur = d[key]
        else:
            raise KeyError(f"{part!r} descends into a leaf")
    return cur


def render(node, indent=0, out=sys.stdout):
    pad = "  " * indent
    if isinstance(node, Node):
        for k, v in node.items:
            if isinstance(v, Node):
                print(f"{pad}{k}:", file=out)
                render(v, indent + 1, out)
            else:
                print(f"{pad}{k} = {v.render()}   [const {v.slot}]", file=out)
    else:
        print(f"{pad}{node.render()}", file=out)


def views(proto):
    root, flow = decode_proto(proto)
    return {"meta": root, "flow": flow}


def resolve(view_map, path):
    """Resolve `timeOfDay` or `flow.timeOfDay` or `activationData.activeList[0]`."""
    parts = path.replace("[", ".").replace("]", "").split(".")
    prefix = None
    if parts[0] in view_map:
        prefix, parts = parts[0], parts[1:]
    order = [prefix] if prefix else ["flow", "meta"]
    last_err = None
    for v in order:
        node = view_map.get(v)
        if node is None:
            continue
        try:
            return find_path(node, ".".join(parts)), v
        except KeyError as e:
            last_err = e
    raise KeyError(last_err or path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="missiondefinit.lua or an .rcf archive")
    ap.add_argument("cmd", choices=["dump", "set", "paths", "export", "apply"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    a.src_path = a.path

    if a.path.endswith(".rcf"):
        from rcf import Rcf
        with Rcf(a.path) as arc:
            blob = arc.read(arc.find("scripts\\missiondefinit.lua"))
    else:
        blob = open(a.path, "rb").read()
    root = load(blob)

    if a.cmd == "export":
        target = a.args[0]
        out = a.args[1] if len(a.args) > 1 else None
        found = next(((p, n, pr) for p, n, pr in mission_protos(root) if n == target), None)
        if found is None:
            print(f"mission not found: {target}", file=sys.stderr)
            return 1
        path, name, proto = found
        lines = [
            f"# {name} — decoded mission definition",
            "#",
            "# Edit the values on the right of '=' and run:",
            "#     ./proto2 apply-aspects <this file>",
            "#     ./proto2 install",
            "#",
            "# Value syntax:  bare text = string   123 or 1.5 = number",
            "#                true / false = boolean   nil = nil",
            "# A value shown in \"quotes\" is a string that would otherwise be",
            "# read as a number, boolean or nil.",
            f"# mission: {name}",
            "",
        ]
        for view, node in views(proto).items():
            if node is None:
                continue
            lines.append(f"# ---- {view}")
            for p_, leaf in flatten(node):
                v = leaf.value
                if leaf.kind == "global":
                    text = v
                elif isinstance(v, bytes):
                    text = v.decode("latin-1")
                elif v is None:
                    text = "nil"
                elif isinstance(v, bool):
                    text = "true" if v else "false"
                elif isinstance(v, float):
                    text = repr(v)
                else:
                    text = str(v)
                if isinstance(v, str) and (text in ("true", "false", "nil") or _is_number(text) or text != text.strip() or text == ""):
                    text = '"' + text + '"'
                if leaf.ref is None:
                    # computed value (e.g. MissionType.Priority): show it, but do
                    # not offer a line that apply would reject.
                    lines.append(f"# {view}.{p_} = {text}   (computed, not editable)")
                else:
                    lines.append(f"{view}.{p_} = {text}")
            lines.append("")
        blob_out = "\n".join(lines) + "\n"
        if out:
            open(out, "w").write(blob_out)
            print(f"wrote {out} ({len(lines)} lines, mission {name})")
        else:
            sys.stdout.write(blob_out)
        return 0

    if a.cmd == "apply":
        flat = a.args[0]
        text = open(flat).read()
        target = None
        for line in text.splitlines():
            if line.startswith("# mission:"):
                target = line.split(":", 1)[1].strip()
        if not target:
            print("no '# mission:' header in the file", file=sys.stderr)
            return 1
        found = next(((p, n, pr) for p, n, pr in mission_protos(root) if n == target), None)
        if found is None:
            print(f"mission not found: {target}", file=sys.stderr)
            return 1
        path, name, proto = found
        current = views(proto)
        changes, errors = [], []
        for lineno, line in enumerate(text.splitlines(), 1):
            raw_line = line.strip()
            if not raw_line or raw_line.startswith("#"):
                continue
            if "=" not in raw_line:
                errors.append(f"{lineno}: no '=' in {raw_line!r}")
                continue
            key, _, rawval = raw_line.partition("=")
            key, rawval = key.strip(), rawval.strip()
            try:
                leaf, view = resolve(current, key)
            except KeyError as e:
                errors.append(f"{lineno}: {e}")
                continue
            if not isinstance(leaf, Leaf) or leaf.ref is None:
                errors.append(f"{lineno}: {key} is not an editable leaf")
                continue
            rawval = _unquote(rawval)
            new = _coerce(rawval)
            if leaf.kind == "global":
                new = rawval.encode("latin-1")
            if leaf.value == new or (isinstance(leaf.value, str) and isinstance(new, bytes)
                                     and leaf.value == new.decode("latin-1")):
                continue
            idx = intern_constant(proto, new)
            rewrite_ref(proto, leaf.ref, idx)
            changes.append((key, leaf.value, rawval))
            # refresh the view so later lines see this edit
            current = views(proto)

        if errors:
            print("errors, nothing written:", file=sys.stderr)
            for e in errors[:20]:
                print("  " + e, file=sys.stderr)
            return 1
        if not changes:
            print(f"{name}: no changes")
            return 0
        out_blob = dump(root)
        again = load(out_blob)
        proto2 = next(pr for p_, n, pr in mission_protos(again) if n == name)
        check = views(proto2)
        for key, _old, want in changes:
            leaf2, _v = resolve(check, key)
            got = leaf2.value
            if leaf2.kind == "global":
                ok = got == want
            else:
                ok = got == _coerce(want)
            if not ok:
                print(f"verification failed for {key}: {got!r} != {want!r}", file=sys.stderr)
                return 1
        open(a.src_path, "wb").write(out_blob)
        print(f"{name}: {len(changes)} field(s) changed")
        for key, old, new in changes:
            print(f"  {key}: {old!r} -> {new!r}")
        print(f"wrote {a.src_path} ({len(blob)} -> {len(out_blob)} bytes, verified)")
        return 0

    if a.cmd == "dump":
        target = a.args[0] if a.args else None
        for p, name, proto in mission_protos(root):
            if target and name != target:
                continue
            v = views(proto)
            print(f"=== {name}  proto[{p}]")
            if v["meta"] is not None:
                print("  [meta]")
                render(v["meta"], 2)
            if v["flow"] is not None:
                print("  [flow]")
                render(v["flow"], 2)
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

    if a.cmd == "paths":
        for view, node in views(proto).items():
            if node is None:
                continue
            for p, leaf in flatten(node):
                print(f"{view}.{p:40} {leaf.render():26} [const {leaf.slot}]")
        return 0

    if len(a.args) < 3:
        print("set needs MISSION PATH VALUE", file=sys.stderr)
        return 1
    field, raw = a.args[1], a.args[2]
    leaf, view = resolve(views(proto), field)
    if not isinstance(leaf, Leaf):
        print(f"{field} is a table, not a leaf", file=sys.stderr)
        return 1
    if leaf.slot < 0:
        print(f"{field} is computed and not editable", file=sys.stderr)
        return 1

    new = coerce_literal(raw)

    if leaf.ref is None:
        print(f"{field} has no editable reference", file=sys.stderr)
        return 1
    old = proto.constants[leaf.slot]
    new_index = intern_constant(proto, new)
    rewrite_ref(proto, leaf.ref, new_index)
    out_blob = dump(root)

    # verify: reparse and read the same path back
    again = load(out_blob)
    proto2 = next(pr for p, n, pr in mission_protos(again) if n == target)
    leaf2, _ = resolve(views(proto2), field)
    expected = raw if isinstance(new, bytes) else new
    ok = isinstance(leaf2, Leaf) and leaf2.value == expected
    print(f"{name}.{field}: {old!r} -> {new!r}  [{view} {leaf.ref[1]}@{leaf.ref[0]} "
          f"-> const {new_index}]")
    print(f"reparse check: {'ok' if ok else 'MISMATCH'}")
    if not ok:
        print(f"  found {leaf2.value!r}, expected {expected!r} — not writing", file=sys.stderr)
        return 1
    open(a.out or a.path, "wb").write(out_blob)
    print(f"wrote {a.out or a.path} ({len(blob)} -> {len(out_blob)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
