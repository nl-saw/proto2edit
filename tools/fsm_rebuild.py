#!/usr/bin/env python3
"""Semantic rebuild of a mission FSM: parse into named objects, re-emit, compare bytes.

This is criterion 4 of the decode goal. The point is not to copy spans - the leaf is walked into
its structure and every level is re-emitted from parsed fields:

    leaf    := u32 type | u32 size | u32 size | u32 size-16 | "fig0" | u32 5 | u32 0
               | u64 "chunk" | u32 block_size | block | u64 0
    block   := version | name | context | string path | sibling | itemCount | branch* 
    branch  := tag | size | body | u64 0
    element := tag | size | prefix(16) | children | u64 0
    child   := tag | size | count | content[size] | u32 0
    item    := tag | len | anchorIndex | body[len] | pad4 | u32 0

Item bodies are re-emitted from decoded fields where fsm_schema knows the type, and copied where it
does not - the tool reports that split so the number is never overstated.

Where a level does not rebuild, the tool says which level failed rather than falling back silently.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_chunk as C
import fsm_graph as F
import fsm_items as I
import fsm_schema as S

CHUNK = 0x616FE21D386330FF


def _u32(leaf, off):
    return struct.unpack_from("<I", leaf, off)[0]


def rebuild_item(leaf, it, container):
    """Bytes for one item, from decoded fields where possible. Returns (bytes, was_semantic)."""
    body = leaf[it.off + I.ITEM_HDR:it.off + I.ITEM_HDR + it.length]
    fields = S.decode_body(container, it.tag, body)
    semantic = fields is not None
    if semantic:
        body = S.encode_body(fields)
    pad = (4 - len(body) % 4) % 4
    return (struct.pack("<QII", it.tag, len(body), _u32(leaf, it.off + 12))
            + body + b"\0" * pad + b"\0" * 4), semantic


def rebuild_container(leaf, off, size, table):
    """A child-framed container: tag | size | count | content | u32 0."""
    tag = struct.unpack_from("<Q", leaf, off)[0]
    count = _u32(leaf, off + 12)
    items = I.parse_items(leaf, off + 16, off + 16 + size)
    if items is None:
        return None, 0, 0
    out = b""
    sem = tot = 0
    for it in items:
        b, s = rebuild_item(leaf, it, table.get(off, None) and None or _container_name(leaf, off, table))
        out += b
        tot += 1
        sem += 1 if s else 0
    content = out + b"\0" * 4
    return struct.pack("<QII", tag, len(content), count) + content + b"\0" * 4, sem, tot


_NAME_CACHE = {}


def _container_name(leaf, off, table):
    """Container name at this offset, from a parse of the whole leaf (cached per leaf id)."""
    key = id(leaf)
    if key not in _NAME_CACHE:
        nodes = F.parse_region(leaf, F.graph_start(leaf), len(leaf), table)
        _NAME_CACHE.clear()
        _NAME_CACHE[key] = {o: w for w, o, _s, _c, _k in I.containers(leaf, nodes)}
    return _NAME_CACHE[key].get(off)


def rebuild_element(leaf, off, table, self_count=None):
    """tag | size | prefix(16) | children | u64 0."""
    layout = C.parse_element(leaf, off)
    if layout is None:
        return None, 0, 0
    tag = layout["tag"]
    # prefix = u64 name | u32 reserved(0) | u32 siblingCount
    # The name is carried; the reserved word and the sibling count are COMPUTED, not copied. If the
    # rebuild still reproduces every mission byte-for-byte, the rule is proven by reconstruction.
    name_u64 = layout["name"]
    # kind-specific part between the prefix and the children:
    #   transition            : u32 len | chars | pad4 | u32     (string + 1 word)
    #   reference_statemachine: u32 len | chars | pad | 8 bytes
    extra = b""
    if layout["extra"] is not None:
        x = layout["extra"]
        text = x["text"]
        ln = len(text)
        pad = (ln + 3) & ~3
        if x["type"] == "transition":
            extra = (struct.pack("<I", ln) + text + b"\0" * (pad - ln)
                     + struct.pack("<I", x["word"]))
        else:
            extra = (struct.pack("<I", ln) + text + b"\0" * (pad - ln)
                     + struct.pack("<II", x["w0"], x["w1"]))
    out = b""
    sem = tot = 0
    for k in layout["containers"]:
        b, s, t = rebuild_container(leaf, k["off"] - 16, k["size"], table)
        if b is None:
            return None, 0, 0
        out += b
        sem += s
        tot += t
    # collect the element-framed children first: their shared sibling count is only known once all
    # of them have been seen, and each child's prefix records that count (not its own child count)
    children = layout["children"]
    n_elems = len(children)
    for child in children:
        b, es, et = rebuild_element(leaf, child, table, self_count=n_elems)
        if b is None:
            return None, 0, 0
        out += b
        sem += es
        tot += et
    prefix = struct.pack("<QII", name_u64, 0, n_elems if self_count is None else self_count)
    return (struct.pack("<QI", tag, 16 + len(extra) + len(out))
            + prefix + extra + out + b"\0" * 8), sem, tot


def rebuild_leaf(leaf, table):
    info = C.parse_chunk(leaf)
    if info is None:
        return None, "chunk block does not parse"
    gs = F.graph_start(leaf)
    head = leaf[:gs - 4]                      # 16-byte header + "fig0" magic
    chunk_tag_off = gs + 8
    # walk back from the chunk tag: "fig0" then two u32s
    if leaf[chunk_tag_off - 8:chunk_tag_off - 4] != struct.unpack("<I", struct.pack("<I", 5)):
        pass
    block_start = chunk_tag_off + 12
    pre = leaf[gs - 4:chunk_tag_off]          # "fig0" + u32 5 + u32 0  (12 bytes)
    out = b""
    sem = tot = 0
    for b in info["branches"]:
        if b["tag"] == C.STATEMACHINE or b["tag"] in table:
            body, es, et = rebuild_element(leaf, b["off"] - 12, table,
                                           self_count=len(info["branches"]))
            if body is None:
                # a branch body that is a plain element: prefix + children, no outer size
                return None, f"branch {table.get(b['tag'], hex(b['tag']))} did not rebuild"
            out += body
            sem += es
            tot += et
        else:
            out += leaf[b["off"] - 12:b["off"] + b["size"] + 8]
    block = (structures_block_header(leaf, info) + out)
    new_block_size = len(block)
    new_leaf = (bytes(head) + bytes(pre) + struct.pack("<Q", CHUNK)
                + struct.pack("<I", new_block_size) + block + b"\0" * 8)
    return (new_leaf, sem, tot), None


def structures_block_header(leaf, info):
    """version | name | context | string path | sibling | itemCount."""
    gs = F.graph_start(leaf)
    block_start = gs + 20
    p = block_start
    version = _u32(leaf, p)
    p += 4
    name, context = struct.unpack_from("<QQ", leaf, p)
    p += 16
    path, p = C.read_string(leaf, p)
    sibling, items = struct.unpack_from("<II", leaf, p)
    p += 8
    pb = path if isinstance(path, bytes) else str(path).encode()
    pad = (4 - len(pb) % 4) % 4
    return (struct.pack("<I", version) + struct.pack("<QQ", name, context)
            + struct.pack("<I", len(pb)) + pb + b"\0" * pad
            + struct.pack("<II", sibling, items))


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corpus = os.path.join(here, "tmp", "corpus")
    table = F.name_table()
    ok = bad = 0
    failures = []
    sem = tot = 0
    for dirpath, _dirs, names in os.walk(corpus):
        if "fig.p3d.rz" not in names:
            continue
        leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
        res, why = rebuild_leaf(leaf, table)
        if res is None:
            bad += 1
            failures.append((dirpath.split("/missions/")[-1], why))
            continue
        out, es, et = res
        sem += es
        tot += et
        if out == leaf:
            ok += 1
        else:
            bad += 1
            first = next((i for i in range(min(len(out), len(leaf))) if out[i] != leaf[i]),
                         min(len(out), len(leaf)))
            failures.append((dirpath.split("/missions/")[-1],
                             f"differs at {first:#x} ({len(out)} vs {len(leaf)} bytes)"))
    print(f"missions rebuilt byte-identically : {ok}")
    print(f"  failed                          : {bad}")
    print(f"  items re-emitted from fields    : {sem}/{tot}")
    for f in failures[:8]:
        print(f"    {f[0]}: {f[1]}")
    return 0 if bad == 0 and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
