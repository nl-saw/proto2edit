#!/usr/bin/env python3
"""Find, read and patch the Lua embedded in a mission's FSM (`fig.p3d`).

A mission's logic is not a `.lua` file. It is an **FSM graph** stored inside
`art/missions/<mission>/<variant>/fig.p3d`, and some of its nodes carry a short Lua
snippet as a plain ASCII string. For `story_intro1` that is 13 fragments totalling
701 bytes - the whole Lua surface of the mission.

The snippets are ordinary readable text with `\\r\\n` line endings, so they are
plain Lua source: editable with no toolchain. Each is stored as a length-prefixed
string (`u32 length` + exactly that many bytes), so a replacement may be of a
**different length**. The tool then fixes up that length field, the enclosing
chunk's `header_size`/`total_size`, and every ancestor's `total_size`, and
re-walks the tree to prove it still tiles. A wrong size silently truncates the
file at load time, so those checks are not optional.

Usage:
  python3 tools/mission_fsm.py list  <fig.p3d|.p3d.rz>
  python3 tools/mission_fsm.py list  <file> --unknown     # include non-Lua runs

  # rewrite by unique substring; any length
  python3 tools/mission_fsm.py patch <file> \
      --replace 'INTRO1_MOVE_TUTORIAL4' --with 'INTRO1_MOVE_TUTORIAL4_EXT' -o out.p3d

  # overwrite exactly at an offset (same length; no length field to fix)
  python3 tools/mission_fsm.py patch <file> --at 0x15cbc \
      --text 'ShowObjectiveMessage("$INTRO1_MOVE_TUTORIAL9")' -o out.p3d

To edit a mission by name and have it installable, use `tools/stage_fsm.py` (or
`./proto2 fsm`): it starts from the archive entry and stages the result into
`work/mod`, ready for `./proto2 install`.

Notes:
  * Output stays RZ-wrapped when the input was, so it can be staged as `.rz`.
  * A grow-then-shrink round trip must reproduce the input byte for byte; that is
    the real test of the size arithmetic (verified for story_intro1).
  * This does not decompile anything: there is no bytecode in these fragments.
"""

import argparse
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rz import unwrap, wrap  # noqa: E402

# Engine/script identifiers that mark a run as real Lua rather than binary noise.
KNOWN = re.compile(
    r"\b(ShowObjectiveMessage|scenario_PostEventString|fsm_PostEventString|"
    r"snd_StopMusic|go_SetTimeScale|go_SetVisibility|RemoveViewPointTrackingTarget|"
    r"mission_IsRetry|CancelBrawlerCam|print|UID|context\.event|RETRY_STATE)\b")


def load(path):
    blob = open(path, "rb").read()
    wrapped = blob[:2] == b"RZ"
    return (unwrap(blob) if wrapped else blob), wrapped


def fragments(data, unknown=False):
    """[(offset, text)] of the Lua-looking ASCII runs."""
    out = []
    for m in re.finditer(rb"[ -~\r\n\t]{6,}", data):
        t = m.group().decode("latin-1")
        if unknown or KNOWN.search(t):
            out.append((m.start(), t))
    return out


def cmd_list(a):
    data, wrapped = load(a.path)
    frags = fragments(data, unknown=a.unknown)
    print(f"{a.path}: {len(data)} bytes{', RZ-wrapped' if wrapped else ''}; "
          f"{len(frags)} fragment(s), {sum(len(t) for _, t in frags)} bytes of text\n")
    for off, text in frags:
        print(f"=== {off:#07x}  ({len(text)} bytes)")
        print(text.replace("\r", "").strip("\n"))
        print()
    return 0



def _walk(data, off, end, out):
    """Collect (offset, header_size, total_size, depth) for chunks in [off,end)."""
    while off + 12 <= end:
        typ, hdr, tot = struct.unpack_from("<3I", data, off)
        if hdr < 12 or tot < 12 or off + tot > end:
            return
        out.append((off, hdr, tot))
        if tot > hdr:
            _walk(data, off + hdr, off + tot, out)
        off += tot


def reflow(data, changed_off, delta, orig=None):
    """Add `delta` to the chunk containing `changed_off` and every ancestor's total.

    A fig.p3d is root -> 0x20000701 (48-byte header) -> 0x20000702 (leaf payload).
    Changing the leaf's payload length requires updating the leaf's own
    header/total and each ancestor's total, or the Pure3D tree no longer tiles.
    """
    chunks = []
    # Always walk the ORIGINAL bytes: after the edit the buffer's own size fields
    # are stale, so walking it would fail to tile and find no owner.
    _walk(orig if orig is not None else data, 0, len(orig if orig is not None else data), chunks)
    out = bytearray(data)
    # find the deepest chunk that contains changed_off in its payload
    owner = None
    for off, hdr, tot in chunks:
        if off <= changed_off < off + tot:
            owner = (off, hdr, tot)
    if owner is None:
        raise ValueError("no enclosing chunk")
    o, hdr, tot = owner
    struct.pack_into("<I", out, o + 4, hdr + delta)
    struct.pack_into("<I", out, o + 8, tot + delta)
    # ancestors
    for aoff, ahdr, atot in chunks:
        if aoff < o <= aoff + atot:
            struct.pack_into("<I", out, aoff + 8, atot + delta)
    return bytes(out)


def patch_bytes(data, old, new, off):
    """Replace `data[off:off+len(old)]` with `new`, of any length.

    Returns the new bytes. Raises ValueError when the edit cannot be made safe.
    The caller has already established that `old` occurs exactly once at `off`.

    A tiny len(old) != len(new) edit changes the enclosing Lua string's `u32`
    length and the sizes of the chunk containing it and every ancestor, so the
    file keeps tiling. Everything else is checked, never assumed.
    """
    old, new = bytes(old), bytes(new)
    delta = len(new) - len(old)
    len_field = None
    if delta:
        # The replaced substring sits inside a longer string (a Lua snippet).
        # That string is  u32 length + bytes, so locate it and adjust ITS length.
        a2 = off
        while a2 > 0 and (32 <= data[a2 - 1] < 127 or data[a2 - 1] in (9, 10, 13)):
            a2 -= 1
        b2 = off + len(old)
        while b2 < len(data) and (32 <= data[b2] < 127 or data[b2] in (9, 10, 13)):
            b2 += 1
        if a2 < 4:
            raise ValueError("cannot locate the enclosing string")
        L = struct.unpack_from("<I", data, a2 - 4)[0]
        if L != b2 - a2:
            raise ValueError(f"no u32 length matches the enclosing string "
                             f"(field {L}, string {b2 - a2})")
        len_field = a2 - 4
    out = bytearray(data)
    if delta:
        struct.pack_into("<I", out, len_field,
                         struct.unpack_from("<I", data, len_field)[0] + delta)
    out[off:off + len(old)] = new
    if delta:
        out = bytearray(reflow(bytes(out), off, delta, orig=data))
    # Verify rather than assume: the tree must tile, the new text must be there,
    # and the enclosing length field must now describe the enclosing string.
    chunks = []
    _walk(bytes(out), 0, len(out), chunks)
    if chunks:
        last_off, _, last_tot = chunks[0]
        if last_off + last_tot != len(out):
            raise ValueError(f"chunk tree does not tile "
                             f"({last_off + last_tot} != {len(out)})")
    if new not in bytes(out):
        raise ValueError("replacement not found after write")
    if delta:
        a2 = off
        while a2 > 0 and (32 <= out[a2 - 1] < 127 or out[a2 - 1] in (9, 10, 13)):
            a2 -= 1
        b2 = off + len(new)
        while b2 < len(out) and (32 <= out[b2] < 127 or out[b2] in (9, 10, 13)):
            b2 += 1
        L = struct.unpack_from("<I", out, a2 - 4)[0]
        if L != b2 - a2:
            raise ValueError(f"length field did not take the edit "
                             f"(field {L}, string {b2 - a2})")
    return bytes(out)


def cmd_patch(a):
    data, wrapped = load(a.path)
    if a.at is not None:
        off = int(a.at, 16) if a.at.lower().startswith("0x") else int(a.at)
        old = data[off:off + len(a.text)]
        if len(a.text) != len(old):
            print(f"error: in-place patch needs {len(old)} bytes, got {len(a.text)}",
                  file=sys.stderr)
            return 2
        new = a.text.encode("latin-1")
    else:
        old = a.replace.encode("latin-1")
        new = a.with_.encode("latin-1")
        hits = [m.start() for m in re.finditer(re.escape(old), data)]
        if len(hits) != 1:
            print(f"error: {a.replace!r} occurs {len(hits)} times; use --at",
                  file=sys.stderr)
            for h in hits[:10]:
                print(f"   {h:#07x}", file=sys.stderr)
            return 2
        off = hits[0]

    try:
        out = patch_bytes(data, old, new, off)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    dest = a.out or (a.path + ".patched.p3d")
    open(dest, "wb").write(out)
    print(f"patched {len(new)} bytes at {off:#07x}")
    if wrapped:
        rz = wrap(out)
        open(dest + ".rz", "wb").write(rz)
        print(f"wrote {dest} and {dest}.rz ({len(rz)} bytes, RZ-wrapped)")
    else:
        print(f"wrote {dest}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show the Lua fragments and their offsets")
    p.add_argument("path")
    p.add_argument("--unknown", action="store_true",
                   help="also show runs that do not look like Lua")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("patch", help="replace text (any length) and fix the sizes")
    p.add_argument("path")
    p.add_argument("--at", default=None, help="byte offset of the replacement")
    p.add_argument("--text", default=None, help="exact replacement for --at")
    p.add_argument("--replace", default=None, help="substring to find (must be unique)")
    p.add_argument("--with", dest="with_", default=None,
                   help="replacement text; may differ in length")
    p.add_argument("-o", "--out", default=None)
    p.set_defaults(func=cmd_patch)

    a = ap.parse_args(argv)
    if a.cmd == "patch" and a.at is None and a.replace is None:
        ap.error("patch needs either --at/--text or --replace/--with")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
