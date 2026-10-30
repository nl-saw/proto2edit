#!/usr/bin/env python3
"""Copy a loose-file mod tree into the game directory, or revert it.

Loose-file loading is provided by the installed PrototypeFix ASI
(`PreferLooseFiles = true`): a file at the mirrored path wins over the same
path inside the .rcf archives. Nothing in the archives is modified.

A manifest (`<mod>/install-manifest.json`) records exactly which files were
written and which pre-existing files were displaced, so `--revert` restores the
directory to its prior state.

Usage:
  python3 tools/apply_mod.py --game "<dir>" --mod work/mod
  python3 tools/apply_mod.py --game "<dir>" --mod work/mod --revert
"""

import argparse
import json
import os
import shutil
import sys

MANIFEST = "install-manifest.json"


BACKUP_DIR = "backup"


def same_bytes(a, b):
    """True when two files hold identical content."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def walk_files(root):
    """Yield the mod's payload files, never its own backups or notes."""
    for dirpath, dirs, files in os.walk(root):
        # `backup/` holds the files a previous install displaced; it must not be
        # installed into the game directory.
        dirs[:] = [d for d in dirs if d != BACKUP_DIR]
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if rel == MANIFEST or rel.endswith(".md") or fn.startswith("."):
                continue
            yield rel, full


def install_one(game, state, rel, src):
    """Install (or refresh) ONE loose file, mirroring main()'s backup rules.

    Used when a revert has to be applied immediately, so a single record can go back to stock without
    re-installing the whole mod.
    """
    manifest_path = os.path.join(state, MANIFEST)
    backup_root = os.path.join(state, BACKUP_DIR)
    rel = rel.replace(os.sep, "/")
    man = {"game": game, "installed": {}}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path) as fh:
                man = json.load(fh)
        except (OSError, ValueError):
            pass
    man.setdefault("installed", {})
    target = os.path.join(game, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    previous = man["installed"].get(rel)
    if previous is not None:
        displaced = bool(previous.get("displaced"))
        note = "prior install state kept"
    elif os.path.exists(target) and same_bytes(target, src):
        # never back up our own output as if it were the shipped original
        backup = os.path.join(backup_root, rel)
        if os.path.exists(backup) and not same_bytes(backup, src):
            displaced = True
            note = "existing backup kept"
        else:
            displaced = False
            note = "already ours; no backup taken"
    elif os.path.exists(target):
        backup = os.path.join(backup_root, rel)
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        shutil.copy2(target, backup)
        displaced = True
        note = "displaced file backed up"
    else:
        displaced = False
        note = ""
    shutil.copy2(src, target)
    man["installed"][rel] = {"displaced": displaced}
    with open(manifest_path, "w") as fh:
        json.dump(man, fh, indent=1, sort_keys=True)
    return note


def uninstall_one(game, state, rel):
    """Remove ONE installed loose file, restoring a displaced original when there was one.

    Used when reverting a record leaves the staged file byte-identical to the shipped one: the override no
    longer does anything, so it should stop masking the archive.
    """
    manifest_path = os.path.join(state, MANIFEST)
    if not os.path.exists(manifest_path):
        return "no manifest; nothing to remove"
    rel = rel.replace(os.sep, "/")
    with open(manifest_path) as fh:
        man = json.load(fh)
    info = man["installed"].get(rel)
    if info is None:
        return "not installed"
    target = os.path.join(game, rel)
    removed = False
    if os.path.exists(target):
        os.remove(target)
        removed = True
    restored = False
    if info.get("displaced"):
        backup = os.path.join(state, BACKUP_DIR, rel)
        if os.path.exists(backup):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(backup, target)
            os.remove(backup)
            restored = True
    d = os.path.dirname(target)
    while d.startswith(game) and d != game:
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)
    del man["installed"][rel]
    if man["installed"]:
        with open(manifest_path, "w") as fh:
            json.dump(man, fh, indent=1, sort_keys=True)
    else:
        os.remove(manifest_path)
    if restored:
        return "override removed, displaced original restored"
    return "override removed" if removed else "override was not in the game directory"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True)
    ap.add_argument("--mod", required=True)
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--state", default=None,
                    help="directory holding install-manifest.json and backup/ "
                         "(default: the mod directory). Keeping state outside the "
                         "mod directory makes that directory disposable.")
    ap.add_argument("--debug-mode", choices=["full", "author", "archives", "none"], default="full",
                    help="full = debug menu + its Lua; author = menu + the author's "
                         "scripts but not ours; archives = menu without the reloadable "
                         "Lua layer; none = our files only (bisect aid)")
    a = ap.parse_args(argv)

    state = a.state or a.mod
    manifest_path = os.path.join(state, MANIFEST)
    backup_root = os.path.join(state, BACKUP_DIR)
    if a.revert:
        if not os.path.exists(manifest_path):
            print("no manifest; nothing to revert", file=sys.stderr)
            return 1
        with open(manifest_path) as fh:
            man = json.load(fh)
        for rel, info in man["installed"].items():
            target = os.path.join(a.game, rel)
            if os.path.exists(target):
                os.remove(target)
                print(f"removed  {rel}")
            if info.get("displaced"):
                backup = os.path.join(backup_root, rel)
                if os.path.exists(backup):
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(backup, target)
                    print(f"restored {rel}")
            # prune empty dirs
            d = os.path.dirname(target)
            while d.startswith(a.game) and d != a.game:
                try:
                    os.rmdir(d)
                except OSError:
                    break
                d = os.path.dirname(d)
        os.remove(manifest_path)
        print("reverted")
        return 0

    # Carry forward what the FIRST install saw, so re-installing never mistakes
    # our own earlier output for a vanilla file and revert stays accurate.
    previous = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path) as fh:
                previous = json.load(fh).get("installed", {})
        except (OSError, ValueError):
            previous = {}

    def is_debug(key):
        """True for the third-party debug-menu payload."""
        return (key in ("enable_debug.rcf", "cementfiles.p3d")
                or key.startswith("scripts/debug_")
                or key == "scripts/proto2_extra.lua")

    def is_debug_script(key):
        return key.startswith("scripts/debug_") or key == "scripts/proto2_extra.lua"

    def skip(key):
        if a.debug_mode == "full":
            return False
        if a.debug_mode == "none":
            return is_debug(key)
        if a.debug_mode == "author":
            return key == "scripts/proto2_extra.lua"   # author's scripts only
        return is_debug_script(key)          # archives only

    install_set = {}
    for rel, src in walk_files(a.mod):
        key = rel.replace(os.sep, "/")
        if skip(key):
            continue
        install_set[key] = (rel, src)

    installed = {}

    # Anything previously installed but no longer in the set must be undone.
    for key, info in previous.items():
        if key in install_set:
            continue
        target = os.path.join(a.game, key.replace("/", os.sep))
        if os.path.exists(target):
            os.remove(target)
            print(f"removed  {key}")
        if info.get("displaced"):
            backup = os.path.join(backup_root, key.replace("/", os.sep))
            if os.path.exists(backup):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(backup, target)
                print(f"restored {key}")

    for key, (rel, src) in install_set.items():
        target = os.path.join(a.game, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)

        if key in previous and not skip(key):
            displaced = previous[key].get("displaced", False)
            note = "  (already ours; prior state kept)"
        elif os.path.exists(target) and same_bytes(target, src):
            # Our own earlier output, with no manifest to say so. Backing this up
            # would replace the shipped original with our file and quietly break
            # revert, so never do that. A usable backup already on disk still
            # counts as displaced, so revert restores it instead of deleting.
            backup = os.path.join(backup_root, rel)
            if os.path.exists(backup) and not same_bytes(backup, src):
                displaced = True
                note = "  (already ours; existing backup kept)"
            else:
                displaced = False
                note = "  (already ours; no backup taken)"
                print(f"warning: {key} already matches our payload but there is "
                      f"no install record and no usable backup - the shipped "
                      f"original cannot be recovered", file=sys.stderr)
        elif os.path.exists(target):
            backup = os.path.join(backup_root, rel)
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(target, backup)
            displaced = True
            note = "  (displaced file backed up)"
        else:
            displaced = False
            note = ""

        shutil.copy2(src, target)
        installed[key] = {"displaced": displaced}
        print(f"installed {rel}{note}")

    with open(manifest_path, "w") as fh:
        json.dump({"game": a.game, "installed": installed}, fh, indent=1)
    print(f"\n{len(installed)} file(s) installed.")
    staged = sorted(p for p in (installed or ()) if "/missions/" in p.replace("\\", "/"))
    if staged:
        names = sorted({p.replace("\\", "/").split("/missions/")[1].split("/")[0] for p in staged})
        print(f"Launch the game and load a mission; {len(names)} overridden: {', '.join(names[:6])}"
              + (" …" if len(names) > 6 else ""))
    else:
        print("Launch the game; the payload is installed (no mission is overridden).")
    print(f"Revert with: python3 tools/apply_mod.py --game \"{a.game}\" --mod {a.mod} --revert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
