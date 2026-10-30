#!/usr/bin/env python3
"""Snapshot and restore the staged mod (work/mod + the install manifest).

A bulk conversion touches many mission files at once. This makes the pre-conversion state restorable in one
command, independent of git and of what the conversions do:

    python3 tools/mod_snapshot.py create [--label NAME]
    python3 tools/mod_snapshot.py list
    python3 tools/mod_snapshot.py restore <archive> [--install]

The archive holds work/mod (the staging directory, git-ignored) and work/state/install-manifest.json, plus a
JSON index of every file's sha256 so a restore can be verified rather than assumed.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import config
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "work", "mod")
STATE = os.path.join(ROOT, "work", "state")
MANIFEST = os.path.join(STATE, "install-manifest.json")
BACKUPS = os.path.join(ROOT, "artifacts", "backups")


def index(root):
    out = {}
    for dirpath, _d, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            out[rel] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def cmd_create(a):
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = f"-{a.label}" if a.label else ""
    path = os.path.join(BACKUPS, f"mod-{stamp}{label}.tar.gz")
    meta = {"created": stamp, "mod_files": index(MOD),
            "manifest": index(STATE).get("install-manifest.json")}
    if os.path.exists(MANIFEST):
        meta["manifest_sha256"] = hashlib.sha256(open(MANIFEST, "rb").read()).hexdigest()
    with tarfile.open(path, "w:gz") as tf:
        tf.add(MOD, arcname="mod")
        if os.path.exists(MANIFEST):
            tf.add(MANIFEST, arcname="manifest.json")
        jp = os.path.join(BACKUPS, f"mod-{stamp}{label}.json")
        with open(jp, "w") as fh:
            json.dump(meta, fh, indent=1, sort_keys=True)
        tf.add(jp, arcname="index.json")
    print(f"snapshot: {os.path.relpath(path, ROOT)}")
    print(f"  mod files : {len(meta['mod_files'])}")
    print(f"  manifest  : {'yes' if meta.get('manifest_sha256') else 'none'}")
    return 0


def cmd_list(a):
    if not os.path.isdir(BACKUPS):
        print("no snapshots")
        return 0
    for fn in sorted(os.listdir(BACKUPS)):
        if not fn.endswith(".tar.gz"):
            continue
        p = os.path.join(BACKUPS, fn)
        print(f"  {fn:<38} {os.path.getsize(p):>9} bytes")
    return 0


def cmd_restore(a):
    path = a.archive
    if not os.path.isabs(path):
        path = os.path.join(BACKUPS, path)
    if not os.path.exists(path):
        raise SystemExit(f"no such snapshot: {path}")
    tmp = os.path.join(ROOT, "tmp", "restore")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    with tarfile.open(path, "r:gz") as tf:
        tf.extractall(tmp)
    new_mod = os.path.join(tmp, "mod")
    if os.path.isdir(MOD):
        shutil.rmtree(MOD)
    shutil.copytree(new_mod, MOD)
    man = os.path.join(tmp, "manifest.json")
    if os.path.exists(man):
        os.makedirs(STATE, exist_ok=True)
        shutil.copy2(man, MANIFEST)
    idx = json.load(open(os.path.join(tmp, "index.json")))
    got = index(MOD)
    bad = [k for k, v in idx["mod_files"].items() if got.get(k) != v]
    print(f"restored {len(idx['mod_files'])} files, mismatches: {len(bad)}")
    if bad:
        print("  MISMATCH:", bad[:5])
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    if a.install:
        rc = subprocess.call([sys.executable, os.path.join(ROOT, "tools", "apply_mod.py"),
                              "--game", config.game_dir(),
                              "--mod", MOD, "--state", STATE])
        print(f"install exit: {rc}")
    else:
        print("run ./proto2 install to put the restored state back in the game")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--label", default=""); c.set_defaults(func=cmd_create)
    l = sub.add_parser("list"); l.set_defaults(func=cmd_list)
    r = sub.add_parser("restore"); r.add_argument("archive"); r.add_argument("--install", action="store_true")
    r.set_defaults(func=cmd_restore)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
