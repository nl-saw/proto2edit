#!/usr/bin/env python3
"""Lua 5.1 bytecode undumper, lister and string extractor.

Prototype 2 ships its scripting layer as precompiled Lua 5.1 (`\\x1bLua\\x51`)
inside boot.rcf, stored under the names scripts/{init,gameinit,black_net,
missiondefinit,datainit}.lua.  This module reimplements `lundump.c` so the
chunks can be inspected and re-listed like `luac -l` without a Lua toolchain.
"""

import argparse
import struct
import sys

# Lua 5.1 instruction opcodes in opcode order.
OPCODES = [
    "MOVE", "LOADK", "LOADBOOL", "LOADNIL", "GETUPVAL", "GETGLOBAL", "GETTABLE",
    "SETGLOBAL", "SETUPVAL", "SETTABLE", "NEWTABLE", "SELF", "ADD", "SUB", "MUL",
    "DIV", "MOD", "POW", "UNM", "NOT", "LEN", "CONCAT", "JMP", "EQ", "LT", "LE",
    "TEST", "TESTSET", "CALL", "TAILCALL", "RETURN", "FORLOOP", "FORPREP",
    "TFORLOOP", "SETLIST", "CLOSE", "CLOSURE", "VARARG",
]
OPMAP = {name: i for i, name in enumerate(OPCODES)}

SIGNATURE = b"\x1bLua"
NUM_SIZE = 8
NUM_INTEGRAL = 0


class LuaError(Exception):
    pass


class Reader:
    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos

    def u8(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u32(self):
        (v,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return v

    def i32(self):
        (v,) = struct.unpack_from("<i", self.data, self.pos)
        self.pos += 4
        return v

    def u64(self):
        (v,) = struct.unpack_from("<Q", self.data, self.pos)
        self.pos += 8
        return v

    def number(self, size, integral):
        if integral:
            fmt = "<i" if size == 4 else "<q"
        else:
            fmt = "<f" if size == 4 else "<d"
        (v,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return v

    def string(self):
        n = self.u32()
        if n == 0:
            return None
        s = self.data[self.pos:self.pos + n]
        self.pos += n
        return s[:-1]  # trailing NUL

    def bytes(self, n):
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v


class Proto:
    def __init__(self):
        self.source = None
        self.line_defined = 0
        self.last_line = 0
        self.nups = 0
        self.num_params = 0
        self.is_vararg = 0
        self.max_stack = 0
        self.code = []
        self.constants = []
        self.protos = []
        self.line_info = []
        self.locals = []
        self.upvalue_names = []
        self.parent = None
        self.index = 0

    @property
    def name(self):
        if self.parent is None:
            return "main chunk"
        line = self.line_defined
        return f"<{self.parent.name} @line {line}>"

    def const_strings(self):
        out = []
        for c in self.constants:
            if isinstance(c, bytes):
                out.append(c.decode("latin-1"))
        return out


def load(data):
    r = Reader(data)
    sig = r.bytes(4)
    if sig != SIGNATURE:
        raise LuaError(f"bad signature {sig!r}")
    version = r.u8()
    fmt = r.u8()
    if version != 0x51:
        raise LuaError(f"unsupported Lua version 0x{version:02x}")
    if fmt != 0:
        raise LuaError(f"unsupported chunk format {fmt}")
    endian = r.u8()
    if endian != 1:
        raise LuaError("only little-endian chunks are supported")
    ints, size_t, instr, num, integral = r.u8(), r.u8(), r.u8(), r.u8(), r.u8()
    if (ints, size_t, instr) != (4, 4, 4) or num not in (4, 8):
        raise LuaError(f"unexpected type sizes {ints}/{size_t}/{instr}/{num}")
    global NUM_SIZE, NUM_INTEGRAL
    NUM_SIZE, NUM_INTEGRAL = num, integral
    return load_function(r, None)


def load_function(r, parent):
    f = Proto()
    f.parent = parent
    f.source = r.string()
    f.line_defined = r.i32()
    f.last_line = r.i32()
    f.nups = r.u8()
    f.num_params = r.u8()
    f.is_vararg = r.u8()
    f.max_stack = r.u8()

    n = r.u32()
    f.code = [r.u32() for _ in range(n)]

    n = r.u32()
    for _ in range(n):
        t = r.u8()
        if t == 0:      # nil
            f.constants.append(None)
        elif t == 1:    # boolean
            f.constants.append(bool(r.u8()))
        elif t == 3:    # number
            f.constants.append(r.number(NUM_SIZE, NUM_INTEGRAL))
        elif t == 4:    # string
            f.constants.append(r.string())
        else:
            raise LuaError(f"bad constant tag {t} at 0x{r.pos:x}")

    n = r.u32()
    for _ in range(n):
        f.protos.append(load_function(r, f))

    n = r.u32()
    f.line_info = [r.i32() for _ in range(n)]
    n = r.u32()
    for _ in range(n):
        f.locals.append((r.string(), r.i32(), r.i32()))
    n = r.u32()
    f.upvalue_names = [r.string() for _ in range(n)]
    return f


# ---------------------------------------------------------------------------
# Serializer (the inverse of `load`).  Because the whole chunk is re-emitted,
# a string constant may change length; only the instruction stream's constant
# indices have to keep pointing at the intended slots.
# ---------------------------------------------------------------------------

def _w_string(out, s):
    if s is None:
        out += struct.pack("<I", 0)
        return
    out += struct.pack("<I", len(s) + 1)
    out += s + b"\0"


def _w_number(out, value):
    if NUM_INTEGRAL:
        out += struct.pack("<i" if NUM_SIZE == 4 else "<q", int(value))
    else:
        out += struct.pack("<f" if NUM_SIZE == 4 else "<d", float(value))


def _w_function(out, f):
    _w_string(out, f.source)
    out += struct.pack("<ii", f.line_defined, f.last_line)
    out += bytes([f.nups, f.num_params, f.is_vararg, f.max_stack])

    out += struct.pack("<I", len(f.code))
    for word in f.code:
        out += struct.pack("<I", word)

    out += struct.pack("<I", len(f.constants))
    for c in f.constants:
        if c is None:
            out += b"\x00"
        elif isinstance(c, bool):
            out += b"\x01" + (b"\x01" if c else b"\x00")
        elif isinstance(c, (int, float)):
            out += b"\x03"
            _w_number(out, c)
        elif isinstance(c, bytes):
            out += b"\x04"
            _w_string(out, c)
        else:
            raise LuaError(f"cannot serialize constant {c!r}")

    out += struct.pack("<I", len(f.protos))
    for child in f.protos:
        _w_function(out, child)

    out += struct.pack("<I", len(f.line_info))
    for line in f.line_info:
        out += struct.pack("<i", line)

    out += struct.pack("<I", len(f.locals))
    for name, start, end in f.locals:
        _w_string(out, name)
        out += struct.pack("<ii", start, end)

    out += struct.pack("<I", len(f.upvalue_names))
    for name in f.upvalue_names:
        _w_string(out, name)


def dump(root):
    """Serialize a Proto tree back to a Lua 5.1 binary chunk."""
    out = bytearray()
    out += SIGNATURE
    out += bytes([0x51, 0, 1, 4, 4, 4, NUM_SIZE, NUM_INTEGRAL])
    _w_function(out, root)
    return bytes(out)


def resolve_path(root, path):
    """Resolve a dotted proto path such as ``"0.3.1"`` (empty means the root)."""
    node = root
    if path:
        for part in path.split("."):
            i = int(part)
            if i < 0 or i >= len(node.protos):
                raise LuaError(f"proto path {path!r} out of range at {part!r}")
            node = node.protos[i]
    return node


def constant_kind(value):
    if value is None:
        return "nil"
    if value is True or value is False:
        return "boolean"
    if isinstance(value, bytes):
        return "string"
    return "number"


def patch_constant(root, index, new_value, path=""):
    """Replace one constant at a proto path (default: the main chunk).

    Strings, numbers, booleans and nil can all be replaced; the caller is responsible for keeping the type
    compatible with how the code uses the value.
    """
    target = resolve_path(root, path)
    if index < 0 or index >= len(target.constants):
        raise LuaError(f"constant index {index} out of range (0..{len(target.constants)-1})")
    old = target.constants[index]
    kind = constant_kind(old)
    if kind == "string":
        new_value = new_value.encode("latin-1") if isinstance(new_value, str) else new_value
        if not isinstance(new_value, bytes):
            raise LuaError("a string constant must be replaced by text")
    elif kind == "number":
        new_value = float(new_value)
    elif kind == "boolean":
        if not isinstance(new_value, bool):
            raise LuaError("a boolean constant must be replaced by true or false")
    else:
        raise LuaError("nil constants cannot be replaced")
    target.constants[index] = new_value
    return old


def walk(f):
    yield f
    for c in f.protos:
        yield from walk(c)


def decode(i):
    op = i & 0x3F
    a = (i >> 6) & 0xFF
    c = (i >> 14) & 0x1FF
    b = (i >> 23) & 0x1FF
    bx = (i >> 14) & 0x3FFFF
    sbx = bx - 131071
    return op, a, b, c, bx, sbx


def signature(proto):
    """A comparable fingerprint of a decompiled chunk: instructions, constants, shape.

    Two chunks with the same signature execute the same way; this is the check used to decide whether a
    LuaDec output is equivalent to the bytecode it replaced. Debug info (line numbers, local names) is
    deliberately excluded - a recompilation will not reproduce it, and it does not affect behaviour.
    """
    consts = []
    for c in proto.constants:
        if isinstance(c, bytes):
            consts.append(("s", c))
        elif isinstance(c, float):
            consts.append(("n", struct.pack("<f", c)))
        elif c is None:
            consts.append(("nil", b""))
        elif isinstance(c, bool):
            consts.append(("b", bytes([int(c)])))
        else:
            consts.append(("i", int(c)))
    return (tuple(int(i) for i in proto.code), tuple(consts),
            int(getattr(proto, "numparams", 0)), int(getattr(proto, "is_vararg", 0)),
            tuple(signature(p) for p in proto.protos))


def listing(root, out=sys.stdout, show_code=True):
    """Emit a luac -l style listing."""
    def const_repr(c):
        if isinstance(c, bytes):
            return f'"{c.decode("latin-1")}"'
        if isinstance(c, float):
            return repr(c)
        return str(c)

    def dump(f, depth=0):
        ind = "  " * depth
        params = f.num_params
        vararg = "..." if f.is_vararg else ""
        comma = "," if f.is_vararg and f.num_params else ""
        print(f"{ind}function {f.name} ({len(f.code)} instructions, "
              f"params={params}{comma}{vararg}, upvalues={f.nups}, "
              f"locals={len(f.locals)}, constants={len(f.constants)}, "
              f"protos={len(f.protos)}) @line {f.line_defined}-{f.last_line}", file=out)
        if f.constants:
            print(f"{ind}  constants: " + ", ".join(f"[{i}]={const_repr(c)}" for i, c in enumerate(f.constants)), file=out)
        if f.upvalue_names:
            print(f"{ind}  upvalues: " + ", ".join(
                (u.decode("latin-1") if isinstance(u, bytes) else str(u)) for u in f.upvalue_names), file=out)
        if show_code:
            for pc, i in enumerate(f.code):
                op, a, b, c, bx, sbx = decode(i)
                name = OPCODES[op] if op < len(OPCODES) else f"OP{op}"
                extra = ""
                if name == "LOADK" and bx < len(f.constants):
                    extra = f"  ; {const_repr(f.constants[bx])}"
                elif name == "GETGLOBAL" and bx < len(f.constants):
                    extra = f"  ; {const_repr(f.constants[bx])}"
                elif name in ("SETGLOBAL",) and bx < len(f.constants):
                    extra = f"  ; {const_repr(f.constants[bx])}"
                line = f.line_info[pc] if pc < len(f.line_info) else 0
                print(f"{ind}  [{pc:4}] line {line:5}  {name:10} a={a:<4} b={b:<4} c={c:<4} bx={bx:<6} sbx={sbx:<6}{extra}", file=out)
        for k in f.protos:
            dump(k, depth + 1)

    dump(root)


def stats(root):
    protos = list(walk(root))
    strings = []
    for f in protos:
        strings.extend(f.const_strings())
    return {
        "protos": len(protos),
        "instructions": sum(len(f.code) for f in protos),
        "string_constants": len(strings),
        "unique_strings": len(set(strings)),
        "max_depth": max(_depth(f) for f in protos),
    }


def _depth(f):
    d = 0
    while f.parent is not None:
        d += 1
        f = f.parent
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lua 5.1 bytecode undumper")
    ap.add_argument("path")
    ap.add_argument("cmd", nargs="?", default="stats",
                    choices=["stats", "list", "strings", "globals", "tree", "roundtrip", "dump", "patch"])
    ap.add_argument("--out", default=None, help="output path for dump/patch")
    ap.add_argument("--index", type=int, default=None, help="constant index for patch")
    ap.add_argument("--value", default=None, help="replacement string for patch")
    ap.add_argument("--proto-path", dest="proto_path", default="", help="dotted proto path for patch, e.g. 0.3.1")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    data = open(a.path, "rb").read()
    root = load(data)
    protos = list(walk(root))
    if a.cmd == "stats":
        for k, v in stats(root).items():
            print(f"{k:20} {v}")
    elif a.cmd == "list":
        listing(root)
    elif a.cmd == "strings":
        seen = set()
        for f in protos:
            for s in f.const_strings():
                if s not in seen:
                    seen.add(s)
                    print(s)
    elif a.cmd == "globals":
        names = []
        for f in protos:
            for i in f.code:
                op, _, _, _, bx, _ = decode(i)
                if op == OPMAP["GETGLOBAL"] and bx < len(f.constants):
                    c = f.constants[bx]
                    if isinstance(c, bytes):
                        names.append(c.decode("latin-1"))
        for n in sorted(set(names)):
            print(n)
    elif a.cmd == "roundtrip":
        again = dump(root)
        print(f"original {len(data)} bytes")
        print(f"reserialized {len(again)} bytes")
        print("byte-identical:", again == data)
        if again != data:
            for i in range(min(len(again), len(data))):
                if again[i] != data[i]:
                    print(f"first difference at 0x{i:x}: {data[i]:02x} vs {again[i]:02x}")
                    break
            return 1
    elif a.cmd == "dump":
        if not a.out:
            raise SystemExit("dump needs --out")
        open(a.out, "wb").write(dump(root))
        print(f"wrote {a.out} ({len(dump(root))} bytes)")
    elif a.cmd == "patch":
        if a.index is None or a.value is None or not a.out:
            raise SystemExit("patch needs --index, --value and --out")
        old = patch_constant(root, a.index, a.value, a.proto_path)
        blob = dump(root)
        open(a.out, "wb").write(blob)
        reloaded = load(blob)
        got = resolve_path(reloaded, a.proto_path).constants[a.index]
        print(f"patched const[{a.index}]: {old!r} -> {got!r}")
        print(f"wrote {a.out} ({len(blob)} bytes); reload ok: {got == a.value.encode('latin-1')}")
    elif a.cmd == "tree":
        def t(f, d=0):
            print("  " * d + f"{f.name} lines {f.line_defined}-{f.last_line} protos={len(f.protos)}")
            for k in f.protos:
                t(k, d + 1)
        t(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
