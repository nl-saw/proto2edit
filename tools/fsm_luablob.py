#!/usr/bin/env python3
"""The compressed-Lua record payload: `7f 00 | u16 size | raw deflate`.

WHAT THESE ARE. A record (item typeId 0x6c357b9d3e) holds
`f32 | f32 -1.0 | u32 len | payload[len]`, and the payload is Lua source in one of two
forms:

    plain     the text itself
    packed    u8 0x7f | u8 0x00 | u16 BIG-endian uncompressed size | raw deflate

Evidence for the shape, all measured over 434 mission files:

  * byte[1] is 0x00 in 3929 of 3929 packed payloads, and byte[2] decays geometrically
    (0:1453, 1:789, 2:387, ...) - which is what a big-endian length looks like when most
    payloads are small.
  * `zlib.decompressobj(-15)` at offset 4 yields Lua whose length equals the big-endian
    u16 for 2764 payloads immediately: `7f 00 0e 7b` -> 0x0e7b = 3707 bytes -> exactly
    `knownSchedules = {}...`.
  * The remaining 1165 fail with "invalid distance too far back", which is a stream that
    back-references data it did not emit: the compressor kept a dictionary. Supplying the
    file's other payloads as `zdict` decodes 1144 of them (99.5% total).
  * 21 payloads still need a dictionary we do not have - they are reported, never guessed.

WHY THE WRITER IS SAFE. `encode()` emits a stream produced by a FRESH zlib object, so it
contains no back-reference outside itself, and therefore inflates to the same bytes under
*any* dictionary. `verify_encoded()` proves exactly that by inflating it back with several
different dictionaries, including a deliberately wrong one. A stream that only decodes
with the right dictionary would be a latent crash; this cannot be one.

    python3 tools/fsm_luablob.py <file>        # decode stats for one file
    python3 tools/fsm_luablob.py --corpus      # the numbers quoted above
"""

import collections
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rz import unwrap          # noqa: E402
import fsm_graph as F          # noqa: E402
import fsm_items as I          # noqa: E402

RECORD_TYPE = 0x0000006C357B9D3E
MAGIC = b"\x7f\x00"
HEADER = 4
WINDOW = 32768


def is_packed(payload):
    return len(payload) >= HEADER and payload[:2] == MAGIC


def declared_size(payload):
    return struct.unpack_from(">H", payload, 2)[0]


# Fixed preset dictionary used for Lua record payloads.
#
# Evidence: compress::Decompressor_Zlib's constructor (prototype2engine.dll+0x8d3c20) calls
# inflateSetDictionary(z_stream, 0x10ABEF5C, 0x38) after inflateInit2_(-13, "1.2.3.0-ATG", 0x38). The 56 bytes
# at VA 0x10ABEF5C are a truncated Lua 5.1 chunk header. Passing them as the zdict decodes every payload that
# previously failed with "invalid distance too far back".
ENGINE_DICT = bytes.fromhex(
    "1b4c756151000104040404000000000000000000000000000000"
    "0202010000001e0080000000000000000000000000000000000000000000"
)


def strip_prefix(payload):
    """The packed payload (`7f 00 | u16 size | stream`), dropping an optional leading 4-byte prefix.

    Some records carry a 4-byte prefix (an id or checksum - measured: 105 for a 112-byte record, so not a
    length) before a normal packed payload. Every consumer must agree on this shape: capture files are keyed
    by the bare stream, so a prefixed payload has to yield the same key as an unprefixed one holding the
    same stream.
    """
    b = bytes(payload)
    if len(b) >= 6 and b[:2] != MAGIC and b[4:6] == MAGIC:
        return b[4:]
    return b


def stream_of(payload):
    """The bare deflate stream of a record payload, with the magic and size header removed."""
    b = strip_prefix(payload)
    return b[HEADER:] if b[:2] == MAGIC else b


def engine_dict_bytes(payload):
    """Raw output of a payload inflated with the engine's fixed preset dictionary, or None.

    Returns bytes (text or Lua bytecode) without judging whether it is text: the mission loader hands the
    result straight to luaL_loadbuffer, so bytecode is the normal case.
    """
    packed = strip_prefix(payload)
    if not is_packed(packed):
        return None
    for body in (packed[HEADER:], packed[4 + HEADER:] if len(packed) > 4 + HEADER else b""):
        if not body:
            continue
        try:
            d = zlib.decompressobj(-13, zdict=ENGINE_DICT)
            t = d.decompress(body)
        except zlib.error:
            continue
        if t and valid_size(t, packed):
            return t
    return None


def decode(payload, zdict=None):
    """(text, how) for a packed payload, or (None, reason). Never guesses a dictionary."""
    # Engine dictionary: the constructor of the engine's Decompressor_Zlib installs this fixed 56-byte preset
    # dictionary, so streams that reference it fail under every other rule. Try it before giving up.
    if is_packed(payload):
        for body in (payload[HEADER:], payload[4 + HEADER:] if len(payload) > 4 + HEADER else b""):
            if not body:
                continue
            try:
                d = zlib.decompressobj(-13, zdict=ENGINE_DICT)
                t = d.decompress(body)
            except zlib.error:
                continue
            if t and valid_size(t, payload) and plausible(t):
                return t, "engine-dictionary"
    if not is_packed(payload):
        return None, "not packed"
    want = declared_size(payload)
    for how, kw in (("plain", {}), ("zdict", {"zdict": zdict})) if zdict else (("plain", {}),):
        try:
            out = zlib.decompressobj(-15, **kw).decompress(payload[HEADER:])
        except zlib.error as exc:
            last = str(exc)
            continue
        if len(out) == want:
            return out, how
        last = f"size {len(out)} != declared {want}"
    return None, last if "last" in dir() else "undecoded"


def encode(text, level=9):
    """A packed payload that inflates to `text` under ANY dictionary."""
    if len(text) > 0xFFFF:
        raise ValueError("payload too large for the 16-bit size field")
    co = zlib.compressobj(level, zlib.DEFLATED, -15)
    body = co.compress(text) + co.flush()
    return MAGIC + struct.pack(">H", len(text)) + body


def bare_encode(text):
    """A `bare`-form payload: u32 length | body | zero padding to a 4-byte total.

    `conditions` records use this form instead of the packed one (measured over story_l1: 18 of 18 are
    `form: bare`, every other role is `form: prefixed`). The shipped bare payloads are either plain text or a
    packed stream followed by padding, and the padding is the smallest n >= 4 that makes
    (len(body) + n) a multiple of 4 - checked against all 18.
    """
    body = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    pad = 4 + ((4 - len(body)) % 4)
    return struct.pack("<I", len(body)) + body + b"\x00" * pad


def valid_size(text, payload):
    """True when a decode produced exactly the length the payload declares.

    The packed header carries the uncompressed size (u16 at +2). zlib does not validate dictionary
    CONTENT, so a wrong dictionary inflates to the wrong length - or to megabytes - while still
    reporting success. Comparing against the declared size is the check that cannot be fooled.
    """
    if text is None:
        return False
    n = len(text.encode("latin-1")) if isinstance(text, str) else len(text)
    return n == declared_size(payload)


def plausible(text, limit=2.0):
    """True when decoded bytes look like text rather than dictionary garbage.

    zlib never validates the CONTENT copied from a preset dictionary, so a wrong dictionary still
    inflates "successfully" and yields binary noise. Counting that as a decode is how an earlier
    measurement reported 100% while 14% of the output was unprintable.
    """
    if text is None:
        return False
    b = text.encode("latin-1") if isinstance(text, str) else text
    if not b:
        return True
    bad = sum(1 for c in b if c < 9 or 13 < c < 32 or c > 126)
    return bad * 100.0 / len(b) < limit


_CACHE = None


def _cache():
    """payload sha1 -> text, harvested from a runtime capture (tools/replay_inflate.py --harvest).

    The engine's window depends on everything it inflated before, so some payloads cannot be decoded
    offline at all. When a real playthrough has been recorded, the exact text for those payloads is
    known; this cache makes that knowledge permanent and deterministic.
    """
    global _CACHE
    if _CACHE is None:
        import hashlib
        import json
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "artifacts", "lua-cache.json")
        try:
            raw = json.load(open(path))
            out = {}
            for k, v in raw.items():
                out[k] = list(v) if isinstance(v, list) else [v]
            _CACHE = out
        except Exception:
            _CACHE = {}
    return _CACHE


def from_cache(payload):
    """Text for this payload if a runtime capture recorded it, else None.

    A stream can decode to DIFFERENT text depending on when the engine inflated it (the window moves),
    so the cache stores every text observed for a stream. Candidates are filtered by the payload's own
    declared size - the check that cannot be fooled - and the most Lua-like one wins. If nothing passes,
    the cache says nothing and the normal decoders run.
    """
    import hashlib
    if os.environ.get("PROTO2_NO_CACHE"):
        return None          # reproducible measurements for the published corpus (the cache is not committed)
    c = _cache()
    if not c:
        return None
    keys = [hashlib.sha1(payload).hexdigest()]
    if len(payload) > 4 and payload[0] == 0x7F and payload[1] == 0x00:
        keys.append(hashlib.sha1(payload[4:]).hexdigest())      # the engine sees the bare stream
    cands = []
    for k in keys:
        got = c.get(k)
        if got is None:
            continue
        cands.extend(got if isinstance(got, list) else [got])
    if not cands:
        return None
    if len(payload) >= 4 and payload[0] == 0x7F and payload[1] == 0x00:
        want = declared_size(payload)
        cands = [x for x in cands if len(x) == want]
    if not cands:
        return None
    # never serve a cached decode that is not text: a wrong window still completes
    cands = [x for x in cands if plausible(x)]
    if not cands:
        return None
    if len(cands) > 1:
        cands.sort(key=_lua_score, reverse=True)
    return cands[0]


def _lua_score(text):
    """Rough 'does this look like Lua source' score, used only to break ties between cached candidates."""
    if not text:
        return 0
    good = sum(1 for ch in text if ch.isalpha() or ch in " _\n\r\t\"'-=(){},")
    bad = sum(1 for ch in text if ord(ch) < 9 or (13 < ord(ch) < 32) or ord(ch) == 0)
    return (good - 3 * bad) / float(len(text))


def read(payload, prev=b""):
    """Best-effort read of one record payload.

    Three forms occur, and only the first is zlib-packed:

      packed           7f 00 | u16 size | deflate            -> ('plain' or 'zdict')
      packed, chained  7f 00 ... but compressed against the PREVIOUS record's text, so it only
                       inflates when that text is supplied as the zlib dictionary ('prev-zdict')
      plain            Lua source, or u32 length | chars     ('text' / 'string')

    `prev` is the previous record's decoded text; the engine clearly compresses each script against
    the one before it, which is why so many payloads looked undecodable in isolation.

    A runtime-harvested cache is consulted first (see `from_cache`), because a real playthrough knows the
    engine's exact window and settles payloads no heuristic can.
    """
    cached = from_cache(payload)
    if cached is not None:
        return cached, "cache"

    # Some records carry a 4-byte prefix (an id or checksum - measured: 105 for a 112-byte record, so not a
    # length) before a normal packed payload. Without this the record was reported undecodable even though its
    # deflate stream was readable. The stripped form still has to pass read()'s own size/plausibility gates.
    if len(payload) >= 8 and payload[4:6] == b"\x7f\x00":
        t, how = read(payload[4:], prev)
        if t is not None:
            return t, "u32len-" + str(how)
    t, how = decode(payload)
    if t:
        if valid_size(t, payload) and plausible(t):
            return t, how
        return None, "wrong-size"
    if str(how).startswith("Error") and prev:
        t2, _why = decode(payload, zdict=prev[-32768:])
        if t2:
            if valid_size(t2, payload) and plausible(t2):
                return t2, "prev-zdict"
            return None, "wrong-size-zdict"
    # Engine dictionary: the constructor of the engine's Decompressor_Zlib installs this fixed 56-byte preset
    # dictionary, so streams that reference it fail under every other rule. Try it before giving up.
    if is_packed(payload):
        for body in (payload[HEADER:], payload[4 + HEADER:] if len(payload) > 4 + HEADER else b""):
            if not body:
                continue
            try:
                d = zlib.decompressobj(-13, zdict=ENGINE_DICT)
                t = d.decompress(body)
            except zlib.error:
                continue
            if t and valid_size(t, payload) and plausible(t):
                return t, "engine-dictionary"
    if not is_packed(payload):
        if len(payload) >= 4:
            n = struct.unpack_from("<I", payload, 0)[0]
            body = payload[4:4 + n]
            if 0 < n <= len(payload) - 4 and body and all(9 <= c < 127 for c in body):
                return body.decode("latin-1"), "string"
        try:
            txt = payload.decode("latin-1")
        except Exception:
            return None, "binary"
        return (txt, "text") if plausible(txt) else (None, "binary")
    return None, str(how)


def decode_sequence(payloads):
    """Decode a run of payloads in order, threading each result as the next dictionary."""
    out = []
    prev = b""
    for p in payloads:
        t, how = read(p, prev)
        out.append((t, how))
        if t and isinstance(t, str):
            prev = t.encode("latin-1")
        elif t:
            prev = t
    return out


def verify_encoded(payload, text):
    """Inflate our own output back under several dictionaries; all must give `text`."""
    for name, zdict in (("none", None),
                        ("wrong", b"this dictionary is not the one" * 40),
                        ("short", b"\x00")):
        try:
            kw = {} if zdict is None else {"zdict": zdict}
            out = zlib.decompressobj(-15, **kw).decompress(payload[HEADER:])
        except zlib.error as exc:
            return False, f"{name}: {exc}"
        if out != text:
            return False, f"{name}: {len(out)} bytes != {len(text)}"
    if declared_size(payload) != len(text):
        return False, "declared size wrong"
    return True, "ok"


def payloads(leaf, nodes):
    """(offset, payload bytes) for every record, in file order."""
    out = []
    for _what, off, size, _count, _kids in I.containers(leaf, nodes):
        items = I.parse_items(leaf, off + 16, off + 16 + size)
        if items is None:
            continue
        for it in items:
            if it.tag != RECORD_TYPE:
                continue
            _f1, _f2, ln = struct.unpack_from("<ffI", leaf, it.off + 20)
            out.append((it.off, leaf[it.off + 32:it.off + 32 + ln]))
    out.sort()
    return out


def decode_all(items, window=b""):
    """Decode every payload of one file, widening the dictionary from its own text.

    Returns (text, pending, results, skipped). `text` is ONLY decoded script text - the dictionary
    seed is never included, and non-packed payloads are classified instead of concatenated, because
    appending them raw is what once made an extraction 15% unprintable.
    """
    plain = bytearray()
    pending = []
    results = []                       # (payload, text, how)
    skipped = 0
    for _off, data in items:
        if not is_packed(data):
            t, _how = read(data)
            if t is None:
                skipped += 1
                continue
            plain += t.encode("latin-1") if isinstance(t, str) else t
            continue
        text, how = decode(data)
        if text is None or not plausible(text):
            pending.append(data)
        else:
            plain += text.encode("latin-1") if isinstance(text, str) else text
            results.append((data, text, how))

    for _ in range(8):
        if not pending:
            break
        seed = bytes(window) + bytes(plain)      # dictionary material, never output
        zdict = seed[-WINDOW:]
        extra = []
        if window:
            extra = [window[-32768:], window[:32768], window[-65536:]]
        still = []
        for data in pending:
            text, how = decode(data, zdict)
            if text is not None and not (valid_size(text, data) and plausible(text)):
                text = None
            if text is None:
                for cand in extra:
                    if not cand:
                        continue
                    text, how = decode(data, zdict=cand)
                    if text is not None and valid_size(text, data) and plausible(text):
                        how = "global-window"
                        break
                    text = None
            if text is None:
                still.append(data)
            else:
                plain += text.encode("latin-1") if isinstance(text, str) else text
                results.append((data, text, how))
        if len(still) == len(pending):
            break
        pending = still
    return bytes(plain), pending, results, skipped


def verify_encoded(payload, text):
    """Inflate our own output back under several dictionaries; all must give `text`."""
    for name, zdict in (("none", None),
                        ("wrong", b"this dictionary is not the one" * 40),
                        ("short", b"\x00")):
        try:
            kw = {} if zdict is None else {"zdict": zdict}
            out = zlib.decompressobj(-15, **kw).decompress(payload[HEADER:])
        except zlib.error as exc:
            return False, f"{name}: {exc}"
        if out != text:
            return False, f"{name}: {len(out)} bytes != {len(text)}"
    if declared_size(payload) != len(text):
        return False, "declared size wrong"
    return True, "ok"


def analyse(path):
    raw = open(path, "rb").read()
    blob = unwrap(raw) if raw[:2] == b"RZ" else raw
    leaf, _ = F.payload(blob)
    table = F.name_table()
    nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
    items = payloads(leaf, nodes)
    packed = [d for _o, d in items if is_packed(d)]
    plain_text = [d for _o, d in items if d and not is_packed(d)]
    _all_plain, undecoded, _results = decode_all(items)
    return {"records": len(items), "packed": len(packed), "plain": len(plain_text),
            "undecoded": len(undecoded)}


def corpus_stats(root):
    total = collections.Counter()
    per_file = []
    for dirpath, _d, names in os.walk(root):
        if "fig.p3d.rz" not in names:
            continue
        p = os.path.join(dirpath, "fig.p3d.rz")
        s = analyse(p)
        for k, v in s.items():
            total[k] += v
        if s["undecoded"]:
            per_file.append((p, s["undecoded"]))
    return total, per_file


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--corpus":
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        total, per_file = corpus_stats(os.path.join(here, "tmp", "corpus"))
        print(f"records {total['records']}  packed {total['packed']}  plain {total['plain']}")
        print(f"packed payloads decoded {total['packed'] - total['undecoded']}/{total['packed']}"
              f"  ({100*(total['packed']-total['undecoded'])/max(total['packed'],1):.2f}%)")
        print(f"files with at least one undecoded payload: {len(per_file)}")
        for p, n in per_file[:10]:
            print(f"   {p.split('missions/')[-1]}: {n}")
        return 0
    for p in argv:
        print(p, analyse(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
