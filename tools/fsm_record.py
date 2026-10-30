#!/usr/bin/env python3
"""The `record` item: Prototype 2's script-carrying item type.

5,142 of the 11,270 items in the corpus are this type (`0x6c357b9d3e`). It has **two forms**,
and which one applies is decided by the container it sits in, not by the item itself:

    prefixed (4,630)   f32 timeBegin | f32 timeEnd | u32 len | payload[len] | pad4 | u64 0
    bare       (512)   payload

`prefixed` appears in the track/function containers (`tracks`, `initialTracks`, `enterTracks`,
`exitTracks`, `functions`, `overrideFunctions`); `bare` appears in `conditions`. That matches the
meaning: a track entry is a timed script (`timeBegin=0.0, timeEnd=-1.0` is "always"), while a
condition is a bare boolean expression.

Measured over the corpus: 4,630 prefixed with **4,630/4,630** re-emitting byte-identically, and
512 bare (363 text, 149 deflate). 4,630 + 512 = 5,142, so no record is unaccounted for.

`decode()` returns fields; `encode(decode(body)) == body` is the gate this module is held to.
"""
import struct

RECORD = 0x0000006C357B9D3E

# containers whose record items carry the time prefix
PREFIXED_CONTAINERS = {"tracks", "initialTracks", "enterTracks", "exitTracks",
                       "functions", "overrideFunctions"}


def decode(body):
    """Fields of a record body, or None if it matches neither form."""
    if len(body) < 12:
        return None
    t0, t1, ln = struct.unpack_from("<ffI", body, 0)
    if ln <= len(body) - 12:
        pad = (4 - ln % 4) % 4
        if 12 + ln + pad + 8 == len(body) and body[12 + ln:12 + ln + pad] == b"\0" * pad \
                and body[12 + ln + pad:] == b"\0" * 8:
            return {"form": "prefixed", "timeBegin": t0, "timeEnd": t1,
                    "payload": body[12:12 + ln]}
    return {"form": "bare", "payload": body}


def encode(fields):
    """Body bytes for decoded fields. encode(decode(b)) must equal b."""
    p = fields["payload"]
    if fields["form"] == "bare":
        return p
    offset = 12 + len(p)
    pad = (4 - len(p) % 4) % 4
    return (struct.pack("<ffI", fields["timeBegin"], fields["timeEnd"], len(p))
            + p + b"\0" * pad + b"\0" * 8)


def is_lua(payload):
    return payload[:2] == b"\x7f\x00"


def main(argv=None):
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fsm_chunk as C
    import fsm_graph as F
    import fsm_items as I

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corpus = os.path.join(here, "tmp", "corpus")
    if not os.path.isdir(corpus):
        print(f"no corpus at {corpus}")
        return 2
    table = F.name_table()
    ok = bad = 0
    by_form = {}
    by_container = {}
    for dirpath, _dirs, names in os.walk(corpus):
        if "fig.p3d.rz" not in names:
            continue
        leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
        nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
        for what, off, size, _count, _kids in I.containers(leaf, nodes):
            items = I.parse_items(leaf, off + 16, off + 16 + size)
            if not items:
                continue
            for it in items:
                if it.tag != RECORD:
                    continue
                body = leaf[it.off + 20:it.off + 20 + it.length]
                fields = decode(body)
                if fields is None or encode(fields) != body:
                    bad += 1
                    continue
                ok += 1
                by_form[fields["form"]] = by_form.get(fields["form"], 0) + 1
                key = (what, fields["form"])
                by_container[key] = by_container.get(key, 0) + 1
    print(f"records decoded and re-emitted : {ok}")
    print(f"  mismatched                   : {bad}")
    print(f"  by form                      : {by_form}")
    print("  by container:")
    for (what, form), n in sorted(by_container.items(), key=lambda kv: -kv[1]):
        print(f"    {what:<18} {form:<10} {n}")
    return 0 if bad == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
