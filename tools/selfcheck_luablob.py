#!/usr/bin/env python3
"""Proofs for the compressed-Lua payload: decode coverage, and that a re-encode is safe.

Two claims are checked, both over the whole corpus:

  1. DECODE. Every packed payload either decodes to Lua whose length equals the declared
     big-endian size, or is reported as undecoded. No payload may decode to the wrong
     length - that would mean the size field is not what we think it is.
  2. RE-ENCODE. `fsm_luablob.encode(text)` must inflate back to `text` under several
     different dictionaries, including a deliberately wrong one. A stream that only
     decodes with the right dictionary would crash on the engine's dictionary; a fresh
     deflate stream references nothing outside itself, and this proves it per payload.

Writes nothing outside memory.

    python3 tools/selfcheck_luablob.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fsm_graph as F          # noqa: E402
import fsm_luablob as L        # noqa: E402
from rz import unwrap          # noqa: E402

CORPUS = os.path.join(os.path.dirname(HERE), "tmp", "corpus")


def files():
    for root, _d, names in os.walk(CORPUS):
        if "fig.p3d.rz" in names:
            yield os.path.join(root, "fig.p3d.rz")


def main():
    table = F.name_table()
    packed = plain = decoded = undecoded = wrong_size = 0
    reencode_ok = reencode_bad = 0
    lua_bytes = 0
    first_bad = []
    samples = []

    for path in files():
        raw = open(path, "rb").read()
        blob = unwrap(raw) if raw[:2] == b"RZ" else raw
        leaf, _ = F.payload(blob)
        nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
        items = L.payloads(leaf, nodes)

        for _off, data in items:
            if data and not L.is_packed(data):
                plain += 1
                lua_bytes += len(data)

        _all_plain, left, results = L.decode_all(items)
        undecoded += len(left)
        packed += sum(1 for _o, d in items if L.is_packed(d))
        for data, text, how in results:
            decoded += 1
            lua_bytes += len(text)
            if len(text) != L.declared_size(data):
                wrong_size += 1
            if len(samples) < 3 and len(text) > 200:
                samples.append((path, text))
            blob2 = L.encode(text)
            ok, why = L.verify_encoded(blob2, text)
            if ok:
                reencode_ok += 1
            else:
                reencode_bad += 1
                if len(first_bad) < 3:
                    first_bad.append((path, why))

    total_packed = packed
    print(f"records with a packed payload : {total_packed}")
    print(f"  decoded                     : {decoded}")
    print(f"  undecoded                   : {undecoded}")
    print(f"  decoded to the WRONG length : {wrong_size}   (must be 0)")
    print(f"plain-text payloads           : {plain}")
    print(f"Lua source recovered          : {lua_bytes} bytes")
    print(f"re-encode inflates under every dictionary tried: {reencode_ok}"
          f"  failed: {reencode_bad}")
    for b in first_bad:
        print("   FAILED:", b)
    for p, text in samples:
        print(f"\n--- {p.split('missions/')[-1]}\n{text[:160]}")
    pass_ = (wrong_size == 0 and reencode_bad == 0 and decoded + undecoded == total_packed
             and total_packed > 0)
    print("\nRESULT:", "PASS" if pass_ else "FAIL")
    return 0 if pass_ else 1


if __name__ == "__main__":
    sys.exit(main())
