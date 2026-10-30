#!/usr/bin/env python3
"""Keep work/mod reproducible.

Two kinds of file live in the staging directory, and confusing them is what made
"revert to clean" dangerous:

  unique   the debug menu payload. Not derivable from the game, so it is tracked
           in assets/debugmenu/ and seeded into work/mod on demand.
  staged   edits made by the tooling (mission definitions, text bibles, mission
           art). Regenerable, and safe to throw away.

`clean` therefore resets work/mod to the payload alone, keeping install state
(backup/, install-manifest.json) so revert still knows what it wrote.

    python3 tools/modstate.py seed  --mod work/mod --assets assets/debugmenu
    python3 tools/modstate.py clean --mod work/mod --assets assets/debugmenu
    python3 tools/modstate.py state --mod work/mod --assets assets/debugmenu
"""

import argparse
import os
import shutil
import sys

STATE_KEEP = {"install-manifest.json", "backup"}


def base_files(assets):
    out = []
    for dirpath, _dirs, names in os.walk(assets):
        for n in names:
            full = os.path.join(dirpath, n)
            out.append(os.path.relpath(full, assets).replace(os.sep, "/"))
    return sorted(out)


def seed(mod, assets, verbose=True):
    """Copy any missing payload file from assets into the staging directory.

    A payload file that already exists is left alone on purpose: work/mod can hold a deliberate edit. That
    also means a *fix* to assets/ does not reach the game through `install` alone, which is silent and has
    already cost a session - so a difference is reported rather than quietly installed.
    """
    copied = []
    stale = []
    for rel in base_files(assets):
        src = os.path.join(assets, rel.replace("/", os.sep))
        dst = os.path.join(mod, rel.replace("/", os.sep))
        if os.path.exists(dst):
            if not same_file(src, dst):
                stale.append(rel)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    if verbose and copied:
        print(f"seeded {len(copied)} payload file(s) from {assets}:")
        for rel in copied:
            print(f"  + {rel}")
    if verbose and stale:
        print(f"note: {len(stale)} staged payload file(s) differ from {os.path.basename(assets)} - "
              f"install will deploy the staged copy:")
        for rel in stale:
            print(f"  ! {rel}")
        print("  re-stage with ./proto2 clean, or delete that file under work/mod to take the payload version")
    return copied


def same_file(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _is_state(rel):
    """True for install state that clean must never touch."""
    return any(rel == k or rel.startswith(k + "/") for k in STATE_KEEP)


def clean(mod, assets, verbose=True, apply_pack=True):
    """Drop everything staged, keep the payload and the install state, then stage the override pack.

    The pack (overrides/) ships with the repository and makes the 43 records that no decompiler can regenerate
    editable, so a "clean" tree means payload + pack - not payload alone, which is what left records showing as
    READ-ONLY after a clean. Pass apply_pack=False for the bare payload state.

    Files are removed individually, never by directory: a payload file and a
    staged file can share a directory, and removing the directory wholesale
    would take the payload with it.
    """
    base = set(base_files(assets))
    removed = []
    if os.path.isdir(mod):
        for dirpath, _dirs, names in os.walk(mod):
            rel_dir = os.path.relpath(dirpath, mod).replace(os.sep, "/")
            for n in names:
                rel = n if rel_dir == "." else f"{rel_dir}/{n}"
                if rel in base or _is_state(rel):
                    continue
                os.remove(os.path.join(dirpath, n))
                removed.append(rel)
        # prune directories left empty, never the state directories
        for dirpath, _dirs, _names in os.walk(mod, topdown=False):
            rel_dir = os.path.relpath(dirpath, mod).replace(os.sep, "/")
            if rel_dir == "." or _is_state(rel_dir):
                continue
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
    if verbose:
        if removed:
            print(f"removed {len(removed)} staged item(s):")
            for rel in removed:
                print(f"  - {rel}")
        else:
            print("nothing staged; already clean")
    seed(mod, assets, verbose=verbose)
    if apply_pack:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pack = os.path.join(root, "overrides")
        if os.path.exists(os.path.join(pack, "manifest.json")):
            try:
                import overrides as OV
                staged, kept, _problems = OV.apply_pack(pack, verbose=False)
                if verbose:
                    print(f"override pack: {staged} record(s) staged, {kept} already in place")
            except SystemExit as exc:
                if verbose:
                    print(f"override pack: not applied ({exc})")
        elif verbose:
            print("override pack: not in this checkout; the 43 records stay bytecode")
    return removed


def state(mod, assets):
    base = set(base_files(assets))
    rows = []
    for dirpath, dirs, names in os.walk(mod):
        dirs[:] = [d for d in dirs if d != "backup"]
        for n in names:
            rel = os.path.relpath(os.path.join(dirpath, n), mod).replace(os.sep, "/")
            if rel == "install-manifest.json":
                kind = "state "
            elif rel in base:
                kind = "base  "
            else:
                kind = "staged"
            rows.append((kind, rel))
    for kind, rel in sorted(rows):
        print(f"  {kind}  {rel}")
    print(f"\n{len(rows)} file(s): "
          f"{sum(1 for k, _ in rows if k.startswith('base'))} payload, "
          f"{sum(1 for k, _ in rows if k == 'staged')} staged")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["seed", "clean", "state"])
    ap.add_argument("--no-pack", action="store_true",
                    help="clean without staging the override pack (bare payload state)")
    ap.add_argument("--mod", required=True)
    ap.add_argument("--assets", required=True)
    a = ap.parse_args(argv)

    if not os.path.isdir(a.assets):
        print(f"error: no payload directory {a.assets}", file=sys.stderr)
        return 2
    if a.cmd == "seed":
        seed(a.mod, a.assets)
        return 0
    if a.cmd == "clean":
        clean(a.mod, a.assets, apply_pack=not a.no_pack)
        return 0
    return state(a.mod, a.assets)


if __name__ == "__main__":
    sys.exit(main())
