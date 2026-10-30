#!/usr/bin/env python3
"""Classify how two Lua 5.1 chunks differ, instead of only detecting that they do.

The conversion gate (`script_edit.roundtrip_equivalent`) accepts a decompiled record only when the recompiled
chunk's instruction signature is identical to the original. That test is deliberately strict, and stricter than
behaviour: a compiler that keeps the same code but stores it in different registers produces a different signature.

This module answers *what kind* of difference there is:

  identical   the instruction streams match exactly
  layout      the same instructions, operands and control-flow graph, under a consistent one-to-one renaming of
              registers and a correspondence of basic blocks
  structural  opcodes, blocks or operand kinds do not correspond: the decompiled source says something else

`layout` is a proof obligation, not a similarity score. It holds only if:

  * every basic block of one chunk maps to exactly one block of the other, consistently with every jump in both
    directions (a bijection on blocks, enforced while walking the graph);
  * every instruction pair has the same opcode;
  * every register operand maps through one consistent, injective renaming for the whole prototype;
  * registers holding parameters (r0 .. numparams-1) are **not** renamed: their contents come from the caller, so
    a bijection that swaps them proves nothing;
  * constant operands compare by value, not by index;
  * upvalue indices are equal, and immediate counts/lengths are equal.

Operand *kinds* come from the Lua 5.1 opcode table (`MODE` below). Treating every operand as a register would
accept chunks that load different constants out of the same slot, so the table is what makes the check sound;
getting it wrong in the other direction (comparing registers literally) only rejects valid layouts, which is
visible in the classification but never dangerous.

`layout` is reported as its own status and is never merged into `identical`: a caller that wants the strict gate
keeps using `identical`, and one that accepts the weaker proof records which proof it used.
"""
import collections
import sys

import lua51 as L51

# Opcodes whose Bx field is a jump target. TFORLOOP is included even though the reference VM reads its target
# only conditionally: the field is compared as a reference either way, which is conservative when the two chunks
# disagree about it.
JUMP_OPS = {"JMP", "FORPREP", "FORLOOP", "TFORLOOP"}

# Ops that test the value produced just before them; when one of these immediately precedes a JMP, the JMP is a
# conditional branch with a fallthrough, not an unconditional jump.
COND_TESTS = {"EQ", "LT", "LE", "TEST", "TESTSET"}

# Opcodes whose operand lives in the Bx field (constant, global name or prototype index), not in B and C.
ABX_OPS = {"LOADK", "GETGLOBAL", "SETGLOBAL", "CLOSURE"}

# Opcodes whose operands are a contiguous *range* of registers: the calling convention and list/multi-value
# builders read or write a window, so a renaming that permutes registers inside a window would reorder arguments.
# Their base register must therefore map to itself, and so must every register in the window.
RANGE_OPS = {"CALL", "TAILCALL", "RETURN", "LOADNIL", "SETLIST", "VARARG", "CONCAT"}

# Operand kinds per opcode, from lopcodes.h: R register, K constant, RK either, U upvalue, N immediate number.
# None means the field is unused. Jumps carry their target in Bx and are handled separately.
MODE = {
    "MOVE": ("R", "R", None),
    "LOADK": ("R", "K", None),
    "LOADBOOL": ("R", "N", "N"),
    "LOADNIL": ("R", "N", None),
    "GETUPVAL": ("R", "U", None),
    "GETGLOBAL": ("R", "K", None),
    "GETTABLE": ("R", "R", "RK"),
    "SETGLOBAL": ("R", "K", None),
    "SETUPVAL": ("R", "U", None),
    "SETTABLE": ("R", "RK", "RK"),
    "NEWTABLE": ("R", "N", "N"),
    "SELF": ("R", "R", "RK"),
    "ADD": ("R", "RK", "RK"),
    "SUB": ("R", "RK", "RK"),
    "MUL": ("R", "RK", "RK"),
    "DIV": ("R", "RK", "RK"),
    "MOD": ("R", "RK", "RK"),
    "POW": ("R", "RK", "RK"),
    "UNM": ("R", "R", None),
    "NOT": ("R", "R", None),
    "LEN": ("R", "R", None),
    "CONCAT": ("R", "N", "N"),
    "JMP": ("R", None, None),
    "EQ": ("N", "RK", "RK"),
    "LT": ("N", "RK", "RK"),
    "LE": ("N", "RK", "RK"),
    "TEST": ("R", "N", "N"),
    "TESTSET": ("R", "N", "R"),
    "CALL": ("R", "N", "N"),
    "TAILCALL": ("R", "N", "N"),
    "RETURN": ("R", "N", None),
    "FORLOOP": ("R", None, None),
    "FORPREP": ("R", None, None),
    "TFORLOOP": ("R", None, "N"),
    "SETLIST": ("R", "N", "N"),
    "CLOSE": ("R", None, None),
    "CLOSURE": ("R", "N", None),
    "VARARG": ("R", "N", None),
}


def _ins(proto, pc):
    """(opname, a, b, c, jump target)."""
    op, a, b, c, bx, sbx = L51.decode(proto.code[pc])
    return L51.OPCODES[op], a, b, c, pc + 1 + sbx


def tokens(proto):
    """Instruction tokens: (opname, A, B, C), with jump targets resolved and ABx operands moved into B.

    The Bx field overlaps B and C, so an ABx opcode (LOADK, GETGLOBAL, ...) carries its operand there. Reading B
    for those would compare two zeroes and accept a chunk that loads a different constant - the tokens have to
    place the real operand where the mode table expects it.
    """
    out = []
    for pc in range(len(proto.code)):
        op, a, b, c, bx, sbx = L51.decode(proto.code[pc])
        name = L51.OPCODES[op]
        if name in JUMP_OPS:
            out.append((name, a, "J", pc + 1 + sbx))
        elif name in ABX_OPS:
            out.append((name, a, bx, 0))
        else:
            out.append((name, a, b, c))
    return out


def blocks(proto):
    """Basic blocks: [{'start', 'end', 'toks'}], split at jump targets and after jumps."""
    n = len(proto.code)
    if not n:
        return []
    toks = tokens(proto)
    leaders = {0}
    for pc in range(n):
        name, _a, _b, _c, target = _ins(proto, pc)
        if name in JUMP_OPS:
            if 0 <= target < n:
                leaders.add(target)
            if pc + 1 < n:
                leaders.add(pc + 1)
    starts = sorted(leaders)
    out = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else n
        out.append({"start": s, "end": e, "toks": toks[s:e]})
    return out


def _successors(bl, index):
    """Block successors, in a fixed order: branch target first, then fallthrough."""
    n = len(bl)
    out = []
    for i, b in enumerate(bl):
        last = b["toks"][-1]
        fall = i + 1 if i + 1 < n else None
        name = last[0]
        if name == "JMP":
            target = index.get(last[3])
            conditional = len(b["toks"]) > 1 and b["toks"][-2][0] in COND_TESTS
            succ = [target] if target is not None else []
            if conditional and fall is not None:
                succ.append(fall)
        elif name in JUMP_OPS:                      # FORPREP jumps; FORLOOP/TFORLOOP also fall through
            target = index.get(last[3])
            succ = [target] if target is not None else []
            if name != "FORPREP" and fall is not None:
                succ.append(fall)
        else:
            succ = [fall] if fall is not None else []
        out.append([s for s in succ if s is not None])
    return out


def _window(name, x, y, pa, pb):
    """Register pairs that a range op forces to correspond, or None if the two windows have different sizes.

    Widths are the reference VM's, not the raw B field: RETURN's B counts the returned values plus one, LOADNIL's
    B is the last register of the run, SETLIST's values start at A+1. Being generous here only rejects valid
    layouts; being short would accept a window that reorders values, so each case is written out.
    """
    if name == "CONCAT":
        lo, hi = x[2], x[3]
        width = hi - lo + 1
        if y[3] - y[2] + 1 != width:
            return None
        return [(lo + i, y[2] + i) for i in range(width)]
    base_a, base_b, b = x[1], y[1], x[2]
    unknown = b == 0 and name != "LOADNIL"   # B=0 means "up to the top of the stack"
    if name in ("CALL", "TAILCALL"):
        base_a, base_b, width = base_a + 1, base_b + 1, b - 1
    elif name == "RETURN":
        width = b - 1
    elif name == "LOADNIL":
        width = b + 1
    elif name == "SETLIST":
        base_a, base_b, width = base_a + 1, base_b + 1, b
    else:                                   # VARARG
        width = b
    if unknown:
        # "to the top of the stack": the width is a run-time value, so the region must keep its register numbers
        width = max(pa.max_stack, pb.max_stack) - base_a
        if width <= 0:
            return [(base_a, base_b)]
        return [(base_a + i, base_a + i) for i in range(width)]
    return [(base_a + i, base_b + i) for i in range(width)]


def _const(proto, i):
    """Comparable identity for a constant slot."""
    if i >= len(proto.constants):
        return ("missing", i)
    c = proto.constants[i]
    if isinstance(c, bytes):
        return ("s", c)
    if isinstance(c, bool):
        return ("b", c)
    if isinstance(c, float):
        return ("n", c)
    if c is None:
        return ("nil",)
    return ("i", int(c))


def _register(ra, rb, regs, inverse, nparams_a, nparams_b):
    """Register renaming, identity on parameters. Returns a detail string or None."""
    if ra < nparams_a or rb < nparams_b:
        if ra != rb:
            return f"parameter r{ra} becomes r{rb}"
    if regs.setdefault(ra, rb) != rb:
        return f"r{ra} maps to both r{regs[ra]} and r{rb}"
    if inverse.setdefault(rb, ra) != ra:
        return f"r{rb} is both r{inverse[rb]} and r{rb}"
    return None


def _operand(mode, xv, yv, pa, pb, regs, inverse):
    """Compare one non-jump operand pair; None when they correspond."""
    if mode is None:
        return None                    # field unused by this opcode
    if mode == "R":
        return _register(xv, yv, regs, inverse, pa.num_params, pb.num_params)
    if mode == "U":
        return None if xv == yv else f"upvalue {xv} vs {yv}"
    if mode == "N":
        return None if xv == yv else f"immediate {xv} vs {yv}"
    if mode == "K":
        if xv == yv:
            return None
        return None if _const(pa, xv) == _const(pb, yv) else f"constant {_const(pa, xv)} vs {_const(pb, yv)}"
    if mode == "RK":
        xk, yk = xv >= 256, yv >= 256
        if xk and yk:
            return None if _const(pa, xv - 256) == _const(pb, yv - 256) \
                else f"constant {xv - 256} vs {yv - 256}"
        if xk != yk:
            return f"one side is a constant ({xv}), the other a register ({yv})"
        return _register(xv, yv, regs, inverse, pa.num_params, pb.num_params)
    return f"unknown operand mode {mode}"


def _compare_proto(pa, pb):
    """('identical'|'layout'|'structural', detail, first differing instruction or None)."""
    if tokens(pa) == tokens(pb):
        return "identical", "", None
    ba, bb = blocks(pa), blocks(pb)
    if len(ba) != len(bb):
        return "structural", f"blocks {len(ba)} vs {len(bb)}", None
    index_a = {b["start"]: i for i, b in enumerate(ba)}
    index_b = {b["start"]: i for i, b in enumerate(bb)}
    succ_a = _successors(ba, index_a)
    succ_b = _successors(bb, index_b)

    to_b = {0: 0}          # block a -> block b
    to_a = {0: 0}          # block b -> block a (bijection)
    regs = {}              # register in a -> register in b
    inverse = {}
    site = None
    queue = collections.deque([(0, 0)])
    while queue:
        ia, ib = queue.popleft()
        if len(ba[ia]["toks"]) != len(bb[ib]["toks"]):
            return "structural", (f"block {ia}: {len(ba[ia]['toks'])} vs "
                                  f"{len(bb[ib]['toks'])} instructions"), site or (pa, ba[ia]["start"])
        for k, (x, y) in enumerate(zip(ba[ia]["toks"], bb[ib]["toks"])):
            where = f"block {ia}"
            if x[0] != y[0]:
                return "structural", f"{where}: opcode {x[0]} vs {y[0]}", site or (pa, ba[ia]["start"])
            name = x[0]
            if name in JUMP_OPS:
                na = index_a.get(x[3])
                nb = index_b.get(y[3])
                if na is None or nb is None:
                    return "structural", f"{where}: jump target inside a block", site or (pa, ba[ia]["start"])
                if na in to_b:
                    if to_b[na] != nb:
                        return "structural", f"{where}: block {na} jumps to two different blocks", \
                            site or (pa, ba[ia]["start"])
                elif nb in to_a:
                    return "structural", f"{where}: block {nb} is claimed by two blocks", \
                        site or (pa, ba[ia]["start"])
                else:
                    to_b[na] = nb
                    to_a[nb] = na
                    queue.append((na, nb))
                if name != "JMP":
                    bad = _operand("R", x[1], y[1], pa, pb, regs, inverse)
                    if bad:
                        return "structural", f"{where}: {bad}", site or (pa, ba[ia]["start"])
                if site is None and (x[1] != y[1] or x[3] != y[3]):
                    site = (pa, ba[ia]["start"] + k)
                continue
            if name in RANGE_OPS:
                # A window is read or written in order, so the renaming has to be order-preserving inside it:
                # the i-th value must stay the i-th value. A width-0 window means "up to the top of the stack",
                # whose size is only known at run time, so identity is required there.
                pairs = _window(name, x, y, pa, pb)
                if pairs is None:
                    return "structural", f"{where}: window width differs", site or (pa, ba[ia]["start"])
                for ra, rb in pairs:
                    bad = _register(ra, rb, regs, inverse, pa.num_params, pb.num_params)
                    if bad:
                        return "structural", f"{where}: {bad}", site or (pa, ba[ia]["start"])
            am, bm, cm = MODE[name]
            for mode, xv, yv in ((am, x[1], y[1]), (bm, x[2], y[2]), (cm, x[3], y[3])):
                bad = _operand(mode, xv, yv, pa, pb, regs, inverse)
                if bad:
                    return "structural", f"{where}: {bad}", site or (pa, ba[ia]["start"])
            if site is None and x[1:] != y[1:]:
                site = (pa, ba[ia]["start"] + k)
    if len(to_b) != len(ba):
        return "structural", f"{len(ba) - len(to_b)} unreachable block(s)", None
    for i in range(len(ba)):
        if [to_b.get(s) for s in succ_a[i]] != succ_b[to_b[i]]:
            return "structural", f"block {i} edges differ", None
    return "layout", "", site


def analyse(chunk_a, chunk_b):
    """Compare two chunks.

    Returns {'status': 'identical'|'layout'|'structural', 'detail', 'site'} where `site` is
    (proto, instruction index) for the first divergence, or None.
    """
    a, b = L51.load(chunk_a), L51.load(chunk_b)
    pa, pb = list(L51.walk(a)), list(L51.walk(b))
    if len(pa) != len(pb):
        return {"status": "structural", "detail": f"protos {len(pa)} vs {len(pb)}", "site": None}
    first = None
    status = "identical"
    for x, y in zip(pa, pb):
        if (x.num_params, x.is_vararg) != (y.num_params, y.is_vararg):
            return {"status": "structural", "detail": "parameter count differs", "site": first}
        got, detail, site = _compare_proto(x, y)
        if got == "structural":
            return {"status": "structural", "detail": detail, "site": site or first}
        if got == "layout":
            status = "layout"
            first = first or site
    return {"status": status, "detail": "", "site": first}


def describe(chunk_a, chunk_b):
    """Human-readable one-liner, for reports."""
    got = analyse(chunk_a, chunk_b)
    where = ""
    if got["site"]:
        proto, pc = got["site"]
        name = getattr(proto, "name", str(proto))
        where = f" at {name}:{pc}"
    return got["status"] + (": " + got["detail"] if got["detail"] else "") + where


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="compare two Lua 5.1 chunks")
    ap.add_argument("a")
    ap.add_argument("b")
    a = ap.parse_args(argv)
    print(describe(open(a.a, "rb").read(), open(a.b, "rb").read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
