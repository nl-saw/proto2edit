#!/usr/bin/env python3
"""Edit a mission's FSM Lua by mission name, and stage it as a loose mod file.

`mission_fsm.py` works on a path you already have. This tool starts one step
earlier and ends one step later:

    archive entry  art/missions/<mission>/<variant>/fig.p3d.rz
      -> unwrap RZ
      -> patch the embedded Lua (any length; sizes and chunk tree reflowed)
      -> re-wrap RZ
      -> write work/mod/art/missions/<mission>/<variant>/fig.p3d.rz

`./proto2 install` copies `work/mod` over the game directory as loose files, which
`PreferLooseFiles = true` (PrototypeFix) makes win over the archives.

This is where a mission's *behaviour* lives: conditions, actions and track groups
are short Lua snippets inside the FSM graph. What it is **not** is the FSM's node
structure - you edit what an existing node does, not add states or transitions.

Usage:
  ./proto2 fsm --mission story_intro1                 # list its fragments
  ./proto2 fsm --mission story_intro1 --show 0x15cbc  # one fragment in full
  ./proto2 fsm --missions                             # which missions have Lua

  # rewrite text anywhere in the mission (may change length)
  ./proto2 fsm --mission story_intro1 \
      --replace 'INTRO1_MOVE_TUTORIAL4' --with 'INTRO1_MOVE_TUTORIAL4_NEW'

  # overwrite exactly at an offset (same length, no reflow)
  ./proto2 fsm --mission story_intro1 --at 0x15cbc --text 'ShowObjectiveMessage("$X")'

  --replace/--with are repeatable and pair up by position. Every edit is resolved
  against the pristine archive bytes first, so applying several at once is safe.
  Add --dry-run to check an edit without staging it, --revert to drop staged files.

Script extraction (docs/19 HANDOFF 3): a record payload is Lua source either as plain
text or as `7f 00 | u16 size | raw deflate`. This decodes both and writes the mission's
script out in file order so it can be read and searched.

  ./proto2 fsm --mission story_a1 --scripts            # -> tmp/scripts/story_a1-active.lua

Node-level editing (docs/19 HANDOFF 3): list the containers, then add or remove one
item inside one of them. This is the difference between changing what a node does and
extending the graph - an item is a condition, an action or a track entry.

  ./proto2 fsm --mission story_a1 --containers               # offsets + editability
  ./proto2 fsm --mission story_a1 --containers --only conditions
  ./proto2 fsm --mission story_a1 --add-item 0x210c --index 1
  ./proto2 fsm --mission story_a1 --del-item 0x210c --index 1

Both refuse unless the container's body parses as an item list whose item count equals
its stated `count`, and both prove the result by an independent byte splice before
anything is staged. --add-item duplicates an existing item, so the new item's layout is
one the engine has already accepted.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import mission_fsm as M  # noqa: E402
from rcf import Rcf  # noqa: E402
from rz import unwrap, wrap  # noqa: E402
import config  # noqa: E402

GAME = config.game_dir()
MOD = os.path.join(ROOT, "work", "mod")
ARCHIVES = ["boot.rcf", "art.rcf"]
REL = "art/missions/{m}/{v}/fig.p3d.rz"


def entries():
    """Yield (mission, variant, archive_path, entry, raw_bytes) for every fig.p3d.rz."""
    for arc in ARCHIVES:
        path = os.path.join(GAME, arc)
        if not os.path.exists(path):
            continue
        rcf = Rcf(path)
        try:
            for e in rcf.entries:
                p = e.path.replace("\\", "/")
                if not p.lower().startswith("art/missions/") or \
                        not p.lower().endswith("/fig.p3d.rz"):
                    continue
                parts = p.split("/")
                if len(parts) != 5:
                    continue
                rcf._fh.seek(e.offset)
                yield parts[2], parts[3], path, e, rcf._fh.read(e.size)
        finally:
            rcf._fh.close()


def find_entry(mission, variant):
    rel = REL.format(m=mission, v=variant).lower()
    for m, v, arc, e, blob in entries():
        if e.path.replace("\\", "/").lower() == rel:
            return arc, e, blob
    return None, None, None


def cmd_missions(a):
    rows = []
    for m, v, arc, e, blob in entries():
        if e.size < 256:            # 188-byte stubs carry no Lua
            continue
        inner = unwrap(blob) if blob[:2] == b"RZ" else blob
        frs = M.fragments(inner)
        rows.append((sum(len(t) for _, t in frs), len(frs), len(inner), m, v, arc))
    rows.sort(reverse=True)
    print(f"{'text':>7} {'frags':>6} {'fig.p3d':>9}  {'mission':<26} {'var':<7} archive")
    for text, nf, size, m, v, arc in rows:
        print(f"{text:>7} {nf:>6} {size:>9}  {m:<26} {v:<7} {arc}")
    print(f"\n{len(rows)} mission/variant entries with fig.p3d content "
          f"({sum(r[0] for r in rows)} bytes of Lua total)")
    return 0


def cmd_list(a, inner, hdr):
    frs = M.fragments(inner, unknown=a.unknown)
    print(f"{hdr}: {len(inner)} bytes; {len(frs)} fragment(s), "
          f"{sum(len(t) for _, t in frs)} bytes of text\n")
    for off, text in frs:
        if a.show is not None:
            want = int(a.show, 16) if a.show.lower().startswith("0x") else int(a.show)
            if off != want:
                continue
        print(f"=== {off:#07x}  ({len(text)} bytes)")
        print(text.replace("\r", "").strip("\n"))
        print()
    return 0


def parse_off(s):
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def resolve(inner, a):
    """Turn the CLI edits into [(offset, old, new)] against the pristine bytes."""
    edits = []
    if (a.at is None) != (a.text is None):
        raise SystemExit("error: --at and --text must be given together")
    if a.at is not None:
        off = parse_off(a.at)
        old = inner[off:off + len(a.text)]
        if len(old) != len(a.text):
            raise SystemExit(f"error: --at patch needs {len(old)} bytes, "
                             f"got {len(a.text)} (use --replace to change length)")
        edits.append((off, old, a.text.encode("latin-1")))
    reps, withs = (a.replace or []), (a.with_ or [])
    if len(reps) != len(withs):
        raise SystemExit(f"error: {len(reps)} --replace but {len(withs)} --with")
    for old_s, new_s in zip(reps, withs):
        old, new = old_s.encode("latin-1"), new_s.encode("latin-1")
        hits = []
        start = 0
        while True:
            h = inner.find(old, start)
            if h < 0:
                break
            hits.append(h)
            start = h + 1
        if len(hits) != 1:
            msg = [f"error: {old_s!r} occurs {len(hits)} times; be more specific "
                   f"or use --at"]
            msg += [f"   {h:#07x}" for h in hits[:10]]
            raise SystemExit("\n".join(msg))
        edits.append((hits[0], old, new))
    # Apply from the end backwards so every pristine offset stays valid.
    edits.sort(key=lambda e: e[0], reverse=True)
    for (o1, _, _), (o2, _, _) in zip(edits, edits[1:]):
        if o1 == o2:
            raise SystemExit(f"error: two edits both target {o1:#07x}")
    return edits


def _pristine(inner):
    """Write the pristine bytes where the path-based fsm_write helpers can read them."""
    import fsm_write as W
    tmp = os.path.join(ROOT, "tmp", "fsmcheck")
    os.makedirs(tmp, exist_ok=True)
    src = os.path.join(tmp, "pristine.p3d")
    with open(src, "wb") as f:
        f.write(inner)
    return W, src, os.path.join(tmp, "items.p3d")


def _scripts(a, inner):
    """Decode every record payload and write the mission's Lua out in file order."""
    import fsm_graph as F
    import fsm_items as I
    import fsm_luablob as L
    W, src, _ = _pristine(inner)
    leaf, _ = F.payload(inner)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
    items = L.payloads(leaf, nodes)
    window = b""
    _wpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "artifacts", "lua-window.bin")
    if os.path.exists(_wpath):
        window = open(_wpath, "rb").read()
    plain, left, results, skipped = L.decode_all(items, window)
    out = a.out or os.path.join(ROOT, "tmp", "scripts",
                                f"{a.mission}-{a.variant}.lua")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(plain)
    print(f"records {len(items)}  packed {len(results) + len(left)}  "
          f"decoded {len(results)}  undecoded {len(left)}  binary/skipped {skipped}")
    print(f"wrote {out} ({len(plain)} bytes of Lua source)")
    if left:
        print(f"note: {len(left)} payload(s) need a dictionary not stored in the file; "
              f"they are omitted rather than guessed")
    return 0


def _containers(inner, only):
    """List containers of the pristine mission file with their editability."""
    W, src, _ = _pristine(inner)
    return W.list_containers(src, only)


def _item_stage(a, inner, hdr):
    """Add or remove one item, then stage the result through the same gate as a text edit."""
    W, src, dst = _pristine(inner)
    off = int(a.add_item if a.add_item is not None else a.del_item, 0)
    rc = W.item_edit(src, dst, off, a.index, a.add_item is not None)
    if rc != 0:
        return rc
    with open(dst, "rb") as f:
        out = f.read()

    # Re-check independently of the writer: the chunk tree must still tile, the leaf
    # must still be found, and no Lua fragment may appear or disappear.
    chunks = []
    M._walk(out, 0, len(out), chunks)
    if not chunks or chunks[0][0] + chunks[0][2] != len(out):
        print("error: chunk tree does not tile after the item edit", file=sys.stderr)
        return 3
    import fsm_graph as F
    F.payload(out)
    before, after = len(M.fragments(inner)), len(M.fragments(out))
    if before != after:
        print(f"error: Lua fragment count changed {before} -> {after}", file=sys.stderr)
        return 3
    print(f"ok: {len(inner)} -> {len(out)} bytes; {after} fragment(s) unchanged; "
          f"tree tiles ({len(chunks)} chunks)")

    dest = os.path.join(MOD, "art", "missions", a.mission, a.variant, "fig.p3d.rz")
    if a.dry_run:
        print(f"dry run: would stage {dest} ({len(wrap(out))} bytes RZ)")
        return 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    rz = wrap(out)
    with open(dest, "wb") as f:
        f.write(rz)
    print(f"staged {dest} ({len(rz)} bytes, RZ)")
    print("next: ./proto2 install  (then ./proto2 play)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mission", default=None)
    ap.add_argument("--variant", default="active", choices=["active", "open"])
    ap.add_argument("--missions", action="store_true",
                    help="list every mission that has FSM Lua")
    ap.add_argument("--show", default=None, metavar="OFF",
                    help="print only the fragment at this offset (hex or decimal)")
    ap.add_argument("--unknown", action="store_true",
                    help="also show runs that do not look like Lua")
    ap.add_argument("--replace", default=None, action="append", metavar="OLD")
    ap.add_argument("--with", dest="with_", default=None, action="append",
                    metavar="NEW")
    ap.add_argument("--at", default=None, metavar="OFF")
    ap.add_argument("--text", default=None)
    ap.add_argument("--scripts", action="store_true",
                    help="decode every Lua payload and write it out")
    ap.add_argument("--out", default=None, metavar="PATH",
                    help="with --scripts, where to write (default tmp/scripts/<m>-<v>.lua)")
    ap.add_argument("--containers", action="store_true",
                    help="list containers: offset, stated count, whether editable")
    ap.add_argument("--only", default=None, metavar="NAME",
                    help="with --containers, show only this container name")
    ap.add_argument("--add-item", default=None, metavar="OFF", dest="add_item",
                    help="duplicate one item inside the container at this offset")
    ap.add_argument("--del-item", default=None, metavar="OFF", dest="del_item",
                    help="remove one item inside the container at this offset")
    ap.add_argument("--index", type=int, default=None,
                    help="which item (default: last for --add-item, first for --del-item)")
    ap.add_argument("--stage", action="store_true",
                    help="install the file unchanged (an override control run)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args(argv)

    if a.revert:
        base = os.path.join(MOD, "art", "missions")
        n = 0
        for dirpath, _, names in os.walk(base):
            for nm in names:
                if nm.startswith("fig.p3d"):
                    os.remove(os.path.join(dirpath, nm))
                    n += 1
        print(f"removed {n} staged fig.p3d file(s)")
        return 0

    if a.missions:
        return cmd_missions(a)
    if not a.mission:
        ap.error("--mission is required (or --missions / --revert)")

    arc, entry, blob = find_entry(a.mission, a.variant)
    if blob is None:
        print(f"error: no {REL.format(m=a.mission, v=a.variant)} in {ARCHIVES}",
              file=sys.stderr)
        return 1
    wrapped = blob[:2] == b"RZ"
    inner = unwrap(blob) if wrapped else blob
    hdr = f"{entry.path}  ({len(blob)} bytes in {arc})"

    if a.scripts:
        return _scripts(a, inner)
    if a.containers:
        return _containers(inner, a.only)
    if a.add_item is not None or a.del_item is not None:
        return _item_stage(a, inner, hdr)

    if not a.stage and a.at is None and not a.replace:
        return cmd_list(a, inner, hdr)

    if a.stage and (a.at is not None or a.replace):
        ap.error("--stage means 'install the file unchanged'; give it no edits")
    edits = [] if a.stage else resolve(inner, a)
    out = inner
    for off, old, new in edits:          # already sorted high offset first
        try:
            # Record-aware: updates the record size field and padding, not just the
            # length. mission_fsm.patch_bytes alone produced files 2 bytes short.
            import fsm_write as _W
            out = _W.edit_text_payload(out, old, new)
        except ValueError as e:
            print(f"error: {e} (editing {old!r} at {off:#07x})", file=sys.stderr)
            return 3
        print(f"  {off:#07x}  {len(old)} -> {len(new)} bytes  {old[:60]!r}")

    # Verify the result, not the intent: tree tiles, fragment count preserved,
    # every new text present, and the size delta is exactly what was asked for.
    # The plain-length expectation does not hold for record payload edits: the record
    # body is padded to 4 bytes, so the real delta differs by up to 3. The record-aware
    # path already proved the result by re-parsing and re-emitting it, so skip this only
    # when that path was used.
    record_aware = bool(edits)
    want = len(inner) + sum(len(n) - len(o) for _, o, n in edits)
    if not record_aware and len(out) != want:
        print(f"error: size {len(out)} != expected {want}", file=sys.stderr)
        return 3
    chunks = []
    M._walk(out, 0, len(out), chunks)
    if not chunks or chunks[0][0] + chunks[0][2] != len(out):
        print("error: chunk tree does not tile", file=sys.stderr)
        return 3
    before, after = M.fragments(inner), M.fragments(out)
    if len(before) != len(after):
        print(f"error: fragment count changed {len(before)} -> {len(after)}",
              file=sys.stderr)
        return 3
    for _, _, new in edits:
        if new not in out:
            print(f"error: {new!r} missing from the result", file=sys.stderr)
            return 3
    print(f"ok: {len(inner)} -> {len(out)} bytes; {len(after)} fragment(s); "
          f"tree tiles ({len(chunks)} chunks)"
          + ("  [unchanged control]" if a.stage else ""))

    dest = os.path.join(MOD, "art", "missions", a.mission, a.variant, "fig.p3d.rz")
    if a.dry_run:
        print(f"dry run: would stage {dest} ({len(wrap(out))} bytes RZ)")
        return 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    rz = wrap(out)
    with open(dest, "wb") as f:
        f.write(rz)
    print(f"staged {dest} ({len(rz)} bytes, RZ)")
    print("next: ./proto2 install  (then ./proto2 play)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
