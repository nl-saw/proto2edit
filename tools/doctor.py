#!/usr/bin/env python3
"""Check this machine can run the tooling, and say exactly what to do for anything missing.

    ./proto2 doctor

FAIL means the basic workflow cannot run. WARN means an optional capability is missing (decompilation, capture,
rebuilding ASIs) or a game-side prerequisite for loose-file overrides is not in place.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import config  # noqa: E402

fails, warns = [], []


def line(state, name, detail="", hint=""):
    print(f"{state:<4}  {name:<34} {detail}")
    if state == "FAIL":
        fails.append((name, hint))
    elif state == "WARN":
        warns.append((name, hint))


def main():
    print(f"repo    {ROOT}")
    print(f"python  {sys.version.split()[0]}")
    print()

    v = sys.version_info
    line("OK" if v >= (3, 8) else "FAIL", "python >= 3.8", f"{v.major}.{v.minor}",
         "install a newer python3")
    try:
        import tkinter  # noqa: F401
        line("OK", "tkinter (GUI)", "available")
    except Exception as exc:
        line("FAIL", "tkinter (GUI)", type(exc).__name__, "install python3-tk (Debian/Ubuntu: apt install python3-tk)")

    gd = config.game_dir()
    engine = os.path.join(gd, "prototype2engine.dll")
    if os.path.isdir(gd) and os.path.exists(engine):
        archives = [f for f in os.listdir(gd) if f.endswith(".rcf")]
        line("OK", "game directory", f"{len(archives)} archive(s)")
    else:
        line("FAIL", "game directory", gd,
             'set it with: ./proto2 config set game_dir "/path/to/Prototype 2"')

    pf = os.path.join(gd, "prototype_fix.asi")
    ini = os.path.join(gd, "prototype_fix.ini")
    loose = ""
    if os.path.exists(ini):
        for ln in open(ini, errors="replace"):
            if "preferloosefiles" in ln.lower() and "=" in ln:
                loose = ln.split("=", 1)[1].strip()
    if os.path.exists(pf) and loose.lower() == "true":
        line("OK", "PrototypeFix (loose files)", f"PreferLooseFiles = {loose}")
    else:
        line("WARN", "PrototypeFix (loose files)",
             "not detected" if not os.path.exists(pf) else f"PreferLooseFiles = {loose or 'missing'}",
             "install PrototypeFix.asi + prototype_fix.ini (PreferLooseFiles = true) in the game dir; "
             "without it the game ignores every file this tooling installs (docs/12)")
    line("OK" if os.path.exists(os.path.join(gd, "binkw32.dll")) else "WARN",
         "ASI loader (binkw32.dll)",
         "present" if os.path.exists(os.path.join(gd, "binkw32.dll")) else "missing",
         "install Ultimate-ASI-Loader as binkw32.dll so .asi plugins load")

    import importlib.util
    luadec = os.path.join(ROOT, "build", "luadec-p2", "bin", "luadec")
    if os.path.exists(luadec):
        line("OK", "LuaDec kit", os.path.relpath(luadec, ROOT))
    else:
        line("WARN", "LuaDec kit", "not built",
             "needed to decompile bytecode records: bash tools/build_luadec.sh (clones upstream once)")

    corpus = os.path.join(ROOT, "tmp", "corpus")
    n = 0
    if os.path.isdir(corpus):
        for dp, _d, names in os.walk(corpus):
            if "fig.p3d.rz" in names:
                n += 1
    if n:
        line("OK", "extracted corpus", f"{n} mission file(s)")
    else:
        line("WARN", "extracted corpus", "not built",
             "corpus-wide tools need it: ./proto2 corpus (rebuilds from the archives)")

    tagged = os.path.exists(os.path.join(ROOT, "artifacts", "tag-names.json"))
    line("OK" if tagged else "WARN", "tag-name table", "present" if tagged else "missing",
         "refreshed by ./proto2 corpus")

    installed = 0
    manifest = os.path.join(ROOT, "work", "state", "install-manifest.json")
    if os.path.exists(manifest):
        import json
        try:
            installed = len(json.load(open(manifest)).get("installed", {}))
        except Exception:
            pass
    debug_installed = 0
    if installed:
        import json
        try:
            debug_installed = sum(1 for p in json.load(open(manifest)).get("installed", {})
                                  if "debug" in p or p == "cementfiles.p3d")
        except Exception:
            pass
    line("OK" if debug_installed else "WARN", "debug menu installed", f"{debug_installed} file(s)",
         "install it with: ./proto2 install - the in-game debug page is the quickest way to check a change")

    rec = None
    for ver in ("6", "5", "4", "3", "2"):
        if os.path.exists(os.path.join(gd, f"proto2_inflate{ver}.asi")):
            rec = f"v{ver}"
            break
    line("OK" if rec else "WARN", "payload recorder", rec or "not installed",
         "optional: ./proto2 capture on v6 (records what the engine decodes during play)")

    gcc = shutil.which("i686-w64-mingw32-gcc")
    line("OK" if gcc else "WARN", "mingw cross compiler", gcc or "not found",
         "only needed to rebuild the .asi probes: apt install gcc-mingw-w64-i686")

    print()
    if fails:
        print(f"doctor: {len(fails)} blocking problem(s)")
        for name, hint in fails:
            if hint:
                print(f"  - {name}: {hint}")
        return 1
    print(f"doctor: ready ({len(warns)} warning(s))")
    for name, hint in warns:
        if hint:
            print(f"  - {name}: {hint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
