#!/usr/bin/env python3
"""Decode, inspect and edit `datainit.lua` straight from its Lua 5.1 bytecode.

`datainit.lua` is a 565 KB precompiled Lua 5.1 chunk (float32 `lua_Number`) that
ships inside `boot.rcf`.  Its root proto contains no code of its own: it is 265
pairs of `CLOSURE`/`CALL` that immediately invoke 265 function bodies.  Each
body is one module of the original source and either

  * defines library functions           -> `name = function() ... end`
  * defines scalar constants           -> `TANK = "tank_ram_marine"`
  * builds a table into a global       -> `EPAwards = { [UIDKey("X")] = {...} }`
  * builds a field of a global table   -> `Interface.Unlockables = { ... }`
  * registers a table through a call   -> `mms_PackageSizeInfo:AddPackage(NAME, {...})`

Those bodies are straight-line table-building code: NEWTABLE, then key/value
pairs through SETTABLE with RK operands, SETLIST for the array part,
GETGLOBAL/GETTABLE for named constants and enum members, LOADK for literals.
That means **every literal leaf is one constant-table entry reached through one
instruction operand**, so a field can be changed by rewriting that operand
exactly the way `tools/mission_tree.py` does it -- without touching any other
field, even when several fields share a constant slot.

Paths are dotted, with `[n]` for array elements (Lua 1-based keys):

    EPAwards.UIDKey(MARINE_TEMPLATE).kill
    Interface.Unlockables.All[1].startingIndex
    GUNSHIP_PACKAGE.xenon

Values that are computed at load time (enum members such as
`Unlockable.Powers_Begin`, constructor calls such as `Vector(0, 0, 100)`) are
shown but are not editable, because no single constant controls them.

Usage (the source may be a chunk on disk, an .rcf archive, or "game" for the
shipped `boot.rcf`):

    python3 tools/datainit.py game extract work/lua/datainit.lua
    python3 tools/datainit.py game list
    python3 tools/datainit.py game list --tables --filter PACKAGE
    python3 tools/datainit.py game tree EPAwards --depth 2
    python3 tools/datainit.py game get 'EPAwards.UIDKey(MARINE_TEMPLATE).kill'
    python3 tools/datainit.py game paths Interface.Unlockables
    python3 tools/datainit.py game export --out tmp/datainit.json
    python3 tools/datainit.py work/lua/datainit.lua set 'COP' '"RiotCop"'
    python3 tools/datainit.py work/lua/datainit.lua verify

`set` writes the chunk back to the same file in place (use `--out` to write a
copy, and `--dry-run` to only report).  It refuses to write into the game
install or into an archive.  Every edit is re-parsed from the written bytes and
compared against the pre-edit decode; the command fails unless exactly the
edited path(s) changed and the new value reads back.

Scope and honesty: the decoder is a symbolic interpreter, not a Lua VM.  It
models the straight-line table building above, the global environment, and
`_G`; it treats a table handed to a statement-level call as a definition named
after the call's identifying argument, but it does **not** execute the callee,
so the call's own effect on its receiver is not modelled.  Chunk bodies that
contain branches or loops are decoded along the fall-through path and flagged;
any values that depend on the skipped path are shown as `<...>` placeholders.
"""

import argparse
import hashlib
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import config  # noqa: E402
from lua51 import load, dump, decode as ins_decode, OPCODES  # noqa: E402

OPMAP = {name: i for i, name in enumerate(OPCODES)}
BITRK = 256
MAX_RK = 255
MAX_BX = (1 << 18) - 1

OP_SET_B = 23
OP_SET_C = 14
OP_BX = 14

# Lua 5.1 opcodes whose Bx field is a constant index.  (CLOSURE's Bx is a
# nested-proto index, not a constant.)
BX_CONST_OPS = {OPMAP["LOADK"], OPMAP["GETGLOBAL"], OPMAP["SETGLOBAL"]}
# Opcodes whose B and/or C fields are RK operands (`>= 256` means constant).
RK_B_OPS = {OPMAP["SETTABLE"], OPMAP["ADD"], OPMAP["SUB"], OPMAP["MUL"],
            OPMAP["DIV"], OPMAP["MOD"], OPMAP["POW"], OPMAP["EQ"],
            OPMAP["LT"], OPMAP["LE"]}
RK_C_OPS = {OPMAP["GETTABLE"], OPMAP["SETTABLE"], OPMAP["SELF"]} | RK_B_OPS
# Conditional-branch predecessors: a JMP right after one of these is the
# conditional jump itself (fall-through = branch taken).
COND_JUMPS = {OPMAP["TEST"], OPMAP["TESTSET"], OPMAP["EQ"], OPMAP["LT"],
              OPMAP["LE"]}

UNSET = object()
GAME_DEFAULT = config.game_dir()
ARCHIVE_ENTRY = "scripts\\datainit.lua"


class DecodeError(Exception):
    pass


# ---------------------------------------------------------------------------
# value model
# ---------------------------------------------------------------------------

class Leaf:
    """A value read through one instruction operand.

    `slot` is the proto constant index (None for instructions with no constant
    operand, e.g. LOADBOOL), `ref` is `(pc, operand)` naming the instruction
    field that points at the slot, `proto` is the proto that owns the
    instruction, and `kind` is one of:

        const     literal from LOADK or an RK operand (editable)
        global    GETGLOBAL reference; value is the global name (editable)
        function  CLOSURE reference; ref is the CLOSURE operand (not editable)
        nil       LOADNIL, no constant slot (not editable)
    """

    __slots__ = ("slot", "kind", "value", "ref", "proto")

    def __init__(self, slot, kind, value, ref=None, proto=None):
        self.slot = slot
        self.kind = kind
        self.value = value
        self.ref = ref
        self.proto = proto

    @property
    def editable(self):
        return self.kind in ("const", "global") and self.ref is not None

    def render(self):
        if self.kind == "global":
            return f"<global {self.value}>"
        if self.kind == "function":
            return f"<function {self.value}>"
        if self.kind == "nil":
            return "nil"
        return render_scalar(self.value)

    def annotation(self):
        if self.kind == "const":
            return f"const {self.slot}"
        if self.kind == "global":
            return f"global, const {self.slot}"
        if self.kind == "function":
            return "function"
        return "nil"


class Opaque:
    """A value computed by the script (enum member, call result, arithmetic)."""

    __slots__ = ("kind", "text")

    def __init__(self, kind, text):
        self.kind = kind
        self.text = text

    def render(self):
        return self.text

    def annotation(self):
        return f"{self.kind}, not editable"


class CallValue(Opaque):
    """A value returned by a call.

    Engine factories such as `fes_CreateMarkerProfile(base)` return a table
    that the script then fills in with SETTABLE.  The result starts out opaque
    (so `Vector(0, 0, 100)` still renders as a call) and is materialised as a
    table the first time the chunk indexes it.
    """

    __slots__ = ("node",)

    def __init__(self, text):
        super().__init__("call", text)
        self.node = None

    def materialise(self):
        if self.node is None:
            self.node = Node()
        return self.node

    def render(self):
        if self.node is not None:
            return f"table({len(self.node)})"
        return self.text

    def annotation(self):
        if self.node is not None:
            return "table from call"
        return "call, not editable"


class Node:
    """A Lua table built by the chunk, preserving assignment order."""

    __slots__ = ("items",)

    def __init__(self):
        self.items = []          # [[key, value], ...]

    def set(self, key, value):
        for pair in self.items:
            if keys_equal(pair[0], key):
                pair[1] = value
                return
        self.items.append([key, value])

    def get(self, key):
        for k, v in self.items:
            if keys_equal(k, key):
                return v
        return UNSET

    def delete(self, key):
        for i, (k, _v) in enumerate(self.items):
            if keys_equal(k, key):
                del self.items[i]
                return

    def __len__(self):
        return len(self.items)

    def is_array(self):
        return bool(self.items) and all(
            isinstance(k, int) and not isinstance(k, bool) for k, _ in self.items)

    def ordered(self):
        if self.is_array():
            return sorted(self.items, key=lambda p: p[0])
        return self.items


class KeyLabel:
    """A table key computed at load time (e.g. `UIDKey("MARINE_TEMPLATE")`)."""

    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    def __eq__(self, other):
        return isinstance(other, KeyLabel) and other.text == self.text

    def __hash__(self):
        return hash(("keylabel", self.text))


class GlobalRef:
    """An external global used as a table (`Interface`, `Game`, ...)."""

    __slots__ = ("name", "fields")

    def __init__(self, name):
        self.name = name
        self.fields = {}

    def render(self):
        return f"<global {self.name}>"


class GlobalEnv:
    """`_G`, the global environment itself."""

    __slots__ = ("env",)

    def __init__(self, env):
        self.env = env

    def render(self):
        return "<global _G>"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def f32(x):
    """Round a Python float to the float32 the chunk will actually store."""
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


def render_scalar(v):
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return format(v, ".7g")
    if isinstance(v, str):
        return quote(v)
    return repr(v)


def quote(s):
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 32 <= ord(ch) < 127:
            out.append(ch)
        else:
            out.append("\\%03d" % (ord(ch) & 0xFF))
    out.append('"')
    return "".join(out)


def key_of(key):
    """Normalise an RK/register key value into a Python table key."""
    if isinstance(key, Leaf):
        return normalize_key(key.value)
    if isinstance(key, KeyLabel):
        return key
    if isinstance(key, Opaque):          # includes CallValue
        return KeyLabel(key.render())
    if isinstance(key, (str, int, float, bool)) or key is None:
        return normalize_key(key)
    return KeyLabel(render_any(key))


def normalize_key(k):
    if isinstance(k, float) and k.is_integer():
        return int(k)
    return k


def keys_equal(a, b):
    a, b = normalize_key(a), normalize_key(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, KeyLabel) or isinstance(b, KeyLabel):
        ta = a.text if isinstance(a, KeyLabel) else key_text(a)
        tb = b.text if isinstance(b, KeyLabel) else key_text(b)
        return ta == tb
    return type(a) is type(b) and a == b


def key_text(k):
    if isinstance(k, KeyLabel):
        return k.text
    if isinstance(k, bool):
        return "true" if k else "false"
    if isinstance(k, float):
        return format(k, ".7g")
    return str(k)


def _path_key_needs_quotes(k):
    if not isinstance(k, str):
        return False
    if k == "" or k != k.strip() or any(ch in k for ch in '.[]"'):
        return True
    if k in ("true", "false", "nil"):
        return True
    return k.lstrip("-").isdigit()


def _quote_path_key(k):
    return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_path(parts):
    """Render a path; keys that cannot be a bare token use ["..."] quoting."""
    out = key_text(parts[0]) if parts else ""
    for p in parts[1:]:
        if isinstance(p, int) and not isinstance(p, bool):
            out += f"[{p}]"
            continue
        text = key_text(p)
        if _path_key_needs_quotes(text):
            out += "[" + _quote_path_key(text) + "]"
        else:
            out += "." + text
    return out


def same_const(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, bytes) and isinstance(b, bytes):
        return a == b
    if isinstance(a, float) and isinstance(b, float):
        return a == b
    return False


def _put_field(word, shift, bits, value):
    mask = ((1 << bits) - 1) << shift
    return (word & ~mask) | ((value & ((1 << bits) - 1)) << shift)


def decode_const(c):
    if isinstance(c, bytes):
        return c.decode("latin-1")
    return c


def const_text(proto, index):
    c = proto.constants[index]
    if isinstance(c, bytes):
        return c.decode("latin-1")
    raise DecodeError(f"constant {index} is not a string: {c!r}")


def walk_protos(proto):
    yield proto
    for child in proto.protos:
        yield from walk_protos(child)


# ---------------------------------------------------------------------------
# constant reference bookkeeping and operand rewriting
# ---------------------------------------------------------------------------

def constant_refs(proto):
    """Map constant slot -> list of (pc, operand) that reference it."""
    refs = {}
    for pc, word in enumerate(proto.code):
        op, a, b, c, bx, _sbx = ins_decode(word)
        if op in BX_CONST_OPS:
            refs.setdefault(bx, []).append((pc, "bx"))
        if op in RK_B_OPS and b >= BITRK:
            refs.setdefault(b - BITRK, []).append((pc, "b"))
        if op in RK_C_OPS and c >= BITRK:
            refs.setdefault(c - BITRK, []).append((pc, "c"))
    return refs


def remap_constants(proto, mapping):
    """Rewrite every constant reference through `mapping` (old -> new slot)."""
    if not mapping:
        return
    for pc, word in enumerate(proto.code):
        op, a, b, c, bx, _sbx = ins_decode(word)
        new = word
        if op in BX_CONST_OPS and bx in mapping:
            new = _put_field(new, OP_BX, 18, mapping[bx])
        if op in RK_B_OPS and b >= BITRK and (b - BITRK) in mapping:
            target = mapping[b - BITRK]
            if target > MAX_RK:
                raise DecodeError(
                    f"cannot move constant {b - BITRK} to slot {target}: "
                    f"RK operands address only slots 0..{MAX_RK}")
            new = _put_field(new, OP_SET_B, 9, BITRK + target)
        if op in RK_C_OPS and c >= BITRK and (c - BITRK) in mapping:
            target = mapping[c - BITRK]
            if target > MAX_RK:
                raise DecodeError(
                    f"cannot move constant {c - BITRK} to slot {target}: "
                    f"RK operands address only slots 0..{MAX_RK}")
            new = _put_field(new, OP_SET_C, 9, BITRK + target)
        proto.code[pc] = new


def rewrite_ref(proto, ref, new_index):
    """Point one instruction operand at a (possibly new) constant."""
    pc, operand = ref
    if pc < 0 or pc >= len(proto.code):
        raise DecodeError(f"instruction {pc} out of range")
    if new_index > MAX_BX:
        raise DecodeError(f"constant index {new_index} does not fit in Bx")
    word = proto.code[pc]
    if operand == "bx":
        proto.code[pc] = _put_field(word, OP_BX, 18, new_index)
    elif operand in ("b", "c"):
        if new_index > MAX_RK:
            raise DecodeError(
                f"constant index {new_index} cannot be an RK operand "
                f"(slots 0..{MAX_RK} only)")
        shift = OP_SET_B if operand == "b" else OP_SET_C
        proto.code[pc] = _put_field(word, shift, 9, BITRK + new_index)
    else:
        raise DecodeError(f"unknown operand {operand!r}")


# ---------------------------------------------------------------------------
# symbolic interpreter
# ---------------------------------------------------------------------------

class Entry:
    __slots__ = ("name", "value", "source", "chunk", "pc", "note", "conditional")

    def __init__(self, name, value, source, chunk, pc, note=None,
                 conditional=False):
        self.name = name
        self.value = value
        self.source = source      # 'global' | 'field' | 'call'
        self.chunk = chunk        # root.protos index
        self.pc = pc
        self.note = note
        self.conditional = conditional


class Decoder:
    """Symbolically execute the chunk protos of datainit.lua in order."""

    def __init__(self, chunk_protos):
        self.chunks = chunk_protos
        self.env = {}
        self.entries = []
        self.entry_index = {}
        self.calls = []
        self.named_objects = {}     # id(value) -> dotted path of the last name
        self.stats = {
            "chunks": len(chunk_protos),
            "chunks_imprecise": 0,
            "chunks_truncated": 0,
            "unsupported_ops": 0,
            "lost_assignments": 0,
            "global_assignments": 0,
            "field_assignments": 0,
            "statement_calls": 0,
            "dead_moves": 0,
            "unset_reads": 0,
            "missing_keys": 0,
        }
        self._imprecise = False

    # -- globals --------------------------------------------------------
    def global_value(self, name):
        if name == "_G":
            return GlobalEnv(self.env)
        if name in self.env:
            return self.env[name]
        ref = GlobalRef(name)
        self.env[name] = ref
        return ref

    def define_global(self, name, value, chunk, pc):
        if value is UNSET:
            self.stats["unset_reads"] += 1
            self._imprecise = True
            value = Opaque("unknown", "<unset register>")
        self.env[name] = value
        self.stats["global_assignments"] += 1
        self.record(name, value, "global", chunk, pc)

    def define_field(self, base_name, key, value, chunk, pc):
        """Record a field of a *root* global as its own top-level entry.

        Deeper bindings (`Interface.Unlockables.All`, ...) are part of the
        parent entry's tree, not separate top-level definitions.
        """
        self.stats["field_assignments"] += 1
        if "." in base_name:
            return
        if isinstance(key, KeyLabel):
            # rows keyed by a value computed at load time (UIDKey("...")) are
            # part of the parent table's tree, not separately named definitions
            return
        name = render_path([base_name, key])
        self.record(name, value, "field", chunk, pc)

    def record(self, name, value, source, chunk, pc, note=None):
        index = self.entry_index.get(name)
        if index is not None:
            entry = self.entries[index]
            entry.value = value
            entry.source = source
            entry.chunk = chunk
            entry.pc = pc
            return
        entry = Entry(name, value, source, chunk, pc, note, self._imprecise)
        self.entry_index[name] = len(self.entries)
        self.entries.append(entry)

    # -- chunk execution ------------------------------------------------
    def run(self):
        for i, proto in enumerate(self.chunks):
            self._imprecise = False
            try:
                self.run_chunk(i, proto)
            except DecodeError as exc:
                print(f"warning: chunk {i}: {exc}", file=sys.stderr)
                self.stats["chunks_truncated"] += 1
            if self._imprecise:
                self.stats["chunks_imprecise"] += 1
        return self.entries

    def run_chunk(self, chunk_index, f, regs=None, args=()):
        """Execute one proto linearly.  Returns (regs, returned)."""
        if regs is None:
            regs = [UNSET] * 256
        regpath = [None] * 256
        for r, value in enumerate(args):
            regs[r] = value
        n = len(f.code)
        pc = 0
        prev_op = None
        while pc < n:
            word = f.code[pc]
            op, a, b, c, bx, sbx = ins_decode(word)
            name = OPCODES[op]
            op_before = prev_op
            prev_op = op
            if name == "MOVE":
                # Some chunk protos contain dead `MOVE Rx, Ry` statements that
                # would overwrite the table a constructor is building in Rx
                # (see docs/14-datainit.md, "MOVE artifacts").  Keep the table.
                if is_table_value(regs[a]) and not is_table_value(regs[b]):
                    self.stats["dead_moves"] += 1
                    regpath[a] = None
                else:
                    regs[a] = regs[b]
                    regpath[a] = regpath[b]
            elif name == "LOADK":
                const = f.constants[bx]
                regs[a] = Leaf(bx, "const", decode_const(const), (pc, "bx"), f)
                regpath[a] = None
            elif name == "LOADBOOL":
                regs[a] = Leaf(None, "const", bool(b), None, f)
                regpath[a] = None
                if c:
                    pc += 1           # skip next instruction
            elif name == "LOADNIL":
                for r in range(a, a + b + 1):
                    regs[r] = Leaf(None, "nil", None, None, f)
                    regpath[r] = None
            elif name == "GETGLOBAL":
                gname = const_text(f, bx)
                regs[a] = self.global_value(gname)
                regpath[a] = gname
            elif name == "GETUPVAL":
                regs[a] = Opaque("upvalue", f"<upvalue {b}>")
                regpath[a] = None
            elif name == "SETGLOBAL":
                gname = const_text(f, bx)
                self.define_global(gname, regs[a], chunk_index, pc)
                regpath[a] = None
            elif name == "GETTABLE":
                key = self.rk(f, c, regs, pc, "c")
                base = regs[b]
                regs[a] = self.index(base, key)
                if isinstance(base, GlobalEnv) and isinstance(key, Leaf) and \
                        isinstance(key.value, str):
                    regpath[a] = key.value      # _G.Interface -> "Interface"
                elif regpath[b]:
                    regpath[a] = regpath[b] + "." + key_text(
                        key.value if isinstance(key, Leaf) else key)
                else:
                    regpath[a] = None
            elif name == "SETTABLE":
                key = self.rk(f, b, regs, pc, "b")
                value = self.rk(f, c, regs, pc, "c")
                self.setindex(regs[a], key, value, regpath[a], chunk_index, pc)
            elif name == "NEWTABLE":
                regs[a] = Node()
                regpath[a] = None
            elif name == "SETLIST":
                table = regs[a]
                base = (c - 1) * 50
                if not isinstance(table, Node):
                    self.stats["lost_assignments"] += 1
                    continue
                for i in range(1, b + 1):
                    value = regs[a + i]
                    if value is UNSET:
                        self.stats["unset_reads"] += 1
                        self._imprecise = True
                    table.set(base + i, self.plain(value))
            elif name == "SELF":
                base = regs[b]
                key = self.rk(f, c, regs, pc, "c")
                regs[a + 1] = base
                regs[a] = self.method(base, key, regpath[b])
                regpath[a] = (regpath[b] + ":" + key_text(
                    key.value if isinstance(key, Leaf) else key)) if regpath[b] else None
                regpath[a + 1] = regpath[b]
            elif name == "CALL":
                callee_text = regpath[a]
                self.do_call(chunk_index, f, pc, a, b, c, regs, callee_text)
                for r in range(a, min(a + max(c - 1, 1) + 1, 256)):
                    regpath[r] = None
            elif name == "TAILCALL":
                # results are the function's return values; record the call but
                # do not treat a table argument as a definition
                self.do_call(chunk_index, f, pc, a, b, 2, regs, regpath[a])
                return regs, True
            elif name == "RETURN":
                return regs, True
            elif name == "CLOSURE":
                if bx >= len(f.protos):
                    raise DecodeError(f"CLOSURE proto {bx} out of range")
                regs[a] = Leaf(None, "function", f"proto {bx}", (pc, "bx"), f)
                regpath[a] = None
            elif name == "CLOSE":
                pass
            elif name == "VARARG":
                regs[a] = Opaque("vararg", "...")
                regpath[a] = None
            elif name in ("ADD", "SUB", "MUL", "DIV", "MOD", "POW"):
                left = self.rk(f, b, regs, pc, "b")
                right = self.rk(f, c, regs, pc, "c")
                regs[a] = Opaque("expr", f"({render_any(left)} {name.lower()} "
                                         f"{render_any(right)})")
                regpath[a] = None
            elif name in ("UNM", "NOT", "LEN"):
                regs[a] = Opaque("expr", f"({name.lower()} {render_any(regs[b])})")
                regpath[a] = None
            elif name == "CONCAT":
                parts = [render_any(regs[r]) for r in range(b, c + 1)]
                regs[a] = Opaque("expr", "(" + " .. ".join(parts) + ")")
                regpath[a] = None
            elif name == "JMP":
                target = pc + 1 + sbx
                if target <= pc:
                    self.stats["chunks_truncated"] += 1
                    return regs, False          # do not follow a loop
                self._imprecise = True
                if op_before not in COND_JUMPS:
                    # an unconditional jump is the "skip the else block" jump
                    # at the end of an if/else: follow it to the join point
                    pc = target - 1
                    prev_op = None
                    continue
            elif name in ("TEST", "TESTSET", "EQ", "LT", "LE",
                          "FORLOOP", "FORPREP", "TFORLOOP"):
                self._imprecise = True
            else:
                self.stats["unsupported_ops"] += 1
                regs[a] = Opaque("unsupported", f"<{name}>")
                regpath[a] = None
            pc += 1
        return regs, False

    # -- operands -------------------------------------------------------
    def rk(self, f, x, regs, pc=None, operand=None):
        if x >= BITRK:
            slot = x - BITRK
            if slot >= len(f.constants):
                raise DecodeError(f"constant {slot} out of range at pc {pc}")
            ref = (pc, operand) if pc is not None and operand else None
            return Leaf(slot, "const", decode_const(f.constants[slot]), ref, f)
        value = regs[x]
        if value is UNSET:
            self.stats["unset_reads"] += 1
            self._imprecise = True
            return Opaque("unknown", f"<unset R{x}>")
        return value

    def plain(self, value):
        if isinstance(value, (Node, Leaf, Opaque, GlobalRef, GlobalEnv)):
            return value
        if value is UNSET:
            self.stats["unset_reads"] += 1
            self._imprecise = True
            return Opaque("unknown", "<unset register>")
        return Leaf(None, "const", value, None, None)

    def index(self, base, key):
        k = key_of(key)
        if isinstance(base, GlobalEnv):
            if isinstance(k, str):
                return self.global_value(k)
            return Opaque("index", f"<_G[{key_text(k)}]>")
        if isinstance(base, GlobalRef):
            if k in base.fields:
                return base.fields[k]
            return Opaque("member", f"{base.name}.{key_text(k)}")
        if isinstance(base, CallValue):
            base = base.materialise()
        if isinstance(base, Node):
            value = base.get(k)
            if value is UNSET:
                # a key the decoder never saw written is nil at run time
                self.stats["missing_keys"] += 1
                return Leaf(None, "nil", None, None, None)
            return value
        if isinstance(base, Leaf) and base.kind == "global":
            return Opaque("member", f"{base.value}.{key_text(k)}")
        if isinstance(base, Leaf) and base.kind == "const" and \
                isinstance(base.value, str):
            return Opaque("index", f"<index {key_text(k)} of {quote(base.value)}>")
        return Opaque("index", f"<index {key_text(k)}>")

    def method(self, base, key, base_path=None):
        """Callee of a `SELF` call, rendered `base:key`."""
        k = key.value if isinstance(key, Leaf) else key_text(key)
        if base_path:
            return Opaque("method", f"{base_path}:{k}")
        if isinstance(base, GlobalRef):
            return Opaque("method", f"{base.name}:{k}")
        if isinstance(base, Leaf) and base.kind == "global":
            return Opaque("method", f"{base.value}:{k}")
        return Opaque("call", f"{k}")

    def setindex(self, base, key, value, base_path, chunk_index, pc):
        if base is UNSET:
            self.stats["lost_assignments"] += 1
            return
        value = self.plain(value)
        k = key_of(key)
        is_nil = value is None or (isinstance(value, Leaf) and
                                   (value.kind == "nil" or
                                    (value.kind == "const" and value.value is None)))
        if isinstance(base, GlobalEnv):
            if not isinstance(k, str):
                self.stats["lost_assignments"] += 1
                return
            if is_nil:
                self.env.pop(k, None)
            else:
                self.env[k] = value
                self.named_objects[id(value)] = k
            self.record(k, value, "global", chunk_index, pc)
            return
        if isinstance(base, GlobalRef):
            if is_nil:
                base.fields.pop(k, None)
            else:
                base.fields[k] = value
                self.named_objects[id(value)] = f"{base.name}.{key_text(k)}"
            self.define_field(base.name, k, value, chunk_index, pc)
            return
        if isinstance(base, CallValue):
            base = base.materialise()
        if isinstance(base, Node):
            if is_nil:
                base.delete(k)
            else:
                base.set(k, value)
                if base_path:
                    self.named_objects[id(value)] = f"{base_path}.{key_text(k)}"
            if base_path and not is_nil:
                self.define_field(base_path, k, value, chunk_index, pc)
            return
        self.stats["lost_assignments"] += 1

    # -- calls ----------------------------------------------------------
    def do_call(self, chunk_index, f, pc, a, b, c, regs, callee_path=None):
        callee = regs[a]
        nres = c - 1
        args = []
        if b > 0:
            for r in range(a + 1, a + b):
                args.append(regs[r] if regs[r] is not UNSET else Opaque("arg", "?"))
        callee_text = callee_path or render_any(callee)
        visible = args[1:] if isinstance(callee, Opaque) and \
            callee.kind == "method" else args
        self.calls.append({
            "chunk": chunk_index, "pc": pc, "callee": callee_text,
            "args": visible, "results": nres,
        })
        if nres == 0:
            self.stats["statement_calls"] += 1
            self.record_call_entries(chunk_index, pc, callee_text, visible)
        else:
            regs[a] = CallValue(render_call(callee_text, visible))
            for r in range(a + 1, min(a + nres + 1, 256)):
                regs[r] = UNSET

    def record_call_entries(self, chunk_index, pc, callee_text, args):
        """Tables handed to a statement-level call are definitions.

        The callee's own effect on its receiver is not modelled; the entry is
        named after the string/global argument that identifies it (the name the
        callee registers it under), falling back to the call expression.
        """
        tables = [(i, v) for i, v in enumerate(args) if is_table_value(v)]
        if not tables:
            return
        by_id = {id(e.value): e for e in self.entries}
        label = None
        for v in args:
            if isinstance(v, Leaf) and isinstance(v.value, str) and \
                    v.kind in ("const", "global"):
                label = v.value
                break
            if isinstance(v, GlobalRef):
                label = v.name
                break
            if isinstance(v, Opaque) and v.kind in ("member", "expr"):
                label = v.text
                break
        short = callee_text.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
        if short.startswith("<"):
            short = f"chunk{chunk_index}"
        for i, table in tables:
            if isinstance(table, CallValue):
                table = table.node
            if id(table) in by_id:
                entry = by_id[id(table)]
                if entry.note is None:
                    entry.note = f"also passed to {callee_text}"
                continue
            if id(table) in self.named_objects:
                continue
            if label and len(tables) == 1:
                name = label
            else:
                name = f"{short}[arg{i}]"
            name = self.unique_name(name)
            entry = Entry(name, table, "call", chunk_index, pc, callee_text,
                          self._imprecise)
            self.entry_index[name] = len(self.entries)
            self.entries.append(entry)
            by_id[id(table)] = entry
            self.named_objects[id(table)] = name

    def unique_name(self, name):
        if name not in self.entry_index:
            return name
        n = 2
        while f"{name}#{n}" in self.entry_index:
            n += 1
        return f"{name}#{n}"


def is_table_value(value):
    return isinstance(value, Node) or (isinstance(value, CallValue) and
                                       value.node is not None)


def render_any(value):
    if isinstance(value, (Leaf, Opaque, GlobalRef, GlobalEnv)):
        return value.render()
    if isinstance(value, Node):
        if value.is_array():
            return "{" + ", ".join(render_any(v) for _k, v in value.ordered()) + "}"
        return "{" + ", ".join(f"{key_text(k)} = {render_any(v)}"
                               for k, v in value.items) + "}"
    return render_scalar(value)


def render_arg(value):
    """Compact rendering for call arguments and call-derived keys."""
    if isinstance(value, Leaf) and value.kind == "global":
        return str(value.value)
    if isinstance(value, GlobalRef):
        return value.name
    return render_any(value)


def render_call(callee, args):
    return f"{callee}(" + ", ".join(render_arg(a) for a in args) + ")"


# ---------------------------------------------------------------------------
# decode driver
# ---------------------------------------------------------------------------

def load_chunk(blob):
    try:
        return load(blob)
    except Exception as exc:
        raise DecodeError(f"not a Lua 5.1 chunk: {exc}") from exc


def decode_root(root):
    decoder = Decoder(root.protos)
    decoder.run()
    return decoder


def decode_chunk(blob):
    return decode_root(load_chunk(blob))


# ---------------------------------------------------------------------------
# tree walking / rendering
# ---------------------------------------------------------------------------

def child_components(path):
    """Split `a.b[1].c` into [(component, end_offset), ...]."""
    out = []
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == ".":
            i += 1
            continue
        if ch == "[":
            j = path.find("]", i)
            if j < 0:
                raise KeyError(f"unterminated '[' in path {path!r}")
            inner = path[i + 1:j].strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                comp = unescape(inner[1:-1])
            else:
                try:
                    comp = int(inner)
                except ValueError:
                    comp = inner
            out.append((comp, j + 1))
            i = j + 1
            continue
        j = i
        while j < n and path[j] not in ".[":
            j += 1
        out.append((path[i:j], j))
        i = j
    return out


class Resolved:
    __slots__ = ("entry", "path", "value")

    def __init__(self, entry, path, value):
        self.entry = entry
        self.path = path
        self.value = value


def resolve(decoder, path):
    """Resolve a path to (entry, canonical path, value).

    The longest prefix that names an entry wins, so `Interface.Unlockables`
    resolves as an entry while `Interface.Unlockables.All[1].startingIndex`
    resolves inside it.  A global that only exists in the decoded environment
    (a pure namespace container) is accepted as a prefix too.
    """
    comps = child_components(path)
    if not comps:
        raise KeyError("empty path")
    for i in range(len(comps), 0, -1):
        prefix = path[:comps[i - 1][1]].strip()
        index = decoder.entry_index.get(prefix)
        if index is not None:
            entry = decoder.entries[index]
            value = entry.value
            parts = [c for c, _ in comps[:i]]
            for comp, _end in comps[i:]:
                value = walk_one(value, comp)
                parts.append(comp)
            return Resolved(entry, render_path(parts), value)
        if i == 1 and isinstance(comps[0][0], str) and \
                comps[0][0] in decoder.env:
            value = decoder.env[comps[0][0]]
            parts = [comps[0][0]]
            for comp, _end in comps[1:]:
                value = walk_one(value, comp)
                parts.append(comp)
            return Resolved(Entry(comps[0][0], value, "env", -1, -1), 
                            render_path(parts), value)
    raise KeyError(f"no entry matches {path!r}")


def walk_one(value, comp):
    if isinstance(value, CallValue) and value.node is not None:
        value = value.node
    if isinstance(value, Node):
        found = value.get(normalize_key(comp))
        if found is UNSET:
            raise KeyError(f"{key_text(comp)!r} not in table")
        return found
    if isinstance(value, GlobalRef):
        if comp in value.fields:
            return value.fields[comp]
        raise KeyError(f"{comp!r} not in <global {value.name}>")
    raise KeyError(f"{comp!r} descends into {render_any(value)}")


def flatten(value, prefix=None, out=None):
    """Yield (path, leaf-or-opaque) for every scalar in a decoded value."""
    if out is None:
        out = []
    if prefix is None:
        prefix = []
    if isinstance(value, CallValue) and value.node is not None:
        flatten(value.node, prefix, out)
        return out
    if isinstance(value, Node):
        if not value.items:
            out.append((render_path(prefix), value))
        for k, v in value.ordered():
            flatten(v, prefix + [k], out)
    elif isinstance(value, GlobalRef):
        if not value.fields:
            out.append((render_path(prefix), value))
        for k, v in value.fields.items():
            flatten(v, prefix + [k], out)
    else:
        out.append((render_path(prefix), value))
    return out


def flatten_entries(decoder):
    """Map every decoded scalar path -> canonical value, plus ref -> paths."""
    out = {}
    refs = {}
    for entry in decoder.entries:
        for path, value in flatten(entry.value, [entry.name]):
            out[path] = canon(value)
            if isinstance(value, Leaf) and value.ref is not None:
                refs.setdefault((id(value.proto), value.ref), set()).add(path)
    return out, refs


def canon(value):
    if isinstance(value, CallValue) and value.node is not None:
        value = value.node
    if isinstance(value, Leaf):
        if value.kind == "const":
            return ("const", type(value.value).__name__, value.value)
        if value.kind == "nil":
            return ("nil",)
        return (value.kind, value.value)
    if isinstance(value, Opaque):
        return ("opaque", value.kind, value.text)
    if isinstance(value, GlobalRef):
        return ("globalref", value.name, tuple(
            (key_text(k), canon(v)) for k, v in value.fields.items()))
    if isinstance(value, GlobalEnv):
        return ("globalenv",)
    if isinstance(value, Node):
        return ("table", tuple((key_text(k), canon(v)) for k, v in value.items))
    return ("raw", repr(value))


def jsonable(value):
    if isinstance(value, CallValue) and value.node is not None:
        value = value.node
    if isinstance(value, Leaf):
        if value.kind == "global":
            return {"$global": value.value}
        if value.kind == "function":
            return {"$function": value.value}
        if value.kind == "nil":
            return None
        return value.value
    if isinstance(value, Opaque):
        return {"$opaque": value.kind, "text": value.text}
    if isinstance(value, GlobalRef):
        return {"$globalref": value.name,
                "fields": {key_text(k): jsonable(v) for k, v in value.fields.items()}}
    if isinstance(value, GlobalEnv):
        return {"$global": "_G"}
    if isinstance(value, Node):
        if value.is_array():
            return [jsonable(v) for _k, v in value.ordered()]
        return {key_text(k): jsonable(v) for k, v in value.items}
    return value


def render_tree(value, indent=0, out=sys.stdout, depth=0, max_depth=0,
                max_lines=0, state=None):
    pad = "  " * indent
    if state is None:
        state = {"lines": 0}
    if isinstance(value, CallValue) and value.node is not None:
        value = value.node
    if isinstance(value, (Node, GlobalRef)):
        array = isinstance(value, Node) and value.is_array()
        items = value.ordered() if isinstance(value, Node) else list(value.fields.items())
        for k, v in items:
            label = f"[{k}]" if array else key_text(k)
            if max_depth and depth + 1 > max_depth:
                print(f"{pad}{label}: ... (depth limit)", file=out)
                state["lines"] += 1
                continue
            if isinstance(v, Node):
                print(f"{pad}{label}: table({len(v)})", file=out)
                state["lines"] += 1
                render_tree(v, indent + 1, out, depth + 1, max_depth, max_lines, state)
            elif isinstance(v, GlobalRef):
                print(f"{pad}{label}: {v.render()} "
                      f"({len(v.fields)} fields)", file=out)
                state["lines"] += 1
                render_tree(v, indent + 1, out, depth + 1, max_depth, max_lines, state)
            else:
                print(f"{pad}{label} = {render_value(v)}   [{annotation(v)}]",
                      file=out)
                state["lines"] += 1
            if max_lines and state["lines"] >= max_lines:
                print(f"{pad}... (line limit {max_lines} reached; "
                      f"--max-lines 0 for all)", file=out)
                raise StopIteration
    else:
        print(f"{pad}{render_value(value)}   [{annotation(value)}]", file=out)


def render_value(value):
    if isinstance(value, (Leaf, Opaque, GlobalRef, GlobalEnv)):
        return value.render()
    if isinstance(value, Node):
        return f"table({len(value)})"
    return render_scalar(value)


def annotation(value):
    if isinstance(value, (Leaf, Opaque)):
        return value.annotation()
    return type(value).__name__.lower()


# ---------------------------------------------------------------------------
# source resolution
# ---------------------------------------------------------------------------

def game_dir(explicit=None):
    return explicit or config.game_dir()


def read_archive_entry(archive):
    from rcf import Rcf
    with Rcf(archive) as arc:
        return arc.read(arc.find(ARCHIVE_ENTRY))


def resolve_source(source, game=None):
    """Return (blob, description, writable_path_or_None)."""
    if source == "game":
        path = os.path.join(game_dir(game), "boot.rcf")
        return read_archive_entry(path), f"{path}::{ARCHIVE_ENTRY}", None
    if source.lower().endswith(".rcf"):
        return read_archive_entry(source), f"{source}::{ARCHIVE_ENTRY}", None
    with open(source, "rb") as fh:
        blob = fh.read()
    return blob, os.path.abspath(source), os.path.abspath(source)


def refuse_game_write(target):
    game = os.path.realpath(game_dir())
    real = os.path.realpath(target)
    if real == game or real.startswith(game + os.sep):
        raise DecodeError(f"refusing to write {target}: the game install is read-only")


def write_atomic(path, blob):
    tmp = path + ".tmp-datainit"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------

class Edit:
    __slots__ = ("old", "new", "slot", "strategy", "ref")

    def __init__(self, old, new, slot, strategy, ref):
        self.old = old
        self.new = new
        self.slot = slot
        self.strategy = strategy
        self.ref = ref


def coerce_value(text, leaf, force_string=False):
    """Interpret a CLI value the way `set` will store it."""
    if force_string:
        return text.encode("latin-1")
    if leaf.kind == "global":
        return text.encode("latin-1")       # a different global name
    if text in ("true", "false"):
        return text == "true"
    if text == "nil":
        return None
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return unescape(text[1:-1]).encode("latin-1")
    try:
        return f32(float(text))
    except ValueError:
        return text.encode("latin-1")


def unescape(text):
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt in ('"', "\\"):
                out.append(nxt)
                i += 2
                continue
            if nxt.isdigit():
                digits = text[i + 1:i + 4]
                if len(digits) == 3 and digits.isdigit():
                    out.append(chr(int(digits)))
                    i += 4
                    continue
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def set_leaf(proto, leaf, value):
    """Point the leaf at `value`, changing as little bytecode as possible.

    Strategies, in order:

      ``reused``    the value already exists in the constant table, so only the
                    operand changes.
      ``in-place``  the current constant slot is referenced by exactly this one
                    instruction operand, so replacing the slot is
                    indistinguishable from rewiring the operand.
      ``appended``  add a new constant (must land at slot <= 255 for RK).
      ``dead-slot`` RK operand and the table is full: reuse an unreferenced slot
                    below 256.
      ``relocated`` RK operand and the table is full: move a constant that is
                    only referenced through Bx operands to a fresh high slot
                    and put the new value in its low slot.
    """
    ref = leaf.ref
    if ref is None:
        raise DecodeError("this value is computed and has no editable constant")
    if leaf.kind not in ("const", "global"):
        raise DecodeError(f"leaf kind {leaf.kind!r} is not editable")
    operand = ref[1]
    limit = MAX_RK if operand in ("b", "c") else MAX_BX

    # 1. the value is already in the constant table
    for i, c in enumerate(proto.constants):
        if same_const(c, value):
            if i != leaf.slot:
                rewrite_ref(proto, ref, i)
                return Edit(leaf.value, value, i, "reused", ref)
            return Edit(leaf.value, value, i, "no-change", ref)

    # 2. the current slot has exactly one reference: this operand
    refs = constant_refs(proto)
    if leaf.slot is not None and refs.get(leaf.slot) == [tuple(ref)]:
        old = proto.constants[leaf.slot]
        proto.constants[leaf.slot] = value
        return Edit(decode_const(old), value, leaf.slot, "in-place", ref)

    # 3. append a constant
    if len(proto.constants) <= limit:
        proto.constants.append(value)
        new = len(proto.constants) - 1
        rewrite_ref(proto, ref, new)
        return Edit(leaf.value, value, new, "appended", ref)

    # 4./5. no room in the RK window
    if operand in ("b", "c"):
        dead = next((i for i in range(MAX_RK + 1) if i not in refs), None)
        if dead is not None:
            proto.constants[dead] = value
            rewrite_ref(proto, ref, dead)
            return Edit(leaf.value, value, dead, "dead-slot", ref)
        for slot in range(MAX_RK + 1):
            uses = refs.get(slot, [])
            if uses and all(op == "bx" for _pc, op in uses):
                new = len(proto.constants)
                proto.constants.append(proto.constants[slot])
                remap_constants(proto, {slot: new})
                proto.constants[slot] = value
                rewrite_ref(proto, ref, slot)
                return Edit(leaf.value, value, slot, "relocated", ref)
        raise DecodeError(
            f"constant table has {len(proto.constants)} entries and every slot "
            f"0..{MAX_RK} is referenced by an RK operand; {decode_const(value)!r} "
            f"cannot be added without rewriting instructions. Pick a value that "
            f"already exists in the chunk or edit a field stored with LOADK.")
    raise DecodeError(f"constant table full ({len(proto.constants)} entries)")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def entry_kind(value):
    if isinstance(value, CallValue) and value.node is not None:
        return "table"
    if isinstance(value, Node):
        return "table"
    if isinstance(value, GlobalRef):
        return "global"
    if isinstance(value, Leaf):
        if value.kind == "function":
            return "function"
        if value.kind == "global":
            return "global"
        return "scalar"
    if isinstance(value, Opaque):
        return "opaque"
    if isinstance(value, GlobalEnv):
        return "global"
    return "scalar"


def describe_entry(entry):
    value = entry.value
    kind = entry_kind(value)
    if isinstance(value, CallValue) and value.node is not None:
        value = value.node
    if isinstance(value, Node):
        if value.items:
            extra = f"{len(value)} " + ("array" if value.is_array() else "children")
        else:
            extra = "empty"
    elif isinstance(value, GlobalRef):
        extra = f"{len(value.fields)} fields"
    else:
        extra = render_value(value)
    return kind, extra


def cmd_list(decoder, args):
    rows = []
    for entry in decoder.entries:
        kind, extra = describe_entry(entry)
        if args.tables and kind != "table":
            continue
        if args.kind and kind != args.kind:
            continue
        if args.filter and args.filter.lower() not in entry.name.lower():
            continue
        rows.append((entry, kind, extra))
    if args.json:
        print(json.dumps([
            {"name": e.name, "kind": k, "summary": x, "source": e.source,
             "chunk": e.chunk, "pc": e.pc, "note": e.note,
             "conditional": e.conditional}
            for e, k, x in rows], indent=1))
        return 0
    for entry, kind, extra in rows:
        note = f"  <- {entry.note}" if entry.note else ""
        cond = " cond" if entry.conditional else ""
        print(f"{entry.name:52} {kind:8} {extra:24} "
              f"chunk[{entry.chunk:>3}] {entry.source}{cond}{note}")
    tables = sum(1 for e in decoder.entries if entry_kind(e.value) == "table")
    print(f"-- {len(rows)}/{len(decoder.entries)} entries shown; "
          f"{tables} tables, {len(decoder.entries) - tables} non-table; "
          f"{decoder.stats['chunks']} chunk protos")
    return 0


def cmd_tree(decoder, args):
    resolved = resolve(decoder, args.name)
    value = resolved.value
    print(f"{resolved.path}  [{entry_kind(value)}] "
          f"(chunk[{resolved.entry.chunk}] pc {resolved.entry.pc})")
    try:
        render_tree(value, 0, sys.stdout, 0, args.depth, args.max_lines)
    except StopIteration:
        pass
    return 0


def cmd_get(decoder, args):
    resolved = resolve(decoder, args.path)
    value = resolved.value
    print(f"{resolved.path} = {render_value(value)}   [{annotation(value)}]")
    if isinstance(value, (Node, GlobalRef)) and args.children:
        try:
            render_tree(value, 1, sys.stdout, 0, args.depth, args.max_lines)
        except StopIteration:
            pass
    return 0


def cmd_paths(decoder, args):
    resolved = resolve(decoder, args.name)
    for path, leaf in flatten(resolved.value, [resolved.entry.name]):
        if isinstance(leaf, Leaf):
            note = f"const {leaf.slot}" if leaf.slot is not None else leaf.kind
            if leaf.ref:
                note += f", ref {leaf.ref[1]}@{leaf.ref[0]}"
            if not leaf.editable:
                note += ", not editable"
        elif isinstance(leaf, Opaque):
            note = f"{leaf.kind}, not editable"
        elif isinstance(leaf, Node):
            note = "empty table"
        else:
            note = type(leaf).__name__.lower()
        print(f"{path:64} {render_value(leaf):28} [{note}]")
    return 0


def cmd_export(decoder, args):
    if args.name:
        resolved = resolve(decoder, args.name)
        payload = {"name": resolved.path, "kind": entry_kind(resolved.value),
                   "value": jsonable(resolved.value)}
        text = json.dumps(payload, indent=1) + "\n"
        if args.out == "-":
            sys.stdout.write(text)
            return 0
        path = args.out or os.path.join(REPO, "tmp", "datainit.json")
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        print(f"wrote {path} ({resolved.path})")
        return 0
    payload = {
        "entries": [
            {"name": e.name, "kind": entry_kind(e.value),
             "value": jsonable(e.value), "source": e.source,
             "chunk": e.chunk, "pc": e.pc, "note": e.note,
             "conditional": e.conditional}
            for e in decoder.entries
        ],
        "stats": decoder.stats,
        "statement_calls": [
            {"chunk": c["chunk"], "pc": c["pc"], "callee": c["callee"],
             "args": [jsonable(a) for a in c["args"]], "results": c["results"]}
            for c in decoder.calls if c["results"] == 0
        ][:args.max_calls],
    }
    text = json.dumps(payload, indent=1) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
        return 0
    path = args.out or os.path.join(REPO, "tmp", "datainit.json")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    print(f"wrote {path} ({len(decoder.entries)} entries, "
          f"{len(payload['statement_calls'])} statement calls, {len(text)} bytes)")
    return 0


def cmd_verify(decoder, args, blob, source_desc):
    ok = True
    again = dump(load(blob))
    print(f"source                 {source_desc}")
    print(f"sha256                 {hashlib.sha256(blob).hexdigest()}")
    print(f"size                   {len(blob)} bytes")
    print(f"round-trip identical   {again == blob}")
    if again != blob:
        ok = False
        for i in range(min(len(again), len(blob))):
            if again[i] != blob[i]:
                print(f"  first difference at 0x{i:x}: "
                      f"{blob[i]:02x} vs {again[i]:02x}")
                break
    root = load(blob)
    print(f"protos                 {len(list(walk_protos(root)))}")
    print(f"chunk protos           {len(root.protos)}")
    print(f"instructions           {sum(len(p.code) for p in walk_protos(root))}")
    tables = sum(1 for e in decoder.entries if entry_kind(e.value) == "table")
    print(f"entries                {len(decoder.entries)} ({tables} tables, "
          f"{len(decoder.entries) - tables} non-table)")
    for key in ("global_assignments", "field_assignments", "statement_calls",
                "chunks_imprecise", "chunks_truncated", "lost_assignments",
                "unsupported_ops", "dead_moves", "unset_reads", "missing_keys"):
        print(f"{key:22} {decoder.stats[key]}")

    editable = 0
    broken = 0
    for entry in decoder.entries:
        for path, leaf in flatten(entry.value, [entry.name]):
            if not isinstance(leaf, Leaf) or not leaf.editable:
                continue
            editable += 1
            try:
                check_operand(leaf)
            except DecodeError as exc:
                broken += 1
                if broken <= 5:
                    print(f"  {path}: {exc}")
    print(f"editable leaves        {editable}")
    print(f"inconsistent operands  {broken}")
    if broken:
        ok = False

    bad_paths = 0
    count = 0
    for entry in decoder.entries:
        for path, leaf in flatten(entry.value, [entry.name]):
            count += 1
            try:
                found = resolve(decoder, path)
            except KeyError as exc:
                bad_paths += 1
                if bad_paths <= 5:
                    print(f"  unresolvable path {path!r}: {exc}")
                continue
            if canon(found.value) != canon(leaf):
                bad_paths += 1
                if bad_paths <= 5:
                    print(f"  path {path!r} resolves to a different value")
    print(f"paths checked          {count}")
    print(f"unresolvable paths     {bad_paths}")
    if bad_paths:
        ok = False
    if decoder.stats["chunks_truncated"]:
        print("note: some chunk protos are not straight-line code (self-invoked "
              "functions); entries decoded before the first backward jump are "
              "still valid and are flagged in `list` output as truncated")
    print(f"verify                 {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def check_operand(leaf):
    """Re-read the instruction operand and confirm it points at leaf.slot."""
    proto = leaf.proto
    if proto is None:
        raise DecodeError("leaf has no owning proto")
    pc, operand = leaf.ref
    word = proto.code[pc]
    op, _a, b, c, bx, _sbx = ins_decode(word)
    if operand == "bx":
        if op not in BX_CONST_OPS:
            raise DecodeError(f"pc {pc} is {OPCODES[op]}, not a Bx constant operand")
        slot = bx
    elif operand == "b":
        if op not in RK_B_OPS or b < BITRK:
            raise DecodeError(f"pc {pc} has no RK B constant")
        slot = b - BITRK
    elif operand == "c":
        if op not in RK_C_OPS or c < BITRK:
            raise DecodeError(f"pc {pc} has no RK C constant")
        slot = c - BITRK
    else:
        raise DecodeError(f"unknown operand {operand!r}")
    if slot != leaf.slot:
        raise DecodeError(f"pc {pc} operand {operand} points at {slot}, "
                          f"leaf says {leaf.slot}")


def canon_render(c):
    if c == "<absent>":
        return "<absent>"
    kind = c[0]
    if kind == "const":
        return render_scalar(c[2])
    if kind == "opaque":
        return c[2]
    if kind == "global":
        return f"<global {c[1]}>"
    return str(c)


def proto_paths(root):
    """[(path, proto)] for the whole proto tree, root first."""
    out = []

    def rec(proto, path):
        out.append((path, proto))
        for i, child in enumerate(proto.protos):
            rec(child, f"{path}.{i}" if path else str(i))

    rec(root, "")
    return out


def proto_snapshot(root):
    """Copy the mutable parts of a proto tree, keyed by proto path."""
    snap = {}
    for path, proto in proto_paths(root):
        snap[path] = {
            "code": list(proto.code),
            "constants": list(proto.constants),
            "max_stack": proto.max_stack,
            "num_params": proto.num_params,
            "is_vararg": proto.is_vararg,
            "children": len(proto.protos),
        }
    return snap


def _const_operands(op, b, c, bx):
    """Yield (operand, is_constant, index) for the constant-capable fields."""
    if op in BX_CONST_OPS:
        yield "bx", True, bx
    if op in RK_B_OPS:
        yield "b", b >= BITRK, (b - BITRK) if b >= BITRK else b
    if op in RK_C_OPS:
        yield "c", c >= BITRK, (c - BITRK) if c >= BITRK else c


def verify_bytecode_change(snapshot, new_root, target_path, target_ref,
                           edit_slot, new_value):
    """Prove that the edit changed only constant operands.

    Returns (word_changes, const_changes, appended) and raises DecodeError when
    the change is wider than a constant re-wiring.
    """
    new_snap = proto_snapshot(new_root)
    if set(snapshot) != set(new_snap):
        raise DecodeError("proto tree changed shape")
    word_changes = []
    const_changes = []
    appended = []
    for path in snapshot:
        old, new = snapshot[path], new_snap[path]
        for key in ("max_stack", "num_params", "is_vararg", "children"):
            if old[key] != new[key]:
                raise DecodeError(f"proto {path or '<root>'} changed {key}: "
                                  f"{old[key]} -> {new[key]}")
        if len(old["code"]) != len(new["code"]):
            raise DecodeError(f"proto {path or '<root>'} changed instruction "
                              f"count {len(old['code'])} -> {len(new['code'])}")
        for pc, (w_old, w_new) in enumerate(zip(old["code"], new["code"])):
            if w_old == w_new:
                continue
            op_old, a_old, b_old, c_old, bx_old, _ = ins_decode(w_old)
            op_new, a_new, b_new, c_new, bx_new, _ = ins_decode(w_new)
            if (op_old, a_old) != (op_new, a_new):
                raise DecodeError(f"proto {path} pc {pc}: opcode/register A "
                                  f"changed")
            old_ops = dict((n, (is_c, idx)) for n, is_c, idx in
                           _const_operands(op_old, b_old, c_old, bx_old))
            new_ops = dict((n, (is_c, idx)) for n, is_c, idx in
                           _const_operands(op_new, b_new, c_new, bx_new))
            for operand in set(old_ops) | set(new_ops):
                if old_ops.get(operand) == new_ops.get(operand):
                    continue
                (old_c, old_idx), (new_c, new_idx) = old_ops[operand], new_ops[operand]
                if not old_c or not new_c:
                    raise DecodeError(
                        f"proto {path} pc {pc} operand {operand}: changed "
                        f"between a register and a constant")
                word_changes.append({
                    "proto": path, "pc": pc, "operand": operand,
                    "old": old_idx, "new": new_idx,
                })
        if len(new["constants"]) < len(old["constants"]) or \
                len(new["constants"]) > len(old["constants"]) + 1:
            raise DecodeError(f"proto {path or '<root>'} constant count "
                              f"{len(old['constants'])} -> {len(new['constants'])}")
        for i in range(len(old["constants"])):
            if not same_const(old["constants"][i], new["constants"][i]):
                const_changes.append({
                    "proto": path, "slot": i,
                    "old": old["constants"][i], "new": new["constants"][i],
                })
        if len(new["constants"]) > len(old["constants"]):
            appended.append({
                "proto": path, "slot": len(old["constants"]),
                "value": new["constants"][-1],
            })
    if len(appended) > 1:
        raise DecodeError(f"{len(appended)} constants appended, expected at most 1")
    if len(const_changes) > 1:
        raise DecodeError(f"{len(const_changes)} existing constants changed, "
                          f"expected at most 1")
    # every changed word must either point at the edited slot or only renumber
    # a constant that moved to its new index
    for change in word_changes:
        proto_path = change["proto"]
        old_new = snapshot[proto_path]["constants"]
        new_consts = new_snap[proto_path]["constants"]
        if proto_path == target_path and change["pc"] == target_ref[0] and \
                change["operand"] == target_ref[1]:
            if change["new"] != edit_slot:
                raise DecodeError("the edited operand does not point at the new "
                                  "constant")
            continue
        old_val = old_new[change["old"]]
        new_val = new_consts[change["new"]]
        if not same_const(old_val, new_val):
            raise DecodeError(
                f"proto {proto_path} pc {change['pc']}: operand {change['operand']} "
                f"was rewired from a different value ({old_val!r} -> {new_val!r})")
    # relocated constants: the value that left a slot must be the appended one
    if const_changes and appended:
        moved = const_changes[0]
        if not same_const(moved["old"], appended[0]["value"]):
            raise DecodeError("a constant slot changed without being moved")

    # definitive check: the target operand now points at the new constant
    target_proto = next(p for path, p in proto_paths(new_root)
                        if path == target_path)
    word = target_proto.code[target_ref[0]]
    op, _a, b, c, bx, _sbx = ins_decode(word)
    fields = dict((n, (is_c, idx)) for n, is_c, idx in
                  _const_operands(op, b, c, bx))
    if target_ref[1] not in fields:
        raise DecodeError(f"the edited instruction is no longer a "
                          f"{target_ref[1]} constant operand")
    is_const, slot = fields[target_ref[1]]
    if not is_const or slot != edit_slot:
        raise DecodeError(f"the edited operand points at "
                          f"{slot if is_const else 'a register'}, "
                          f"expected const {edit_slot}")
    if not same_const(target_proto.constants[edit_slot], new_value):
        raise DecodeError("the new constant does not hold the requested value")
    return word_changes, const_changes, appended


def cmd_set(decoder, args, blob, writable, root):
    resolved = resolve(decoder, args.path)
    leaf = resolved.value
    if not isinstance(leaf, Leaf):
        raise DecodeError(f"{resolved.path} is a {entry_kind(leaf)}, not a "
                          f"scalar leaf")
    if not leaf.editable:
        raise DecodeError(f"{resolved.path} is {leaf.kind} and has no editable "
                          f"constant")
    proto = leaf.proto
    if proto is None:
        raise DecodeError("cannot locate the instruction that stores this value")
    check_operand(leaf)
    value = coerce_value(args.value, leaf, force_string=args.string)
    if value is None and leaf.kind == "global":
        raise DecodeError("a global reference cannot become nil")

    target_path = next(path for path, p in proto_paths(root) if p is proto)
    snapshot = proto_snapshot(root)
    before, refs_before = flatten_entries(decoder)
    before_bytes = len(blob)
    edit = set_leaf(proto, leaf, value)
    if edit.strategy == "no-change":
        print(f"{resolved.path}: already {edit.new!r}, nothing to do")
        return 0
    new_blob = dump(root)
    if dump(load_chunk(new_blob)) != new_blob:
        raise DecodeError("the re-serialised chunk does not round-trip")
    new_root = load_chunk(new_blob)

    try:
        words, consts, appended = verify_bytecode_change(
            snapshot, new_root, target_path, leaf.ref, edit.slot, value)
    except DecodeError as exc:
        print(f"verification FAILED: {exc}")
        return 1

    new_decoder = decode_root(new_root)
    after, _refs_after = flatten_entries(new_decoder)
    expected_paths = refs_before.get((id(leaf.proto), leaf.ref),
                                     {resolved.path})
    diffs = {}
    for path in set(before) | set(after):
        a = before.get(path, "<absent>")
        b = after.get(path, "<absent>")
        if a != b:
            diffs[path] = (a, b)
    hard = {p: d for p, d in diffs.items() if not is_derived_change(d)}
    derived = {p: d for p, d in diffs.items() if is_derived_change(d)}
    if value is None:
        hard.pop(resolved.path, None)       # nil removes the field at runtime

    print(f"{resolved.path}")
    print(f"  old       {render_value(leaf)}   "
          f"[{leaf.kind}, const {leaf.slot}, ref {leaf.ref[1]}@{leaf.ref[0]}, "
          f"chunk {resolved.entry.chunk}, proto {target_path}]")
    print(f"  new       {render_scalar(decode_const(value))}   "
          f"[const {edit.slot}, strategy {edit.strategy}]")
    print(f"  bytecode  {len(words)} operand(s) rewired, "
          f"{len(consts)} constant slot(s) changed, {len(appended)} appended")
    for w in words[:12]:
        print(f"    proto {w['proto']} pc {w['pc']} {w['operand']}: "
              f"const {w['old']} -> {w['new']}")
    if len(words) > 12:
        print(f"    ... and {len(words) - 12} more rewired operands")
    for c in consts:
        print(f"    proto {c['proto']} slot {c['slot']}: "
              f"{decode_const(c['old'])!r} -> {decode_const(c['new'])!r}")
    for a in appended:
        print(f"    proto {a['proto']} const {a['slot']}: "
              f"appended {decode_const(a['value'])!r}")
    print(f"  tree diff {len(diffs)} path(s): {len(hard)} direct, "
          f"{len(derived)} computed")

    # Several decoded paths can share one instruction operand: when the chunk
    # copies a value out of one table into another, both fields decode to the
    # same Leaf.  Rewiring that operand changes all of them, which is the real
    # load-time behaviour, so they are expected - and must all read back.
    want = canon(Leaf(edit.slot, "const", decode_const(value), None))
    problems = []
    for path, (old, new) in sorted(hard.items()):
        if path not in expected_paths:
            problems.append(f"unexpected value change at {path}: "
                            f"{canon_render(old)} -> {canon_render(new)}")
            continue
        if value is not None and new != want:
            problems.append(f"{path} re-parsed as {canon_render(new)}, expected "
                            f"{canon_render(want)}")
    removed = set(before) - set(after)
    added = set(after) - set(before)
    if value is None:
        if removed != expected_paths:
            problems.append(f"removed paths {sorted(removed)}, expected "
                            f"{sorted(expected_paths)}")
        if added:
            problems.append(f"unexpected added paths: {sorted(added)}")
    else:
        if removed:
            problems.append(f"unexpected removed paths: {sorted(removed)}")
        if added:
            problems.append(f"unexpected added paths: {sorted(added)}")
        missing = expected_paths - set(hard)
        if missing:
            problems.append(f"paths sharing the edited operand did not change: "
                            f"{sorted(missing)}")
    if problems:
        print("verification FAILED:")
        for problem in problems:
            print("  " + problem)
        return 1
    if len(expected_paths) > 1:
        fate = "are removed" if value is None else "all read back"
        print(f"verification ok: bytecode changed only constant operands; "
              f"{len(expected_paths)} paths share this operand and {fate}")
        for path in sorted(expected_paths):
            print(f"  shared: {path}")
    else:
        print("verification ok: bytecode changed only constant operands, exactly "
              "the edited path changed")
    if derived:
        for path, (old, new) in sorted(derived.items()):
            print(f"  note: computed value {path}: {canon_render(old)} -> "
                  f"{canon_render(new)} (derived from the edit)")
    if args.dry_run:
        print("dry run, nothing written")
        return 0
    target = args.out or writable
    if not target:
        raise DecodeError("source is an archive; pass --out to write a chunk")
    refuse_game_write(target)
    write_atomic(target, new_blob)
    print(f"wrote {target} ({before_bytes} -> {len(new_blob)} bytes)")
    return 0


def is_derived_change(pair):
    old, new = pair
    return old != "<absent>" and new != "<absent>" and \
        old[0] == "opaque" and new[0] == "opaque"


def cmd_extract(args, blob, source_desc):
    refuse_game_write(args.out)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "wb") as fh:
        fh.write(blob)
    print(f"wrote {args.out} ({len(blob)} bytes, sha256 "
          f"{hashlib.sha256(blob).hexdigest()})")
    print(f"source {source_desc}")
    return 0


def cmd_stats(decoder, args):
    for k, v in decoder.stats.items():
        print(f"{k:22} {v}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    config.require_game_dir()
    ap = argparse.ArgumentParser(
        description="Decode/inspect/edit datainit.lua (Lua 5.1 bytecode)")
    ap.add_argument("source",
                    help="a .lua bytecode chunk, an .rcf archive, or 'game' for "
                         "the shipped boot.rcf")
    ap.add_argument("--game", default=None, help="game install directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list top-level entries")
    p.add_argument("--filter", default=None)
    p.add_argument("--kind", default=None,
                   help="table | scalar | function | opaque | global")
    p.add_argument("--tables", action="store_true", help="only table entries")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("tree", help="symbolic dump of one entry")
    p.add_argument("name")
    p.add_argument("--depth", type=int, default=0, help="0 = unlimited")
    p.add_argument("--max-lines", dest="max_lines", type=int, default=2000,
                   help="0 = unlimited")

    p = sub.add_parser("get", help="print one path")
    p.add_argument("path")
    p.add_argument("--children", action="store_true", help="also dump table children")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--max-lines", dest="max_lines", type=int, default=200)

    p = sub.add_parser("paths", help="list every scalar path under an entry")
    p.add_argument("name")

    p = sub.add_parser("set", help="edit one scalar in place")
    p.add_argument("path")
    p.add_argument("value")
    p.add_argument("--out", default=None, help="write the chunk here instead")
    p.add_argument("--dry-run", dest="dry_run", action="store_true")
    p.add_argument("--string", action="store_true",
                   help="force the value to be a string")

    p = sub.add_parser("export", help="write the decoded tree as JSON")
    p.add_argument("--out", default=None, help="path, or - for stdout")
    p.add_argument("--name", default=None, help="export only one entry")
    p.add_argument("--max-calls", dest="max_calls", type=int, default=200)

    p = sub.add_parser("extract", help="write the resolved chunk to a file")
    p.add_argument("out")

    sub.add_parser("verify", help="round-trip + decode self-checks")
    sub.add_parser("stats", help="decoder counters")

    a = ap.parse_args(argv)
    try:
        blob, source_desc, writable = resolve_source(a.source, a.game)
    except OSError as exc:
        print(f"error: cannot read {a.source}: {exc}", file=sys.stderr)
        return 2
    except KeyError:
        print(f"error: {ARCHIVE_ENTRY} not present in {a.source}", file=sys.stderr)
        return 2
    if a.cmd == "extract":
        return cmd_extract(a, blob, source_desc)
    try:
        root = load_chunk(blob)
        decoder = decode_root(root)
        if a.cmd == "list":
            return cmd_list(decoder, a)
        if a.cmd == "tree":
            return cmd_tree(decoder, a)
        if a.cmd == "get":
            return cmd_get(decoder, a)
        if a.cmd == "paths":
            return cmd_paths(decoder, a)
        if a.cmd == "export":
            return cmd_export(decoder, a)
        if a.cmd == "verify":
            return cmd_verify(decoder, a, blob, source_desc)
        if a.cmd == "stats":
            return cmd_stats(decoder, a)
        if a.cmd == "set":
            return cmd_set(decoder, a, blob, writable, root)
        raise SystemExit(f"unknown command {a.cmd}")
    except (DecodeError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
