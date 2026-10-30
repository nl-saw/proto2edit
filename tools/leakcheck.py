#!/usr/bin/env python3
"""Refuse to publish a tree that carries game content, capture data or host paths.

The manual audit that caught `branch-diffs.json` (360 decompiled Lua lines) and six recorder logs was luck with a
grep. This makes it a gate: publishing runs it over every commit in the range being replayed, so a blob that is
added and later deleted still fails, and so does one that only exists in an intermediate commit.

Three kinds of finding:

  path     a file whose name cannot be published: captures (*.in), session logs (*.log), game archives and scene
           files (*.rcf, *.rz, *.p3d, *.luac), theme payloads. Two exceptions that ship on purpose:
           `assets/debugmenu/` (the debug menu) and `overrides/` (the hand-written Lua pack, docs/26).
  content  a blob that looks like game data wherever it lives: a Lua chunk header, recorder magic, LuaDec's
           `DECOMPILER ERROR` markers, or decompiled generator names (`local l_0_52`). Documentation is exempt from
           the prose-level patterns because it legitimately discusses the API, but never from binary magic.
  host     a developer machine path: /mnt/..., /home/<user>/..., C:\\Users...

Usage:
    python3 tools/leakcheck.py --range refs/proto2/published-master..master   # what a publish would replay
    python3 tools/leakcheck.py --tree HEAD                                   # everything in one tree
    python3 tools/leakcheck.py --range 06c1137..master --all-commits          # ignore the default diff filter
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A file name that must never be published, with the one disclosed exception.
DENY_EXT = (".in", ".log", ".luac", ".rz", ".p3d", ".rcf", ".bin", ".dmp")
DENY_PREFIX = ("artifacts/inflate/", "artifacts/traces/")
ALLOW_PREFIX = ("assets/debugmenu/", "overrides/")
# the override pack is our own hand-written Lua; it is prose, not a chunk, so only the content
# patterns apply to it (binary magic and host paths are still checked).
DERIVED_LUA_PREFIX = "overrides/"

# Blob content that is game data, checked everywhere.
BINARY_MAGIC = (b"\x1bLua", b"PP23", b"PP24", b"PP25", b"PP26", b"PP27")

# The audits themselves contain the patterns they search for; a comment naming "/mnt/gamest2/..." is not a leak.
HOST_EXEMPT = {"tools/test_paths.py", "tools/leakcheck.py"}
# Same reason, for the prose patterns: this file defines them, so it cannot avoid containing them.
# script_edit.py names the marker in order to strip it from reconstructions (strip_decompiler_noise), which is the
# opposite of leaking: the string is a filter constant, never decompiled output.
CONTENT_EXEMPT = {"tools/leakcheck.py", "tools/script_edit.py"}

# Prose-level patterns: real decompiled script text, not API names in documentation.
LUA_TEXT = (re.compile(rb"-- DECOMPILER ERROR"),
            re.compile(rb"\blocal l_\d+_\d+\b"),
            re.compile(rb"\bfunction num\b"),
            re.compile(rb"^\s*-- params : \.\.\.", re.M))

# A machine path, not the bare mount point: the path audit's own regex contains "/mnt/" as a literal.
HOST_PATHS = (re.compile(rb"/mnt/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+"),
              re.compile(rb"/home/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
              re.compile(rb"/Users/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
              re.compile(rb"[A-Za-z]:\\\\Users\\\\[A-Za-z0-9_.-]+"))

MAX_BYTES = 2 * 1024 * 1024
SIZE_ALLOW = ("work/manifests/", "assets/debugmenu/", "docs/", "artifacts/")


def git(*args, binary=False):
    out = subprocess.run(["git", "-C", ROOT, *args], capture_output=True)
    if out.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {out.stderr.decode()[:300]}")
    return out.stdout if binary else out.stdout.decode("utf-8", "replace")


def is_doc(path):
    return path.startswith("docs/") or path.lower().endswith((".md", ".txt"))


def check_blob(path, data, findings):
    if path not in HOST_EXEMPT:
        for pat in HOST_PATHS:
            if pat.search(data):
                findings.append(("host", path, "developer path"))
                break
    # the magic check is for real chunks and captures: a source file that names the magic ("PP24") is not one
    head = data[:4096]
    if b"\x00" in head:
        for magic in BINARY_MAGIC:
            if magic in head:
                findings.append(("content", path, f"carries {magic!r}"))
                break
    if len(data) > MAX_BYTES and not path.startswith(SIZE_ALLOW):
        findings.append(("content", path, f"{len(data) / 1e6:.1f} MB"))
    if is_doc(path) or path in CONTENT_EXEMPT:
        return
    if path.startswith(DERIVED_LUA_PREFIX):
        # our reconstructions, not decompiler output: the prose patterns exist to catch generated text, so they
        # do not apply here, but the checks above (binary magic, host paths) still do.
        return
    for pat in LUA_TEXT:
        if pat.search(data):
            findings.append(("content", path, "decompiled Lua text"))
            return


def check_path(path, findings):
    if path.startswith(ALLOW_PREFIX):
        return
    if path.startswith(DENY_PREFIX) or path.endswith(DENY_EXT):
        findings.append(("path", path, "unpublishable file type"))


def blobs_in_range(rev_range):
    """Every path added or modified by any commit in the range.

    This used to be `git diff marker..master`, which only reports the *net* change between the endpoints. A file
    added by one replayed commit and deleted by a later one leaves no trace in that diff, so the gate would pass a
    commit that shipped it - and publishing replays commits, not just the tip. `git log --name-only` walks every
    commit instead.
    """
    out = git("log", "--no-renames", "--diff-filter=AM", "--name-only", "-z", "--pretty=format:", rev_range)
    return [p for p in out.split("\0") if p]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default=None)
    ap.add_argument("--tree", default=None, help="check a single tree instead of a range")
    a = ap.parse_args(argv)

    findings = []
    if a.tree:
        rev = git("rev-parse", a.tree).strip()
        listing = git("ls-tree", "-r", "-l", "-z", rev)
        for row in listing.split("\0"):
            if not row:
                continue
            meta, path = row.split("\t", 1)
            mode, typ, sha, size = meta.split()
            check_path(path, findings)
            data = git("cat-file", "blob", sha, binary=True)
            check_blob(path, data, findings)
        scope = f"tree {a.tree} ({rev[:9]})"
    else:
        rev_range = a.range
        if not rev_range:
            marker = "refs/proto2/published-master"
            has = subprocess.run(["git", "-C", ROOT, "rev-parse", "--verify", "-q", marker],
                                 capture_output=True).returncode == 0
            rev_range = f"{marker}..master" if has else "master"
        paths = blobs_in_range(rev_range)
        seen = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            check_path(path, findings)
            # every distinct version of that blob inside the range, not just the one at the tip
            blobs = set()
            for commit in git("rev-list", rev_range, "--", path).split():
                sha = subprocess.run(["git", "-C", ROOT, "rev-parse", f"{commit}:{path}"],
                                     capture_output=True)
                if sha.returncode == 0:
                    blobs.add(sha.stdout.decode().strip())
            for blob in blobs:
                check_blob(path, git("cat-file", "blob", blob, binary=True), findings)
        scope = f"range {rev_range} ({len(seen)} path(s))"

    print(f"leakcheck: {scope}")
    if findings:
        for kind, path, why in findings[:20]:
            print(f"  {kind:<8} {path:<50} {why}")
        print(f"REFUSED: {len(findings)} finding(s)")
        return 1
    print("clean: no game data, capture files or host paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
