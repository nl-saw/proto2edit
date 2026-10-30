#!/usr/bin/env python3
"""Every shipped record payload must be readable: source text, or a Lua chunk under the engine dictionary.

The regression this pins. Four `conditions` payloads (story_a1, story_l1, story_l2, story_e2) carry a 4-byte
prefix before the usual `7f 00 | u16 size | raw deflate` form. The engine never loaded them in any recorded
session, so no capture existed, and the offline path refused anything that did not *start* with the magic
bytes - which made its own prefix handling unreachable and reported the four as permanently locked. They
inflate under the engine dictionary like every other payload.

Two halves: a synthetic payload proves the framing rules without needing the game, and a corpus scan proves
that no shipped payload lands in `unknown`.
"""
import os
import json
os.environ["PROTO2_NO_CACHE"] = "1"   # the runtime capture cache is git-ignored; measure the published corpus
import struct
import sys
import shutil
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corpus_baseline as CB   # noqa: E402
import fsm_luablob as L        # noqa: E402
import recovery_report as R    # noqa: E402
import script_edit as SE       # noqa: E402

fails = []


def ck(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name:<44} {detail}")
    if not ok:
        fails.append(name)


def pack(chunk):
    """A packed payload in the engine's own framing (big-endian size, preset dictionary)."""
    c = zlib.compressobj(9, zlib.DEFLATED, -13, 9, 0, L.ENGINE_DICT)
    body = c.compress(chunk) + c.flush()
    return b"\x7f\x00" + struct.pack(">H", len(chunk)) + body


chunk = b"\x1bLua" + bytes(range(40))
packed = pack(chunk)
prefixed = b"\x01\x02\x03\x00" + packed

ck("prefixed payload stripped to packed form", L.strip_prefix(prefixed) == packed, "")
ck("stream identical for both shapes", L.stream_of(prefixed) == L.stream_of(packed),
   f"sha1 {L.stream_of(prefixed)[:4].hex()} / {L.stream_of(packed)[:4].hex()}")
ck("engine dictionary decodes the plain form", L.engine_dict_bytes(packed) == chunk, "")
ck("engine dictionary decodes the prefixed form", L.engine_dict_bytes(prefixed) == chunk,
   "the case that was locked")

# Corpus: classification must never fall through to unknown.
counts = {"source": 0, "bytecode": 0, "unknown": 0}
prefixed_records = 0
prefixed_readable = 0
for mission, variant, model in R.missions():
    for (it, payload, _role), (_r, text, _h, _x) in zip(SE.records_with_roles(model), SE._decoded(model)):
        kind = SE.record_kind(payload, text)
        counts[kind] += 1
        if kind == "unknown":
            print(f"      locked: {mission}/{variant} {_role}")
        if payload[:2] != L.MAGIC and payload[4:6] == L.MAGIC:
            prefixed_records += 1
            if kind != "unknown":
                prefixed_readable += 1

total = sum(counts.values())
ck("corpus classified", total == CB.RECORDS,
   f"{total} records (baseline {CB.RECORDS}; a different build changes this)")
ck("no locked payloads", counts["unknown"] == 0, f"unknown {counts['unknown']}")
ck("bytecode records recoverable", counts["bytecode"] == CB.BYTECODE,
   f"{counts['bytecode']} chunks (baseline {CB.BYTECODE})")
ck("prefixed payloads exist", prefixed_records >= 4, f"{prefixed_records} prefixed")
ck("every prefixed payload readable", prefixed_readable == prefixed_records,
   f"{prefixed_readable}/{prefixed_records}")

# Framing invariant. A record's payload shape follows its container `form`: `bare` records are
# `u32 length | body | zero padding`, `prefixed` ones are the packed 7f00 stream. Writing the wrong shape is
# what crashed story_l1 at load on 2026-10-25 (a prefixed payload in a bare conditions slot), so this scans
# what is staged and reports any source payload whose framing does not match its form.
violations = []
missions_root = os.path.join(SE.ROOT, "work", "mod", "art", "missions")
if os.path.isdir(missions_root):
    for dirpath, _dirs, names in os.walk(missions_root):
        if "fig.p3d.rz" not in names:
            continue
        mission = os.path.basename(os.path.dirname(dirpath))
        variant = os.path.basename(dirpath)
        try:
            staged = SE.open_side(mission, variant, "staged")
            shipped = SE.open_side(mission, variant, "shipped")
        except Exception:
            continue
        if not staged or not shipped:
            continue
        s_rows = SE.records_with_roles(staged[3])
        h_rows = SE.records_with_roles(shipped[3])
        for i, ((_a, ps, role), (_b, ph, _r2)) in enumerate(zip(s_rows, h_rows)):
            if bytes(ps) == bytes(ph):
                continue
            form = (_a.get("fields") or {}).get("form")
            body = bytes(ps)
            if form == "bare":
                declared = int.from_bytes(body[:4], "little") if len(body) >= 4 else -1
                bad = (declared <= 0 or declared + 4 > len(body) or len(body) % 4 != 0)
            else:
                bad = body[:2] != b"\x7f\x00"
            if bad:
                violations.append(f"{mission}#{i} {role} form={form}")
ck("staged payload framing matches each record's form", not violations, ", ".join(violations[:4]))

# LuaDec's banner and DECOMPILER ERROR comments are not part of any program; they name the machine that ran
# the decompiler and describe blocks it could not reconstruct. Staged text must not carry them (docs/26).
noisy = []
if os.path.isdir(missions_root):
    for dirpath, _dirs, names in os.walk(missions_root):
        if "fig.p3d.rz" not in names:
            continue
        mission = os.path.basename(os.path.dirname(dirpath))
        variant = os.path.basename(dirpath)
        try:
            staged = SE.open_side(mission, variant, "staged")
        except Exception:
            continue
        if not staged:
            continue
        for i, (_role, text, _how, _x) in enumerate(SE._decoded(staged[3])):
            if text is None:
                continue
            body = text if isinstance(text, str) else text.decode("latin-1")
            if any(line.lstrip().startswith(SE.DECOMPILER_NOISE) for line in body.splitlines()):
                noisy.append(f"{mission}#{i}")
ck("no staged record carries decompiler noise", not noisy, ", ".join(noisy[:4]))

if fails:
    print("records: FAILED " + ", ".join(fails))
    sys.exit(1)
print(f"records: {total} readable ({counts['source']} source, {counts['bytecode']} bytecode, 0 unknown)")

# --- constant edits: the path for records whose decompilation is not proven -------------------------------------
import lua51 as L51


def _flat_constants(root):
    out = []
    for p in [root] + root.protos:
        out.extend(c for c in p.constants if isinstance(c, bytes))
    return out


got = SE.open_side("story_intro1", "active", "shipped")
payload = SE.records_with_roles(got[3])[12][1]
spec = SE.chunk_constants(payload)
ck("constants are listed for a compiled record", spec is not None and len(spec) > 0, f"{len(spec or [])}")
target = next(c for c in spec if c["kind"] == "string")
before = L51.load(SE.bytecode_chunk(payload))
new_chunk, why = SE.patched_chunk(payload, target["path"], target["index"], "constants-test-value")
ck("a string constant can be replaced", new_chunk is not None, why or "")
after = L51.load(new_chunk)
ck("the instruction words are untouched by a constant edit", tuple(before.code) == tuple(after.code))
sb, sa = _flat_constants(before), _flat_constants(after)
ck("exactly one constant changed",
   len(sb) == len(sa) and sum(1 for a, b in zip(sb, sa) if a != b) == 1,
   f"{sum(1 for a, b in zip(sb, sa) if a != b)} changed")
ck("the replacement is the value that changed", b"constants-test-value" in sa)
bad, why = SE.patched_chunk(payload, target["path"], 9999, "x")
ck("an out-of-range constant is refused", bad is None and "out of range" in why, why or "")
num = next((c for c in spec if c["kind"] == "number"), None)
if num:
    nb, nwhy = SE.patched_chunk(payload, num["path"], num["index"], 123.5)
    ck("a number constant can be replaced", nb is not None, nwhy or "")
    ck("number replacement keeps the code identical",
       tuple(L51.load(nb).code) == tuple(before.code))

# --- the verdict cache must not outlive the toolchain that produced it ------------------------------------------
import tempfile
sandbox = tempfile.mkdtemp(dir=os.path.join(SE.ROOT, "tmp"), prefix="verdict-cache-")
saved_cache, saved_stamp = SE.VERDICT_CACHE, SE.VERDICT_STAMP
try:
    SE.VERDICT_CACHE = os.path.join(sandbox, "verdicts.json")
    SE.VERDICT_STAMP = SE.VERDICT_CACHE + ".version"
    with open(SE.VERDICT_CACHE, "w") as f:
        json.dump({"deadbeef": {"status": "equivalent", "detail": "stale"}}, f)
    with open(SE.VERDICT_STAMP, "w") as f:
        f.write("not-the-current-stamp\n")
    SE._verdicts = None
    ck("a stale verdict cache is ignored", SE.load_verdict_cache() == {})
    ck("cached_verdict ignores a stale cache",
       SE.cached_verdict(SE.records_with_roles(got[3])[12][1]) is None)
    with open(SE.VERDICT_STAMP, "w") as f:
        f.write(SE.tool_version() + "\n")
    SE._verdicts = None
    ck("a current cache is read", len(SE.load_verdict_cache()) == 1)
finally:
    SE.VERDICT_CACHE, SE.VERDICT_STAMP = saved_cache, saved_stamp
    SE._verdicts = None
    shutil.rmtree(sandbox, ignore_errors=True)

