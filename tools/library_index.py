#!/usr/bin/env python3
"""The engine's built-in Lua library: locate it, decode it, index it.

WHERE IT LIVES. `art\\startup_fig.p3d.rz` inside `boot.rcf`. That file is not a mission figure: it is a
810-chunk Lua library (47,520 instructions) that the engine inflates as the game boots. It was found by
content: every Lua chunk the payload recorder saw inflated during a story_intro1 session was searched for
across the archives, and 143 of the 146 chunks that are not mission records were in this one file.

WHY IT MATTERS. Mission scripts call into it constantly - `mission_DefineRetryStates`, `CreateFramerCam`,
`SpawnSecureDoor`, `SpawnPatrolInstance`, `schedule`, the presentation/NIS template machinery behind
`sequenceTable`. Before this index those calls were black boxes: the native bindings were inventoried in
docs/21 but the Lua-level library was not.

DECODING. The chunks are packed exactly like mission record payloads - `7f 00 | u16 size | raw deflate`,
back-referencing the engine's 56-byte preset dictionary - so `fsm_luablob.engine_dict_bytes` reads them and
no capture is needed. Everything here is offline from the shipped archive.

    ./proto2 library build            decode the library into work/library/ (git-ignored, game content)
    ./proto2 library list [--top N]   modules with their top-level globals
    ./proto2 library grep PATTERN     search globals, and constant strings, across the library
    ./proto2 library show KEY         decompile one chunk (KEY = hash prefix, or an index from `list`)
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config                                    # noqa: E402
import fsm_luablob as FL                         # noqa: E402
import lua51 as L                                # noqa: E402
import lua_equiv as EQ                           # noqa: E402
import rcf                                       # noqa: E402
import rz                                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_DIR = os.path.join(ROOT, "work", "library")
INDEX = os.path.join(LIB_DIR, "index.json")
HOST_ARCHIVE = "boot.rcf"
HOST_ENTRY = "art\\startup_fig.p3d.rz"

# Two more Lua chunks live outside the library container: found by content while accounting for the 146
# chunks a playthrough captured (see docs/21). They are per-character scripts, not part of the library, but
# they are Lua the engine runs, so they are indexed alongside it and tagged with their source.
PACKAGE_ENTRIES = (
    ("boot.rcf", "art\\packages\\missions\\heli_bh_marine\\heli_bh_marine.p3d"),
    ("boot.rcf", "art\\packages\\missions\\heli_gunship_marine_core\\heli_gunship_marine_core.p3d"),
)


def _entry_bytes(archive_name, entry_name):
    """Unwrapped bytes for one archive entry, or None when the shipped file is absent."""
    path = os.path.join(config.game_dir(), archive_name)
    if not os.path.exists(path):
        return None
    archive = rcf.Rcf(path)
    try:
        for entry in archive.entries:
            name = getattr(entry, "name", "") or ""
            if isinstance(name, bytes):
                name = name.decode("latin-1", "replace")
            if name.lower() == entry_name.lower():
                data = archive.read(entry)
                return rz.unwrap(data) if data[:2] == b"RZ" else data
    finally:
        archive.close()
    return None


def _host_bytes():
    """The unwrapped bytes of the library container, straight from the shipped archive."""
    data = _entry_bytes(HOST_ARCHIVE, HOST_ENTRY)
    if data is None:
        raise SystemExit(f"{HOST_ENTRY} not found in {HOST_ARCHIVE}")
    return data


def _globals(chunk):
    """Names this chunk assigns to globals at its top level, in order."""
    root = L.load(chunk)
    out = []
    for token in EQ.tokens(root):
        if token[0] == "SETGLOBAL":
            name = root.constants[token[2]]
            out.append(name.decode("latin-1", "replace") if isinstance(name, bytes) else str(name))
    return out


def _strings(chunk, limit=400):
    root = L.load(chunk)
    out = []
    for proto in [root] + list(root.protos):
        for c in proto.constants:
            if isinstance(c, bytes):
                s = c.decode("latin-1", "replace")
                if len(s) >= 3 and s not in out:
                    out.append(s)
                    if len(out) >= limit:
                        return out
    return out


def _scan(data, source, seen, index):
    """Add every packed Lua chunk in `data` to the index (deduplicated by content)."""
    at = data.find(FL.MAGIC)
    while at != -1:
        out = FL.engine_dict_bytes(data[at:at + 2_000_000])
        if out and out[:4] == b"\x1bLua":
            digest = hashlib.sha256(out).hexdigest()
            if digest not in seen:
                seen.add(digest)
                name = digest[:16] + ".luac"
                open(os.path.join(LIB_DIR, name), "wb").write(out)
                root = L.load(out)
                protos = [root] + list(root.protos)
                index.append({
                    "key": digest[:16],
                    "source": source,
                    "offset": at,
                    "bytes": len(out),
                    "protos": len(protos) - 1,
                    "insns": sum(len(p.code) for p in protos),
                    "globals": _globals(out),
                    "strings": _strings(out, 60),
                })
        at = data.find(FL.MAGIC, at + 1)


def package_hosts():
    """Every other boot.rcf p3d entry that carries Lua: per-entity scripts (behemoth, heli, apc...).

    Mission figures are excluded on purpose - those are the mission corpus (docs/19), not this library.
    """
    import rcf as _rcf
    path = os.path.join(config.game_dir(), HOST_ARCHIVE)
    out = []
    if not os.path.exists(path):
        return out
    archive = _rcf.Rcf(path)
    try:
        for entry in archive.entries:
            name = getattr(entry, "name", "") or ""
            if isinstance(name, bytes):
                name = name.decode("latin-1", "replace")
            low = name.lower()
            if not low.endswith((".p3d", ".p3d.rz")):
                continue
            if low == HOST_ENTRY.lower() or low.startswith("art\\missions\\"):
                continue
            out.append(name)
    finally:
        archive.close()
    return out


def cmd_build(args):
    """Scan the container for packed Lua chunks and write them plus an index."""
    data = _host_bytes()
    os.makedirs(LIB_DIR, exist_ok=True)
    for name in os.listdir(LIB_DIR):
        if name.endswith(".luac"):
            os.remove(os.path.join(LIB_DIR, name))
    seen, index = set(), []
    _scan(data, "startup_fig", seen, index)
    for archive_name, entry_name in PACKAGE_ENTRIES:
        blob = _entry_bytes(archive_name, entry_name)
        if blob:
            _scan(blob, entry_name.split("\\")[-1], seen, index)
    if not args.no_packages:
        for entry_name in package_hosts():
            blob = _entry_bytes(HOST_ARCHIVE, entry_name)
            if blob:
                _scan(blob, entry_name.split("\\")[-1], seen, index)
    index.sort(key=lambda d: -d["insns"])
    for i, row in enumerate(index):
        row["index"] = i
    json.dump(index, open(INDEX, "w"), indent=1)
    print(f"decoded {len(index)} chunks, {sum(r['insns'] for r in index)} instructions")
    print(f"host      {HOST_ARCHIVE}!{HOST_ENTRY}")
    by_source = {}
    for row in index:
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1
    for source, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {source:46} {n:>4} chunks")
    print(f"written   {os.path.relpath(LIB_DIR, ROOT)}/ ({len(index)} .luac + index.json)")
    print(f"next      ./proto2 library list --top 15")
    return 0


def _load_index():
    if not os.path.exists(INDEX):
        raise SystemExit("no index yet: run ./proto2 library build")
    return json.load(open(INDEX))


def cmd_list(args):
    index = _load_index()
    rows = index
    if args.filter:
        needle = args.filter.lower()
        rows = [r for r in rows if any(needle in g.lower() for g in r["globals"])
                or any(needle in s.lower() for s in r["strings"])]
    print(f"{'#':>4} {'ins':>6} {'protos':>6}  {'key':16}  globals")
    for r in rows[:args.top]:
        globs = ", ".join(r["globals"][:4]) or "-"
        print(f"{r['index']:>4} {r['insns']:>6} {r['protos']:>6}  {r['key']:16}  {globs}")
    if len(rows) > args.top:
        print(f"... {len(rows) - args.top} more (--top N)")
    return 0


def cmd_grep(args):
    index = _load_index()
    needle = args.pattern.lower()
    hits = 0
    for r in index:
        names = [g for g in r["globals"] if needle in g.lower()]
        strs = [s for s in r["strings"] if needle in s.lower()]
        if names or strs:
            hits += 1
            print(f"#{r['index']}  {r['key']}  {r['insns']} ins, {r['protos']} protos")
            if names:
                print(f"    globals: {', '.join(names[:8])}")
            if strs:
                print(f"    strings: {', '.join(strs[:6])}")
    if not hits:
        print(f"no chunk mentions {args.pattern!r}")
    return 0 if hits else 1


def cmd_usage(args):
    """Which library functions shipped missions actually call, and which chunk defines each."""
    import collections
    import re
    import recovery_report as R
    import script_edit as SE

    index = _load_index()
    owner = {}
    for row in index:
        for name in row["globals"]:
            owner.setdefault(name, row["index"])
    if not owner:
        raise SystemExit("index has no globals: run ./proto2 library build")
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(owner)) + r")\s*\(")
    def arity(body, open_idx):
        """Arguments at the call site starting at open_idx (the '('), by counting top-level commas."""
        depth, args, i, start = 0, 0, open_idx, open_idx + 1
        while i < len(body):
            ch = body[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    return args + (1 if body[start:i].strip() else 0)
            elif ch == "," and depth == 1:
                args += 1
            i += 1
        return None

    counts, missions = collections.Counter(), collections.defaultdict(set)
    arities = collections.defaultdict(collections.Counter)
    records = 0
    for mission, variant, _model in R.missions():
        got = SE.open_side(mission, variant, "working")
        if not got:
            continue
        for _a, text, _h, _x in SE._decoded(got[3]):
            if text is None:
                continue
            records += 1
            body = text if isinstance(text, str) else text.decode("latin-1")
            for m in pattern.finditer(body):
                counts[m.group(1)] += 1
                missions[m.group(1)].add(mission)
                n = arity(body, m.end() - 1)
                if n is not None:
                    arities[m.group(1)][n] += 1
    print(f"{records} records scanned, {len(owner)} library globals, "
          f"{len(counts)} called by name ({sum(counts.values())} call sites)")
    print(f"{'calls':>6}  {'missions':>8}  {'chunk':>6}  {'args':>10}  name")
    for name, n in counts.most_common(args.top):
        seen = arities.get(name) or {}
        spread = "-" if not seen else (
            str(max(seen)) if len(seen) == 1 else f"{min(seen)}-{max(seen)} ({max(seen, key=seen.get)})")
        print(f"{n:>6}  {len(missions[name]):>8}  #{owner[name]:<5}  {spread:>10}  {name}")
    out = os.path.join(LIB_DIR, "usage.json")
    json.dump({"counts": dict(counts),
               "chunk": {n: owner[n] for n in counts},
               "arities": {n: dict(arities.get(n, {})) for n in counts}},
              open(out, "w"), indent=1)
    print(f"written {os.path.relpath(out, ROOT)}")
    return 0


def cmd_show(args):
    """Decompile one chunk with the project's pinned LuaDec, or print its listing if that fails."""
    import script_edit as SE
    index = _load_index()
    key = str(args.key)
    row = None
    if key.isdigit():
        row = next((r for r in index if r["index"] == int(key)), None)
    if row is None:
        row = next((r for r in index if r["key"].startswith(key)), None)
    if row is None:
        raise SystemExit(f"no chunk matches {key!r} (use an index from `list`, or a key prefix)")
    chunk = open(os.path.join(LIB_DIR, row["key"] + ".luac"), "rb").read()
    print(f"-- {row['key']}  {row['insns']} instructions, {row['protos']} prototypes, "
          f"{row['bytes']} bytes, at offset {row['offset']} of the container")
    if row["globals"]:
        print(f"-- defines: {', '.join(row['globals'])}")
    text, note = SE.luadec_source(chunk)
    if text:
        print(text)
    else:
        print(f"-- decompilation failed ({note}); listing follows")
        import io
        buf = io.StringIO()
        L.listing(L.load(chunk), out=buf, show_code=True)
        print(buf.getvalue())
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", help="decode the library out of boot.rcf")
    p_build.add_argument("--no-packages", action="store_true",
                         help="library container only, skip the per-entity package scripts")
    p_build.set_defaults(func=cmd_build)
    p_list = sub.add_parser("list", help="modules and their globals")
    p_list.add_argument("--top", type=int, default=25)
    p_list.add_argument("--filter", default=None, help="only chunks mentioning this text")
    p_list.set_defaults(func=cmd_list)
    p_grep = sub.add_parser("grep", help="search globals and constant strings")
    p_grep.add_argument("pattern")
    p_grep.set_defaults(func=cmd_grep)
    p_usage = sub.add_parser("usage", help="library functions shipped missions call")
    p_usage.add_argument("--top", type=int, default=20)
    p_usage.set_defaults(func=cmd_usage)
    p_show = sub.add_parser("show", help="decompile one chunk")
    p_show.add_argument("key")
    p_show.set_defaults(func=cmd_show)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
