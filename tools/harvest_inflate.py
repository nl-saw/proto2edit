#!/usr/bin/env python3
"""Harvest exact (compressed stream -> decompressed text) pairs from a proto2_inflate2 capture.

proto2_inflate2.asi records each inflate call as:

    'PP23' | u32 avail_in | u32 produced | u32 total_in_at_entry | input | output

No window reconstruction is involved: the output bytes are the ones zlib wrote. Entries are stored in
artifacts/lua-cache.json as sha1(stream) -> [texts], filtered to text that reads like text (a wrong call
cannot silently pass, because there is no dictionary guess to be wrong about).

    python3 tools/harvest_inflate.py --capture proto2_inflate2.in --cache artifacts/lua-cache.json
"""
import argparse
import hashlib
import json
import os
import struct
import sys

MAGIC = 0x33325050   # PP23: avail_in, produced, total_in
MAGIC2 = 0x34325050  # PP24: PP23 + caller return address
MAGIC3 = 0x35325050  # PP25: PP24 + state block + window probe blocks
MAGIC4 = 0x36325050  # PP26: PP25 + the caller's caller
MAGIC5 = 0x37325050  # PP27: PP26 + a 0x200 raw stack window from entry ESP


def records(path):
    """Yield (input, output, total_in), resynchronising if a record is malformed.

    A capture can contain a torn record (the previous recorder shared one saved-state slot between
    threads, so a record's declared length can disagree with what was written). Treating that as EOF
    would silently discard everything after it, so scan for the next magic instead.
    """
    data = open(path, "rb").read()
    pos = 0
    n = len(data)
    while pos + 16 <= n:
        magic, avail_in, produced, total_in = struct.unpack_from("<IIII", data, pos)
        caller = None
        caller2 = None
        state = None
        blocks = b""
        stack = b""
        hdr = 16
        if magic == MAGIC2:
            if pos + 20 > n:
                break
            caller = struct.unpack_from("<I", data, pos + 16)[0]
            hdr = 20
        elif magic == MAGIC3:
            if pos + 28 + 0x90 > n:
                break
            caller = struct.unpack_from("<I", data, pos + 16)[0]
            state = struct.unpack_from("<I", data, pos + 20)[0]
            block_len = struct.unpack_from("<I", data, pos + 24)[0]
            hdr = 28 + 0x90 + block_len
            blocks = data[pos + 28 + 0x90:pos + 28 + 0x90 + block_len]
        elif magic == MAGIC5:
            if pos + 32 + 0x90 + 0x200 > n:
                break
            caller, caller2 = struct.unpack_from("<II", data, pos + 16)
            state = struct.unpack_from("<I", data, pos + 24)[0]
            block_len = struct.unpack_from("<I", data, pos + 28)[0]
            stack = data[pos + 32 + 0x90:pos + 32 + 0x90 + 0x200]
            hdr = 32 + 0x90 + 0x200 + block_len
            blocks = data[pos + 32 + 0x90 + 0x200:pos + 32 + 0x90 + 0x200 + block_len]
        elif magic == MAGIC4:
            if pos + 32 + 0x90 > n:
                break
            caller, caller2 = struct.unpack_from("<II", data, pos + 16)
            state = struct.unpack_from("<I", data, pos + 24)[0]
            block_len = struct.unpack_from("<I", data, pos + 28)[0]
            hdr = 32 + 0x90 + block_len
            blocks = data[pos + 32 + 0x90:pos + 32 + 0x90 + block_len]
        end = pos + hdr + avail_in + produced
        if magic not in (MAGIC, MAGIC2, MAGIC3, MAGIC4, MAGIC5) or avail_in > 65536 or produced > (1 << 20) or end > n:
            nxt = data.find(b"PP23", pos + 1)
            nxt2 = data.find(b"PP24", pos + 1)
            nxt3 = data.find(b"PP25", pos + 1)
            nxt4 = data.find(b"PP26", pos + 1)
            nxt5 = data.find(b"PP27", pos + 1)
            cand = [x for x in (nxt, nxt2, nxt3, nxt4, nxt5) if x >= 0]
            if not cand:
                break
            yield None            # marker: a resync happened here
            pos = min(cand)
            continue
        yield (data[pos + hdr:pos + hdr + avail_in],
               data[pos + hdr + avail_in:end], total_in, caller, state, blocks, caller2, stack)
        pos = end


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True)
    ap.add_argument("--cache", default=os.path.join("artifacts", "lua-cache.json"))
    ap.add_argument("--max-bad", type=float, default=2.0)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--stacks", action="store_true",
                    help="report stack words that look like engine return addresses (PP27)")
    ap.add_argument("--windows", default=None,
                    help="write a JSONL dump of the PP25 window probes (for offline dictionary analysis)")
    ap.add_argument("--callers", action="store_true",
                    help="report the top inflate callers (PP24 captures only)")
    ap.add_argument("--bytecode-dir", default=None,
                    help="store streams that decode to Lua 5.1 bytecode here (one file per stream)")
    a = ap.parse_args(argv)

    cache = {}
    if os.path.exists(a.cache):
        try:
            cache = json.load(open(a.cache))
        except Exception:
            cache = {}
    before = len(cache)

    total = kept = junk = starts = bytecode = 0
    resyncs = 0
    callers = {}
    stk = {}
    windows = open(a.windows, "w") if a.windows else None
    for rec in records(a.capture):
        if rec is None:
            resyncs += 1
            continue
        stream_in, out, total_in, caller, state, blocks, caller2, stack = rec
        total += 1
        starts += 1 if total_in == 0 else 0
        if windows is not None and blocks:
            parsed = []
            p = 0
            while p + 8 <= len(blocks):
                off, ln = struct.unpack_from("<II", blocks, p)
                p += 8
                if p + ln > len(blocks):
                    break
                parsed.append({"off": off, "len": ln, "data": blocks[p:p + ln].hex()})
                p += ln
            kind = "lua" if out[:4] == b"\x1bLua" else ("text" if out else "none")
            windows.write(json.dumps({"caller": caller, "caller2": caller2, "state": state, "kind": kind,
                                      "in_sha1": hashlib.sha1(stream_in).hexdigest(),
                                      "out_len": len(out), "blocks": parsed}) + "\n")
        if a.stacks and stack:
            for i in range(0, len(stack) - 3, 4):
                v = struct.unpack_from("<I", stack, i)[0]
                if 0x10001000 <= v <= 0x109E5000:
                    stk[v] = stk.get(v, 0) + 1
        if caller is not None:
            c = callers.setdefault((caller, caller2), [0, 0, 0])   # calls, lua, text
            c[0] += 1
            if out[:4] == b"\x1bLua":
                c[1] += 1
            elif out:
                bad = sum(1 for ch in out[:4096] if ch < 9 or 13 < ch < 32 or ch > 126)
                if bad * 100 <= len(out[:4096]) * a.max_bad:
                    c[2] += 1
        if not out:
            continue
        if out[:4] == b"\x1bLua":
            bytecode += 1
            if a.bytecode_dir:
                os.makedirs(a.bytecode_dir, exist_ok=True)
                open(os.path.join(a.bytecode_dir,
                                  hashlib.sha1(stream_in).hexdigest() + ".luac"), "wb").write(out)
            continue
        bad = sum(1 for c in out if c < 9 or 13 < c < 32 or c > 126)
        if 100.0 * bad / len(out) > a.max_bad:
            junk += 1
            continue
        key = hashlib.sha1(stream_in).hexdigest()
        slot = cache.setdefault(key, [])
        if isinstance(slot, str):
            slot = cache[key] = [slot]
        text = out.decode("latin-1")
        if text not in slot:
            slot.append(text)
        kept += 1

    os.makedirs(os.path.dirname(a.cache) or ".", exist_ok=True)
    json.dump(cache, open(a.cache, "w"))
    if windows is not None:
        windows.close()
        print(f"window probe -> {a.windows}")
    if a.stats:
        print(f"records      : {total} (stream starts: {starts})")
        print(f"lua bytecode : {bytecode}" + (" (saved)" if a.bytecode_dir else ""))
        print(f"kept         : {kept}")
        print(f"discarded    : {junk} (decoded output did not read as text)")
        print(f"resyncs      : {resyncs} (torn records skipped)")
        print(f"cache        : {before} -> {len(cache)} streams")
    if a.stacks:
        print("\nstack words inside the engine's .text (candidate return addresses):")
        for v, n in sorted(stk.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  prototype2engine.dll+0x{v - 0x10000000:06x}   seen {n}")
    if a.callers:
        print(f"\ncallers ({'PP25/PP24' if callers else 'not recorded - this is a PP23 capture'}):")
        def rva(a):
            if a is None:
                return "-"
            off = a - 0x10000000
            return f"+0x{off:06x}" if 0 < off < 0x2000000 else f"0x{a:08x}"
        for (a1, a2), (calls, lua, text) in sorted(callers.items(), key=lambda kv: -kv[1][0])[:12]:
            print(f"  caller {rva(a1):<12} <- {rva(a2):<12} calls {calls:>6}  lua {lua:>5}  text {text:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
