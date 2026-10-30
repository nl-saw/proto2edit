#!/usr/bin/env python3
"""List, extract and edit a mission's embedded Lua scripts.

A mission's Lua lives in record items inside `art/missions/<m>/<v>/fig.p3d.rz`. Each record payload is
either zlib-packed (`7f 00 | u16 size | deflate`), plain text, or a u32-length-prefixed string. Records are
reported in three states: `source` (editable text), `[BYTECODE]` (a compiled Lua 5.1 chunk recovered from a
runtime capture - readable, and convertible when `decompile --all` proves equivalence) and `[LOCKED]`
(unknown; preserved byte-for-byte, never guessed at).

An edit is only written when ALL of these hold, so a partial result can never reach the game:

  1. the text was located unambiguously (the anchor occurs exactly once in that record);
  2. the edited text re-compresses and re-decodes to exactly itself;
  3. the re-emitted leaf re-parses and re-encodes byte-identically;
  4. the spliced file still finds its leaf at the same offset;
  5. every payload that was not edited is byte-identical to the original, including undecodable ones;
  6. the edited record still compiles as Lua when the record is a whole script (tools/luacheck).

    ./proto2 script --mission story_a1 list
    ./proto2 script --mission story_a1 extract --index 3 --out tmp/a1-3.lua
    ./proto2 script --mission story_a1 replace  --index 3 --old OLD --new NEW
    ./proto2 script --mission story_a1 replace  --old OLD --new NEW      # across all records
    ./proto2 script --mission story_a1 replace  --old OLD --new NEW --stage
"""
import argparse
import os
import re
import difflib
import json
import hashlib
import types
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_edit as E
import fsm_graph as F
import fsm_luablob as L
import fsm_write as W
import rz
import config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = ROOT
CORPUS = os.path.join(ROOT, "tmp", "corpus")

# Roles whose payload must stay compiled bytecode.
#
# HISTORY, because the old rule here was wrong. Measured 2026-10-25: converting ONE `conditions` record in
# story_l1 (record #25) made the mission crash at load, bisected down to that single record. That was recorded
# as "the FSM loads condition scripts as chunks".
#
# Corrected 2026-10-27: every `conditions` record uses container `form: bare` (u32 length | body | zero
# padding to a 4-byte total) while every other role uses `form: prefixed` (the packed 7f00 stream). The bisect
# had written a prefixed payload into a bare slot - a framing bug, not a role rule. Bare payloads are as often
# plain text as packed streams in the shipped files, so the engine already loads text there. Re-encoded with
# `fsm_luablob.bare_encode`, story_l1 #25 loads the mission in game (owner test, 2026-10-27).
#
# The set is kept because the *mechanism* is still worth naming: a role that cannot be converted would go here.
NON_CONVERTIBLE_ROLES = set()
MOD = os.environ.get("PROTO2_SCRIPT_MOD") or os.path.join(ROOT, "work", "mod")
LUACHECK = os.path.join(ROOT, "build", "luacheck")


def _staged_path(mission, variant):
    return os.path.join(MOD, "art", "missions", mission, variant or "active", "fig.p3d.rz")


def base_path(mission, variant=None):
    """The file edits build on: the staged working copy if one exists, else the shipped mission.

    Staging used to start from the shipped file every time, so a second edit silently threw the first one
    away. Treating the staged file as the working copy makes edits compose: what you stage accumulates,
    and `diff --against staged` keeps showing the total change relative to the shipped mission.
    """
    staged = _staged_path(mission, variant)
    if os.path.exists(staged):
        return staged, True
    return find_mission(mission, variant), False


def find_mission(name, variant=None):
    """Path to the mission's fig.p3d.rz in the extracted corpus."""
    hits = []
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        parts = [p for p in dirpath.split("/") if p]
        if parts[-2] != name:
            continue
        if variant and parts[-1] != variant:
            continue
        hits.append(os.path.join(dirpath, "fig.p3d.rz"))
    if not hits:
        raise SystemExit(f"mission {name!r} not found under {CORPUS}")
    hits.sort()
    return hits[0]


_variant_cache = {}


def variants(name):
    """Variant names that exist for this mission in the corpus, 'active' first.

    The GUI used to offer a fixed ["active", "open"] list, so a mission with any other variant was unreachable
    through the interface with no sign that it existed. Derived from the corpus instead.
    """
    if name in _variant_cache:
        return list(_variant_cache[name])
    found = set()
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        parts = [p for p in dirpath.replace("\\", "/").split("/") if p]
        if len(parts) >= 2 and parts[-2] == name:
            found.add(parts[-1])
    out = sorted(found, key=lambda v: (v != "active", v))
    _variant_cache[name] = out
    return list(out)


def records_with_roles(model):
    """(item, payload, role) for every record, role being its container's tag name.

    The role is what makes a locked record reportable ("[LOCKED] enterTracks[4]") instead of just
    "undecodable": the container names the part of the mission the user cannot reach.
    """
    import tag_names as TN
    out = []
    for el in E.walk_elements(model):
        for c in el["containers"]:
            try:
                role = TN.lookup(c["tag"])[0]
            except Exception:
                role = f"{c['tag']:#x}"
            for it in c["items"]:
                if (it.get("fields") or {}).get("type") == "record":
                    out.append((it, it["fields"]["payload"], role))
    return out


def records(model):
    """(item, payload) for every record in model order."""
    return [it for it in (r[0] for r in records_with_roles(model))]


def open_mission(path):
    blob = open(path, "rb").read()
    if blob[:2] == b"RZ":            # staged files are RZ-wrapped; the model works on the inner bytes
        blob = rz.unwrap(blob)
    leaf, leaf_off = F.payload(blob)
    model = E.parse(leaf)
    return blob, leaf, leaf_off, model


def open_side(mission, variant=None, side="working"):
    """(blob, leaf, leaf_off, model) for a mission from a named side, or None if absent.

    Sides match the GUI selector: working (staged else shipped), shipped, staged, installed.
    """
    if side == "working":
        blob = open(base_path(mission, variant)[0], "rb").read()
    elif side == "shipped":
        blob = open(find_mission(mission, variant), "rb").read()
    else:
        blob, _why = _load_side(mission, variant or "active", side)
        if blob is None:
            return None
    if blob[:2] == b"RZ":
        blob = rz.unwrap(blob)
    leaf, leaf_off = F.payload(blob)
    return blob, leaf, leaf_off, E.parse(leaf)


def decode_all_records(model):
    """(text or None, how) per record, threading the previous text as the dictionary."""
    out, prev = [], b""
    for it in records(model):
        t, how = L.read(it["fields"]["payload"], prev[-32768:] if prev else b"")
        out.append((t, how))
        if t is not None:
            prev += t.encode("latin-1") if isinstance(t, str) else t
    return out


def _recover_engine_chunk(payload):
    """If the payload inflates to Lua bytecode under the engine's preset dictionary, store the chunk.

    compress::Decompressor_Zlib installs a fixed 56-byte dictionary (see fsm_luablob.ENGINE_DICT), so payloads
    that reference it are unreadable under any other rule. Their output is Lua bytecode, not text - which is why
    they were classed 'unknown'. Saving it to work/bytecode lets the normal bytecode path (listing, LuaDec,
    equivalence) pick it up.

    The file is keyed by the bare stream (`L.stream_of`), the same key `bytecode_chunk` computes, so a record
    that carries a 4-byte prefix before the packed payload resolves to the same file as one that does not.
    Four shipped payloads (all `conditions` records) only decode this way; before the prefix was stripped they
    were reported as locked forever, because the engine never loaded them in any recorded session.
    """
    out = L.engine_dict_bytes(bytes(payload))
    if not out or out[:4] != b"\x1bLua":
        return None
    d = os.path.join(ROOT, "work", "bytecode")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, hashlib.sha1(L.stream_of(payload)).hexdigest() + ".luac")
    if not os.path.exists(path):
        open(path, "wb").write(out)
    return path


def _decoded(model):
    """(role, text or None, how, item) per record, threading the previous text as the dictionary."""
    out, prev = [], b""
    for it, payload, role in records_with_roles(model):
        t, how = L.read(payload, prev[-32768:] if prev else b"")
        if t is None:
            # last resort that is actually the engine's own rule: its fixed preset dictionary
            if _recover_engine_chunk(payload) is not None:
                how = "engine-dictionary (bytecode)"
        out.append((role, t, how, it))
        if t is not None:
            prev += t.encode("latin-1") if isinstance(t, str) else t
    return out


def cmd_list(a):
    path, staged_base = base_path(a.mission, a.variant)
    blob, leaf, _off, model = open_mission(path)
    dec = _decoded(model)
    print(f"{a.mission}: {len(dec)} record(s) in {len(blob)} bytes")
    print(f"{'idx':>4} {'role':<14} {'bytes':>7}  {'state':<10} first line")
    counts, by_role = {}, {}
    bc = 0
    for i, (role, t, how, it) in enumerate(dec):
        payload = it["fields"]["payload"]
        if t is not None:
            first = (t.encode("latin-1") if isinstance(t, str) else t).split(b"\n", 1)[0]
            head = first[:52].decode("latin-1", "replace").rstrip()
            print(f"{i:>4} {role:<14} {len(payload):>7}  {'editable':<10} {head}")
            counts[role] = counts.get(role, [0, 0, 0]); counts[role][0] += 1
            by_role[role] = by_role.get(role, [0, 0, 0]); by_role[role][0] += 1
        else:
            kind = record_kind(payload, None)
            if kind == "bytecode":
                info, _lst = bytecode_listing(payload)
                detail = (f"<Lua 5.1 chunk: {info['insns']} insns, {len(info['strings'])} strings>"
                          if info else "<bytecode>")
                print(f"{i:>4} {role:<14} {len(payload):>7}  {'[BYTECODE]':<10} {detail}")
                bc += 1
            else:
                print(f"{i:>4} {role:<14} {len(payload):>7}  {'[LOCKED]':<10} <undecodable: preserved byte-for-byte>")
            counts[role] = counts.get(role, [0, 0, 0])
            counts[role][1 if kind != "bytecode" else 2] += 1
            by_role[role] = by_role.get(role, [0, 0, 0])
            by_role[role][1 if kind != "bytecode" else 2] += 1
    editable = sum(1 for _r, t, _h, _i in dec if t is not None)
    print()
    for role in sorted(by_role):
        ok, unk, bcr = by_role[role]          # do not reuse `bc` here: it holds the running total
        bits = [f"{ok:>3} editable"]
        if bcr:
            bits.append(f"{bcr:>3} bytecode")
        if unk:
            bits.append(f"{unk:>3} unknown")
        print(f"  {role:<14} " + ", ".join(bits))
    print(f"\neditable {editable} of {len(dec)}; bytecode {bc}; unknown {len(dec) - editable - bc} "
          f"(bytecode and unknown payloads are never modified unless a bytecode edit is requested "
          f"explicitly)")
    return 0


def cmd_listing(a):
    got = open_side(a.mission, a.variant, "shipped")   # listings describe the shipped bytecode
    if got is None:
        raise SystemExit("mission not found")
    rows = records_with_roles(got[3])
    i = a.index
    if not (0 <= i < len(rows)):
        raise SystemExit(f"record {i} out of range")
    payload = rows[i][1]
    got_list = bytecode_listing(payload)
    if got_list is None:
        raise SystemExit(f"record {i} is not known bytecode (no captured chunk)")
    info, text = got_list
    print(f"{a.mission} record #{i} ({rows[i][2]}): {info['bytes']} byte chunk, "
          f"{info['insns']} instructions, {info['protos']} nested proto(s)")
    print("strings:", ", ".join(repr(x) for x in info["strings"][:12]))
    print()
    print(text)
    return 0


def cmd_extract(a):
    path, staged_base = base_path(a.mission, a.variant)
    _blob, _leaf, _off, model = open_mission(path)
    if getattr(a, "report", False):
        rows = _decoded(model)
        locked = [(r, i) for i, (r, t, _h, _x) in enumerate(rows) if t is None]
        print(f"{a.mission}: {len(rows) - len(locked)} editable, {len(locked)} locked")
        for role, i in locked:
            print(f"  [LOCKED] {role} #{i}  (content unreachable; never modified)")
        return 0
    dec = [(t, h) for _r, t, h, _i in _decoded(model)]
    if a.index is None:
        text = b"\n".join((t.encode("latin-1") if isinstance(t, str) else t)
                          for t, _ in dec if t is not None)
        out = a.out or os.path.join(ROOT, "tmp", "scripts", f"{a.mission}.lua")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "wb").write(text)
        print(f"wrote {out} ({len(text)} bytes from {sum(1 for t, _ in dec if t is not None)} records)")
        return 0
    t, how = dec[a.index]
    if t is None:
        raise SystemExit(f"record {a.index} is undecodable ({how}); it is preserved untouched")
    text = t.encode("latin-1") if isinstance(t, str) else t
    if a.out:
        open(a.out, "wb").write(text)
        print(f"wrote {a.out} ({len(text)} bytes)")
    else:
        sys.stdout.write(text.decode("latin-1"))
    return 0


def compile_ok(text, name):
    """Compile the fragment with LuaJIT; returns (ok, message). Not a proof of intent, only of syntax."""
    if not os.path.exists(LUACHECK):
        return True, "luacheck not built; skipped"
    tmp = os.path.join(ROOT, "tmp", "script-edit-check.lua")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    open(tmp, "wb").write(text)
    r = subprocess.run([LUACHECK, tmp], capture_output=True, text=True)
    return ("syntax OK" in (r.stdout + r.stderr)), (r.stdout + r.stderr).strip().splitlines()[-1:] and (r.stdout + r.stderr).strip().splitlines()[-1] or ""


GAME = config.game_dir()
MARK = "-- proto2:record #%d %s"
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def escape_ctrl(text):
    r"""Render raw control bytes as \xNN so they cannot masquerade as font glyphs.

    Some payloads decode with stray control bytes (an imperfect decode, not text the game uses). Shown
    raw, a terminal or a Tk text widget draws them as control pictures and they look like corruption the
    user might try to "fix" by deleting - which would change bytes they did not intend to touch. Escaping
    is reversible, and `unescape_ctrl` restores the exact bytes before anything is written.
    """
    return _CTRL.sub(lambda m: "\\x%02x" % ord(m.group()), text)


def unescape_ctrl(text):
    """Inverse of escape_ctrl - only sequences that escape_ctrl could have produced."""
    return re.sub(r"\\x([0-9a-f]{2})",
                  lambda m: chr(int(m.group(1), 16)) if ord(chr(int(m.group(1), 16))) in _CTRL_CODES
                  else m.group(), text)


_CTRL_CODES = set(list(range(0, 9)) + [11, 12] + list(range(14, 32)))


def control_bytes(text):
    """The control bytes present in a text, as {hex: count} - used to warn about imperfect decodes."""
    out = {}
    for ch in text:
        if ord(ch) in _CTRL_CODES:
            k = "0x%02x" % ord(ch)
            out[k] = out.get(k, 0) + 1
    return out


READONLY_END = "-- proto2:end-readonly"


def export_script(mission, variant=None, side="stock", include_bytecode=False):
    """The whole mission script as one editable text, with a marker per record.

    `side` selects which bytes to show: "stock" (the shipped file - the base every edit starts from),
    "staged" (work/mod) or "installed" (the game directory). Only stock is editable; the other two are
    for seeing what an edit actually produced.

    Locked records appear as a marker plus a note and no body: they have no text, and import refuses to
    change them. Bodies are shown with LF endings; import converts back to CRLF, which is what every
    payload uses (measured: 2983 CRLF, 0 bare LF in story_a1).
    """
    if side in ("stock", "working"):
        path = (find_mission(mission, variant) if side == "stock"
                else base_path(mission, variant)[0])
        _blob, _leaf, _off, model = open_mission(path)
    else:
        blob, src = _load_side(mission, variant or "active", side)
        if blob is None:
            raise SystemExit(src)
        inner = rz.unwrap(blob) if blob[:2] == b"RZ" else blob
        _leaf, _off = F.payload(inner)
        _blob, model = inner, E.parse(_leaf)
    # A compiled record has no text payload. Say which of the three situations it is, because they need different
    # actions from the reader: converted already, convertible, or a reconstruction that is not proven.
    import json as _json
    try:
        _verdicts = _json.load(open(os.path.join(ROOT, "artifacts", "bytecode-verdicts.json")))
    except Exception:
        _verdicts = {}
    side_tag = f"-- proto2:side {side}{' include-bytecode' if include_bytecode else ''}"
    out = [side_tag]
    for i, (role, t, _how, _it) in enumerate(_decoded(model)):
        out.append(MARK % (i, role))
        if t is None:
            payload = _it["fields"]["payload"]
            chunk = bytecode_chunk(payload)
            if chunk is None:
                out.append("-- [LOCKED] content unreachable; preserved byte-for-byte")
            else:
                import hashlib as _h
                entry = _verdicts.get(_h.sha1(chunk).hexdigest()) or {}
                status, detail = entry.get("status"), (entry.get("detail") or "")
                if status == "equivalent":
                    out.append(f"-- [BYTECODE] provably convertible - open record #{i} in the Script Map and "
                               f"press 'Stage as source' to edit it")
                else:
                    why = f"decompilation not verified ({detail})" if status == "differs" else "no verified source"
                    packed = None
                    try:
                        import overrides as _OV
                        packed = _OV.find(mission, variant or "active", i)
                    except Exception:
                        packed = None
                    hint = (" - a hand-written reconstruction of this record is in the override pack: run "
                            "./proto2 overrides apply (or press 'Apply override pack' on the Deploy tab)") if packed else ""
                    if include_bytecode:
                        note = (f"-- [READ-ONLY] {why} - text below is for reference and cannot be applied; "
                                f"the shipped bytecode is what runs{hint}")
                    else:
                        note = (f"-- [READ-ONLY] {why} - tick 'show read-only text' to display it here, "
                                f"or open record #{i} in the Script Map{hint}")
                    out.append(note)
                    if include_bytecode:
                        src, _note = decompiled_text(payload)
                        if src is not None:
                            out.append(src.rstrip("\n"))
                            out.append(READONLY_END)
        else:
            tb = t.encode("latin-1") if isinstance(t, str) else t
            out.append(escape_ctrl(tb.decode("latin-1").replace("\r\n", "\n")))
        out.append("")
    return "\n".join(out)


def import_script(a):
    """Apply an edited whole-script text back, record by record, through the verified path."""
    text = a.text.replace("\r\n", "\n")
    parts = {}
    cur = None
    for line in text.split("\n"):
        if line.startswith("-- proto2:side "):
            continue
        m = re.match(r"-- proto2:record #(\d+) (\S+)\s*$", line)
        if m:
            cur = int(m.group(1))
            parts[cur] = []
            continue
        if cur is not None:
            parts[cur].append(line)

    path, staged_base = base_path(a.mission, a.variant)
    side_line = re.search(r"^-- proto2:side (\S+)( include-bytecode)?", text, re.M)
    if side_line:
        exported = side_line.group(1)
        # "working" and "staged" both mean the base this apply writes to; anything else would compare an export of
        # different bytes against it and turn every converted record into an "edit"
        base_is_staged = staged_base
        if exported in ("stock",) and base_is_staged:
            raise SystemExit(f"this text was exported from the shipped mission (-- proto2:side stock) but "
                             f"{a.mission} has staged edits; applying it would rewrite them. Re-export the "
                             f"working copy, or revert the staging first.")
        if exported in ("staged", "installed") and not base_is_staged:
            raise SystemExit(f"this text was exported from the {'staged' if exported == 'staged' else 'installed'} "
                             f"copy but there is no staged file for {a.mission}; export the shipped mission or "
                             f"stage something first.")
    blob, leaf, leaf_off, model = open_mission(path)
    rows = records_with_roles(model)
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    dec = _decoded(model)
    before = [bytes(it["fields"]["payload"]) for it in recs]

    missing = [i for i in range(len(recs)) if i not in parts]
    if missing:
        raise SystemExit(f"markers missing for record(s) {missing[:6]} - "
                         f"the text was edited in a way that removed them; refusing to write")

    changed = []
    for i, (role, t, _how, _it) in enumerate(dec):
        lines_i = parts[i]
        if lines_i and lines_i[-1] == "":      # exactly the one separator newline export added
            lines_i = lines_i[:-1]
        body = unescape_ctrl("\n".join(lines_i))
        if t is None:
            if body.strip() and READONLY_END in body:
                # a read-only block was included for reference: it must match what export would produce now,
                # otherwise someone edited text that cannot be applied and the apply would silently drop it
                payload = recs[i]["fields"]["payload"]
                src, _n = decompile_payload(payload)
                expected = None
                if src is not None:
                    expected_body = body
                    if src.rstrip("\n") not in expected_body:
                        expected = "differs"
                if expected or src is None:
                    raise SystemExit(f"record {i} ({role}) is a read-only reference block and it was modified; "
                                     f"refusing to write. Revert that block, or edit the record in the Script Map.")
                body = ""
            elif body.strip():
                stripped = body.strip()
                if stripped.startswith("-- [") and "\n" not in stripped:
                    body = ""
                if body:
                    raise SystemExit(f"record {i} ({role}) is a compiled chunk and cannot be edited as text; "
                                     f"its block in the whole script is reference only. Edit it in the Script Map "
                                     f"once it is convertible.")
            continue
        tb = t.encode("latin-1") if isinstance(t, str) else t
        old = tb.decode("latin-1").replace("\r\n", "\n")
        if body == old:
            continue
        new = body.replace("\n", "\r\n").encode("latin-1")
        payload = L.encode(new)
        ok, msg = L.verify_encoded(payload, new)
        if not ok:
            raise SystemExit(f"record {i}: re-pack failed to verify ({msg})")
        recs[i]["fields"]["payload"] = payload
        changed.append((i, len(before[i]), len(payload)))

    if not changed:
        print("no changes vs the shipped script; nothing staged")
        return 0
    return _finalize(a, path, blob, leaf, leaf_off, model, dec, roles, before, changed)


def _proto_paths(root, prefix=""):
    """(path, proto) for the root and every nested prototype, depth first."""
    out = [(prefix, root)]
    for i, child in enumerate(root.protos):
        out.extend(_proto_paths(child, f"{prefix}.{i}" if prefix else str(i)))
    return out


def _structure(root):
    """Everything except the constant tables: code, layout, nesting, debug names."""
    return (tuple(root.code), root.num_params, root.is_vararg, root.max_stack, root.nups,
            tuple(_structure(c) for c in root.protos))


def chunk_constants(payload):
    """The constants of a bytecode record, flattened across its nested functions.

    Each entry is {"path", "index", "kind", "value"} where `value` is display text. Constants are what a record
    can still be edited through when its decompilation cannot be proven: changing one leaves every instruction word
    untouched.
    """
    chunk = bytecode_chunk(payload)
    if chunk is None:
        return None
    import lua51 as L51
    root = L51.load(chunk)
    out = []
    for path, proto in _proto_paths(root):
        for i, value in enumerate(proto.constants):
            kind = L51.constant_kind(value)
            if kind == "string":
                shown = value.decode("latin-1")
            elif kind == "number":
                shown = repr(float(value))
            elif kind == "boolean":
                shown = "true" if value else "false"
            else:
                shown = "nil"
            out.append({"path": path, "index": i, "kind": kind, "value": shown,
                        "proto": proto.name})
    return out


def patched_chunk(payload, path, index, new_value):
    """A new chunk with one constant replaced, or (None, reason).

    The guarantee is stronger than decompiling: the instruction words, prototype layout and every other constant
    are compared before and after, so only the intended value can differ.
    """
    chunk = bytecode_chunk(payload)
    if chunk is None:
        return None, "no captured chunk"
    import lua51 as L51
    before = L51.load(chunk)
    after = L51.load(chunk)
    try:
        L51.patch_constant(after, index, new_value, path)
    except Exception as exc:
        return None, f"{exc}"
    if _structure(before) != _structure(after):
        return None, "patching changed the structure (refused)"
    pa = {p: proto for p, proto in _proto_paths(before)}
    pb = {p: proto for p, proto in _proto_paths(after)}
    for p in pa:
        a, b = list(pa[p].constants), list(pb[p].constants)
        if len(a) != len(b):
            return None, "constant count changed (refused)"
        for i, (x, y) in enumerate(zip(a, b)):
            if p == path and i == index:
                if x == y:
                    return None, "the constant already has that value"
            elif x != y:
                return None, "a different constant changed (refused)"
    return L51.dump(after), None


def set_record_constant(a):
    """Replace one constant of a compiled record (the GUI's constants panel, and the CLI).

    Compiled records whose decompilation cannot be proven are still editable this way: the payload is rebuilt from a
    chunk whose code words are verified identical, so nothing but the chosen value can move.
    """
    path, staged_base = base_path(a.mission, a.variant)
    blob, leaf, leaf_off, model = open_mission(path)
    rows = records_with_roles(model)
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    dec = _decoded(model)
    before = [bytes(it["fields"]["payload"]) for it in recs]

    i = a.index
    if not (0 <= i < len(recs)):
        raise SystemExit(f"record {i} out of range (0..{len(recs) - 1})")
    if dec[i][1] is not None:
        raise SystemExit(f"record {i} ({roles[i]}) is source text; edit the text instead")
    value = a.value
    spec = chunk_constants(before[i])
    if spec is None:
        raise SystemExit(f"record {i} ({roles[i]}) has no captured chunk")
    found = [c for c in spec if c["path"] == a.path and c["index"] == a.const]
    if not found:
        raise SystemExit(f"record {i}: no constant #{a.const} at path {a.path or '(root)'}")
    kind = found[0]["kind"]
    if kind == "string":
        value = value
    elif kind == "number":
        value = float(value)
    elif kind == "boolean":
        value = value.strip().lower() in ("true", "1", "yes")
    else:
        raise SystemExit(f"constant #{a.const} is nil and cannot be replaced")
    new_chunk, why = patched_chunk(before[i], a.path, a.const, value)
    if new_chunk is None:
        raise SystemExit(f"record {i}: {why}")
    payload = L.encode(new_chunk)
    ok, msg = L.verify_encoded(payload, new_chunk)
    if not ok:
        raise SystemExit(f"record {i}: re-pack failed to verify ({msg})")
    recs[i]["fields"]["payload"] = payload
    changed = [(i, len(before[i]), len(payload))]
    # a compiled record has no decoded text, so the "undecodable payload changed" guard must be told this edit is
    # deliberate; the chunk comparison above is the real guarantee
    return _finalize(types.SimpleNamespace(
        mission=a.mission, variant=a.variant, stage=getattr(a, "stage", False),
        dry_run=getattr(a, "dry_run", False), no_compile_check=True,
        allow_locked_edit=True, allow_bytecode=True, allow_window_shift=True),
        path, blob, leaf, leaf_off, model, dec, roles, before, changed)


def set_record_text(a):
    """Replace one record's whole text (the GUI editor's path).

    Same guarantees as `replace`: the record must be decodable, the edited text must re-pack and
    verify, the spliced file must carry the intended leaf, every other payload must stay byte-identical
    (including the locked ones), and the result is compiled before it is written.
    """
    path, staged_base = base_path(a.mission, a.variant)
    blob, leaf, leaf_off, model = open_mission(path)
    rows = records_with_roles(model)
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    dec = _decoded(model)
    before = [bytes(it["fields"]["payload"]) for it in recs]

    i = a.index
    if not (0 <= i < len(recs)):
        raise SystemExit(f"record {i} out of range (0..{len(recs) - 1})")
    if dec[i][0] is None:
        raise SystemExit(f"record {i} ({roles[i]}) is LOCKED - its content cannot be decoded, so it "
                         f"cannot be edited; it will be preserved byte-for-byte")

    new_text = a.text.encode("latin-1") if isinstance(a.text, str) else a.text
    # A no-op must be a no-op: re-compressing identical text produces different BYTES (the original was
    # packed with the engine's 32 KB window), so comparing payloads is not enough - compare the text.
    old_text = dec[i][0]
    old_bytes = old_text.encode("latin-1") if isinstance(old_text, str) else old_text
    if new_text == old_bytes:
        print(f"record #{i} ({roles[i]}) is unchanged; nothing staged")
        return 0
    payload = L.encode(new_text)
    ok, msg = L.verify_encoded(payload, new_text)
    if not ok:
        raise SystemExit(f"record {i}: re-pack failed to verify ({msg})")
    recs[i]["fields"]["payload"] = payload
    old_len = len(before[i])
    changed = [(i, old_len, len(payload))]
    return _finalize(a, path, blob, leaf, leaf_off, model, dec, roles, before, changed)


_STOCK_DECODE = {}


def _stock_decode(mission, variant):
    """The shipped file's per-record text - the reference untouched records must still match."""
    key = (mission, variant or "active")
    if key not in _STOCK_DECODE:
        try:
            path = find_mission(mission, variant)
            _b, _l, _o, model = open_mission(path)
            _STOCK_DECODE[key] = [t for (_r, t, _h, _i) in _decoded(model)]
        except Exception:
            _STOCK_DECODE[key] = None
    return _STOCK_DECODE[key]


def _window_check(newleaf, dec, changed, roles, allow=False, stock=None):
    """Fail if editing a record changes how any OTHER record decodes.

    The engine reuses one inflate state, so each payload's dictionary is the previous output. Shortening
    or lengthening a record therefore shifts the window for everything after it, and a copy-heavy payload
    downstream can decode to different text. `changed` records are expected to differ; untouched ones must
    not.
    """
    touched = {i for i, _b, _n in changed}
    try:
        again = decode_all_records(E.parse(newleaf))
    except Exception as exc:
        raise SystemExit(f"re-decoding the edited leaf failed: {exc}")
    # compare against the file this edit was built on (the working copy), NOT the shipped one: edits
    # already staged are legitimately different from shipped and must not be counted as damage
    reference = [row[1] for row in dec]
    bad = []
    for i, (t, _how) in enumerate(again):
        if i in touched:
            continue
        old = reference[i] if i < len(reference) else None
        if isinstance(old, tuple):          # (text, how) rows from decode_all_records
            old = old[0]
        if old is None and t is None:
            continue
        ob = (old.encode("latin-1") if isinstance(old, str) else old) if old is not None else None
        nb = (t.encode("latin-1") if isinstance(t, str) else t) if t is not None else None
        if ob != nb:
            bad.append((i, roles[i] if i < len(roles) else "?"))
    if bad and allow:
        print(f"  WARNING: {len(bad)} other record(s) decode differently after this edit "
              f"({', '.join(f'#{i}' for i, _r in bad[:6])}) - allowed by --allow-window-shift")
        return
    if bad:
        where = ", ".join(f"#{i} {r}" for i, r in bad[:6])
        raise SystemExit(
            f"refusing: this edit changes how {len(bad)} other record(s) decode ({where}). "
            f"The engine threads one inflate window, so a longer or shorter record shifts every payload "
            f"after it. Apply the edit to a later record, or make it length-neutral.")


def _finalize(a, path, blob, leaf, leaf_off, model, dec, roles, before, changed):
    """Verify, emit, report and stage. Shared by every edit path so they cannot diverge."""
    newleaf = E.encode(model)
    if E.encode(E.parse(newleaf)) != newleaf:
        raise SystemExit("re-emitted leaf does not round-trip; refusing to write")

    _window_check(newleaf, dec, changed, roles, allow=getattr(a, "allow_window_shift", False))
    patched = bytearray(blob[:leaf_off] + newleaf + blob[leaf_off + len(leaf):])
    W.bump_chunk_sizes(patched, blob, leaf_off, len(newleaf) - len(leaf),
                       [c for c in W.chunk_path(blob, leaf_off) if c != leaf_off])
    out = bytes(patched)

    leaf2, off2 = F.payload(out)
    if off2 != leaf_off or leaf2 != newleaf:
        raise SystemExit("spliced file does not carry the intended leaf; refusing to write")
    model2 = E.parse(leaf2)
    after = [bytes(it["fields"]["payload"]) for it in records(model2)]
    if len(after) != len(before):
        raise SystemExit("record count changed; refusing to write")
    untouched_bad = [i for i, (x, y) in enumerate(zip(before, after))
                     if x != y and i not in {c[0] for c in changed}]
    if untouched_bad:
        raise SystemExit(f"records changed that should not have: {untouched_bad}; refusing to write")
    # `dec` may be (text, how) pairs or (role, text, how, item) rows; normalise to the text
    texts = [r[1] if len(r) == 4 else r[0] for r in dec]
    opaque = [i for i, t in enumerate(texts) if t is None]
    touched_opaque = [i for i in opaque if before[i] != after[i]]
    if touched_opaque:
        if not getattr(a, "allow_locked_edit", False):
            raise SystemExit("an undecodable payload changed; refusing to write "
                             "(pass --allow-locked-edit only for an explicit experiment)")
        print(f"  OVERRIDE: replacing locked payload(s) {touched_opaque} - bytecode is being swapped "
              f"for content this tool cannot decode, so the usual guarantee does not apply")

    # compile check, per changed record
    for i, _b, _n in changed:
        t2, _how = L.read(after[i])
        if t2 is None:
            if getattr(a, "allow_bytecode", False):
                # only used when putting a record back to its shipped payload: becoming bytecode again is
                # the point of the operation, not a decode failure
                print(f"  note: record {i} is bytecode again (reverted to the shipped payload)")
                continue
            raise SystemExit(f"record {i} no longer decodes after the edit; refusing to write")
        tb = t2.encode("latin-1") if isinstance(t2, str) else t2
        ok, msg = compile_ok(tb, i)
        if not ok and not a.no_compile_check:
            print(f"  note: record {i} does not compile standalone ({msg}) - "
                  f"it may be a fragment, not a whole script")

    if getattr(a, "dry_run", False):
        # Validate everything, write nothing. Batch edits use this to refuse atomically.
        print(f"  [dry-run] {a.mission}: would change {len(changed)} record(s), "
              f"file {len(blob)} -> {len(out)} ({len(out) - len(blob):+d})")
        return out

    out_path = os.path.join(ROOT, "tmp", f"{a.mission}.edited.p3d")
    open(out_path, "wb").write(out)
    print("pre-install diff summary")
    print(f"  records changed : {len(changed)}")
    for i, b, n in changed:
        print(f"    #{i:<4} {roles[i]:<13} {b} -> {n} bytes  ({n - b:+d})")
    print(f"  records locked  : {sum(1 for t in texts if t is None)} (all preserved byte-for-byte)")
    print(f"  file delta      : {len(out) - len(blob):+d} bytes ({len(blob)} -> {len(out)})")
    print(f"file {len(blob)} -> {len(out)} bytes (leaf {len(leaf)} -> {len(newleaf)})")
    print(f"untouched payloads byte-identical: yes ({len(before) - len(changed)} of {len(before)}, "
          f"including {len(opaque)} undecodable)")
    print(f"wrote {out_path}")
    if a.stage:
        # `path` is None on the single-record staging path (cmd_decompile), so the variant has to come from the
        # argument or the corpus rather than from a file that does not exist yet.
        variant = a.variant
        if not variant and path:
            variant = os.path.basename(os.path.dirname(path))
        if not variant:
            found = variants(a.mission)
            variant = found[0] if found else "active"
        dest = os.path.join(MOD, "art", "missions", a.mission, variant, "fig.p3d.rz")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(rz.wrap(out))
        print(f"staged {dest} ({os.path.getsize(dest)} bytes RZ) - run ./proto2 install")
    return 0


def batch(a):
    """Apply one replacement across many missions, refusing atomically.

    Phase 1 plans and VALIDATES every mission with a dry run; if any mission fails a gate, or the anchor
    is ambiguous where it appears, nothing is written at all. Phase 2 writes only when every mission
    passed, so a batch can never leave a half-applied set behind.
    """
    import fnmatch

    # path shape is .../<mission>/<variant>/fig.p3d.rz
    missions = sorted({os.path.basename(os.path.dirname(os.path.dirname(p)))
                       for p in _mission_files()})
    if a.missions and a.missions != "*":
        pats = a.missions.split(",")
        missions = [m for m in missions if any(fnmatch.fnmatch(m, p.strip()) for p in pats)]
    if not missions:
        raise SystemExit("no missions matched")

    old_b, new_b = a.old.encode("latin-1"), a.new.encode("latin-1")
    plans, skipped, failed = [], [], []
    for m in missions:
        try:
            path, _staged = base_path(m, a.variant)
            blob, leaf, leaf_off, model = open_mission(path)
            rows = records_with_roles(model)
            recs = [r[0] for r in rows]
            roles = [r[2] for r in rows]
            dec = _decoded(model)
            before = [bytes(it["fields"]["payload"]) for it in recs]
            hits = []
            for i, (_role, t, _h, _it) in enumerate(dec):
                if t is None:
                    continue
                tb = t.encode("latin-1") if isinstance(t, str) else t
                n = tb.count(old_b)
                if n:
                    hits.append((i, n))
            if not hits:
                skipped.append(m)
                continue
            if not a.all and any(n > 1 for _i, n in hits):
                failed.append((m, "anchor occurs more than once in a record; use --all"))
                continue
            changed = []
            for i, _n in hits:
                t, _h = dec[i]
                tb = t.encode("latin-1") if isinstance(t, str) else t
                nt = tb.replace(old_b, new_b) if a.all else tb.replace(old_b, new_b, 1)
                payload = L.encode(nt)
                ok, msg = L.verify_encoded(payload, nt)
                if not ok:
                    raise SystemExit(f"record {i}: re-pack failed to verify ({msg})")
                recs[i]["fields"]["payload"] = payload
                changed.append((i, len(before[i]), len(payload)))
            probe = types.SimpleNamespace(mission=m, variant=a.variant, dry_run=True,
                                          stage=False, no_compile_check=True)
            if not a.no_compile_check:
                probe.no_compile_check = False
            batch_preview(blob, leaf, leaf_off, model, dec, roles, before, changed, probe)
            plans.append((m, path, blob, leaf, leaf_off, model, dec, roles, before, changed))
        except SystemExit as exc:
            failed.append((m, str(exc)))
        except Exception as exc:
            failed.append((m, f"{type(exc).__name__}: {exc}"))

    print(f"\nbatch: {len(missions)} mission(s) considered, {len(plans)} would change, "
          f"{len(skipped)} have no match, {len(failed)} failed")
    for m, why in failed:
        print(f"  REFUSED {m}: {why}")
    if failed:
        raise SystemExit("batch refused: nothing was written")
    if not plans:
        print("nothing to do")
        return 0

    # Phase 2: everything validated - now write/stage.
    for m, path, blob, leaf, leaf_off, model, dec, roles, before, changed in plans:
        real = types.SimpleNamespace(mission=m, variant=a.variant, stage=a.stage,
                                     no_compile_check=True, dry_run=False)
        _finalize(real, path, blob, leaf, leaf_off, model, dec, roles, before, changed)
    print(f"\nbatch applied to {len(plans)} mission(s)"
          + (" (staged - run ./proto2 install)" if a.stage else " (files written to tmp/, not staged)"))
    return 0


def batch_preview(blob, leaf, leaf_off, model, dec, roles, before, changed, a):
    """Validate one mission without writing; used by batch phase 1."""
    # replay _finalize's checks by running it with dry_run
    return _finalize(a, None, blob, leaf, leaf_off, model, dec, roles, before, changed)


def _load_side(mission, variant, side):
    """Bytes of one side of a comparison: 'stock' (the extracted corpus), 'staged' (work/mod) or
    'installed' (the game directory). Returns (blob, source description) or (None, reason)."""
    if side == "stock":
        path = find_mission(mission, variant)
        return open(path, "rb").read(), path
    if side == "staged":
        path = os.path.join(MOD, "art", "missions", mission, variant, "fig.p3d.rz")
    elif side == "installed":
        path = os.path.join(GAME, "art", "missions", mission, variant, "fig.p3d.rz")
    else:
        return None, f"unknown side {side!r} (use stock, staged or installed)"
    if not os.path.exists(path):
        return None, f"not present: {path}"
    return open(path, "rb").read(), path


def diff_mission(mission, variant=None, against="installed"):
    variant = variant or "active"
    """Per-record comparison of `against` versus the shipped file.

    Returns a dict with a row per record: index, role, state (unchanged / changed / locked / LOCKED
    CHANGED), byte delta, and for changed records the two texts so a caller can render a diff.
    """
    stock_b, stock_src = _load_side(mission, variant, "stock")
    other_b, other_src = _load_side(mission, variant, against)
    if other_b is None:
        return {"ok": False, "why": other_src}

    def decode_side(blob):
        inner = rz.unwrap(blob) if blob[:2] == b"RZ" else blob
        sizes.append(len(inner))
        leaf, _off = F.payload(inner)
        model = E.parse(leaf)
        rows = []
        prev = b""
        for it, payload, role in records_with_roles(model):
            t, _how = L.read(payload, prev[-32768:] if prev else b"")
            if t is not None:
                prev += t.encode("latin-1") if isinstance(t, str) else t
            rows.append((role, t, bytes(payload)))
        return rows

    sizes = []
    a_rows = decode_side(stock_b)
    b_rows = decode_side(other_b)
    if len(a_rows) != len(b_rows):
        return {"ok": False,
                "why": f"record count differs ({len(a_rows)} stock vs {len(b_rows)} {against})"}

    rows = []
    for i, ((ra, ta, pa), (rb, tb, pb)) in enumerate(zip(a_rows, b_rows)):
        if ta is None and tb is None:
            state = "locked" if pa == pb else "LOCKED CHANGED"
        elif pa == pb:
            state = "unchanged"
        else:
            state = "changed"
        rows.append({"index": i, "role": ra, "state": state,
                     "delta": len(pb) - len(pa),
                     "old_text": ta, "new_text": tb})
    changed = [r for r in rows if r["state"] in ("changed", "LOCKED CHANGED")]
    locked = [r for r in rows if r["state"].startswith("LOCKED")]
    return {"ok": True, "rows": rows, "changed": changed, "locked": locked,
            "stock": stock_src, "against_src": other_src,
            "bytes": sizes[1] - sizes[0], "sizes": tuple(sizes)}


def scan_ctrl():
    """Every decodable record whose text contains stray control bytes.

    These are decode artifacts (all 34 found in the shipped corpus are a single leading 0x1B in a 5-byte
    pure-copy payload), not authored data - but they are real bytes in the payload, so they are reported
    rather than silently removed.
    """
    out = []
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        mission = os.path.basename(os.path.dirname(dirpath))
        variant = os.path.basename(dirpath)
        try:
            _b, _l, _o, model = open_mission(os.path.join(dirpath, "fig.p3d.rz"))
            rows = records_with_roles(model)
            dec = _decoded(model)
        except Exception:
            continue
        for i, (row, d) in enumerate(zip(rows, dec)):
            role, t, _how, _it = d
            if not isinstance(t, (bytes, str)):
                continue
            tb = t.encode("latin-1") if isinstance(t, str) else t
            ctrl = control_bytes(tb.decode("latin-1"))
            if ctrl:
                out.append({"mission": mission, "variant": variant, "index": i, "role": role,
                            "payload": len(row[1]), "text": len(tb), "ctrl": ctrl,
                            "head": tb[:40]})
    return out


def _mission_files():
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" in names:
            yield os.path.join(dirpath, "fig.p3d.rz")


def apply_edit(a):
    path, staged_base = base_path(a.mission, a.variant)
    blob, leaf, leaf_off, model = open_mission(path)
    rows = records_with_roles(model)
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    dec = _decoded(model)
    before = [bytes(it["fields"]["payload"]) for it in recs]

    old = a.old.encode("latin-1")
    new = a.new.encode("latin-1")
    targets = [a.index] if a.index is not None else range(len(recs))
    hits = []
    for i in targets:
        _role, t, _how, _it = dec[i]
        if t is None:
            continue
        tb = t.encode("latin-1") if isinstance(t, str) else t
        n = tb.count(old)
        if n:
            hits.append((i, n))
    if not hits:
        locked = [(roles[i], i) for i, (_r, t, _h, _x) in enumerate(dec) if t is None]
        msg = [f"text not found in any decodable record: {a.old!r}"]
        if locked:
            msg.append(f"{len(locked)} record(s) are LOCKED and were not searched:")
            for role, i in locked[:12]:
                msg.append(f"    [LOCKED] {role} #{i}")
            if len(locked) > 12:
                msg.append(f"    ... and {len(locked) - 12} more")
            msg.append("run './proto2 script --mission <m> list' to see the full locked map")
        raise SystemExit("\n".join(msg))
    if len(hits) > 1 and not a.all:
        raise SystemExit("text found in several records (" +
                         ", ".join(f"#{i} x{n}" for i, n in hits) +
                         "); pick one with --index or pass --all")
    if len(hits) == 1 and hits[0][1] > 1 and not a.all:
        raise SystemExit(f"text occurs {hits[0][1]} times in record #{hits[0][0]}; "
                         "make the anchor unique or pass --all")

    changed = []
    for i, _n in hits:
        _role, t, _how, _it = dec[i]
        tb = t.encode("latin-1") if isinstance(t, str) else t
        nt = tb.replace(old, new) if a.all else tb.replace(old, new, 1)
        recs[i]["fields"]["payload"] = L.encode(nt)
        ok, msg = L.verify_encoded(recs[i]["fields"]["payload"], nt)
        if not ok:
            raise SystemExit(f"record {i}: re-pack failed to verify ({msg})")
        changed.append((i, len(tb), len(nt)))

    return _finalize(a, path, blob, leaf, leaf_off, model, dec, roles, before, changed)


def bytecode_hashes():
    """sha1(stream) for every Lua 5.1 chunk saved from a runtime capture (work/bytecode)."""
    d = os.path.join(ROOT, "work", "bytecode")
    try:
        return {f[:-5] for f in os.listdir(d) if f.endswith(".luac")}
    except OSError:
        return set()


def bytecode_chunk(payload):
    """The Lua 5.1 chunk for this payload, or None.

    Two sources, one key. A runtime capture writes work/bytecode/<sha1(stream)>.luac; a payload the engine
    never loaded is inflated offline with the same engine dictionary and stored under the same key by
    `_recover_engine_chunk`. Keying on the bare stream (not on the payload bytes) is what makes a record with
    a 4-byte prefix resolve to the same file as one without it.

    Bytecode records were classified as locked for months because they are not text. They are readable -
    `tools/lua51.py` undumps them - so the tooling distinguishes three states: source, bytecode, unknown.
    """
    import hashlib
    h = hashlib.sha1(L.stream_of(payload)).hexdigest()
    path = os.path.join(ROOT, "work", "bytecode", h + ".luac")
    if not os.path.exists(path):
        return None
    return open(path, "rb").read()


VERDICT_CACHE = os.path.join(ROOT, "artifacts", "bytecode-verdicts.json")
VERDICT_STAMP = VERDICT_CACHE + ".version"


def tool_version():
    """A stamp over everything that decides whether a record converts.

    The cache is keyed by chunk hash, so without this a verdict computed under an older kit survives every code
    change - which is exactly what happened: records that had become convertible still counted as differing, and
    records already converted were still reported as blockers.
    """
    parts = []
    for rel in ("tools/script_edit.py", "tools/refold.py", "tools/lua51.py", "tools/lua_equiv.py"):
        path = os.path.join(ROOT, rel)
        parts.append(hashlib.sha1(open(path, "rb").read()).hexdigest() if os.path.exists(path) else "-")
    for rel in ("build/luadec-p2/bin/luadec", "build/luadec-p2/bin/luac",
                "build/unluac-rs/unluac-cli", "build/unluac/REVISION", "build/unluac-rs/REVISION"):
        path = os.path.join(ROOT, rel)
        parts.append(hashlib.sha1(open(path, "rb").read()).hexdigest() if os.path.exists(path) else "-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def verdict_cache_current():
    try:
        return open(VERDICT_STAMP).read().strip() == tool_version()
    except Exception:
        return False


def load_verdict_cache():
    """The cached verdicts, or {} when the file was written by a different toolchain."""
    if not verdict_cache_current():
        return {}
    try:
        return json.load(open(VERDICT_CACHE))
    except Exception:
        return {}


def save_verdict_cache(cache):
    json.dump(cache, open(VERDICT_CACHE, "w"), indent=1, sort_keys=True)
    open(VERDICT_STAMP, "w").write(tool_version() + "\n")


_verdicts = None


def cached_verdict(payload):
    """The recorded round-trip verdict for this payload's chunk, or None when it is not cached.

    `artifacts/bytecode-verdicts.json` is maintained by `./proto2 progress` and keyed by chunk sha1, so the GUI
    can label a record without decompiling it on the spot (which costs about a second per record).
    """
    global _verdicts
    chunk = bytecode_chunk(payload)
    if chunk is None:
        return None
    if _verdicts is None:
        _verdicts = load_verdict_cache()
    entry = _verdicts.get(hashlib.sha1(chunk).hexdigest())
    return (entry or {}).get("status")


def record_kind(payload, text):
    """'source' | 'bytecode' | 'unknown' for one record payload."""
    if text is not None:
        return "source"
    return "bytecode" if bytecode_chunk(payload) is not None else "unknown"


def bytecode_listing(payload):
    """(summary dict, luac-style listing text) for a bytecode record, or None."""
    import io
    chunk = bytecode_chunk(payload)
    if chunk is None:
        return None
    import lua51 as L5
    root = L5.load(chunk)
    insns = len(root.code)
    strings = root.const_strings()
    buf = io.StringIO()
    L5.listing(root, out=buf, show_code=True)
    return {"insns": insns, "strings": strings, "protos": len(root.protos),
            "bytes": len(chunk), "listing": buf.getvalue()}, buf.getvalue()


# Our own LuaDec/Lua build (tools/build_luadec.sh): 32-bit, 4-byte floats. The prebuilt kit that was used
# during development is no longer required or consulted.
GAME = config.game_dir()
STATE_DIR = os.path.join(ROOT, "work", "state")

LUADEC_DIR = os.environ.get("PROTO2_LUADEC_DIR", os.path.join(ROOT, "build", "luadec-p2", "bin"))


def decompile_payload(payload):
    """(source or None, note) - run the pinned LuaDec on a bytecode record's chunk.

    The chunk must have been captured from the running game (work/bytecode). Output is cleaned of LuaDec's
    banner and then validated with `luac -p`; an unvalidated decompilation is returned as None with the
    reason, because a payload that only *looks* like the original is worse than a locked one.
    """
    import subprocess
    import tempfile
    chunk = bytecode_chunk(payload)
    if chunk is None:
        return None, "no captured chunk for this payload"
    luadec = os.path.join(LUADEC_DIR, "luadec")
    luac = os.path.join(LUADEC_DIR, "luac")
    if not (os.path.exists(luadec) and os.path.exists(luac)):
        return None, f"LuaDec kit not found at {LUADEC_DIR}"
    os.makedirs(os.path.join(ROOT, "tmp"), exist_ok=True)
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "tmp")) as td:
        src_path = os.path.join(td, "chunk.luac")
        out_path = os.path.join(td, "out.lua")
        open(src_path, "wb").write(chunk)
        try:
            r = subprocess.run([luadec, src_path], capture_output=True, text=True, timeout=120)
        except Exception as exc:
            return None, f"luadec failed to run: {exc}"
        text = r.stdout
        if not text.strip():
            return None, f"luadec produced nothing (rc={r.returncode})"
        keep = [ln for ln in text.splitlines()
                if not ln.startswith("-- Decompiled using luadec")
                and not ln.startswith("-- Command line:")]
        src = "\n".join(keep).strip("\n") + "\n"
        open(out_path, "w").write(src)
        try:
            chk = subprocess.run([luac, "-p", out_path], capture_output=True, text=True, timeout=60)
        except Exception as exc:
            return None, f"luac failed to run: {exc}"
        if chk.returncode != 0:
            return None, f"decompiled output does not parse: {chk.stderr.strip().splitlines()[:2]}"
        warns = sum(1 for ln in r.stderr.splitlines()
                    if "processing OP_JMP" in ln or "currLocVar" in ln)
        return src, f"ok ({len(src.splitlines())} lines, {warns} decompiler warning(s))"


DECOMPILER_NOISE = ("-- DECOMPILER ERROR", "-- Decompiled using", "-- Command line:")


def strip_decompiler_noise(text):
    """Drop LuaDec's banner and its DECOMPILER ERROR comments from a source string.

    The banner names the machine that ran the decompiler (`-- Command line: /mnt/.../a.luac`) and the ERROR lines
    describe blocks LuaDec could not reconstruct structurally. Neither is part of the program: comments do not
    reach the compiler, so removing them leaves the chunk instruction-identical, which is why this is safe to
    apply to anything we stage.
    """
    body = text if isinstance(text, str) else text.decode("latin-1")
    keep = [ln for ln in body.splitlines(True) if not ln.lstrip().startswith(DECOMPILER_NOISE)]
    out = "".join(keep)
    return out


def _compile_text(text):
    """The chunk `text` compiles to, or None when it does not compile."""
    import tempfile
    luac = os.path.join(LUADEC_DIR, "luac")
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "tmp")) as td:
        lua = os.path.join(td, "a.lua")
        out = os.path.join(td, "a.luac")
        open(lua, "w", encoding="latin-1").write(text)
        try:
            c = subprocess.run([luac, "-s", "-o", out, lua], capture_output=True, text=True, timeout=60)
        except Exception:
            return None
        if c.returncode != 0:
            return None
        return open(out, "rb").read()


def _signature_of_text(text):
    """Signature of the chunk `text` compiles to, or None."""
    import lua51 as L51
    blob = _compile_text(text)
    if blob is None:
        return None
    try:
        return L51.signature(L51.load(blob))
    except Exception:
        return None


_DECOMP_CACHE = os.path.join(ROOT, "work", "luadec-cache")


def decompiled_text(payload):
    """(text, note) for a bytecode record, cached on disk by chunk hash.

    The whole-script export decompiles every read-only record when 'show read-only text' is on, which is the
    default; LuaDec costs a fraction of a second per record, so the result is kept under `work/luadec-cache/`
    (git-ignored) and a second export of the same mission is instant. The cache is keyed by the chunk itself, so a
    different kit produces a different chunk and a different entry.
    """
    chunk = bytecode_chunk(payload)
    if chunk is None:
        return None, "no captured chunk"
    import hashlib
    path = os.path.join(_DECOMP_CACHE, hashlib.sha1(chunk).hexdigest() + ".lua")
    if os.path.exists(path):
        try:
            return open(path, encoding="latin-1").read(), "cached"
        except Exception:
            pass
    src, note = decompile_payload(payload)
    if src is not None:
        try:
            os.makedirs(_DECOMP_CACHE, exist_ok=True)
            open(path, "w", encoding="latin-1").write(src)
        except Exception:
            pass
    return src, note


UNLUAC_RS = os.path.join(ROOT, "build", "unluac-rs", "unluac-cli")


def unluac_rs_source(chunk):
    """(text, note) from the Rust decompiler, or (None, reason).

    An independent implementation with newer structure analysis than the Java unluac: on the records LuaDec cannot
    reproduce it verifies 63, the largest single recovery step.
    """
    if not (os.path.isfile(UNLUAC_RS) and os.access(UNLUAC_RS, os.X_OK)):
        return None, "unluac-rs is not built (tools/build_unluac_rs.sh)"
    import tempfile
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "tmp")) as td:
        path = os.path.join(td, "a.luac")
        open(path, "wb").write(chunk)
        try:
            r = subprocess.run([UNLUAC_RS, "-i", path, "-D", "lua51"], capture_output=True, timeout=120, cwd=td)
        except Exception as exc:
            return None, f"unluac-rs failed to run ({type(exc).__name__})"
        if r.returncode != 0 or not r.stdout.strip():
            return None, "unluac-rs produced no output"
        return r.stdout.decode("latin-1"), "unluac-rs"


def luadec_source(chunk, flags=()):
    """(text, note) from the pinned kit run with extra flags, or (None, reason).

    `-dg` disables LuaDec's own local guessing, which is a different reconstruction of the same chunk: ten records
    that the default mode cannot reproduce recompile identically this way.
    """
    import tempfile
    luadec = os.path.join(LUADEC_DIR, "luadec")
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "tmp")) as td:
        path = os.path.join(td, "a.luac")
        open(path, "wb").write(chunk)
        try:
            r = subprocess.run([luadec, *flags, path], capture_output=True, timeout=120)
        except Exception as exc:
            return None, f"luadec {' '.join(flags)} failed to run ({type(exc).__name__})"
        if r.returncode != 0 or not r.stdout.strip():
            return None, f"luadec {' '.join(flags)} produced no output"
        # bytes, decoded as latin-1: the text pipeline is byte-preserving, and a UTF-8 string in the chunk must
        # survive as its original bytes rather than raise on encode
        return r.stdout.decode("latin-1"), "luadec" + (" " + " ".join(flags) if flags else "")


UNLUAC_DIR = os.environ.get("PROTO2_UNLUAC_DIR") or os.path.join(ROOT, "build", "unluac")


def unluac_source(chunk):
    """(text, note) from the second decompiler, or (None, reason).

    LuaDec and unluac reconstruct differently: for 43 of the records LuaDec cannot reproduce, unluac's output
    recompiles to the identical chunk. It is used only when LuaDec and every rewrite have failed, so it costs
    nothing on the 800+ records that already convert.
    """
    classes = os.path.join(UNLUAC_DIR, "classes")
    if not os.path.isdir(classes):
        return None, "unluac is not built (tools/build_unluac.sh)"
    import tempfile
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "tmp")) as td:
        path = os.path.join(td, "a.luac")
        open(path, "wb").write(chunk)
        try:
            r = subprocess.run(["java", "-cp", classes, "unluac.Main", path],
                               capture_output=True, timeout=120, cwd=td)
        except Exception as exc:
            return None, f"unluac failed to run ({type(exc).__name__})"
        if r.returncode != 0 or not r.stdout.strip():
            return None, "unluac produced no output"
        return r.stdout.decode("latin-1"), "unluac"


def verified_source(payload):
    """(text, kind) for a bytecode record whose source provably reproduces its chunk, else (None, reason).

    Two kinds: 'as-decompiled' when LuaDec's output already recompiles identically, and 'refolded' when a
    rewrite of that output does (`refold.fold`). The rewrite is never trusted: it is accepted only when the
    recompiled chunk's signature is identical, so a wrong transform costs nothing but a failed attempt.
    """
    chunk = bytecode_chunk(payload)
    if chunk is None:
        return None, "no captured chunk"
    src, note = decompile_payload(payload)
    if src is None:
        return None, note
    import lua51 as L51
    want = L51.signature(L51.load(chunk))
    raw = _compile_text(src)
    if raw is not None:
        try:
            if L51.signature(L51.load(raw)) == want:
                return strip_decompiler_noise(src), "as-decompiled"
        except Exception:
            raw = None
    import refold
    for cand in refold.candidates(src):
        if _signature_of_text(cand) == want:
            return strip_decompiler_noise(cand), "refolded"
    chunk = bytecode_chunk(payload)
    # the same decompiler in a different mode: -dg turns off its local guessing
    alt, how = luadec_source(chunk, ("-dg",))
    if alt is not None:
        if _signature_of_text(alt) == want:
            return strip_decompiler_noise(alt), how
        for cand in refold.candidates(alt):
            if _signature_of_text(cand) == want:
                return cand, how + "+refolded"
    alt, how = unluac_rs_source(chunk)
    if alt is not None:
        if _signature_of_text(alt) == want:
            return strip_decompiler_noise(alt), how
        for cand in refold.candidates(alt):
            if _signature_of_text(cand) == want:
                return strip_decompiler_noise(cand), how + "+refolded"
    alt, how = unluac_source(chunk)
    if alt is not None:
        if _signature_of_text(alt) == want:
            return strip_decompiler_noise(alt), how
        for cand in refold.candidates(alt):
            if _signature_of_text(cand) == want:
                return strip_decompiler_noise(cand), how + "+refolded"
    # say where the reconstruction stops matching: that is the sentence the GUI prints over the grey block
    detail = ""
    if raw is not None:
        try:
            import lua_equiv
            got = lua_equiv.analyse(chunk, raw)
            detail = got.get("detail") or ""
        except Exception:
            detail = ""
    return None, detail or "no verified source"


def roundtrip_equivalent(payload):
    """(status, detail) - does a source exist that recompiles to the SAME chunk?

    'equivalent' now covers two cases: LuaDec's output as-is, and a verified refold of it (`verified_source`).
    Anything else is reported, never assumed: equal signature means the source executes like the original.
    """
    src, note = verified_source(payload)
    if src is not None:
        return "equivalent", f"recompiled chunk is instruction-identical ({note})"
    if bytecode_chunk(payload) is None:
        return "nochunk", "no captured chunk"
    raw, _how = decompile_payload(payload)
    if raw is None:
        return "decompile-failed", note
    return "differs", note


def _flatten(sig):
    yield sig
    for child in sig[4]:
        yield from _flatten(child)


def changed_indices(mission, variant, side="working"):
    """Indices whose payload on `side` differs from the shipped one.

    Used by the Script Map to colour edited rows; the side matters, because the tab can display the shipped,
    staged, installed or working copy and only some of those can differ.
    """
    if side == "shipped":
        return set()
    got = open_side(mission, variant, side)
    if got is None:
        return set()
    stock = find_mission(mission, variant)
    a = records_with_roles(got[3])
    b = records_with_roles(open_mission(stock)[3])
    out = set()
    for i, (x, y) in enumerate(zip(a, b)):
        if bytes(x[1]) != bytes(y[1]) or x[2] != y[2]:
            out.add(i)
    return out



def _override_source_for(payload):
    """Source from the hand-reconstructed pack for this exact shipped chunk, or None.

    A few records (43 of 4,793) were reconstructed by hand because the decompiler cannot reproduce them: their
    `verified_source` fails with "unreachable block" style notes. Reverting such a record leaves it bytecode and
    therefore blocked. This looks the chunk up by sha so a revert can put the hand-written source straight back and
    say so. The pack ships in `overrides/` (docs/26); without it the record stays bytecode and the GUI
    reports it as locked.
    """
    try:
        import overrides as OV
        import hashlib as _h
        chunk = bytecode_chunk(payload)
        if chunk is None:
            return None
        pack = os.path.join(ROOT, "overrides")
        manifest = os.path.join(pack, OV.MANIFEST)
        if not os.path.exists(manifest):
            return None
        import json as _j
        data = _j.load(open(manifest))
        if data.get("version") != OV.VERSION:
            return None
        sha = _h.sha256(chunk).hexdigest()
        for rec in data.get("records", []):
            if rec.get("chunk_sha256") == sha:
                path = os.path.join(pack, rec["file"])
                if os.path.exists(path):
                    return open(path, encoding="utf-8").read()
    except Exception:
        return None
    return None

def revert_record(mission, variant, index, apply_now=True):
    """Put ONE record back to the shipped payload, keeping every other edit.

    Used from the Diff tab: the record stops being a change. Returns (index, old_len, new_len, note).
    """
    base_path_, staged = base_path(mission, variant)
    if not staged:
        raise SystemExit(f"{mission} has no staged file - it already matches the shipped copy")
    stock = find_mission(mission, variant)
    blob, leaf, leaf_off, model = open_mission(base_path_)
    _sb, _sl, _so, smodel = open_mission(stock)
    rows = records_with_roles(model)
    srows = records_with_roles(smodel)
    if len(rows) != len(srows):
        raise SystemExit(f"record count differs ({len(rows)} staged vs {len(srows)} shipped); refusing")
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    dec = _decoded(model)
    before = [bytes(it["fields"]["payload"]) for it in recs]
    if not (0 <= index < len(recs)):
        raise SystemExit(f"record {index} out of range")
    want = bytes(srows[index][1])
    already = before[index] == want
    if not already:
        recs[index]["fields"]["payload"] = want
        _finalize(types.SimpleNamespace(
            mission=mission, variant=variant, stage=True, dry_run=False, no_compile_check=True,
            allow_locked_edit=True, allow_window_shift=True, allow_bytecode=True),
            base_path_, blob, leaf, leaf_off, model, dec, roles, before,
            [(index, len(before[index]), len(want))])

    # If that was the last difference, the staged file now equals the shipped one: drop it rather than keep an
    # override that masks the archive, and take the loose copy back out of the game directory.
    # A shipped payload that the decompiler cannot reproduce (43 records) would be left blocked. Put the
    # hand-reconstructed source from the local overrides pack back instead, and say it happened - never silently.
    recovered = False
    try:
        now_text = _decoded(open_mission(base_path_)[3])[index][1]
    except Exception:
        now_text = "?"
    if now_text is None:
        pack_src = _override_source_for(want)
        if pack_src:
            stage_record_source(mission, variant, index, pack_src)
            recovered = True

    base = "already matched the shipped payload" if already else "reverted to the shipped payload"
    if recovered:
        base += (" (that payload has no decompilable source: restored the hand-reconstructed source "
                 "from the local overrides/ pack)")

    note = base
    # Compare RECORDS, not files: re-emitting the container can differ by padding even when every payload
    # matches the shipped one, and what matters is whether any difference is left.
    try:
        after_rows = records_with_roles(open_mission(base_path_)[3])
        same = (len(after_rows) == len(srows)
                and all(bytes(x[1]) == bytes(y[1]) for x, y in zip(after_rows, srows)))
    except Exception:
        same = False
    if same:
        rel = os.path.relpath(base_path_, os.path.join(ROOT, "work", "mod")).replace(os.sep, "/")
        os.remove(base_path_)
        try:
            import apply_mod
            how = apply_mod.uninstall_one(GAME, STATE_DIR, rel)
        except Exception as exc:
            how = f"staged file removed; game override not touched ({exc})"
        note = f"reverted and unstaged - the file now matches stock byte-for-byte; {how}"
    elif apply_now:
        # apply it, so the record is gone from a stock-vs-installed diff instead of lingering until Install
        rel = os.path.relpath(base_path_, os.path.join(ROOT, "work", "mod")).replace(os.sep, "/")
        try:
            import apply_mod
            how = apply_mod.install_one(GAME, STATE_DIR, rel, base_path_)
            note = f"{base} and applied to the game ({how})"
        except Exception as exc:
            note = f"{base}; applying failed ({exc}) - run ./proto2 install"
    return index, len(before[index]), len(want), note


def stage_record_constant(mission, variant, index, path, const, value):
    """Replace ONE constant of a compiled record and stage it (the GUI's constants panel)."""
    base_path_, _staged = base_path(mission, variant)
    blob, leaf, leaf_off, model = open_mission(base_path_)
    rows = records_with_roles(model)
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    dec = _decoded(model)
    before = [bytes(it["fields"]["payload"]) for it in recs]
    if not (0 <= index < len(recs)):
        raise SystemExit(f"record {index} out of range")
    spec = chunk_constants(before[index])
    if spec is None:
        raise SystemExit(f"record {index} has no captured chunk")
    found = [c for c in spec if c["path"] == path and c["index"] == const]
    if not found:
        raise SystemExit(f"record {index}: no constant #{const} at path {path or '(root)'}")
    kind = found[0]["kind"]
    if kind == "number":
        value = float(value)
    elif kind == "boolean":
        value = value.strip().lower() in ("true", "1", "yes")
    elif kind == "nil":
        raise SystemExit(f"constant #{const} is nil and cannot be replaced")
    new_chunk, why = patched_chunk(before[index], path, const, value)
    if new_chunk is None:
        raise SystemExit(f"record {index}: {why}")
    payload = L.encode(new_chunk)
    ok, msg = L.verify_encoded(payload, new_chunk)
    if not ok:
        raise SystemExit(f"record {index}: re-pack failed to verify ({msg})")
    recs[index]["fields"]["payload"] = payload
    return _finalize(types.SimpleNamespace(
        mission=mission, variant=variant, stage=True, dry_run=False, no_compile_check=True,
        allow_locked_edit=True, allow_window_shift=True),
        base_path_, blob, leaf, leaf_off, model, dec, roles, before,
        [(index, len(before[index]), len(payload))])


def encode_payload_for_form(text, form):
    """(payload, ok, reason) - encode source for a record's container form.

    Two forms exist. `prefixed` (tracks, enterTracks, exitTracks, initialTracks, functions) takes the packed
    `7f 00 | size | deflate` payload. `bare` (every `conditions` record - 416 measured, 18-of-18 in story_l1)
    takes `u32 length | body | zero padding to a 4-byte total`, and the shipped bare payloads are as often
    plain text as packed streams, so bare text is a form the engine already loads.
    """
    raw = text.encode("latin-1") if isinstance(text, str) else bytes(text)
    if form == "bare":
        payload = L.bare_encode(raw)
        body = payload[4:4 + len(raw)]
        ok = body == raw and len(payload) % 4 == 0
        return payload, ok, "" if ok else "bare payload did not round-trip"
    payload = L.encode(raw)
    ok, msg = L.verify_encoded(payload, raw)
    return payload, ok, msg


def stage_record_source(mission, variant, index, src):
    """Replace ONE bytecode record's payload with the given source text (explicit, single-record)."""
    base_path_, _staged = base_path(mission, variant)
    blob, leaf, leaf_off, model = open_mission(base_path_)
    rows = records_with_roles(model)
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    dec = _decoded(model)
    before = [bytes(it["fields"]["payload"]) for it in recs]
    if not (0 <= index < len(recs)):
        raise SystemExit(f"record {index} out of range")
    src = strip_decompiler_noise(src)
    raw = src.encode("latin-1")
    form = (recs[index].get("fields") or {}).get("form")
    payload, ok, msg = encode_payload_for_form(raw, form)
    if not ok:
        raise SystemExit(f"record {index}: re-pack failed to verify ({msg})")
    recs[index]["fields"]["payload"] = payload
    return _finalize(types.SimpleNamespace(
        mission=mission, variant=variant, stage=True, dry_run=False, no_compile_check=True,
        allow_locked_edit=True, allow_window_shift=True),
        base_path_, blob, leaf, leaf_off, model, dec, roles, before,
        [(index, len(before[index]), len(payload))])


def cmd_stage_equivalent(a):
    """Replace every PROVABLY equivalent bytecode record with its decompiled source.

    Only records whose recompiled chunk is instruction-identical to the original are touched; anything that
    differs, fails to decompile or has no chunk is left exactly as it was. Existing staged edits are built on,
    because the base is the working copy.
    """
    base_path_, staged = base_path(a.mission, a.variant)
    blob, leaf, leaf_off, model = open_mission(base_path_)
    rows = records_with_roles(model)
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    dec = _decoded(model)
    before = [bytes(it["fields"]["payload"]) for it in recs]
    changed, skipped = [], []
    for i, ((it, payload, role), (_r, t, _h, _x)) in enumerate(zip(rows, dec)):
        if t is not None or bytecode_chunk(payload) is None:
            continue
        if role in NON_CONVERTIBLE_ROLES:
            skipped.append((i, role, "role must stay bytecode"))
            continue
        src, note = verified_source(payload)
        if src is None:
            skipped.append((i, role, note))
            continue
        raw = src.encode("latin-1")
        form = (it.get("fields") or {}).get("form")
        new_payload, ok, msg = encode_payload_for_form(raw, form)
        if not ok:
            skipped.append((i, role, f"repack: {msg}"))
            continue
        recs[i]["fields"]["payload"] = new_payload
        changed.append((i, len(before[i]), len(new_payload)))
    print(f"{a.mission}: {len(changed)} record(s) proven equivalent -> source, "
          f"{len(skipped)} left as bytecode"
          + (f" (base: {'staged' if staged else 'shipped'})" if changed else ""))
    for i, role, why in skipped:
        print(f"   skipped #{i} {role}: {why}")
    if not changed:
        return 0
    return _finalize(types.SimpleNamespace(
        mission=a.mission, variant=a.variant, stage=True, dry_run=False, no_compile_check=True,
        allow_locked_edit=True, allow_window_shift=True),
        base_path_, blob, leaf, leaf_off, model, dec, roles, before, changed)


def cmd_decompile_all(a):
    got = open_side(a.mission, a.variant, "working")
    if got is None:
        raise SystemExit("mission not found")
    rows = records_with_roles(got[3])
    dec = _decoded(got[3])
    counts = {}
    print(f"{a.mission} ({a.variant or 'active'}): {len(rows)} record(s)")
    print(f"{'idx':>4} {'role':<14} {'chunk':>6}  status")
    for i, ((it, payload, role), (_r, t, _h, _x)) in enumerate(zip(rows, dec)):
        if t is not None:
            continue
        chunk = bytecode_chunk(payload)
        if chunk is None:
            continue
        status, detail = roundtrip_equivalent(payload)
        counts[status] = counts.get(status, 0) + 1
        print(f"{i:>4} {role:<14} {len(chunk):>6}  {status:<16} {detail}")
    print()
    for k in sorted(counts):
        print(f"  {k:<16} {counts[k]}")
    return 0


def cmd_revert(a):
    idx, old, new, note = revert_record(a.mission, a.variant, a.index,
                                        apply_now=not getattr(a, "no_apply", False))
    print(f"record #{idx}: {note}")
    if "unstaged" in note:
        print("  nothing to install - the game now loads this mission from the archive again")
    elif "applied" in note:
        print(f"  payload {old} -> {new} bytes, game copy updated")
    elif old != new:
        print(f"  payload {old} -> {new} bytes; run ./proto2 install to apply")
    return 0


def cmd_decompile(a):
    if getattr(a, "stage_equivalent", False):
        return cmd_stage_equivalent(a)
    if getattr(a, "all", False):
        return cmd_decompile_all(a)
    if a.index is None:
        raise SystemExit("decompile needs --index N, or --all")
    got = open_side(a.mission, a.variant, "working")
    if got is None:
        raise SystemExit("mission not found")
    rows = records_with_roles(got[3])
    if not (0 <= a.index < len(rows)):
        raise SystemExit(f"record {a.index} out of range")
    if rows[a.index][2] in NON_CONVERTIBLE_ROLES and a.stage and not getattr(a, "allow_conditions", False):
        raise SystemExit(f"record #{a.index} is a {rows[a.index][2]} payload: converting it is the open question - "
                         f"the crash bisected in 2026-10-25 was a `prefixed` payload written into a `bare` slot, so "
                         f"retest with --allow-conditions before trusting the old rule")
    payload = rows[a.index][1]
    src, note = decompile_payload(payload)
    if src is None:
        raise SystemExit(f"record {a.index}: {note}")
    print(f"record #{a.index} ({rows[a.index][2]}): {note}", file=sys.stderr)
    if a.out:
        open(a.out, "w").write(src)
        print(f"wrote {a.out}", file=sys.stderr)
    else:
        sys.stdout.write(src)
    if not a.stage:
        return 0
    dec = _decoded(got[3])
    recs = [r[0] for r in rows]
    roles = [r[2] for r in rows]
    before = [bytes(it["fields"]["payload"]) for it in recs]
    payload_new = L.encode(src.encode("latin-1"))
    ok, msg = L.verify_encoded(payload_new, src.encode("latin-1"))
    if not ok:
        raise SystemExit(f"re-pack failed to verify: {msg}")
    recs[a.index]["fields"]["payload"] = payload_new
    return _finalize(types.SimpleNamespace(
        mission=a.mission, variant=a.variant, stage=True, dry_run=False,
        no_compile_check=True, allow_locked_edit=True, allow_window_shift=True),
        None, got[0], got[1], got[2], got[3], dec, roles, before,
        [(a.index, len(before[a.index]), len(payload_new))])


def coverage():
    """Per-mission coverage: source text, known bytecode, and still unknown."""
    import hashlib
    bc = bytecode_hashes()
    rows = []
    total = locked = 0
    bc_records = 0
    for dirpath, _dirs, names in os.walk(CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        mission = os.path.basename(os.path.dirname(dirpath))
        variant = os.path.basename(dirpath)
        try:
            got = open_side(mission, variant, "shipped")
            if got is None:
                continue
            dec = _decoded(got[3])
        except Exception:
            continue
        n = len(dec)
        l = 0
        for (it, payload, _role), (_r, t, _h, _i) in zip(records_with_roles(got[3]), dec):
            if t is None:
                # Ask the same classifier the rest of the tooling uses: it knows about the 4-byte prefix and
                # about chunks recovered offline with the engine dictionary. Keying on sha1(payload[4:]) here
                # alone counted those payloads as locked forever.
                if bytecode_chunk(payload) is not None:
                    bc_records += 1
                else:
                    l += 1
        total += n
        locked += l
        rows.append((l, n, mission, variant))
    rows.sort(reverse=True)
    cache_n = len(L._cache())
    return rows, total, locked, cache_n, bc_records


def cmd_coverage(a):
    rows, total, locked, cache_n, bc_records = coverage()
    ok = total - locked - bc_records
    print(f"records        : {total}")
    print(f"source text    : {ok} ({100.0 * ok / max(1, total):.1f}%)")
    print(f"known bytecode : {bc_records}  (Lua 5.1 chunks: captured, or decoded with the engine dictionary)")
    print(f"unknown        : {locked}")
    print(f"runtime cache  : {cache_n} stream(s) recovered from playthroughs (artifacts/lua-cache.json)")
    print("\nmissions with the most locked records (play these first):")
    shown = set()
    for l, n, m, v in rows:
        if l == 0 or (m, v) in shown:
            continue
        shown.add((m, v))
        print(f"  {m:<28} {v:<7} {l:>4} locked / {n:>4} records")
        if len(shown) >= 15:
            break
    return 0


def cmd_ctrl(a):
    rows = scan_ctrl()
    if not rows:
        print("no records contain stray control bytes")
        return 0
    print(f"{len(rows)} record(s) contain stray control bytes\n")
    for r in rows:
        ctrl = ", ".join(f"{k} x{v}" for k, v in sorted(r["ctrl"].items()))
        print(f"  {r['mission']}/{r['variant']} #{r['index']:<3} {r['role']:<13} "
              f"payload {r['payload']:>3}B text {r['text']:>4}B  {ctrl}")
        print(f"      {r['head']!r}")
    return 0


def cmd_diff(a):
    d = diff_mission(a.mission, a.variant, a.against)
    if not d["ok"]:
        raise SystemExit(d["why"])
    print(f"mission {a.mission} ({a.variant or 'active'}): stock vs {a.against}")
    print(f"  stock   : {d['stock']}")
    print(f"  compared: {d['against_src']}")
    print(f"  file    : {d['sizes'][0]} -> {d['sizes'][1]} bytes ({d['bytes']:+d})")
    print(f"  records : {len(d['changed'])} changed, "
          f"{sum(1 for r in d['rows'] if r['state'] == 'unchanged')} unchanged, "
          f"{len(d['locked'])} locked")
    for bad in [r for r in d["rows"] if r["state"] == "LOCKED CHANGED"]:
        print(f"  WARNING: locked record #{bad['index']} ({bad['role']}) differs - that should be "
              f"impossible through this tooling")
    for r in d["changed"]:
        print(f"\n  #{r['index']} {r['role']} ({r['state']}, {r['delta']:+d} bytes)")
        a_t, b_t = r["old_text"], r["new_text"]
        if a_t is None or b_t is None:
            print("    (bytecode on both sides - byte comparison only; there is no text to diff)")
            continue
        a_s = (a_t.encode("latin-1") if isinstance(a_t, str) else a_t).decode("latin-1").splitlines()
        b_s = (b_t.encode("latin-1") if isinstance(b_t, str) else b_t).decode("latin-1").splitlines()
        for line in difflib.unified_diff(a_s, b_s, "stock", a.against, lineterm="", n=1):
            print("    " + line[:160])
    return 0


def cmd_explain(a):
    """Per-prototype diagnosis of a record that does not reconstruct.

    These are the blocked records: LuaDec's text compiles, but one or more nested functions come out
    different. Printing which prototypes differ, and the first instructions that do, is what makes the
    difference inferable - the same information a screenshot provides, without the screenshot.
    """
    import lua_equiv
    import lua51 as L51
    import branch_report as BR
    payload = None
    for side in ("working", "shipped", "installed"):
        got = open_side(a.mission, a.variant or "active", side)
        if got is None:
            continue
        rows = records_with_roles(got[3])
        if 0 <= a.index < len(rows) and bytecode_chunk(rows[a.index][1]) is not None:
            payload = rows[a.index][1]
            break
    if payload is None:
        raise SystemExit(f"record {a.index}: no captured chunk")
    chunk = bytecode_chunk(payload)
    src, how = decompile_payload(payload)
    if src is None:
        raise SystemExit(f"record {a.index}: no decompilation ({how})")
    verdict, detail = roundtrip_equivalent(payload)
    print(f"record #{a.index} {records_with_roles(open_side(a.mission, a.variant or 'active', 'working')[3])[a.index][2]}"
          f"  verdict: {verdict} ({detail})")
    raw = _compile_text(src)
    if raw is None:
        raise SystemExit("the decompiled text does not compile")
    a_protos, b_protos = list(L51.walk(L51.load(chunk))), list(L51.walk(L51.load(raw)))
    if len(a_protos) != len(b_protos):
        print(f"prototype count differs: {len(a_protos)} shipped vs {len(b_protos)} rebuilt")
        return 0
    print(f"prototypes: {len(a_protos)}")
    for k, (x, y) in enumerate(zip(a_protos, b_protos)):
        ta, tb = lua_equiv.tokens(x), lua_equiv.tokens(y)
        if ta == tb:
            print(f"  {k:2}: identical ({len(ta)} instructions)")
            continue
        print(f"  {k:2}: DIFFERS ({len(ta)} vs {len(tb)} instructions)")
        shown = 0
        for i, (u, v) in enumerate(zip(ta, tb)):
            if u == v:
                continue
            print(f"      {i:3} shipped {u}")
            print(f"          rebuilt {v}")
            shown += 1
            if shown >= a.tokens:
                break
        if len(ta) != len(tb):
            print(f"      (lengths differ, so only the first {shown} position(s) are shown)")
    print("\nThe shipped side is the truth; a difference here is a source form the decompiler did not guess.")
    return 0


def cmd_constants(a):
    """List a record's constants from whichever copy still holds its bytecode.

    A record that has already been converted is text in the staged copy, so the shipped copy is consulted then.
    """
    spec = None
    rows = None
    side = None
    for candidate in ("working", "shipped", "installed"):
        got = open_side(a.mission, a.variant or "active", candidate)
        if got is None:
            continue
        rows = records_with_roles(got[3])
        if not (0 <= a.index < len(rows)):
            raise SystemExit(f"record {a.index} out of range (0..{len(rows)-1})")
        spec = chunk_constants(rows[a.index][1])
        if spec is not None:
            side = candidate
            break
    if spec is None:
        raise SystemExit(f"record {a.index} ({rows[a.index][2] if rows else '?'}) has no captured chunk")
    print(f"record #{a.index} {rows[a.index][2]}: {len(spec)} constant(s)")
    for c in spec:
        print(f"  path {c['path'] or '(root)':<6} #{c['index']:<3} {c['kind']:<7} {c['value'][:80]}")
    return 0


def cmd_const_set(a):
    return set_record_constant(a)


def cmd_export(a):
    txt = export_script(a.mission, a.variant, include_bytecode=getattr(a, "with_bytecode", False))
    if a.out:
        open(a.out, "w", encoding="latin-1").write(txt)
        print(f"wrote {a.out} ({len(txt)} chars, {txt.count('-- proto2:record #')} records, "
              f"{txt.count('[LOCKED]')} locked)")
    else:
        sys.stdout.write(txt)
    return 0


def cmd_apply_file(a):
    text = open(a.file, encoding="latin-1").read()
    return import_script(types.SimpleNamespace(mission=a.mission, variant=a.variant, text=text,
                                               stage=a.stage, no_compile_check=a.no_compile_check))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="proto2 script", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mission", required=True)
    ap.add_argument("--variant", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="every record with decodability")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("extract", help="write one record, or all decodable text")
    p.add_argument("--index", type=int, default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--report", action="store_true", help="list locked records by role instead of extracting")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("export", help="write the whole mission script as one editable text")
    p.add_argument("--with-bytecode", action="store_true",
                   help="include a read-only reconstruction inside compiled records (reference only)")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("apply", help="apply an edited whole-script text back, record by record")
    p.add_argument("file")
    p.add_argument("--stage", action="store_true")
    p.add_argument("--no-compile-check", action="store_true")
    p.add_argument("--allow-window-shift", action="store_true",
                   help="stage even if other records would decode differently")
    p.set_defaults(func=cmd_apply_file)

    p = sub.add_parser("explain", help="show which prototypes of a compiled record do not reconstruct")
    p.add_argument("--index", type=int, required=True, help="record number")
    p.add_argument("--tokens", type=int, default=6, help="how many differing instructions to show per prototype")
    p.set_defaults(func=cmd_explain)
    p = sub.add_parser("constants", help="list the constants of a compiled record")
    p.add_argument("--index", type=int, required=True, help="record number")
    p.set_defaults(func=cmd_constants)
    p = sub.add_parser("const-set", help="replace one constant of a compiled record")
    p.add_argument("--index", type=int, required=True, help="record number")
    p.add_argument("--path", default="", help="prototype path, e.g. '' or '1' or '1.2'")
    p.add_argument("--const", type=int, required=True, help="constant index within that prototype")
    p.add_argument("--value", required=True, help="new value (text, number, true/false)")
    p.add_argument("--stage", action="store_true", help="stage into work/mod for ./proto2 install")
    p.set_defaults(func=cmd_const_set)
    p = sub.add_parser("listing", help="luac-style listing for a bytecode record")
    p.add_argument("--index", type=int, required=True)
    p.set_defaults(func=cmd_listing)

    p = sub.add_parser("revert", help="put one record back to the shipped payload, keeping other edits")
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--no-apply", action="store_true",
                   help="stage only; do not copy the result into the game directory")
    p.set_defaults(func=cmd_revert)

    p = sub.add_parser("decompile", help="turn a bytecode record into Lua source (LuaDec), optionally stage it")
    p.add_argument("--out", default=None)
    p.add_argument("--stage", action="store_true",
                   help="replace the bytecode payload with the decompiled source (experimental)")
    p.add_argument("--all", action="store_true",
                   help="check every bytecode record: decompile, recompile, compare instruction streams")
    p.add_argument("--dry-run", action="store_true",
                   help="explicitly report only; --all never writes anything")
    p.add_argument("--stage-equivalent", action="store_true",
                   help="replace only provably equivalent bytecode records with source and stage the result")
    p.add_argument("--index", type=int, default=None)
    p.add_argument("--allow-conditions", action="store_true",
                   help="allow staging a `conditions` record, now that bare-form text is the encoder "
                        "(the 2026-10-25 crash was a prefixed payload in a bare slot)")
    p.set_defaults(func=cmd_decompile)

    p = sub.add_parser("coverage", help="how many records decode, and which missions are worst")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("ctrl", help="list records whose text contains stray control bytes")
    p.set_defaults(func=cmd_ctrl)

    p = sub.add_parser("diff", help="compare the shipped mission with a staged or installed one")
    p.add_argument("--against", default="installed", choices=["staged", "installed"])
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("batch", help="apply one replacement across many missions, atomically")
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--missions", default="*", help="comma-separated globs, e.g. 'story_*,gz_*'")
    p.add_argument("--all", action="store_true", help="replace every occurrence")
    p.add_argument("--allow-window-shift", action="store_true")
    p.add_argument("--stage", action="store_true")
    p.add_argument("--no-compile-check", action="store_true")
    p.set_defaults(func=batch)

    p = sub.add_parser("replace", help="replace text in one or all records, with full verification")
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--index", type=int, default=None)
    p.add_argument("--all", action="store_true", help="replace every occurrence, in every record")
    p.add_argument("--stage", action="store_true", help="stage into work/mod for ./proto2 install")
    p.add_argument("--allow-window-shift", action="store_true",
                   help="stage even if other records would decode differently")
    p.add_argument("--no-compile-check", action="store_true")
    p.set_defaults(func=apply_edit)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
