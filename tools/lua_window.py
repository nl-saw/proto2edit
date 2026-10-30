#!/usr/bin/env python3
"""DEPRECATED (kept for provenance): build the global decompression window for mission scripts.

The premise below - that payloads are compressed against previously decompressed script text - was disproven in
docs/19 HANDOFF 161: the engine installs a fixed 56-byte preset dictionary (compress::Decompressor_Zlib's
constructor calls inflateSetDictionary with the bytes at 0x10ABEF5C). Use fsm_luablob.ENGINE_DICT /
engine_dict_bytes() instead; this tool is kept only because earlier handoffs cite it.

Mission script payloads are zlib-compressed against **previously decompressed script text**, not
against their own file. The window spans missions, which is why a payload at the start of one mission
only inflates once another mission's script has been recovered first.

This tool performs the decode over the whole corpus in passes, accumulating the recovered text, and
writes the window to `artifacts/lua-window.bin` so single-mission tools can reuse it without decoding
everything again.

    python3 tools/lua_window.py                 # build/refresh the window, report coverage
    python3 tools/lua_window.py --mission X     # decode one mission using the window
    python3 tools/lua_window.py --stats         # coverage only, no writes
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_chunk as C
import fsm_edit as E
import fsm_graph as F
import fsm_luablob as L

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "tmp", "corpus")
WINDOW = os.path.join(ROOT, "artifacts", "lua-window.bin")
MAX_WINDOW = 4 << 20


def collect():
    """(mission, offset, payload) for every record item in the corpus, in file order."""
    items = []
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        parts = [p for p in dirpath.split("/") if p]
        mission = f"{parts[-2]}/{parts[-1]}"
        leaf, _off = F.payload(open(os.path.join(dirpath, "fig.p3d.rz"), "rb").read())
        try:
            model = E.parse(leaf)
        except Exception:
            continue
        for el in E.walk_elements(model):
            for c in el["containers"]:
                for it in c["items"]:
                    if (it.get("fields") or {}).get("type") == "record":
                        items.append((mission, it["off"], it["fields"]["payload"]))
    return items


def decode_all(items, passes=6):
    """Multi-pass decode with a global window. Returns (decoded dict, window bytes, passes used)."""
    dec = {}
    buf = b""
    for n in range(passes):
        progress = False
        for i, (_m, _off, p) in enumerate(items):
            if i in dec:
                continue
            t, _how = L.read(p, buf[-32768:] if buf else b"")
            if t is None and buf:
                for sl in (buf, buf[:32768], buf[-65536:]):
                    if not sl:
                        continue
                    d, _w = L.decode(p, zdict=sl)
                    if d is not None and L.plausible(d):
                        t = d
                        break
            if t is not None:
                dec[i] = t
                buf += t.encode("latin-1") if isinstance(t, str) else t
                if len(buf) > MAX_WINDOW:
                    buf = buf[-MAX_WINDOW // 2:]
                progress = True
        if not progress:
            break
    return dec, buf, n + 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    items = collect()
    if not items:
        print(f"no record payloads under {CORPUS}")
        return 2
    dec, buf, passes = decode_all(items)
    print(f"record payloads : {len(items)}")
    print(f"decoded         : {len(dec)} ({len(dec) * 100 // len(items)}%)")
    print(f"passes          : {passes}")
    print(f"window          : {len(buf):,} bytes")
    if len(dec) < len(items):
        missing = [(m, off) for i, (m, off, p) in enumerate(items) if i not in dec]
        print(f"still unreadable: {len(missing)}")
        for m, off in missing[:10]:
            print(f"   {m} at {off:#x}")
    if "--stats" in argv:
        return 0
    if "--mission" in argv:
        want = argv[argv.index("--mission") + 1]
        got = [t for i, (m, _o, _p) in enumerate(items) if m.startswith(want + "/") and i in dec
               for t in [dec[i]]]
        text = "\n".join(t if isinstance(t, str) else t.decode("latin-1") for t in got)
        print(f"\n{want}: {len(got)} payload(s), {len(text):,} bytes of Lua")
        return 0
    os.makedirs(os.path.dirname(WINDOW), exist_ok=True)
    with open(WINDOW, "wb") as fh:
        fh.write(buf)
    print(f"wrote {WINDOW}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
