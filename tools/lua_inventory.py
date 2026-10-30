#!/usr/bin/env python3
"""Inventory the engine's Lua API against what missions actually call.

Two independent sources, compared:

  registered  every identifier-shaped literal in prototype2engine.dll matching
              `<lowercase_family>_<Name>` - what the engine binds into Lua
  used        every name called from decoded mission scripts, with the arity
              (argument count) observed at each call site

The point is to answer "what can I call, and what does it take" from evidence:

  * names used but not registered are script-defined helpers (mms_* and friends), not engine calls;
  * names registered but never used are available surface nobody has documented yet;
  * arity comes from real call sites, so it is an observation, not a guess.

    python3 tools/lua_inventory.py --md analysis/lua-api-inventory.md
    python3 tools/lua_inventory.py --json analysis/lua-api-inventory.json
    python3 tools/lua_inventory.py --family ai        # one family, with per-name arity
"""
import argparse
import collections
import json
import os
import re
import sys
import config

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "tmp", "corpus")
GAME = config.game_dir()
REGISTERED_RE = re.compile(r"[a-z][a-z0-9]*_[A-Za-z0-9_]+")
# Lua keywords and library functions: called constantly, never engine bindings.
LUA_CORE = {
    "function", "if", "elseif", "while", "for", "return", "and", "or", "not", "then", "do", "end",
    "local", "pairs", "ipairs", "print", "tostring", "tonumber", "type", "pcall", "xpcall", "error",
    "assert", "select", "unpack", "setmetatable", "getmetatable", "rawget", "rawset", "next", "require",
    "string", "table", "math", "os", "io", "coroutine", "loadstring", "collectgarbage",
}
CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def registered(dll_path):
    """{family: set(names)} for identifier-shaped literals in the DLL."""
    data = open(dll_path, "rb").read()
    out = collections.defaultdict(set)
    for m in re.finditer(rb"[ -~]{4,}", data):
        s = m.group().decode("latin-1")
        if REGISTERED_RE.fullmatch(s):
            out[s.split("_")[0]].add(s)
    return out


def arity(text, start):
    """Top-level argument count of the call whose '(' is at `start`."""
    depth = 0
    n = 0
    seen = False
    i = start
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return n + (1 if seen else 0)
        elif c == "," and depth == 1:
            n += 1
        elif depth >= 1 and not c.isspace():
            seen = True
        elif c == '"' or c == "'":
            q = c
            i += 1
            while i < len(text) and text[i] != q:
                if text[i] == "\\":
                    i += 1
                i += 1
        i += 1
    return None


def corpus_usage():
    """{name: {'calls': n, 'args': Counter}} over every decodable mission record.

    Records whose payload is a compiled Lua chunk are decompiled with our LuaDec build and scanned too, so a
    call made only from bytecode is not invisible. Decompilation is cached per chunk hash for the run.
    """
    import fsm_edit as E
    import fsm_graph as F
    import fsm_luablob as L
    import hashlib
    import script_edit as SE

    calls = collections.Counter()
    args = collections.defaultdict(collections.Counter)
    missions = set()
    unreadable = 0
    stats = collections.Counter()
    decompiled = {}

    def text_of(payload):
        """(text or None, kind) - text payloads directly, bytecode via LuaDec, else None."""
        t, _how = L.read(payload, b"")
        if t is not None:
            return t, "text"
        chunk = SE.bytecode_chunk(payload)
        if chunk is None:
            return None, "unknown"
        h = hashlib.sha1(chunk).hexdigest()
        if h not in decompiled:
            src, _note = SE.decompile_payload(payload)
            decompiled[h] = src
        return decompiled[h], "bytecode"
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        leaf, _off = F.payload(open(os.path.join(dirpath, "fig.p3d.rz"), "rb").read())
        try:
            model = E.parse(leaf)
        except Exception:
            continue
        missions.add(os.path.basename(os.path.dirname(dirpath)))
        recs = [it for el in E.walk_elements(model) for c in el["containers"]
                for it in c["items"] if (it.get("fields") or {}).get("type") == "record"]
        recs.sort(key=lambda it: it["off"])
        prev = b""
        for it in recs:
            payload = it["fields"]["payload"]
            t, _how = L.read(payload, prev[-32768:] if prev else b"")
            kind = "text" if t is not None else None
            if t is None:
                t, kind = text_of(payload)
                if t is None:
                    unreadable += 1
                    stats["unknown"] += 1
                    continue
                stats["decompiled"] += 1
            else:
                stats["text"] += 1
            tb = t.encode("latin-1") if isinstance(t, str) else t
            prev += tb
            txt = tb.decode("latin-1")
            for m in CALL_RE.finditer(txt):
                name = m.group(1)
                calls[name] += 1
                a = arity(txt, m.end() - 1)
                if a is not None:
                    args[name][a] += 1
    print(f"  record payloads: {stats['text']} text, {stats['decompiled']} decompiled bytecode, "
          f"{stats['unknown']} unknown")
    return calls, args, missions, unreadable


def main(argv=None):
    config.require_game_dir()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=os.path.join(GAME, "prototype2engine.dll"))
    ap.add_argument("--md", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--family", default=None)
    a = ap.parse_args(argv)

    reg = registered(a.dll)
    calls, args, missions, unreadable = corpus_usage()
    reg_names = set().union(*reg.values()) if reg else set()

    helpers = {n for n in calls if n not in reg_names and n not in LUA_CORE and "_" in n}
    core = {n for n in calls if n in LUA_CORE or "_" not in n}
    used_registered = {n for n in calls if n in reg_names}
    used_unregistered = {n for n in calls if n not in reg_names}
    unused = reg_names - set(calls)

    print(f"missions decoded          : {len(missions)}  (records unreadable: {unreadable})")
    print(f"registered names          : {len(reg_names)} across {len(reg)} families")
    print(f"names called in missions  : {len(calls)}")
    print(f"  registered and used     : {len(used_registered)}")
    print(f"  used, not registered    : {len(used_unregistered)}")
    print(f"    script-side helpers   : {len(helpers)}  (defined in the shipped .lua files)")
    print(f"    Lua core / keywords   : {len(core)}")
    print(f"  registered, never used  : {len(unused)}")

    fam = collections.Counter()
    for n in reg_names:
        fam[n.split("_")[0]] += 1
    print("\ntop registered families:")
    for p, c in fam.most_common(15):
        u = sum(1 for n in calls if n.startswith(p + "_"))
        print(f"   {p + '_':<12} {c:>4} registered, {u:>4} used in missions")

    if a.family:
        pref = a.family.rstrip("_") + "_"
        rows = sorted((n, calls.get(n, 0), args.get(n)) for n in reg_names if n.startswith(pref))
        print(f"\n{pref}* - {len(rows)} names")
        for n, c, ar in rows:
            if c:
                sig = ", ".join(f"{k}x{v}" for k, v in sorted(ar.items())) if ar else "?"
                print(f"   {n:<34} called {c:<5} argc {sig}")
        print(f"   ({sum(1 for _n, c, _a in rows if not c)} never called in the corpus)")

    if a.json:
        os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
        json.dump({
            "registered": {p: sorted(v) for p, v in reg.items()},
            "usage": {n: {"calls": c, "argc": dict(args.get(n, {}))} for n, c in calls.most_common()},
            "unused": sorted(unused),
            "script_helpers": sorted(helpers),
            "missions": len(missions),
            "unreadable_records": unreadable,
        }, open(a.json, "w"), indent=1)
        print(f"wrote {a.json}")

    if a.md:
        os.makedirs(os.path.dirname(a.md) or ".", exist_ok=True)
        with open(a.md, "w") as fh:
            fh.write("# Lua API inventory\n\n")
            fh.write("Generated by `tools/lua_inventory.py`; every number is re-derivable.\n\n")
            fh.write(f"- missions decoded: **{len(missions)}** (records unreadable: {unreadable})\n")
            fh.write(f"- registered names: **{len(reg_names)}** across {len(reg)} families\n")
            fh.write(f"- names called in missions: **{len(calls)}**\n")
            fh.write(f"  - registered and used: {len(used_registered)}\n")
            fh.write(f"  - script-side helpers (used, not registered): {len(helpers)}\n")
            fh.write(f"  - Lua core / keywords: {len(core)}\n")
            fh.write(f"  - registered but never used: {len(unused)}\n\n")
            fh.write("## Families\n\n| family | registered | used in missions |\n|---|---|---|\n")
            for p, c in fam.most_common():
                u = sum(1 for n in calls if n.startswith(p + "_"))
                if c >= 5 or u:
                    fh.write(f"| `{p}_` | {c} | {u} |\n")
            fh.write("\n## Most-called engine functions\n\n")
            fh.write("| name | calls | argc observed |\n|---|---|---|\n")
            for n, c in calls.most_common(60):
                if n in reg_names:
                    ar = ", ".join(str(k) for k, _v in sorted(args.get(n, {}).items())) or "?"
                    fh.write(f"| `{n}` | {c} | {ar} |\n")
            fh.write("\n## Script-side helpers (called but not registered)\n\n")
            fh.write("Defined in the shipped `.lua` files, not the engine.\n\n")
            for n in sorted(helpers, key=lambda x: -calls[x])[:120]:
                ar = ", ".join(str(k) for k, _v in sorted(args.get(n, {}).items())) or "?"
                fh.write(f"- `{n}` - {calls[n]} calls, argc {ar}\n")
        print(f"wrote {a.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
