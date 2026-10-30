#!/usr/bin/env python3
"""Project-wide path audit: one config source, no hard-coded install paths, every lookup resolvable.

Guards the class of bug found repeatedly in this repo: a tool keeping its own copy of the game path, a scratch file
in tmp/ being the only source of a lookup, or a module computing ROOT one level off. Run by ./proto2 check.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

fails = []


def ck(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def py_files():
    """Every tool except the audits: they necessarily contain the literals they search for."""
    for fn in sorted(os.listdir(TOOLS)):
        if fn.endswith(".py") and fn not in ("test_paths.py", "leakcheck.py"):
            yield os.path.join(TOOLS, fn)


# 1. no hard-coded game install paths outside config.py
GAME_LITERAL = re.compile(r"SteamLibrary/steamapps/common/Prototype")
offenders = []
for path in py_files():
    if os.path.basename(path) == "config.py":
        continue
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        if GAME_LITERAL.search(line):
            offenders.append(f"{os.path.relpath(path, ROOT)}:{i}")
ck("no hard-coded game path in tools", not offenders, ", ".join(offenders[:4]))

wrapper = open(os.path.join(ROOT, "proto2"), encoding="utf-8").read()
ck("no hard-coded game path in the wrapper", not GAME_LITERAL.search(wrapper))

# 2. one environment contract
bad_env = []
for path in py_files():
    if os.path.basename(path) == "config.py":
        continue
    text = open(path, encoding="utf-8").read()
    if "PROTO2_GAME_DIR" in text or re.search(r'os\.environ\.get\(\s*"PROTO2_GAME"', text):
        bad_env.append(os.path.basename(path))
ck("PROTO2_GAME is read only by config.py", not bad_env, ", ".join(bad_env))

# 3. no absolute host paths in code. The host this was developed on keeps the game under /mnt, so an audit that
# only knew /home and /tmp would have passed a leaked /mnt/gamest2/... path - which is how one shipped once.
ABS = re.compile(r'"(?:/(?:home|tmp|var|mnt|media|opt|srv|usr/local)/|[A-Za-z]:\\\\)')
abs_off = [f"{os.path.basename(p)}" for p in py_files()
           if ABS.search(open(p, encoding="utf-8").read())]
abs_off += [f"{os.path.basename(p)}:{i}" for p in [os.path.join(ROOT, "proto2")]
            for i, line in enumerate(open(p, encoding="utf-8"), 1) if ABS.search(line)]
ck("no absolute host paths in code", not abs_off, ", ".join(abs_off[:4]))

# 4. config keys agree with the committed example
import config  # noqa: E402
example = json.load(open(os.path.join(ROOT, "proto2.config.example.json")))
keys_missing = [k for k in config.DEFAULTS if k not in example]
ck("example documents every config key", not keys_missing, ", ".join(keys_missing))
ck("example has no unknown keys", not [k for k in example if k != "_readme" and k not in config.DEFAULTS],
   ", ".join(k for k in example if k != "_readme" and k not in config.DEFAULTS))

# 5. the configured game directory is real
gd = config.game_dir()
ck("game_dir exists", os.path.isdir(gd), gd)
ck("game_dir has the engine", os.path.exists(os.path.join(gd, "prototype2engine.dll")), gd)

# 6. archive list in mission_inventory matches disk
import mission_inventory as MI  # noqa: E402
missing = [a for a in MI.ARCHIVES if not os.path.exists(os.path.join(gd, a))]
ck("mission_inventory archives all exist", not missing, ", ".join(missing))
# an archive this project built and mounted for an experiment is ours, not a shipped one we forgot to
# inventory; the naming convention (`proto2_*.rcf`) is what tells them apart, and the archive is added to
# cementfiles.p3d by the experiment, never shipped.
extra = sorted(f for f in os.listdir(gd)
               if f.endswith(".rcf") and f not in MI.ARCHIVES and not f.startswith("proto2_"))
ck("no archive on disk is unknown to mission_inventory", not extra, ", ".join(extra))

# 7. every module resolves the same game directory and the same corpus root
import build_tag_names, check_asi, corpus_build, datainit, fsm_behaviour  # noqa: E402
import fsm_edit, luaapi, mission_graph, mission_gui, script_edit, stage_address, stage_fsm  # noqa: E402
same = {
    "script_edit": script_edit.GAME,
    "mission_gui": mission_gui.GAME,
    "corpus_build": corpus_build.GAME,
    "build_tag_names": build_tag_names.GAME,
    "check_asi": check_asi.GAME,
    "datainit": datainit.GAME_DEFAULT,
    "stage_address": stage_address.GAME,
    "stage_fsm": stage_fsm.GAME,
    "luaapi": os.path.dirname(luaapi.DEFAULT_DLL),
    "mission_graph": os.path.dirname(mission_graph.GAME_LOG),
    "fsm_behaviour": os.path.dirname(fsm_behaviour.DEFAULT_LOG),
}
disagree = {k: v for k, v in same.items() if os.path.normpath(v) != os.path.normpath(gd)}
ck("every module agrees on game_dir", not disagree, str(disagree))

corpora = {"script_edit": script_edit.CORPUS, "fsm_edit": fsm_edit.CORPUS}
expect = os.path.join(ROOT, "tmp", "corpus")
bad_corpus = {k: v for k, v in corpora.items() if os.path.normpath(v) != expect}
ck("corpus root is shared", not bad_corpus, str(bad_corpus))

# 8. module roots point at the repository, not a parent
bad_roots = [name for name, mod in (("script_edit", script_edit), ("mission_gui", mission_gui),
                                    ("corpus_build", corpus_build))
             if os.path.normpath(getattr(mod, "ROOT", ROOT)) != ROOT]
ck("module ROOT values are the repository root", not bad_roots, ", ".join(bad_roots))

# 9. runtime assets the tools need are where the code says they are
luadec = os.path.join(script_edit.LUADEC_DIR, "luadec")
luac = os.path.join(script_edit.LUADEC_DIR, "luac")
ck("LuaDec kit present", os.path.exists(luadec) and os.path.exists(luac), script_edit.LUADEC_DIR)
ck("LuaDec kit executable", os.access(luadec, os.X_OK) and os.access(luac, os.X_OK), "")
ck("debug-menu payload present", os.path.isdir(os.path.join(ROOT, "assets", "debugmenu")), "")
# optional by design: the decoded cache is not committed (it holds game text), so a fresh clone lacks it
cache = os.path.join(ROOT, "artifacts", "lua-cache.json")
print(f"INFO  runtime cache                 {'present' if os.path.exists(cache) else 'absent (optional; playthrough capture creates it)'}")
ck("bytecode store present", os.path.isdir(os.path.join(ROOT, "work", "bytecode")), "")
ck("recovery doc present", os.path.exists(os.path.join(ROOT, "docs", "23-lua-recovery-progress.md")), "")

# .gitignore has no inline comments: git reads everything after the pattern as part of the pattern, so a trailing
# "# note" silently disables the ignore. This bit twice (a decompiled-text cache was committed, and four
# game-content patterns were dead), so the syntax is checked rather than trusted.
gitignore = os.path.join(ROOT, ".gitignore")
bad = []
for n, line in enumerate(open(gitignore, encoding="utf-8", errors="replace").read().split("\n"), 1):
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    if re.search(r"\s#", line):
        bad.append(f"line {n}: {line.strip()[:50]}")
ck(".gitignore has no inline comments", not bad, "; ".join(bad))

if fails:
    print("paths: FAILED", ", ".join(fails))
    sys.exit(1)
print("paths: all lookups agree")
