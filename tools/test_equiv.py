#!/usr/bin/env python3
"""The equivalence proof must accept exactly the renamings it claims, and nothing else.

`lua_equiv` is route B of the recovery plan: a second, weaker gate for records whose recompiled chunk differs only
by register allocation. A weak gate that is wrong is worse than no gate, so this test builds the cases by hand:

  * a uniform permutation of local registers is a layout difference and must be accepted;
  * renaming a parameter register must be rejected (r0..numparams-1 are filled by the caller);
  * renaming a register that participates in a CALL/SETLIST-style window must be rejected (the window is ordered);
  * changing which constant an instruction loads must be rejected;
  * a chunk compared with itself is identical.

Everything is compiled at test time with the pinned LuaDec kit, so the cases do not depend on the corpus.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lua51 as L51           # noqa: E402
import lua_equiv as EQ        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LUAC = os.path.join(ROOT, "build", "luadec-p2", "bin", "luac")
ABX = {"LOADK", "GETGLOBAL", "SETGLOBAL", "CLOSURE"}
fails = []


def ck(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name:<52} {detail}")
    if not ok:
        fails.append(name)


def enc(name, a, b=0, c=0, bx=None):
    """Encode one instruction word in the format its opcode uses."""
    op = L51.OPMAP[name]
    if name in EQ.JUMP_OPS or name in ABX:
        return op | (a << 6) | (((bx if bx is not None else b) & 0x3FFFF) << 14)
    return op | (a << 6) | (c << 14) | (b << 23)


def compile_src(src):
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "tmp")) as td:
        lua = os.path.join(td, "a.lua")
        out = os.path.join(td, "a.luac")
        open(lua, "w").write(src)
        subprocess.run([LUAC, "-o", out, lua], check=True, capture_output=True)
        return open(out, "rb").read()


def rename(chunk, sigma, only_numparams=None):
    """Apply a register permutation to every register operand, leaving everything else alone."""
    root = L51.load(chunk)
    for proto in L51.walk(root):
        if only_numparams is not None and proto.num_params != only_numparams:
            continue
        for pc in range(len(proto.code)):
            op, a, b, c, bx, sbx = L51.decode(proto.code[pc])
            name = L51.OPCODES[op]
            am, bm, cm = EQ.MODE[name]
            na = sigma.get(a, a) if am == "R" else a
            if name in EQ.JUMP_OPS:
                target = pc + 1 + sbx
                proto.code[pc] = enc(name, na, bx=target - (pc + 1))
            elif name in ABX:
                proto.code[pc] = enc(name, na, bx=bx)      # Bx holds the operand, not B
            else:
                nb = sigma.get(b, b) if bm in ("R", "RK") and b < 256 else b
                nc = sigma.get(c, c) if cm in ("R", "RK") and c < 256 else c
                proto.code[pc] = enc(name, na, b=nb, c=nc)
    return L51.dump(root)


def status(chunk, other):
    got = EQ.analyse(chunk, other)
    return got["status"], got["detail"]


locals_src = ("local a, b = 1, 2\n"
              "local t = {a, b}\n"
              "return t[1] + t[2] + a * b\n")
locals_chunk = compile_src(locals_src)
ck("chunk compared with itself is identical", status(locals_chunk, locals_chunk)[0] == "identical",
   status(locals_chunk, locals_chunk)[0])
got = status(locals_chunk, rename(locals_chunk, {0: 1, 1: 0}))
ck("uniform local-register permutation is layout", got[0] == "layout", got)

params_src = "local function f(a, b) return a - b end\nreturn f(10, 3)\n"
params_chunk = compile_src(params_src)
got = status(params_chunk, rename(params_chunk, {0: 1, 1: 0}, only_numparams=2))
ck("renaming a parameter is rejected", got[0] == "structural" and "parameter" in got[1], got)

window_src = "local x, y = 4, 5\nlocal t = {x, y}\nreturn #t\n"
window_chunk = compile_src(window_src)
# r3/r4 feed the table constructor through a window; permuting them reorders the values
got = status(window_chunk, rename(window_chunk, {3: 4, 4: 3}))
ck("renaming a window register is rejected", got[0] == "structural", got)


def change_constant(chunk):
    """Load a different constant from the same slot kind: must be structural."""
    root = L51.load(chunk)
    proto = list(L51.walk(root))[0]
    for pc in range(len(proto.code)):
        op, a, b, c, bx, sbx = L51.decode(proto.code[pc])
        if L51.OPCODES[op] == "LOADK" and b + 1 < len(proto.constants):
            proto.code[pc] = enc("LOADK", a, bx=b + 1)
            return L51.dump(root)
    raise SystemExit("no LOADK to rewrite")


got = status(locals_chunk, change_constant(locals_chunk))
ck("a different constant is rejected", got[0] == "structural", got)

if fails:
    print("equiv: FAILED " + ", ".join(fails))
    sys.exit(1)
print("equiv: proof accepts renamings, rejects parameter/window/constant changes")
