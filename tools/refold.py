#!/usr/bin/env python3
"""Fold a constructor back into the expression that consumes it, when that reproduces the original bytecode.

LuaDec sometimes prints a table constructor as a temporary local followed by field assignments:

    local t = {}
    t.someField = someValue
    target = t

The original source was `target = {someField = someValue}`. The two differ in the bytecode: the
constructor's hash-size hint is 0 instead of 1 (NEWTABLE C), and the constants are interned in the order the text
mentions them, so the destination name ends up last instead of first.

Folding the statements back into one assignment can reproduce the original exactly. Nothing here is trusted: a
rewrite is offered, then it must recompile to a chunk whose signature is identical to the shipped one. If it does
not, the record stays as it was - so the transform can be wrong without being dangerous.

    python3 tools/refold.py --mission <mission> --record N      # show what it does for one record
    python3 tools/refold.py --measure                           # how many of the 392 differs records it converts
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("PROTO2_NO_CACHE", "1")

import branch_report as BR      # noqa: E402
import lua51 as L51             # noqa: E402
import recovery_report as R     # noqa: E402
import script_edit as SE        # noqa: E402

LOCAL_EMPTY = re.compile(r"^(\s*)local\s+(\w+)\s*=\s*\{\s*\}\s*$")
FIELD = re.compile(r"^(\s*)(\w+)\.(\w+)\s*=\s*(.+?)\s*$")
FIELD_BRACKET = re.compile(r'^(\s*)(\w+)\[\s*"([^"]+)"\s*\]\s*=\s*(.+?)\s*$')


def _field(line, name):
    """(key, value) when `line` assigns a field of `name`, else None."""
    for pat in (FIELD, FIELD_BRACKET):
        m = pat.match(line)
        if m and m.group(2) == name:
            return m.group(3), m.group(4)
    return None


def _consumer(lines, start, name):
    """Index of the single line that consumes `name`, or None.

    A consumer is the first line after the field assignments that mentions `name` exactly once and does not use it
    as an assignment target - `x = t`, `return t`, `f(t)`, `holder.k = t`. That covers the shapes LuaDec emits and
    lets a fold nest: the consumer of an inner table can itself be a field assignment of an outer constructor.
    """
    word = re.compile(rf"\b{re.escape(name)}\b")
    for j in range(start, len(lines)):
        hits = list(word.finditer(lines[j]))
        if not hits:
            continue
        if len(hits) != 1:
            return None
        m = hits[0]
        after = lines[j][m.end():m.end() + 1]
        if after in (".", "["):
            return None                      # a field of the temporary: still part of the constructor
        if re.match(rf"^\s*local\s+{re.escape(name)}\s*=", lines[j]):
            return None                      # re-declaration, not a consumer
        if re.match(rf"^\s*{re.escape(name)}\s*=", lines[j]):
            return None                      # assigning to the temporary itself
        return j
    return None


def fold_once(text):
    """One left-to-right pass: fold every complete site. Returns the new text, or None if nothing folded.

    The temporary's declaration and field assignments are dropped, and the constructor is written where the
    temporary was consumed. Statements between the fields and the consumer are kept in order - if moving the
    constructor changes the bytecode, the gate rejects the candidate.
    """
    lines = text.splitlines()
    word_of = {}
    changed = False
    out = []
    i = 0
    while i < len(lines):
        m = LOCAL_EMPTY.match(lines[i])
        name = m.group(2) if m else None
        if name:
            fields, j = [], i + 1
            while j < len(lines):
                f = _field(lines[j], name)
                if f is None:
                    break
                fields.append(f"{f[0]} = {f[1]}")
                j += 1
            if fields:
                k = _consumer(lines, j, name)
                if k is not None:
                    w = word_of.setdefault(name, re.compile(rf"\b{re.escape(name)}\b"))
                    if not any(w.search(ln) for ln in lines[k + 1:]):
                        ctor = "{ " + ", ".join(fields) + " }"
                        out.extend(lines[j:k])
                        out.append(w.sub(lambda _m: ctor, lines[k], count=1))
                        changed = True
                        i = k + 1
                        continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else "") if changed else None


def fold(text):
    """Candidate rewrites of `text`, one per successive pass.

    Pass 1 folds every complete site; pass 2 folds sites that only became complete after pass 1 (a constructor
    consumed by another constructor's field), and so on, up to four passes. Every candidate is still unverified
    text - the caller must prove it by recompiling.
    """
    out = []
    current = text
    for _ in range(4):
        nxt = fold_once(current)
        if nxt is None or nxt == current:
            break
        out.append(nxt)
        current = nxt
    return out


CALL_LINE = re.compile(r"^(\s*)([\w.:]+)\s*\((.*)\)\s*$")


def _split_args(s):
    """Split an argument list on top-level commas."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _call_spans(text):
    """Spans of `name(...)` calls, with the closing paren found across newlines.

    String literals are skipped so a paren inside one cannot unbalance the scan. Returned innermost-last, and the
    caller processes them from the end of the text backwards so earlier offsets stay valid.
    """
    spans = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            q = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    break
                i += 1
            i += 1
            continue
        if ch == "(" and i > 0:
            # a call needs an identifier (or a closing bracket) immediately before the paren
            j = i - 1
            while j >= 0 and (text[j].isalnum() or text[j] in "_.:"):
                j -= 1
            if j < i - 1:
                depth, k = 1, i + 1
                while k < n and depth:
                    c = text[k]
                    if c in "\"'":
                        q = c
                        k += 1
                        while k < n:
                            if text[k] == "\\":
                                k += 2
                                continue
                            if text[k] == q:
                                break
                            k += 1
                    elif c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                    k += 1
                if depth == 0:
                    spans.append((j + 1, i, k - 1))     # (name start, open paren, close paren)
        i += 1
    return spans


def hoist(text, do_wrapped=False):
    """Candidates that pull constructors out of call arguments into locals.

    LuaDec inlines a table that the original code built in a local and passed as an argument:

        f({ ... })                    ->   local t = { ... }
                                           f(t)

    The original bytecode has a MOVE of the table into the argument register, so the inline form recompiles
    short. Calls can span several lines and can take several constructors (`f({a=1}, {b=2})` builds two locals
    and two MOVEs), so the scan matches parentheses across newlines and hoists every constructor argument. The
    gate decides whether a guess was right.
    """
    changed = False
    counter = 0
    for name_start, open_paren, close_paren in reversed(_call_spans(text)):
        inner = text[open_paren + 1:close_paren]
        parts = _split_args(inner)
        if not any(p.startswith("{") and p.endswith("}") and len(p) > 2 for p in parts):
            continue
        line_start = text.rfind("\n", 0, name_start) + 1
        prefix = text[line_start:name_start]
        indent = prefix[:len(prefix) - len(prefix.lstrip())]
        # a call may sit on the right of an assignment (`x = f({...})`); hoisting still works there, the locals go
        # above the whole statement. Anything else before the call means it is part of a larger expression.
        if prefix.strip():
            plain = re.match(r"^[\w.:\[\]\"']+\s*=\s*$", prefix)
            wrapped = do_wrapped and re.match(r"^do\s+\(.+\)\.[\w.\[\]]+\s*=\s*$", prefix)
            if not (plain or wrapped):
                continue
        made, newparts = [], []
        for part in parts:
            if part.startswith("{") and part.endswith("}") and len(part) > 2:
                tmp = f"__ctor{counter}"
                counter += 1
                made.append(f"{indent}local {tmp} = {part}")
                newparts.append(tmp)
            else:
                newparts.append(part)
        if not made:
            continue
        call = text[name_start:open_paren + 1] + ", ".join(newparts) + ")"
        text = text[:line_start] + "\n".join(made) + "\n" + prefix + call + text[close_paren + 1:]
        changed = True
    return [text] if changed else []


CALL_ARG = re.compile(r"^[\w.:]+\(.*\)$", re.S)


def hoist_calls(text, hoist_all=False):
    """Candidates that pull a call argument out into a local.

    LuaDec inlines a value the original computed first:

        print("T:", go_GetTickCount(), ": ", ...)     ->   local t = go_GetTickCount()
                                                           print("T:", t, ": ", ...)

    The original bytecode has the inner call's result in a register before the outer call's function is loaded, which
    the inline form does not reproduce. Arguments are hoisted in source order; with `hoist_all`, every argument that
    is a plain call, otherwise only the first. The gate decides whether a guess was right.
    """
    changed = False
    counter = 0
    for name_start, open_paren, close_paren in reversed(_call_spans(text)):
        inner = text[open_paren + 1:close_paren]
        parts = _split_args(inner)
        if not any(CALL_ARG.match(p) for p in parts):
            continue
        line_start = text.rfind("\n", 0, name_start) + 1
        indent = text[line_start:name_start]
        if indent.strip():
            continue
        made, newparts = [], []
        for part in parts:
            if CALL_ARG.match(part) and (hoist_all or not made):
                tmp = f"__call{counter}"
                counter += 1
                made.append(f"{indent}local {tmp} = {part}")
                newparts.append(tmp)
            else:
                newparts.append(part)
        if not made:
            continue
        call = text[name_start:open_paren + 1] + ", ".join(newparts) + ")"
        text = text[:line_start] + "\n".join(made) + "\n" + indent + call + text[close_paren + 1:]
        changed = True
    return [text] if changed else []


SELF_CALL = re.compile(r"^(\s*)(\w+) = \2\((.*)\)\s*$")
LOCAL_DECL = re.compile(r"^(\s*)local (\w+) = (.+)$")
ASSIGN = re.compile(r"^(\s*)([\w.:\[\]\"]+) = (\w+)\s*$")


def collapse_call(text, keep_side_effects=False):
    """Candidates that undo LuaDec's 'call through materialised locals'.

    LuaDec sometimes lowers a call into a local for the function, a local per argument, the call through those
    locals, and finally the assignment of the result:

        local tmp2 = schedule
        local tmp3 = 3
        ...
        tmp2 = tmp2(tmp3, ...)
        target = tmp2

    The original source is one call. Locals whose value is used exactly once are inlined - repeatedly, so chains
    like `local t = u[1]` collapse too - while a local used twice is kept, because inlining it would duplicate a
    side effect. The gate decides whether the guess is right.
    """
    lines = text.splitlines()
    out, changed = [], False
    i = 0
    while i < len(lines):
        m = SELF_CALL.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent, name, args = m.group(1), m.group(2), m.group(3)
        j = len(out) - 1
        exprs, order = {}, []
        while j >= 0:
            d = LOCAL_DECL.match(out[j])
            if not d:
                break
            exprs[d.group(2)] = d.group(3).strip()
            order.insert(0, d.group(2))
            j -= 1
        first = j + 1
        if name not in exprs or i + 1 >= len(lines):
            out.append(lines[i])
            i += 1
            continue
        a = ASSIGN.match(lines[i + 1])
        if not a or a.group(3) != name:
            out.append(lines[i])
            i += 1
            continue
        target, fexpr = a.group(2), exprs[name]
        args = args
        body = {k: v for k, v in exprs.items() if k != name}
        # inline single-use locals into their one use, repeatedly
        progress = True
        while progress:
            progress = False
            for ident in list(body):
                pat = re.compile(rf"\b{re.escape(ident)}\b")
                in_args = len(pat.findall(args))
                in_others = sum(len(pat.findall(v)) for k, v in body.items() if k != ident)
                if in_args + in_others != 1:
                    continue
                if keep_side_effects and "(" in body[ident]:
                    continue
                if in_args:
                    args = pat.sub(lambda _m, v=body[ident]: v, args, count=1)
                else:
                    for k, v in body.items():
                        if k != ident and pat.search(v):
                            body[k] = pat.sub(lambda _m, v=body[ident]: v, v, count=1)
                            break
                del body[ident]
                progress = True
        kept = [f"{indent}local {k} = {body[k]}" for k in order if k in body]
        call = f"{indent}{target} = {fexpr}({args})"
        del out[first:]
        out.extend(kept + [call])
        i += 2
        changed = True
    return ["\n".join(out) + ("\n" if text.endswith("\n") else "")] if changed else []


GUARD_IF = re.compile(r"^(\s*)if not (\w+) then\s*$")
GUARD_INIT = re.compile(r"^(\s+)(\w+) = (.+?)\s*$")


def guard_or(text):
    """Candidates that turn a decompiled `if not X then X = {} ... end` into `X = X or {}` plus the body.

    When the shipped code initialises a table only if it is unset but continues unconditionally:

        if not State then
          State = {}
          ... rest of the function ...
        end

    the reconstruction puts the rest *inside* the conditional, and its jump layout is wrong. The bytecode shows the
    conditional skipping just the assignment. Two of the remaining blocked records differ only this way.
    """
    lines = text.splitlines()
    out, changed = [], False
    i = 0
    while i < len(lines):
        m = GUARD_IF.match(lines[i])
        init = GUARD_INIT.match(lines[i + 1]) if i + 1 < len(lines) else None
        if not m or not init or m.group(2) != init.group(2):
            out.append(lines[i])
            i += 1
            continue
        indent, name = m.group(1), m.group(2)
        j = i + 2
        while j < len(lines) and lines[j] != indent + "end":
            j += 1
        if j >= len(lines):
            out.append(lines[i])
            i += 1
            continue
        out.append(f"{indent}{name} = {name} or ({init.group(3)})" if not init.group(3).startswith("{")
                   else f"{indent}{name} = {name} or {init.group(3)}")
        for body_line in lines[i + 2:j]:
            out.append(body_line[2:] if body_line.startswith(indent + "  ") else body_line)
        i = j + 1
        changed = True
    return ["\n".join(out) + ("\n" if text.endswith("\n") else "")] if changed else []


def hoist_earlier(text, steps=4):
    """Candidates that move a hoisted constructor local up by one statement at a time.

    `hoist` places the local immediately before the call that uses it, but the shipped source sometimes declares the
    table earlier - before the statement that precedes the call - because that is where the expression sat in the
    original. Moving the declaration up a statement at a time and letting the gate decide costs nothing and covers
    both orderings.
    """
    out = []
    for cand in hoist(text):
        lines = cand.splitlines()
        for k, line in enumerate(lines):
            if line.strip().startswith("local __ctor"):
                for step in range(1, steps + 1):
                    j = k - step
                    if j < 0:
                        break
                    if not lines[j].strip():
                        continue
                    moved = lines[:j] + [lines[k]] + lines[j:k] + lines[k + 1:]
                    out.append("\n".join(moved) + ("\n" if cand.endswith("\n") else ""))
                break
    return out


COND_STMT = re.compile(r"^(\s*)(if|elseif|while) (.+?)( then| do)\s*$")
CMP = re.compile(r"^(.*?)\s*(<=|>=|==|~=|<|>)\s*(.+)$")
FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "~=": "~="}


def _split_terms(cond):
    """Split on top-level `and` / `or`, keeping the operators."""
    terms, ops, depth, cur = [], [], 0, ""
    i = 0
    while i < len(cond):
        ch = cond[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if depth == 0:
            for op in (" and ", " or "):
                if cond.startswith(op, i):
                    terms.append(cur.strip())
                    ops.append(op.strip())
                    cur = ""
                    i += len(op)
                    break
            else:
                cur += ch
                i += 1
        else:
            cur += ch
            i += 1
    terms.append(cur.strip())
    return terms, ops


def comparison_swaps(text, limit=10):
    """Candidates that turn `A <= B` into `B >= A` inside a condition.

    The comparison instructions are symmetric, but the compiler loads the left operand first, so the order the
    globals are fetched in distinguishes `targetThreshold <= targetHealth` from
    `targetHealth >= targetThreshold`. Three records needed exactly this (story_i1 #63, test_rampage01 #5,
    story_g2 #217), and a whole-condition rewrite cannot express it when the comparison sits inside an `and`/`or`
    chain, which is why the earlier condition variants missed them.
    """
    out = []
    for k, line in enumerate(text.splitlines()):
        m = COND_STMT.match(line)
        if not m:
            continue
        indent, kw, cond, tail = m.group(1), m.group(2), m.group(3), m.group(4)
        terms, ops = _split_terms(cond)
        for t, term in enumerate(terms):
            cm = CMP.match(term)
            if not cm or not cm.group(1).strip() or not cm.group(3).strip():
                continue
            left, op, right = cm.group(1).strip(), cm.group(2), cm.group(3).strip()
            flipped = FLIP.get(op)
            if not flipped:
                continue
            new_terms = list(terms)
            new_terms[t] = f"{right} {flipped} {left}"
            new_cond = new_terms[0]
            for o, nt in zip(ops, new_terms[1:]):
                new_cond += f" {o} {nt}"
            if new_cond == cond:
                continue
            cand = text.splitlines()
            cand[k] = f"{indent}{kw} {new_cond}{tail}"
            out.append("\n".join(cand) + ("\n" if text.endswith("\n") else ""))
            if len(out) >= limit:
                return out
    return out


# ----------------------------------------------------------------------------------------------------------
# Recovery rules measured against the hand-reconstructed records (docs/32).
#
# The records in `overrides/` are the ones the fold/hoist rules above could not reproduce. A few of them are one
# local construct away from the decompiler output, and these rules generate that construct as a candidate:
#
#   * an empty `else` (Lua 5.1 emits an extra JMP for `if c then A else end`, even with an empty else)
#   * a local value inlined into its single use (the decompiler lowers calls through materialised locals)
#   * a statement call whose result the original bound to an unused local (`local _ = f()` is CALL B=1)
#   * a `do` block that copies a multiple assignment back into existing locals (`x, g = f()`)
#   * a constructor the decompiler split into `local t = {}` + `t.k = v` (with no consumer)
#   * LuaDec's empty multi-assignment slot (`local a, b = , f()` is not valid Lua)
#
# Nothing is trusted: every candidate is offered to the caller, which recompiles it and accepts it only when the
# signature is identical. A wrong rule costs one failed compilation.
# ----------------------------------------------------------------------------------------------------------
_TOKEN = re.compile(r'--\[\[.*?\]\]|--[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
                    r'\b(if|then|elseif|else|end|do|while|for|function|repeat|until)\b', re.S)
_OPENERS = {"if", "do", "function", "repeat"}
_NON_IF_OPENERS = {"do", "function", "repeat"}
_EMPTY_SLOT = re.compile(r"(=)\s*,")
_LOCAL_DECL = re.compile(r"^(\s*)local\s+([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")
_ATOMIC = re.compile(r'^[A-Za-z_]\w*(?:[.:][A-Za-z_]\w*|\[[^\]]+\])*$|^"[^"]*"$|^\'[^\']*\'$|'
                     r'^-?\d+(?:\.\d+)?$|^(?:true|false|nil)$')
_DEAD_CTOR = re.compile(r"^(\s*)local\s+([A-Za-z_]\w*)\s*=\s*\{\s*\}\s*$")
_DEAD_FIELD = re.compile(r'^(\s*)([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*)|\[\s*"([^"]+)"\s*\])\s*=\s*(.+?)\s*$')
_DO_LOCAL = re.compile(r"^(\s*)local\s+([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*=\s*(.+?)\s*$")
_COPY = re.compile(r"^(\s*)([A-Za-z_][\w.]*)\s*=\s*([A-Za-z_]\w*)\s*$")
_STMT_CALL = re.compile(r'^(\s*)([A-Za-z_][\w.:]*)\s*\((.*)\)\s*$')
_CALL_KEYWORDS = ("if", "while", "for", "return", "local", "elseif", "until")


def _tokens(text):
    for m in _TOKEN.finditer(text):
        if m.group(1):
            yield m.start(), m.group(1)


def _block_end(text, start):
    """Offset of the token that closes the block enclosing `start` (`end`/`until`/`else`/`elseif`)."""
    depth = 0
    for pos, tok in _tokens(text):
        if pos < start:
            continue
        if tok in _OPENERS:
            depth += 1
        elif tok in ("end", "until"):
            if depth == 0:
                return pos
            depth -= 1
        elif tok in ("else", "elseif") and depth == 0:
            return pos
    return len(text)


def fill_empty_slots(text):
    """`local a, b = , f()` (LuaDec's dropped value) -> `local a, b = nil, f()`."""
    out, n = [], 0
    for line in text.splitlines():
        fixed, k = _EMPTY_SLOT.subn(r"\1 nil,", line)
        n += k
        out.append(fixed)
    return "\n".join(out) + "\n" if n else None


def inline_single_use(text, calls=False):
    """Inline a local that is used exactly once in its enclosing block.

    `calls=False` inlines atoms (name, field, literal) only; `calls=True` also inlines call and operator
    expressions. Inlining a call changes when its side effect happens, so only the gate may accept it.
    """
    lines = text.splitlines()
    changed = True
    did = False
    while changed:
        changed = False
        for i, line in enumerate(lines):
            m = _LOCAL_DECL.match(line)
            if not m:
                continue
            _indent, name, expr = m.group(1), m.group(2), m.group(3)
            if expr.startswith("{") or expr.startswith("function"):
                continue
            atomic = bool(_ATOMIC.match(expr))
            if not atomic and not calls:
                continue
            if re.search(r"\b" + re.escape(name) + r"\b", expr):
                continue
            off = sum(len(x) + 1 for x in lines[:i])
            start = off + len(line) + 1
            body = text[start:_block_end(text, start)]
            word = re.compile(r"\b" + re.escape(name) + r"\b")
            if len(word.findall(body)) != 1:
                continue
            text = text[:off] + word.sub(lambda _m: expr, body, count=1) + text[_block_end(text, start):]
            lines = text.splitlines()
            changed = did = True
            break
    return text if did else None


def flatten_do(text):
    """Remove `do`/`end` wrappers whose bodies are only assignments, keeping statement order."""
    lines = text.splitlines()
    out, i, n = [], 0, 0
    while i < len(lines):
        if lines[i].strip() == "do":
            indent = len(lines[i]) - len(lines[i].lstrip())
            j = i + 1
            while j < len(lines) and not (lines[j].strip() == "end" and
                                          len(lines[j]) - len(lines[j].lstrip()) == indent):
                j += 1
            body = lines[i + 1:j]
            if j < len(lines) and body and all(not b.strip().startswith("local ") for b in body):
                out.extend(body)
                i = j + 1
                n += 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) + "\n" if n else None


def fold_dead_constructor(text):
    """`local t = {}` + only `t.k = v` field assignments -> `local t = {k = v, ...}`.

    `fold()` above folds into the statement that consumes the temporary; this handles the other shape the
    decompilers emit, where the constructor's value is never consumed (the original kept it in an unused local)
    and the constructor still has to be built because the field values can call functions.
    """
    lines = text.splitlines()
    out, i, n = [], 0, 0
    while i < len(lines):
        m = _DEAD_CTOR.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent, name = m.group(1), m.group(2)
        fields, j = [], i + 1
        while j < len(lines):
            f = _DEAD_FIELD.match(lines[j])
            if not f or f.group(2) != name:
                break
            fields.append((f.group(3) or f.group(4), f.group(5)))
            j += 1
        if not fields or any("{" in v for _k, v in fields):
            out.append(lines[i])
            i += 1
            continue
        out.append(f"{indent}local {name} = {{" + ", ".join(f"{k} = {v}" for k, v in fields) + "}")
        i = j
        n += 1
    return "\n".join(out) + "\n" if n else None


def merge_copyback(text):
    """`do local a, b = f(); x = a; g = b end` -> `x, g = f()` (restores register and constant order)."""
    lines = text.splitlines()
    out, i, n = [], 0, 0
    while i < len(lines):
        if lines[i].strip() != "do":
            out.append(lines[i])
            i += 1
            continue
        indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
        j = i + 1
        while j < len(lines) and lines[j].strip() != "end":
            j += 1
        body = lines[i + 1:j]
        m = _DO_LOCAL.match(body[0]) if body else None
        if j >= len(lines) or not m:
            out.append(lines[i])
            i += 1
            continue
        names = [x.strip() for x in m.group(2).split(",")]
        targets, ok = {}, True
        for line in body[1:]:
            c = _COPY.match(line)
            if not c or c.group(3) not in names or c.group(3) in targets:
                ok = False
                break
            targets[c.group(3)] = c.group(2)
        if not ok or len(targets) != len(names):
            out.append(lines[i])
            i += 1
            continue
        out.append(f"{indent}{', '.join(targets[x] for x in names)} = {m.group(3)}")
        i = j + 1
        n += 1
    return "\n".join(out) + "\n" if n else None


def materialise_results(text, max_sites=16):
    """`f(x)` -> `local _rN = f(x)`; the shipped CALL asked for a result the decompiler discarded."""
    lines = text.splitlines()
    sites = []
    for i, line in enumerate(lines):
        m = _STMT_CALL.match(line)
        if m and m.group(2) not in _CALL_KEYWORDS:
            sites.append((i, m.group(1), m.group(2), m.group(3)))
    out = []
    for idx, (i, indent, name, args) in enumerate(sites[:max_sites]):
        cand = list(lines)
        cand[i] = f"{indent}local _r{idx} = {name}({args})"
        out.append("\n".join(cand) + "\n")
    if len(sites) > max_sites:
        cand = list(lines)
        for idx, (i, indent, name, args) in enumerate(sites):
            cand[i] = f"{indent}local _r{idx} = {name}({args})"
        out.append("\n".join(cand) + "\n")
    return out


def _if_blocks(text):
    stack, blocks = [], []
    for pos, tok in _tokens(text):
        if tok == "if":
            stack.append(["if", pos, False])
        elif tok in ("else", "elseif"):
            if stack and stack[-1][0] == "if":
                stack[-1][2] = True
        elif tok in _NON_IF_OPENERS:
            stack.append([tok, None, False])
        elif tok in ("end", "until"):
            if stack:
                kind, start, has_else = stack.pop()
                if kind == "if":
                    blocks.append((start, pos, has_else))
    return blocks


def empty_else(text, max_sites=24):
    """Insert an empty `else` before the `end` of every if-block that has neither else nor elseif.

    Lua 5.1 compiles `if c then A end` and `if c then A else end` differently: the explicit else emits a JMP
    before the end, even though the branch is empty. A shipped chunk carrying that JMP cannot be reproduced
    from source that dropped the keyword.
    """
    blocks = [b for b in _if_blocks(text) if not b[2]]
    if not blocks or len(blocks) > max_sites:
        return []
    out = []
    for _start, endpos, _has in blocks:
        line_start = text.rfind("\n", 0, endpos) + 1
        if text[line_start:endpos].strip():
            continue
        indent = text[line_start:endpos]
        out.append(text[:line_start] + indent + "else\n" + text[line_start:])
    if len(out) > 1:
        all_text = text
        for _start, endpos, _has in reversed(blocks):
            line_start = all_text.rfind("\n", 0, endpos) + 1
            if all_text[line_start:endpos].strip():
                continue
            indent = all_text[line_start:endpos]
            all_text = all_text[:line_start] + indent + "else\n" + all_text[line_start:]
        out.append(all_text)
    return out


def recovery_candidates(text, cap=400):
    """Candidates from the recovery rules, breadth-first over short pipelines.

    Level 1 applies every rule to the decompiler text; deeper levels re-apply the structural rules (inline,
    flatten, fold, copy-back), because the shapes compose: inlining a call-through-locals exposes a `do` block,
    flattening that exposes a constructor, folding it restores NEWTABLE's size hint. Value-level rules
    (materialise a result, add an empty else) are only tried on the original text to keep the search small.
    """
    out, seen = [], {text}

    def add(cand):
        if cand and cand not in seen and len(out) < cap:
            seen.add(cand)
            out.append(cand)
            return True
        return False

    def structural(src):
        res = [flatten_do(src), fold_dead_constructor(src), merge_copyback(src),
               inline_single_use(src, calls=False), inline_single_use(src, calls=True)]
        return [x for x in res if x]

    level = []
    add(fill_empty_slots(text))
    for cand in structural(text):
        if add(cand):
            level.append(cand)
    for cand in materialise_results(text):
        if add(cand):
            level.append(cand)
    for cand in empty_else(text):
        if add(cand):
            level.append(cand)

    for _depth in range(2):
        nxt = []
        for cand in level:
            for child in structural(cand):
                if add(child):
                    nxt.append(child)
        level = nxt
        if not level:
            break
    return out


def candidates(text):
    """Every verified-candidate rewrite, in the order they are worth trying."""
    out = []
    for folded in fold(text):
        out.append(folded)
        out.extend(hoist(folded))
    out.extend(hoist(text))
    out.extend(hoist(text, do_wrapped=True))
    out.extend(hoist_earlier(text))
    out.extend(collapse_call(text))
    out.extend(collapse_call(text, keep_side_effects=True))
    for cand in hoist(text):
        out.extend(collapse_call(cand))
        out.extend(collapse_call(cand, keep_side_effects=True))
    out.extend(hoist_calls(text))
    for cand in hoist(text):
        out.extend(hoist_calls(cand))
    out.extend(hoist_calls(text, hoist_all=True))
    out.extend(comparison_swaps(text))
    out.extend(guard_or(text))
    for cand in list(out):
        out.extend(guard_or(cand))
    out.extend(recovery_candidates(text))
    seen, unique = set(), []
    for cand in out:
        if cand not in seen:
            seen.add(cand)
            unique.append(cand)
    return unique


def proven(text, chunk):
    """(ok, detail) - does `text` recompile to a chunk with the same signature as `chunk`?"""
    recomp, why = BR.recompile(text)
    if recomp is None:
        return False, why
    try:
        return L51.signature(L51.load(recomp)) == L51.signature(L51.load(chunk)), ""
    except Exception as exc:
        return False, str(exc)[:80]


def best_source(payload):
    """(source, kind) where kind is 'as-decompiled' or 'refolded'; (None, reason) when neither proves."""
    src, note = SE.decompile_payload(payload)
    if src is None:
        return None, note
    chunk = SE.bytecode_chunk(payload)
    ok, _why = proven(src, chunk)
    if ok:
        return src, "as-decompiled"
    for cand in fold(src):
        if proven(cand, chunk)[0]:
            return cand, "refolded"
    return None, "no fold reproduces the chunk"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mission")
    ap.add_argument("--variant", default="active")
    ap.add_argument("--record", type=int)
    ap.add_argument("--measure", action="store_true")
    a = ap.parse_args(argv)

    if a.record is not None and a.mission:
        got = SE.open_side(a.mission, a.variant, "shipped")
        payload = SE.records_with_roles(got[3])[a.record][1]
        src, note = SE.decompile_payload(payload)
        print("--- as decompiled:\n" + (src or note).strip())
        for n, cand in enumerate(fold(src or "")):
            print(f"--- candidate {n}:\n" + cand.strip())
        print("--- proof:", proven(src or "", SE.bytecode_chunk(payload)))
        for cand in fold(src or ""):
            if proven(cand, SE.bytecode_chunk(payload))[0]:
                print("--- refold PROVES the chunk")
                break
        return 0

    # measure over every record that does not convert as decompiled
    gained = 0
    checked = 0
    fails = []
    for mission, variant, model in R.missions():
        for i, ((it, payload, role), (_r, text, _h, _x)) in enumerate(
                zip(SE.records_with_roles(model), SE._decoded(model))):
            if text is not None:
                continue
            chunk = SE.bytecode_chunk(payload)
            if chunk is None:
                continue
            if SE.roundtrip_equivalent(payload)[0] == "equivalent":
                continue
            checked += 1
            src, kind = best_source(payload)
            if src is not None:
                gained += 1
                fails.append((mission, variant, i, role, kind))
    print(f"records that do not convert today : {checked}")
    print(f"recovered by a verified refold     : {gained}")
    for f in fails[:10]:
        print("  ", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
