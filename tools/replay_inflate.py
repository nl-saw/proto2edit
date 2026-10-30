#!/usr/bin/env python3
"""Replay the runtime inflate capture to recover the engine's exact output.

proto2_inflate.asi writes two block types to proto2_inflate.in, interleaved:

  PP22  u32 magic | u32 strm | u32 n | u32 call | u32 total_in | n bytes input
  pair  u32 avail_in | u32 declared | u32 call | u32 0 | input | 32768-byte window

PP22 marks a stream start (total_in == 0). The engine reuses one inflate state, so the dictionary for
stream N is the concatenation of streams 1..N-1's output, truncated to 32 KB. Replaying the starts in
file order therefore reproduces the engine's own window without needing the struct layout.

    python3 tools/replay_inflate.py --capture /path/proto2_inflate.in --find 9326531f00
"""
import argparse
import hashlib
import json
import os
import struct
import sys
import zlib

MAGIC = 0x50503232


def blocks(path):
    data = open(path, "rb").read()
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        first = struct.unpack_from("<I", data, pos)[0]
        if first == MAGIC:
            if pos + 20 > n:
                return
            _m, strm, ln, call, total_in = struct.unpack_from("<IIIII", data, pos)
            body = data[pos + 20:pos + 20 + ln]
            yield ("start", call, body, None)
            pos += 20 + ln
        else:
            if pos + 16 > n:
                return
            avail, declared, call, _z = struct.unpack_from("<IIII", data, pos)
            if avail > 65536 or pos + 16 + avail + 32768 > n:
                return
            body = data[pos + 16:pos + 16 + avail]
            win = data[pos + 16 + avail:pos + 16 + avail + 32768]
            yield ("pair", call, body, win)
            pos += 16 + avail + 32768


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True)
    ap.add_argument("--find", default=None, help="hex of the stream input to report")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--max-bad", type=float, default=2.0,
                    help="percent of non-printable bytes allowed before a decoded stream is discarded")
    ap.add_argument("--harvest", default=None,
                    help="write payload sha1 -> decoded text into this cache file")
    a = ap.parse_args(argv)

    window = b""
    starts = pairs = ok = failed = 0
    found = []
    cache = {}
    skipped_junk = [0]
    if a.harvest and os.path.exists(a.harvest):
        try:
            cache = json.load(open(a.harvest))
        except Exception:
            cache = {}
    for kind, call, body, win in blocks(a.capture):
        if kind == "pair":
            pairs += 1
            continue
        starts += 1
        try:
            out = zlib.decompressobj(-15, zdict=window[-32768:]).decompress(body) if window \
                  else zlib.decompressobj(-15).decompress(body)
            ok += 1
        except Exception:
            failed += 1
            out = b""
        window += out
        if a.harvest and out:
            # completion is not correctness: a wrong window still produces exactly `declared` bytes of
            # noise. Keep only text that reads like text.
            bad = sum(1 for c in out if c < 9 or 13 < c < 32 or c > 126)
            if not out or (100.0 * bad / len(out)) > a.max_bad:
                skipped_junk[0] += 1
                continue
            key = hashlib.sha1(body).hexdigest()
            text = out.decode("latin-1")
            slot = cache.setdefault(key, [])
            if isinstance(slot, str):
                slot = cache[key] = [slot]
            if text not in slot:
                slot.append(text)
        if a.find and body.hex() == a.find.lower():
            found.append((call, out, len(window)))
        if a.limit and starts >= a.limit:
            break

    if a.stats:
        print(f"stream starts replayed : {starts}  (decoded {ok}, failed {failed})")
        print(f"input+window pairs     : {pairs}")
        print(f"total output           : {len(window)} bytes")
    for call, out, wlen in found:
        print(f"\ncall {call}: output {len(out)} bytes")
        print(f"  repr : {out!r}")
        print(f"  hex  : {out[:8].hex(' ')} ...")
        print(f"  first byte: 0x{out[0]:02x} {chr(out[0])!r}" if out else "  (empty)")
    if a.harvest:
        with open(a.harvest, "w") as fh:
            json.dump(cache, fh)
        print(f"harvested {len(cache)} payload text(s) into {a.harvest} "
              f"(discarded {skipped_junk[0]} streams that decoded to noise)")
    if a.find and not found:
        print(f"\nstream {a.find} not found among the captured starts", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
