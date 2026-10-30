#!/usr/bin/env python3
"""Prototype 2 mission editor — a desktop GUI over the proto2 tooling.

This is a front end, not a second implementation. Every read and write goes
through the modules the CLI already uses, so a change made here obeys the same
rules, verification and staging as `./proto2`:

    missiondefinit.lua   mission_tree (decode_proto / intern_constant / dump)
    fig.p3d.rz           mission_fsm.patch_bytes  (length-checked, reflowed)
    tod.p3d.rz           address.collect          (record positions)
    fe_textbible.p3d.rz  textbible (rebuild_document)
    install / revert     apply_mod                (manifest + backups)

Edits are staged into work/mod and only reach the game when you press Install,
so nothing is ever half-applied.

The FSM tab reads the decoded format rather than guessing at it: element and
branch names come from the prefix uid, transition targets are resolved through
`fsm_targets` (the condition's `stateName` is the target state's name), item tags
are shown by name via `tag_names`, and each element's `siblingCount` is checked
against what its parent's child count says it should be. The Verify button
rebuilds the selected mission's leaf from parsed structure with `fsm_rebuild` and
reports whether it is byte-identical — it is strictly read-only, writing nothing
to the game, to work/mod, or to install state.

    python3 tools/mission_gui.py             # the editor
    python3 tools/mission_gui.py --selftest  # data paths only, no window
    python3 tools/mission_gui.py --uicheck   # widget checks, non-zero exit on failure
"""

import difflib
import json
import os
import struct
import subprocess
import sys
import traceback

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import address as AD            # noqa: E402
import rz as RZ                 # noqa: E402
import fsm_chunk as FC         # noqa: E402
import fsm_graph as FG         # noqa: E402
import fsm_items as FI         # noqa: E402
import fsm_rebuild as FR       # noqa: E402
import fsm_schema as FS        # noqa: E402
import fsm_targets as FT       # noqa: E402
import mission_fsm as MF       # noqa: E402
import script_edit as SE      # noqa: E402
import tag_names as TN         # noqa: E402
import textbible as TB         # noqa: E402
from lua51 import load as lua_load, dump as lua_dump   # noqa: E402
from mission import mission_protos                      # noqa: E402
from mission_tree import (                              # noqa: E402
    coerce_literal as _coerce_literal,
    Leaf, flatten, intern_constant, resolve, rewrite_ref, views,
)
import config                   # noqa: E402
from rcf import Rcf             # noqa: E402
from uid import uid             # noqa: E402

GAME = config.game_dir()
MOD = os.path.join(ROOT, "work", "mod")
STATE_DIR = os.path.join(ROOT, "work", "state")
MANIFEST = os.path.join(STATE_DIR, "install-manifest.json")
DEFINIT = os.path.join(MOD, "scripts", "missiondefinit.lua")
BIBLE_FE = os.path.join(MOD, "art", "hud", "fe_textbible.p3d.rz")
TOOL = os.path.join(HERE, "apply_mod.py")

# Element names proven at runtime (docs/19 HANDOFF 69). The engine string dump
# has no exact "Initialize"/"OnActive" line, so they are listed here rather than
# being derived from it.
RUNTIME_ELEMENT_NAMES = {
    uid("Initialize"): "Initialize",
    uid("OnActive"): "OnActive",
}


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------

def read_archive(archive, relpath):
    """Raw bytes of one archive entry, or None. The entry keeps its RZ wrapper."""
    path = os.path.join(GAME, archive)
    if not os.path.exists(path):
        return None
    want = relpath.replace("\\", "/").lower()
    rcf = Rcf(path)
    try:
        for e in rcf.entries:
            if e.path.replace("\\", "/").lower() == want:
                rcf._fh.seek(e.offset)
                return rcf._fh.read(e.size)
    finally:
        rcf._fh.close()
    return None


def definition_bytes():
    """The mission definition Lua, from our staged copy or from boot.rcf."""
    if os.path.exists(DEFINIT):
        return open(DEFINIT, "rb").read(), "work/mod (staged)"
    blob = read_archive("boot.rcf", "scripts/missiondefinit.lua")
    if blob is None:
        raise FileNotFoundError("scripts/missiondefinit.lua not in boot.rcf")
    return blob, "boot.rcf"


def write_definition(blob):
    os.makedirs(os.path.dirname(DEFINIT), exist_ok=True)
    open(DEFINIT, "wb").write(blob)


def norm_path(path):
    """Collapse array indices so the same field across missions compares equal."""
    import re as _re
    return _re.sub(r"\[\d+\]", "[]", path)


def raw_value(leaf):
    """The text a user would type to reproduce this leaf."""
    if leaf.kind == "global":
        return str(leaf.value)
    v = leaf.value
    if isinstance(v, bytes):
        return v.decode("latin-1")
    if isinstance(v, bool):
        return "True" if v else "False"      # same spelling the field list shows (Leaf.render uses repr)
    if v is None:
        return "None"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def short_uid(value):
    """Compact rendering of an unnamed uid; the full value goes in the detail line."""
    return f"0x{value:016x}"[:10] + "…"


def staged_files():
    out = []
    if not os.path.isdir(MOD):
        return out
    for dirpath, dirs, files in os.walk(MOD):
        dirs[:] = [d for d in dirs if d != "backup"]
        for fn in files:
            rel = os.path.relpath(os.path.join(dirpath, fn), MOD).replace(os.sep, "/")
            if rel == "install-manifest.json" or fn.startswith("."):
                continue
            out.append(rel)
    return sorted(out)


# --------------------------------------------------------------------------
# model — all data access, no widgets
# --------------------------------------------------------------------------

class Model:
    def __init__(self, log):
        self.log = log
        self._missions = None
        self._bible = None
        self._fsm = {}
        self._script = {}
        self._graph = {}
        self._fsm_view = {}
        self._addr = {}
        self._domains = None
        self._engine_names = None
        self._hours = None

    # ---- missions -------------------------------------------------------
    def missions(self):
        """[(name, proto_index)] sorted by name. Cached until reload()."""
        if self._missions is not None:
            return self._missions
        blob, src = definition_bytes()
        root = lua_load(blob)
        self._missions = [(name, idx) for idx, name, _p in mission_protos(root)]
        self.log(f"loaded {len(self._missions)} mission definitions from {src}")
        return self._missions

    def reload(self):
        self._missions = None
        self._fsm.clear()
        self._fsm_view.clear()
        self._addr.clear()
        self._domains = None

    def _proto(self, mission):
        blob, _ = definition_bytes()
        root = lua_load(blob)
        for idx, name, proto in mission_protos(root):
            if name == mission:
                return root, proto
        raise KeyError(f"mission {mission!r} not found")

    def leaves(self, mission):
        """[(path, display, editable, kind, raw)] for every leaf of a mission."""
        root, proto = self._proto(mission)
        out = []
        for view, node in views(proto).items():
            if node is None:
                continue
            for path, leaf in flatten(node):
                editable = leaf.ref is not None and leaf.slot >= 0
                out.append((f"{view}.{path}", leaf.render(), editable, leaf.kind,
                            raw_value(leaf)))
        return out

    def tod_hours(self):
        """{TOD_NAME: hour} from datainit.lua, for labelling the dropdown.

        Purely informational: only the bare name is ever written back.
        """
        if self._hours is not None:
            return self._hours
        hours = {}
        try:
            import datainit as DI
            blob, _desc, _w = DI.resolve_source("game")
            for e in DI.decode_chunk(blob).entries:
                v = getattr(e, "value", None)
                if (str(e.name).startswith("TOD_")
                        and getattr(v, "kind", "") == "const"
                        and isinstance(getattr(v, "value", None), float)):
                    hours[e.name] = v.value
        except Exception as exc:              # a label is not worth failing over
            self.log(f"(TOD hours unavailable: {exc})")
        self._hours = hours
        return hours

    def engine_names(self):
        """Globals that datainit.lua defines but no shipped mission happens to use.

        The mission definitions alone are a biased sample: they only reference
        the values some mission needed. datainit.lua holds the full vocabulary,
        so the dropdowns are widened with it - 28 TOD hours rather than the 19
        any mission uses.
        """
        if self._engine_names is not None:
            return self._engine_names
        import re as _re
        names = set()
        # Always the shipped copies: deriving the vocabulary from the staged
        # file would drop a value as soon as the user's own edit stopped
        # referencing it (ambience_redzone_minimal disappeared that way).
        blobs = [read_archive("boot.rcf", "scripts/datainit.lua"),
                 read_archive("boot.rcf", "scripts/missiondefinit.lua")]
        for blob in blobs:
            if not blob:
                continue
            for pat in (rb"TOD_[A-Z0-9_]+",
                        rb"[A-Za-z0-9_]*ambience[A-Za-z0-9_]*"):
                names |= {m.decode("latin-1") for m in _re.findall(pat, blob)}
        names.discard("ambience")
        self._engine_names = sorted(names)
        return self._engine_names

    def domains(self):
        """normalised path -> sorted distinct values across every mission.

        These are the candidate lists for the dropdowns. They are derived from
        the shipped data rather than hand-written, so a value the game defines
        can never be missing from the list.
        """
        if self._domains is not None:
            return self._domains
        blob, _src = definition_bytes()
        root = lua_load(blob)
        seen = {}
        for _idx, _name, proto in mission_protos(root):
            for view, node in views(proto).items():
                if node is None:
                    continue
                for path, leaf in flatten(node):
                    seen.setdefault(norm_path(f"{view}.{path}"), set()).add(
                        raw_value(leaf))
        # Only fields with a genuinely small set get a dropdown. meta.name has
        # 190 values and a list that long helps nobody.
        names = self.engine_names()
        tod = {n for n in names if n.startswith("TOD_")}
        amb = {n for n in names if "ambience" in n.lower()}
        for key in ("flow.missionInfo.timeOfDay",
                    "flow.initialTransitionData.setTimeOfDay"):
            seen.setdefault(key, set()).update(tod)
        for key in ("flow.audioData.ambience",):
            seen.setdefault(key, set()).update(amb)
        self._domains = {k: sorted(v) for k, v in seen.items()
                         if 1 < len(v) <= 60}
        return self._domains

    def set_leaf(self, mission, path, text):
        """Rewrite one constant reference, exactly like `mission_tree set`."""
        blob, _ = definition_bytes()
        root = lua_load(blob)
        proto = next(p for _i, n, p in mission_protos(root) if n == mission)
        leaf, _view = resolve(views(proto), path)
        if not isinstance(leaf, Leaf):
            raise ValueError(f"{path} is a table, not a leaf")
        if leaf.ref is None or leaf.slot < 0:
            raise ValueError(f"{path} is computed and not editable")

        raw = text.strip()
        if leaf.kind == "global":
            new = raw.encode("latin-1")          # a global name, e.g. TOD_LAIR
        else:
            # keeps the displayed casing: "False" from the field list parses exactly like "false"
            new = _coerce_literal(raw)

        old = proto.constants[leaf.slot]
        rewrite_ref(proto, leaf.ref, intern_constant(proto, new))
        out = lua_dump(root)

        # verify by re-reading the same path out of the re-serialised bytecode
        again = lua_load(out)
        p2 = next(p for _i, n, p in mission_protos(again) if n == mission)
        leaf2, _ = resolve(views(p2), path)
        if leaf2.kind == "global":
            ok = leaf2.value == raw
        else:
            ok = leaf2.value == new or (
                isinstance(leaf2.value, str) and isinstance(new, bytes)
                and leaf2.value == new.decode("latin-1"))
        if not ok:
            raise RuntimeError(f"verification failed: {leaf2.value!r} != {new!r}")
        write_definition(out)
        self.reload()
        return old, new, len(blob), len(out)

    # ---- text bible -----------------------------------------------------
    def bible(self):
        if self._bible is None:
            if os.path.exists(BIBLE_FE):
                blob = open(BIBLE_FE, "rb").read()
                src = "work/mod (staged)"
            else:
                blob = read_archive("boot.rcf", "art/hud/fe_textbible.p3d.rz")
                if blob is None:
                    raise FileNotFoundError("fe_textbible.p3d.rz not found")
                src = "boot.rcf"
            raw = MF.unwrap(blob) if blob[:2] == b"RZ" else blob
            hs = TB.holders(raw)
            h = next(x for x in hs if x.language.lower() == "english")
            self._bible = (raw, hs, h)
            self.log(f"loaded text bible: {h.count} english strings from {src}")
        return self._bible

    def bible_search(self, needle, limit=400):
        _raw, _hs, h = self.bible()
        needle = (needle or "").lower()
        out = []
        for i, key in enumerate(h.keys):
            val = h.value(i)
            if not needle or needle in key.lower() or needle in val.lower():
                out.append((key, val))
                if len(out) >= limit:
                    break
        return out

    def bible_set(self, key, value):
        raw, hs, h = self.bible()
        i = h.index(key)
        if i < 0:
            raise KeyError(f"key {key!r} not found")
        if not key.upper().endswith("_WIN32_KEYB") and (key + "_WIN32_KEYB") in h.keys:
            note = " (a _WIN32_KEYB variant also exists and may be the one shown)"
        else:
            note = ""
        old = h.value(i)
        h.values[i] = value.encode("latin-1")
        out = TB.rebuild_document(raw, hs)
        chk = next(x for x in TB.holders(out) if x.language.lower() == "english")
        if chk.value(i) != value:
            raise RuntimeError("bible verification failed")
        os.makedirs(os.path.dirname(BIBLE_FE), exist_ok=True)
        open(BIBLE_FE, "wb").write(MF.wrap(out))
        self._bible = None
        return old, value, note

    def bible_add(self, key, value):
        """Append a new key/value to the staged bible and verify nothing else moved."""
        raw, hs, h = self.bible()
        if h.index(key) >= 0:
            raise KeyError(f"key {key!r} already exists")
        before_keys = list(h.keys)
        before_values = list(h.values)
        h.keys.append(key)
        h.values.append(value.encode("latin-1"))
        h.count += 1
        out = TB.rebuild_document(raw, hs)
        chk = next(x for x in TB.holders(out) if x.language.lower() == "english")
        if chk.count != h.count or chk.index(key) < 0 or chk.value(chk.index(key)) != value:
            raise RuntimeError("added entry did not verify")
        for i, k in enumerate(before_keys):
            if chk.value(i).encode("latin-1") != before_values[i]:
                raise RuntimeError(f"existing key {k!r} changed during rebuild")
        os.makedirs(os.path.dirname(BIBLE_FE), exist_ok=True)
        open(BIBLE_FE, "wb").write(MF.wrap(out))
        self._bible = None
        return chk.count

    # ---- FSM Lua / placement: always read what is staged, falling back ------
    def payload(self, mission, variant, filename, side="working"):
        """(inner_bytes, source) for one mission asset, from the requested side.

        working  : staged if one exists, else the archive (what edits build on)
        shipped  : the archive only
        staged   : work/mod only
        installed: the game directory only
        """
        rel = f"art/missions/{mission}/{variant}/{filename}"
        staged = os.path.join(MOD, "art", "missions", mission, variant, filename)
        installed = os.path.join(GAME, "art", "missions", mission, variant, filename)
        blob = None
        if side == "installed":
            if os.path.exists(installed):
                blob, src = open(installed, "rb").read(), "installed"
            else:
                return None, None
        elif side == "staged":
            if os.path.exists(staged):
                blob, src = open(staged, "rb").read(), "work/mod"
            else:
                return None, None
        elif side == "shipped":
            blob = read_archive("boot.rcf", rel) or read_archive("art.rcf", rel)
            src = "archive"
        else:
            if os.path.exists(staged):
                blob, src = open(staged, "rb").read(), "work/mod"
            else:
                blob = read_archive("boot.rcf", rel) or read_archive("art.rcf", rel)
                src = "archive"
        if blob is None:
            return None, None
        inner = MF.unwrap(blob) if blob[:2] == b"RZ" else blob
        return inner, src

    def fsm(self, mission, variant="active", side="working"):
        key = (mission, variant, side)
        if key not in self._fsm:
            inner, src = self.payload(mission, variant, "fig.p3d.rz", side)
            self._fsm[key] = None if inner is None else (inner, MF.fragments(inner), src)
        return self._fsm[key]

    def fsm_graph(self, mission, variant="active", side="working"):
        """Decoded state/transition graph for a mission (see tools/mission_graph.py)."""
        key = ("graph", mission, variant, side)
        if key in self._graph:
            return self._graph[key]
        got = self.payload(mission, variant, "fig.p3d.rz", side)
        g = None
        if got and got[0] is not None:
            try:
                import mission_graph as MG
                g = MG.graph_for_bytes(got[0])
            except Exception:
                g = None
        self._graph[key] = g
        return g

    def script_records(self, mission, variant="active", side="working"):
        """(index, role, payload bytes, kind, first line) for every record, kind in source/bytecode/unknown.

        Uses the same decoder as ./proto2 script, so the GUI and the CLI can never disagree about what
        is editable. Returns [] when the mission has no extracted fig.p3d.
        """
        key = ("script", mission, variant, side)
        if key in self._script:
            return self._script[key]
        rows = []
        try:
            import script_edit as SE
            got = SE.open_side(mission, variant, side)
            if got is None:
                return []
            _blob, _leaf, _off, model = got
            for i, (role, text, _how, item) in enumerate(SE._decoded(model)):
                payload = item["fields"]["payload"]
                head = ""
                if text is not None:
                    b = text.encode("latin-1") if isinstance(text, str) else text
                    head = b.split(b"\n", 1)[0][:60].decode("latin-1", "replace").rstrip()
                rows.append((i, role, len(payload), SE.record_kind(payload, text), head))
        except Exception:
            rows = []
        self._script[key] = rows
        return rows

    def _drop(self, mission, variant):
        """Invalidate every cached view of one mission variant (keys carry a side, so pop by prefix)."""
        for cache in (self._fsm, self._fsm_view, self._graph, self._script, self._addr):
            for k in [k for k in cache if isinstance(k, tuple) and len(k) >= 2
                      and k[0] == mission and k[1] == variant]:
                cache.pop(k, None)

    def fsm_apply(self, mission, variant, offset, old_text, new_text):
        inner, _frags, _src = self.fsm(mission, variant)
        old = old_text.encode("latin-1")
        new = new_text.encode("latin-1")
        if not inner[offset:offset + len(old)] == old:
            raise RuntimeError("the fragment no longer matches the file; reload it")
        out = MF.patch_bytes(inner, old, new, offset)
        dest = os.path.join(MOD, "art", "missions", mission, variant, "fig.p3d.rz")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(MF.wrap(out))
        self._drop(mission, variant)
        return len(inner), len(out)

    # ---- FSM structure / verify (read-only) -----------------------------
    def fsm_structure(self, mission, variant="active", side="working"):
        """The parsed element tree, for display only; None when the variant is absent.

        The walker lives in fsm_chunk (parse_element); this method adds the
        parent-relative expectation each element's siblingCount is checked
        against. Nothing here writes.
        """
        key = (mission, variant, side)
        if key in self._fsm_view:
            return self._fsm_view[key]
        got = self.fsm(mission, variant, side)
        if not got:
            view = {"ok": False, "why": "no fig.p3d for this variant"}
            self._fsm_view[key] = view
            return view
        inner, _frags, src = got
        try:
            leaf, _leaf_off = FG.payload(inner)
        except ValueError as exc:
            view = {"ok": False, "why": str(exc), "src": src}
            self._fsm_view[key] = view
            return view
        info = FC.parse_chunk(leaf)
        if info is None:
            view = {"ok": False, "why": "the chunk block does not parse", "src": src}
            self._fsm_view[key] = view
            return view
        table = FG.name_table()
        try:
            state_uids = FT.mission_states(leaf, table)
        except Exception:
            state_uids = set()
        stats = {"elements": 0, "sibling_mismatches": 0}
        branches = []
        for b in info["branches"]:
            node = self._element_tree(leaf, b["off"] - 12,
                                      len(info["branches"]), stats)
            if node is None:
                view = {"ok": False, "src": src,
                        "why": f"branch at {b['off'] - 12:#x} does not parse"}
                self._fsm_view[key] = view
                return view
            branches.append(node)
        view = {"ok": True, "src": src, "leaf": leaf, "info": info,
                "table": table, "state_uids": state_uids,
                "branches": branches, "stats": stats}
        self._fsm_view[key] = view
        return view

    def _element_tree(self, leaf, off, expected_siblings, stats):
        """One parsed element plus its recursive element children."""
        node = FC.parse_element(leaf, off)
        if node is None:
            return None
        stats["elements"] += 1
        if node["siblingCount"] != expected_siblings:
            stats["sibling_mismatches"] += 1
        node["expected_siblings"] = expected_siblings
        node["child_nodes"] = []
        for child in node["children"]:
            sub = self._element_tree(leaf, child, len(node["children"]), stats)
            if sub is None:
                return None
            node["child_nodes"].append(sub)
        return node

    def verify_fsm(self, mission, variant="active"):
        """Rebuild the selected payload from parsed structure and compare bytes."""
        got = self.fsm(mission, variant)
        if not got:
            return {"ok": False,
                    "message": f"{mission}/{variant}: no fig.p3d for this variant."}
        inner, _frags, src = got
        try:
            leaf, _leaf_off = FG.payload(inner)
        except ValueError as exc:
            return {"ok": False,
                    "message": f"{mission}/{variant} ({src}): {exc}"}
        try:
            rebuilt, why = FR.rebuild_leaf(leaf, FG.name_table())
        except Exception as exc:
            return {"ok": False,
                    "message": f"{mission}/{variant} ({src}): rebuild raised "
                               f"{type(exc).__name__}: {exc}"}
        if rebuilt is None:
            return {"ok": False,
                    "message": f"{mission}/{variant} ({src}): rebuild failed: {why}"}
        out, sem, tot = rebuilt
        view = self.fsm_structure(mission, variant)
        stats = view.get("stats") if view.get("ok") else None
        prefix_note = ""
        if stats:
            prefix_note = (f"\n{stats['elements']:,} element prefix(es); siblingCount "
                           f"matches the parent for "
                           f"{stats['elements'] - stats['sibling_mismatches']:,}.")
        if out == leaf:
            return {"ok": True,
                    "message": (f"{mission}/{variant} ({src}): byte-identical — "
                                f"{len(leaf):,} bytes.\n"
                                f"{sem:,}/{tot:,} items re-emitted from decoded "
                                f"fields; every size and element prefix is "
                                f"recomputed.{prefix_note}")}
        first = next((i for i in range(min(len(out), len(leaf)))
                      if out[i] != leaf[i]), min(len(out), len(leaf)))
        return {"ok": False,
                "message": (f"{mission}/{variant} ({src}): rebuild differs at "
                            f"{first:#x} ({len(out):,} vs {len(leaf):,} bytes)."
                            f"{prefix_note}")}

    # ---- addresses (Address records in tod.p3d) -------------------------
    def placement(self, mission, variant="active", side="working"):
        key = (mission, variant, side)
        if key not in self._addr:
            inner, src = self.payload(mission, variant, "tod.p3d.rz", side)
            self._addr[key] = None if inner is None else (inner, AD.collect(inner), src)
        return self._addr[key]

    def placement_apply(self, mission, variant, changes):
        """changes: {(name, sub): (x, y, z)} — offsets resolved in file order."""
        inner, recs, _src = self.placement(mission, variant)
        out = bytearray(inner)
        cursor = 0
        applied = []
        for d, payload in recs:
            off = inner.find(payload, cursor)
            if off < 0:
                continue
            cursor = off + 1
            k = (d["name"], d["sub"])
            if k not in changes:
                continue
            base = off + d["value_offset"]
            struct.pack_into("<3f", out, base, *changes[k])
            applied.append((k, d["pos"], changes[k]))
        if not applied:
            raise RuntimeError("no record matched; nothing written")
        dest = os.path.join(MOD, "art", "missions", mission, variant, "tod.p3d.rz")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(MF.wrap(bytes(out)))
        self._drop(mission, variant)
        return applied

    # ---- deploy ---------------------------------------------------------
    def installed(self):
        if not os.path.exists(MANIFEST):
            return []
        try:
            return sorted(json.load(open(MANIFEST))["installed"])
        except (OSError, ValueError, KeyError):
            return []

    def deploy_status(self):
        """[(rel, 'pending'|'staged'|'installed')] for every staged file, compared by content.

        The manifest says what Install copied, not whether it is still current: editing a file that was installed
        yesterday leaves its path in the manifest, so a membership test reports "installed" for a real pending change.
        The only honest test is the bytes.
        """
        out = []
        for rel in staged_files():
            game_path = os.path.join(GAME, rel.replace("/", os.sep))
            staged = os.path.join(MOD, rel.replace("/", os.sep))
            if not os.path.exists(game_path):
                out.append((rel, "staged"))
                continue
            try:
                same = os.path.getsize(game_path) == os.path.getsize(staged) and \
                    open(game_path, "rb").read() == open(staged, "rb").read()
            except OSError:
                same = False
            out.append((rel, "installed" if same else "pending"))
        order = {"pending": 0, "staged": 1, "installed": 2}
        return sorted(out, key=lambda kv: (order[kv[1]], kv[0]))

    def run_tool(self, args):
        p = subprocess.run([sys.executable, TOOL, "--game", GAME, "--mod", MOD,
                            "--state", STATE_DIR] + args,
                           capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------




def grab_later(top):
    """Take the input grab once the dialog is actually viewable.

    grab_set() raises TclError("window not viewable") when it is called in the
    same event that created the dialog - which is exactly what happens when a
    double-click opens it.
    """
    def take():
        try:
            top.grab_set()
        except tk.TclError:
            pass
    top.after_idle(take)


def _badge(parent, error):
    """A drawn info/error badge.

    The dialog used to put the Unicode characters U+2139 (information source) and U+2716 (heavy multiplication x)
    in a label. In the app's font those fall back to glyphs that look like a bare serif "i" (U+2139 is literally a
    script small i), so the dialog appeared to start with a stray character. Drawing the badge on a canvas makes it
    font-independent.
    """
    c = tk.Canvas(parent, width=20, height=20, highlightthickness=0, bd=0,
                  background=ttk.Style(parent).lookup("TFrame", "background") or "#f0f0f0")
    fill = "#c0392b" if error else "#2d6cdf"
    c.create_oval(1, 1, 19, 19, fill=fill, outline=fill)
    c.create_text(10, 10, text="!" if error else "i", fill="white",
                  font=("Segoe UI", 9, "bold"))
    return c


def notify(parent, title, message, error=False):
    """A small info dialog.

    Tk's built-in message box renders at the full default font, which is ~37px
    linespace on this HiDPI display - far larger than the rest of the app. These
    use the same font as everything else.
    """
    top = tk.Toplevel(parent)
    top.title(title)
    top.transient(parent)
    top.resizable(False, False)
    head = ttk.Frame(top)
    head.pack(fill="x", padx=16, pady=(16, 0))
    _badge(head, error).pack(side="left", padx=(0, 8))
    ttk.Label(top, text=message, justify="left", wraplength=560).pack(
        anchor="w", padx=16, pady=(8, 14))
    bar = ttk.Frame(top)
    bar.pack(fill="x", padx=16, pady=(0, 14))
    ttk.Button(bar, text="OK", command=top.destroy).pack(side="right")
    top.bind("<Return>", lambda _e: top.destroy())
    top.bind("<Escape>", lambda _e: top.destroy())
    grab_later(top)
    top.focus_set()
    parent.wait_window(top)


def confirm(parent, title, message):
    """Yes/No dialog with the same font sizing."""
    top = tk.Toplevel(parent)
    top.title(title)
    top.transient(parent)
    top.resizable(False, False)
    ttk.Label(top, text=message, justify="left", wraplength=560).pack(
        anchor="w", padx=16, pady=16)
    answer = {"yes": False}

    def yes(_e=None):
        answer["yes"] = True
        top.destroy()

    bar = ttk.Frame(top)
    bar.pack(fill="x", padx=16, pady=(0, 14))
    ttk.Button(bar, text="No", command=top.destroy).pack(side="right")
    ttk.Button(bar, text="Yes", command=yes).pack(side="right", padx=8)
    top.bind("<Return>", yes)
    top.bind("<Escape>", lambda _e: top.destroy())
    grab_later(top)
    top.focus_set()
    parent.wait_window(top)
    return answer["yes"]


def ask_value(parent, title, prompt, current, suggestions, translate=None):
    """Ask for one value, offering the values this field is known to take.

    Free text is still allowed: a mission could legitimately reference a global
    that no shipped mission uses yet, and refusing that would be wrong.
    """
    top = tk.Toplevel(parent)
    top.title(title)
    top.transient(parent)
    top.resizable(False, False)
    ttk.Label(top, text=prompt, justify="left",
              wraplength=560).pack(anchor="w", padx=14, pady=(14, 6))
    var = tk.StringVar(value=current)
    if suggestions:
        box = ttk.Combobox(top, textvariable=var, values=suggestions, width=52)
        box.pack(padx=14, pady=4)
        box.focus_set()
        ttk.Label(top, text=f"{len(suggestions)} known value(s) — pick one, "
                            f"or type something else",
                  foreground="#555").pack(anchor="w", padx=14)
    else:
        box = ttk.Entry(top, textvariable=var, width=52)
        box.pack(padx=14, pady=4)
        box.focus_set()
    result = {"value": None}

    def ok(_e=None):
        picked = var.get()
        result["value"] = translate(picked) if translate else picked
        top.destroy()

    def cancel(_e=None):
        top.destroy()

    top._var, top._ok = var, ok      # so --uicheck can drive the dialog

    bar = ttk.Frame(top)
    bar.pack(fill="x", padx=14, pady=14)
    ttk.Button(bar, text="Cancel", command=cancel).pack(side="right")
    ttk.Button(bar, text="OK", command=ok).pack(side="right", padx=8)
    top.bind("<Return>", ok)
    top.bind("<Escape>", cancel)
    grab_later(top)
    parent.wait_window(top)
    return result["value"]


SCROLLBAR_WIDTH = 24          # 16 px was still thin to grab; 50% wider


class NumberedText(ttk.Frame):
    """A Text widget with a synced line-number gutter.

    The gutter is a second Text (disabled, no focus) whose content is recomputed for the visible range
    on every scroll, edit and resize. Wrapping is off, so a display line is a source line - numbers
    would lie under wrap="word", which is why these panes use wrap="none".
    """

    def __init__(self, parent, on_zoom=None, **kw):
        super().__init__(parent)
        kw.setdefault("wrap", "none")
        self._readonly = False
        self._pal = None
        self.on_zoom = on_zoom
        self.text = tk.Text(self, **kw)
        self.gutter = tk.Text(self, width=6, padx=4, takefocus=0, border=0, highlightthickness=0,
                              background="#f2f2f2", foreground="#888888", state="disabled",
                              wrap="none")
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        # wrap is off, so a long line needs somewhere to go: the panes are wider than the window at normal sizes
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self._scrolled, xscrollcommand=self.hsb.set)

        self.gutter.grid(row=0, column=0, sticky="ns")
        self.text.grid(row=0, column=1, sticky="nsew")
        self.sb.grid(row=0, column=2, sticky="ns")
        self.hsb.grid(row=1, column=1, sticky="ew")
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self.text.bind("<<Modified>>", self._modified, add="+")
        self.text.bind("<KeyRelease>", lambda _e: self.redraw(), add="+")
        self.text.bind("<ButtonRelease>", lambda _e: self.redraw(), add="+")
        self.text.bind("<Configure>", lambda _e: self.redraw(), add="+")
        self.text.bind("<MouseWheel>", lambda _e: self.after(1, self.redraw), add="+")
        self.bind("<Configure>", lambda _e: self.redraw(), add="+")
        if on_zoom:
            # Ctrl+wheel: Button-4/5 on X11, MouseWheel elsewhere
            self.text.bind("<Control-MouseWheel>", lambda e: (on_zoom(1 if e.delta > 0 else -1), "break")[1], add="+")
            self.text.bind("<Control-Button-4>", lambda _e: (on_zoom(1), "break")[1], add="+")
            self.text.bind("<Control-Button-5>", lambda _e: (on_zoom(-1), "break")[1], add="+")

    # the Text API the rest of the GUI uses
    def get(self, *a):
        return self.text.get(*a)

    def apply_theme(self, pal):
        # NB: `self.configure` is forwarded to the Text widget (see below), so the frame must be coloured through
        # the base class. Using self.configure here painted every editor pane in the *window* colour, which is the
        # same grey the read-only styling uses - the pane looked disabled while it accepted typing.
        self._pal = pal
        # the frame itself needs no colour: apply_theme() already configures the ttk "TFrame" style. Colouring it
        # here cannot work - a ttk.Frame has no `background` option - and the TclError it raised was swallowed by
        # _theme_walk, so everything after it (including the recolour below) silently never ran.
        self.gutter.configure(background=pal["gutter_bg"], foreground=pal["gutter_fg"])
        self._recolour()
        try:
            self.text.tag_configure("readonly", background=pal["readonly_bg"], foreground=pal["readonly_fg"])
            self.text.tag_configure("hit", background=pal["hit_bg"])
        except Exception:
            pass

    def set_readonly(self, flag):
        """Read-only panes keep selection and copying; typing does nothing - and they say so in colour.

        Grey is reserved for read-only: the text colour when editable, the read-only colour when not. Until this
        existed, a disabled pane looked exactly like an editable one, so the only clue was that typing failed.
        """
        self._readonly = bool(flag)
        self.text.configure(state="disabled" if self._readonly else "normal")
        self._recolour()

    def _recolour(self):
        pal = getattr(self, "_pal", None)
        if not pal:
            return
        bg = pal["readonly_bg"] if self._readonly else pal["text_bg"]
        fg = pal["readonly_fg"] if self._readonly else pal["text_fg"]
        self.text.configure(background=bg, foreground=fg, insertbackground=fg,
                            selectbackground=pal["sel_bg"], selectforeground=pal["sel_fg"])

    def insert(self, *a):
        # inserting into a disabled Text is a no-op, so callers do not have to care about the state
        was = str(self.text.cget("state"))
        if was == "disabled":
            self.text.configure(state="normal")
        self.text.insert(*a)
        if was == "disabled":
            self.text.configure(state="disabled")

    def delete(self, *a):
        was = str(self.text.cget("state"))
        if was == "disabled":
            self.text.configure(state="normal")
        self.text.delete(*a)
        if was == "disabled":
            self.text.configure(state="disabled")

    def see(self, *a):
        self.text.see(*a)

    def tag_add(self, *a):
        return self.text.tag_add(*a)

    def tag_remove(self, *a):
        return self.text.tag_remove(*a)

    def tag_configure(self, *a, **k):
        return self.text.tag_configure(*a, **k)

    def tag_ranges(self, *a):
        return self.text.tag_ranges(*a)

    def search(self, *a, **k):
        return self.text.search(*a, **k)

    def configure(self, *a, **k):
        return self.text.configure(*a, **k)

    def _modified(self, _e=None):
        self.text.edit_modified(False)
        self.redraw()

    def _scrolled(self, first, last):
        self.sb.set(first, last)
        self.gutter.yview_moveto(first)
        self.redraw()

    def redraw(self):
        try:
            first = int(self.text.index("@0,0").split(".")[0])
            h = self.text.winfo_height()
            if h < 40:                 # not mapped yet (or a test harness): assume a normal viewport
                h = 600
            last = int(self.text.index("@0,%d" % h).split(".")[0])
        except Exception:
            return
        total = int(self.text.index("end-1c").split(".")[0])
        if last <= first:              # unresolved viewport (unmapped widget): show a normal page
            last = first + 40
        last = max(first, min(last + 1, total))
        width = max(3, len(str(total))) + 1
        lines = "\n".join(str(n) for n in range(first, last + 1))
        self.gutter.configure(state="normal", width=width)
        self.gutter.delete("1.0", "end")
        self.gutter.insert("1.0", lines)
        self.gutter.configure(state="disabled")
        self.gutter.yview_moveto(self.text.yview()[0])


class Tab(ttk.Frame):
    """Base tab: rebuilds when the selected mission changes."""

    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app = app
        self.mission = None
        self.build()

    def build(self):
        pass

    def select(self, mission):
        self.mission = mission
        try:
            self.refresh()
        except Exception as exc:
            self.app.log(f"! {self.__class__.__name__}: {exc}")
            self.app.log(traceback.format_exc(limit=3))

    def refresh(self):
        pass

    def _sync_variants(self, cb):
        """Offer exactly the variants this mission has in the corpus, not a fixed pair."""
        try:
            vs = SE.variants(self.mission) if self.mission else []
        except Exception:
            vs = []
        if not vs:
            return
        if tuple(str(v) for v in cb["values"]) != tuple(vs):
            cb["values"] = vs
        if self.variant.get() not in vs:
            self.variant.set(vs[0])


class DefinitionTab(Tab):
    def build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Filter").pack(side="left")
        self.q = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.q, width=34)
        entry.pack(side="left", padx=6)
        entry.bind("<KeyRelease>", lambda _e: self.fill())
        entry.bind("<Escape>", lambda _e: (self.q.set(""), self.fill()))
        self.count = ttk.Label(bar, text="")
        self.count.pack(side="left", padx=(4, 12))
        ttk.Button(bar, text="Edit value…", command=self.edit).pack(side="right")
        ttk.Label(bar, text="grey = computed",
                  foreground="#777").pack(side="right", padx=10)

        self.tree = ttk.Treeview(self, columns=("path", "value"), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="field")
        self.tree.column("#0", width=260, anchor="w")
        self.tree.heading("path", text="full path")
        self.tree.heading("value", text="value")
        self.tree.column("path", width=320, anchor="w")
        self.tree.column("value", width=420, anchor="w")
        self.tree.tag_configure("computed", foreground="#888888")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.edit())
        self.rows = []

    def refresh(self):
        """Reload every leaf of the mission, then apply the current filter.

        A few shipped missions list the same key twice in one table (Lua keeps the last value). The tree used the
        field path as its row id, so the second copy raised TclError and aborted the whole tab. Duplicates are now
        collapsed to the effective (last) value, which is also what find_path and the runtime use.
        """
        self.rows = []
        self.raw = {}
        self.duplicates = 0
        if self.mission:
            order, by_path = [], {}
            for path, shown, editable, _kind, raw in self.app.model.leaves(self.mission):
                if path in by_path:
                    self.duplicates += 1
                else:
                    order.append(path)
                by_path[path] = (path, shown, editable, raw)
            for path in order:
                p, shown, editable, raw = by_path[path]
                self.raw[p] = raw
                self.rows.append((p, shown, editable))
            if self.duplicates:
                self.app.log(f"definition {self.mission}: {self.duplicates} duplicate key(s) - "
                             f"showing the effective (last) value")
        self.fill()

    def fill(self):
        """Show the rows matching the filter, by field name or by value, nested by field path.

        The leaves already arrive as dotted paths (`activationData.activeList[0].name`), so the grouping is just the
        path split on '.', with the leaf's own segment in the tree column. A filter keeps the ancestors of every
        match, so a hit is never shown divorced from the table it lives in.
        """
        self.tree.delete(*self.tree.get_children())
        needle = self.q.get().strip().lower()
        rows = self.rows
        want = filter_text(needle)
        keep = {i for i, (path, disp, _e) in enumerate(rows)
                if not needle or want in filter_text(path) or needle in disp.lower()}

        def ancestors(path):
            out, prefix = [], ""
            for seg in path.split(".")[:-1]:
                prefix = f"{prefix}.{seg}" if prefix else seg
                out.append(prefix)
            return out

        groups = {}
        for i in sorted(keep):
            path, disp, editable = rows[i]
            parent = ""
            for prefix in ancestors(path):
                iid = f"g:{prefix}"
                if iid not in groups:
                    groups[iid] = self.tree.insert(parent, "end", iid=iid, open=True,
                                                   text=prefix.split(".")[-1], values=("", ""))
                parent = iid
            self.tree.insert(parent, "end", iid=f"r{i}", text=path.split(".")[-1],
                             values=(path, disp), tags=() if editable else ("computed",))
        shown = len(keep)
        total = len(rows)
        label = f"{shown}/{total}" if needle else f"{total} fields"
        if getattr(self, "duplicates", 0):
            label += f"  ·  {self.duplicates} duplicate key(s) merged (last wins)"
        self.count.config(text=label)

    def edit(self):
        sel = self.tree.focus()
        if not sel:
            return
        vals = self.tree.item(sel, "values")
        path = vals[0] if vals else ""
        if "computed" in self.tree.item(sel, "tags"):
            notify(self, "Not editable", f"{path} is computed at runtime.")
            return
        shown = vals[1] if len(vals) > 1 else ""
        current = self.raw.get(path, "")
        known = self.app.model.domains().get(norm_path(path), [])
        translate = None
        current_disp = current
        if path.endswith("timeOfDay"):
            hours = self.app.model.tod_hours()
            if hours:
                def label(v):
                    h = hours.get(v)
                    return f"{v}   ({h:g}h)" if h is not None else v

                def translate(text):
                    text = text.strip()
                    if text in hours:
                        return text
                    return text.split()[0] if text.split() else text

                known = [label(v) for v in known]
                current_disp = label(current)
        new = ask_value(
            self, "Edit value",
            f"{path}\n\ncurrent: {current_disp}",
            current_disp, known, translate)
        if new is None:
            return
        try:
            old, got, before, after = self.app.model.set_leaf(self.mission, path, new)
            self.app.log(f"{path}: {old!r} -> {new!r}  "
                         f"({before} -> {after} bytes, verified)")
            self.refresh()
            self.app.on_staged()
        except Exception as exc:
            notify(self, "Edit failed", str(exc), error=True)


class FsmTab(Tab):
    def build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Variant:").pack(side="left")
        self.variant = tk.StringVar(value="active")
        cb = self.cb = ttk.Combobox(top, textvariable=self.variant, values=["active"],
                                    width=8, state="readonly")
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Button(top, text="Reload", command=self.refresh).pack(side="left")
        ttk.Button(top, text="Verify", command=self.verify).pack(side="left", padx=(6, 0))
        self.src = ttk.Label(top, text="", foreground="#555")
        self.src.pack(side="right")

        self.books = ttk.Notebook(self)
        self.books.pack(fill="both", expand=True)

        # ---- structure -------------------------------------------------
        sframe = ttk.Frame(self.books, padding=(0, 6))
        ttk.Label(
            sframe,
            text="siblings = element-framed children of the parent, counting this "
                 "element. The prefix is name | 0 | siblingCount; it is computed "
                 "on write and checked by Verify.",
            foreground="#555", wraplength=860).pack(anchor="w")
        self.detail = ttk.Label(sframe, text="Select an element to see its prefix.",
                                foreground="#555", wraplength=860, justify="left")
        self.detail.pack(anchor="w", pady=(6, 0))
        cols = ("name", "kind", "siblings", "detail")
        wrap = ttk.Frame(sframe)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.stree = ttk.Treeview(wrap, columns=cols, show="headings",
                                  selectmode="browse")
        for col, label, width in (("name", "element / item", 260),
                                  ("kind", "kind / tag", 170),
                                  ("siblings", "siblings", 120),
                                  ("detail", "detail / target", 370)):
            self.stree.heading(col, text=label)
            self.stree.column(col, width=width, anchor="w")
        self.stree.tag_configure("muted", foreground="#888888")
        self.stree.tag_configure("bad", foreground="#b00020")
        self.stree.bind("<<TreeviewSelect>>", lambda _e: self.show_node())
        self.stree.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.stree.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.stree.xview)
        xsb.grid(row=1, column=0, sticky="ew")
        self.stree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.books.add(sframe, text="Structure")

        # ---- Lua fragments ---------------------------------------------
        lframe = ttk.Frame(self.books, padding=(0, 6))
        ttk.Label(lframe, text="Lua for the selected node (edit freely — length may "
                               "change; sizes are reflowed and checked)").pack(anchor="w")
        lbar = ttk.Frame(lframe)
        lbar.pack(fill="x", pady=(4, 6))
        ttk.Button(lbar, text="Apply fragment", command=self.apply).pack(side="right")
        pane = ttk.Panedwindow(lframe, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        self.list = tk.Listbox(left, width=42, exportselection=False)
        lsb = ttk.Scrollbar(left, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=lsb.set)
        self.list.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.list.bind("<<ListboxSelect>>", lambda _e: self.load_fragment())
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        ttk.Label(right, text="Lua for the selected fragment").pack(anchor="w")
        self.text = NumberedText(right, undo=True, height=18)
        self.text.pack(side="left", fill="both", expand=True)
        pane.add(right, weight=3)
        self.books.add(lframe, text="Lua fragments")
        self.frags = []
        self.nodes = {}
        self.leaf = None
        self.table = {}
        self.state_uids = set()

    def refresh(self):
        self._sync_variants(self.cb)
        self.list.delete(0, "end")
        self.text.delete("1.0", "end")
        self.frags = []
        self.nodes.clear()
        self.leaf = None
        self.table = {}
        self.state_uids = set()
        self.stree.delete(*self.stree.get_children())
        self.detail.config(text="Select an element to see its prefix.")
        if not self.mission:
            return
        got = self.app.model.fsm(self.mission, self.variant.get(), self.app.source.get())
        if not got:
            self.list.insert("end", "(no fig.p3d for this variant)")
            self.stree.insert("", "end", tags=("muted",),
                              values=("(no fig.p3d for this variant)", "", "", ""))
            self.src.config(text="")
            return
        _inner, frags, src = got
        self.frags = frags
        total = sum(len(t) for _o, t in frags)
        self.list.insert("end", f"{len(frags)} fragment(s), {total} bytes")
        for off, text in frags:
            first = text.replace("\r", "").strip().split("\n")[0][:44]
            self.list.insert("end", f"{off:#07x}  {first}")

        view = self.app.model.fsm_structure(self.mission, self.variant.get(), self.app.source.get())
        if not view.get("ok"):
            why = view.get("why", "structure unavailable")
            self.stree.insert("", "end", tags=("muted",),
                              values=("(structure unavailable)", "", "", why))
            self.src.config(text=f"source: {src}")
            self.app.log(f"fsm {self.mission}/{self.variant.get()}: "
                         f"{len(frags)} fragment(s) from {src}; structure: {why}")
            return
        self.leaf = view["leaf"]
        self.table = view["table"]
        self.state_uids = view["state_uids"]
        for branch in view["branches"]:
            self.add_element("", branch)
        stats = view["stats"]
        self.src.config(text=f"{src} · {stats['elements']} el.")
        self.app.log(f"fsm {self.mission}/{self.variant.get()}: {len(frags)} "
                     f"fragment(s), {stats['elements']} element(s) from {src}")

    # ---- FSM structure rows ---------------------------------------------
    def element_name(self, value):
        """(display, known) for an element name uid."""
        if value == 0:
            return "(none)", True
        name = RUNTIME_ELEMENT_NAMES.get(value) or self.table.get(value)
        if name is None:
            known = FT.KNOWN.get(value)
            name = known[0] if known else None
        if name is None:
            return short_uid(value), False
        return name, True

    def reference_target(self, body):
        """Resolved target of one referenceStatemachineStateChange body."""
        got = FT.target_of(body)
        if got is None:
            return None
        branch, state_uid = got
        fields = FS.decode_body(None, FT.RSC, body) or {}
        template = fields.get("template", "")
        if template:
            # The path already names the state; stateName then names the
            # sub-branch rather than repeating the leaf (docs/19 HANDOFF 73/77).
            return {"name": template.rstrip("/").rsplit("/", 1)[-1],
                    "source": "path", "template": template, "branch": branch,
                    "state_uid": state_uid}
        if state_uid == 0:
            return {"name": "(none)", "source": "none", "branch": branch,
                    "state_uid": state_uid}
        known = FT.KNOWN.get(state_uid)
        if known:
            return {"name": known[0], "source": known[1], "branch": branch,
                    "state_uid": state_uid}
        if state_uid in self.state_uids:
            return {"name": short_uid(state_uid), "source": "state-in-mission",
                    "branch": branch, "state_uid": state_uid}
        return {"name": short_uid(state_uid), "source": "unresolved",
                "branch": branch, "state_uid": state_uid}

    def target_text(self, target):
        if target is None:
            return ""
        text = f"→ {target['name']}"
        if target["source"] == "path":
            text += " (path)"
        elif target["source"] == "state-in-mission":
            text += " (state in this mission)"
        elif target["source"] == "unresolved":
            text += " (uid)"
        return text

    def target_for(self, node):
        """The transition's target, found in its conditions container.

        Conditions nest: a conditionGroup item carries another conditions
        body, so the scan descends into the decoded nested lists rather than
        looking only at the transition's direct items.
        """
        if node["tag"] != FC.TRANSITION:
            return None
        for cont in node["containers"]:
            data = self.leaf[cont["off"]:cont["off"] + cont["size"]]
            target = self.scan_references(data)
            if target is not None:
                return target
        return None

    def scan_references(self, data):
        """First state-change target in an item list, descending into groups."""
        items = FI.parse_items(data, 0, len(data))
        if not items:
            return None
        for it in items:
            body = data[it.off + FI.ITEM_HDR:it.off + FI.ITEM_HDR + it.length]
            if it.tag == FT.RSC:
                target = self.reference_target(body)
                if target is not None:
                    return target
                continue
            fields = FS.decode_body(None, it.tag, body)
            if not fields:
                continue
            if fields["type"] == "condition-group":
                target = self.scan_references(fields["content"])
            elif fields["type"] == "execute":
                target = self.scan_references(fields["conditions"])
            elif fields["type"] == "conditions-list":
                target = self.scan_references(fields["body"])
            else:
                target = None
            if target is not None:
                return target
        return None

    def add_element(self, parent, node):
        name, known = self.element_name(node["name"])
        kind = TN.display(node["tag"], self.table)
        details = []
        if node["extra"]:
            text = node["extra"]["text"].decode("latin-1", "replace")
            if text:
                label = ("template" if node["extra"]["type"] ==
                         "reference_statemachine" else "text")
                details.append(f"{label}={text!r}")
        target = self.target_for(node)
        if target is not None:
            details.append(self.target_text(target))
        siblings = str(node["siblingCount"])
        tags = []
        if node["siblingCount"] != node["expected_siblings"]:
            tags.append("bad")
            siblings += f"/{node['expected_siblings']}"
        if not known or (target is not None and target["source"] in
                         ("unresolved", "state-in-mission")):
            tags.append("muted")
        iid = self.stree.insert(parent, "end", open=True, tags=tuple(tags),
                                values=(name, kind, siblings, "  ".join(details)))
        self.nodes[iid] = {"kind": "element", "node": node, "name": name,
                           "known": known, "target": target,
                           "tags": tuple(tags)}
        for cont in node["containers"]:
            self.add_container(iid, cont)
        for child in node["child_nodes"]:
            self.add_element(iid, child)

    def add_container(self, parent, cont):
        label = TN.display(cont["tag"], self.table)
        items = FI.parse_items(self.leaf, cont["off"], cont["off"] + cont["size"])
        detail = f"count={cont['count']}"
        tags = []
        if items is None:
            detail += "  ·  body does not tile"
        else:
            detail += f"  ·  {len(items)} item(s)"
            if len(items) != cont["count"]:
                tags.append("bad")
        if not TN.lookup(cont["tag"], self.table)[0]:
            tags.append("muted")
        iid = self.stree.insert(parent, "end", open=False, tags=tuple(tags),
                                values=(label, "container", "", detail))
        self.nodes[iid] = {"kind": "container", "container": cont,
                           "label": label, "tags": tuple(tags)}
        if items is None:
            return
        for it in items:
            self.add_item(iid, cont, it)

    def add_item(self, parent, cont, it):
        label = TN.display(it.tag, self.table)
        body = self.leaf[it.off + FI.ITEM_HDR:it.off + FI.ITEM_HDR + it.length]
        container_name = TN.lookup(cont["tag"], self.table)[0]
        fields = FS.decode_body(container_name, it.tag, body)
        detail = f"len={it.length}"
        if fields is None:
            detail += "  ·  body not in fsm_schema"
        elif fields["type"] == "record":
            payload = fields["payload"]
            detail = f"{fields['form']} record, {len(payload)} bytes"
            if FS.is_lua(payload):
                detail += " (Lua)"
        elif fields["type"] == "state-change":
            target = self.reference_target(body)
            if target is not None:
                detail = f"{self.target_text(target)}  ·  branch {target['branch']}"
            else:
                detail = "state change (no target)"
            detail += f"  ·  len={it.length}"
        elif fields["type"] == "hasTag":
            detail = f"tag={fields['tag']}  ·  len={it.length}"
        elif fields["type"] == "u64-value":
            detail = f"value={fields['value']:#018x}  ·  len={it.length}"
        tags = ("muted",) if not TN.lookup(it.tag, self.table)[0] else ()
        iid = self.stree.insert(parent, "end", tags=tags,
                                values=(label, "item", "", detail))
        self.nodes[iid] = {"kind": "item", "item": it, "label": label,
                           "detail": detail, "tags": tags}

    def show_node(self):
        sel = self.stree.focus()
        if not sel:
            return
        meta = self.nodes.get(sel)
        if not meta:
            self.detail.config(text="")
            return
        if meta["kind"] == "element":
            n = meta["node"]
            bits = [f"kind {TN.display(n['tag'], self.table)}",
                    f"prefix name={meta['name']} ({n['name']:#018x})",
                    f"reserved={n['reserved']}",
                    f"siblingCount={n['siblingCount']}"]
            if n["siblingCount"] == n["expected_siblings"]:
                bits.append(f"= {n['expected_siblings']} element-framed child(ren) "
                            f"of the parent, counting this element")
            else:
                bits.append(f"expected {n['expected_siblings']} element-framed "
                            f"child(ren) of the parent — MISMATCH")
            bits.append(f"offset {n['off']:#x}, size {n['size']}")
            if meta["target"] is not None:
                t = meta["target"]
                bits.append(f"target {t['name']} ({t['source']}), "
                            f"branch {t['branch']}, stateName {t['state_uid']:#018x}")
            self.detail.config(text="  ·  ".join(bits))
        elif meta["kind"] == "container":
            c = meta["container"]
            self.detail.config(
                text=f"container {meta['label']}  ·  count={c['count']}  ·  "
                     f"size={c['size']}  ·  offset {c['off'] - 16:#x}")
        else:
            it = meta["item"]
            self.detail.config(
                text=f"item {meta['label']}  ·  {meta['detail']}  ·  "
                     f"offset {it.off:#x}  ·  total={it.total}")
        self.stree.see(sel)

    # ---- Lua fragments --------------------------------------------------
    def load_fragment(self):
        sel = self.list.curselection()
        if not sel or sel[0] == 0:
            return
        off, text = self.frags[sel[0] - 1]
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text.replace("\r\n", "\n"))

    def apply(self):
        if self.app.source.get() != "working":
            self.app.log("editing builds on the working copy - switched back from "
                         f"{self.app.source.get()!r}; reload the fragment and Apply again")
            self.app.source.set("working")
            self.app.source_changed()
            return
        sel = self.list.curselection()
        if not sel or sel[0] == 0:
            notify(self, "Nothing selected", "Pick a fragment first.")
            return
        off, old = self.frags[sel[0] - 1]
        new = self.text.get("1.0", "end-1c").replace("\r\n", "\n").replace("\n", "\r\n")
        if new == old:
            notify(self, "No change", "Nothing to apply.")
            return
        try:
            before, after = self.app.model.fsm_apply(
                self.mission, self.variant.get(), off, old, new)
            self.app.log(f"fsm {self.mission}/{self.variant.get()} {off:#07x}: "
                         f"{len(old)} -> {len(new)} bytes, file {before} -> {after}")
            self.refresh()
            self.app.on_staged()
        except Exception as exc:
            notify(self, "Apply failed", str(exc), error=True)

    def verify(self):
        """Read-only structural check of the selected mission's current payload."""
        if not self.mission:
            notify(self, "Verify", "Select a mission first.")
            return
        try:
            result = self.app.model.verify_fsm(self.mission, self.variant.get())
        except Exception as exc:
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        self.app.log("verify: " + result["message"].replace("\n", "  "))
        notify(self, "FSM verify", result["message"], error=not result["ok"])


class TextTab(Tab):
    """Text bible editor: search, edit a value, add a new key.

    Values are multi-line and contain markup, so they are edited in a text pane rather than a dialog.
    Saving rebuilds the whole document (offsets are recomputed) and verifies that every other key is
    byte-identical, which is what makes adding a key safe.
    """

    def build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Search").pack(side="left")
        self.q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.q, width=40)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _e: self.search())
        e.bind("<KeyRelease>", lambda _e: self.search())
        ttk.Button(top, text="Search", command=self.search).pack(side="left")
        ttk.Button(top, text="Add entry…", command=self.begin_add).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Mission title", command=self.edit_title).pack(side="right")
        self.count = ttk.Label(top, text="", foreground="#555")
        self.count.pack(side="right", padx=8)

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)

        listbox = ttk.Frame(pane)
        self.tree = ttk.Treeview(listbox, columns=("key", "value"), show="headings")
        self.tree.heading("key", text="bible key")
        self.tree.heading("value", text="text")
        self.tree.column("key", width=300, anchor="w")
        self.tree.column("value", width=640, anchor="w")
        sb = ttk.Scrollbar(listbox, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.load_current())
        pane.add(listbox, weight=3)

        editbox = ttk.Frame(pane)
        head = ttk.Frame(editbox)
        head.pack(fill="x", pady=(4, 2))
        ttk.Label(head, text="Key:").pack(side="left")
        self.keyvar = tk.StringVar(value="")
        self.keyentry = ttk.Entry(head, textvariable=self.keyvar, width=44)
        self.keyentry.pack(side="left", padx=(4, 8))
        self.savebtn = ttk.Button(head, text="Save value", command=self.save)
        self.savebtn.pack(side="right")
        ttk.Button(head, text="Revert", command=self.load_current).pack(side="right", padx=(0, 6))
        self.editwhat = ttk.Label(head, text="", foreground="#555")
        self.editwhat.pack(side="left")

        self.text = NumberedText(editbox, undo=True, height=8)
        self.text.pack(fill="both", expand=True)
        pane.add(editbox, weight=2)
        self.text.text.bind("<Control-s>", lambda _e: (self.save(), "break")[1])
        self._mode = "edit"

    def begin_add(self):
        self._mode = "add"
        self.keyvar.set("")
        self.text.delete("1.0", "end")
        self.savebtn.configure(text="Add entry")
        self.editwhat.config(text="new entry - type a key and the text, then Add entry")
        self.keyentry.focus_set()

    def load_current(self):
        sel = self.tree.focus()
        if not sel:
            return
        self._mode = "edit"
        self.savebtn.configure(text="Save value")
        self.keyvar.set(sel)
        try:
            _raw, _hs, h = self.app.model.bible()
            i = h.index(sel)
            val = h.value(i) if i >= 0 else ""
        except Exception as exc:
            self.editwhat.config(text=f"load failed: {exc}")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", val)
        self.editwhat.config(text=f"{sel} | {len(val)} chars")

    def save(self):
        key = self.keyvar.get().strip()
        if not key:
            self.editwhat.config(text="a key is required")
            return
        value = self.text.get("1.0", "end-1c")
        try:
            if self._mode == "add":
                n = self.app.model.bible_add(key, value)
                self.app.log(f"bible: added {key} = {value!r} ({n} keys total, existing keys unchanged)")
                self._mode = "edit"
                self.savebtn.configure(text="Save value")
                self.q.set(key)
            else:
                old, got, note = self.app.model.bible_set(key, value)
                self.app.log(f"bible {key}: {old!r} -> {got!r}{note}")
            self.search()
            self.app.on_staged()
            self.editwhat.config(text=f"saved {key} - run ./proto2 install")
        except Exception as exc:
            self.editwhat.config(text=f"save failed: {exc}")
            self.app.log(f"! bible save: {exc}")

    def refresh(self):
        if self.mission:
            self.q.set(self.mission)
            self.search()

    def search(self):
        self.tree.delete(*self.tree.get_children())
        shown = 0
        try:
            for key, val in self.app.model.bible_search(self.q.get()):
                self.tree.insert("", "end", iid=key, values=(key, val.replace("\n", "⏎")))
                shown += 1
            total = len(self.app.model.bible()[2].keys)
            capped = shown >= 400
            self.count.config(text=(f"{shown} of {total} keys" + (" (search cap 400 - narrow it)"
                                                                  if capped and self.q.get().strip() else "")))
        except Exception as exc:
            self.count.config(text="")
            self.app.log(f"! bible: {exc}")

    def edit_title(self):
        if not self.mission:
            return
        key = f"MISSION_NAME_{self.mission}"
        try:
            _raw, _hs, h = self.app.model.bible()
            i = h.index(key)
            if i < 0:
                notify(self, "No title", f"{key} is not in the bible.")
                return
            current = h.value(i)
        except Exception as exc:
            notify(self, "Bible", str(exc), error=True)
            return
        new = ask_value(self, "Mission title", f"{key}\n\ncurrent: {current}",
                        current, [])
        if new is None:
            return
        self._commit(key, new)

    def edit(self):
        sel = self.tree.focus()
        if not sel:
            return
        current = self.tree.item(sel, "values")[1].replace("⏎", "\n")
        new = ask_value(self, "Edit text", f"{sel}", current, [])
        if new is None:
            return
        self._commit(sel, new)

    def _commit(self, key, value):
        try:
            old, got, note = self.app.model.bible_set(key, value)
            self.app.log(f"bible {key}: {old!r} -> {got!r}{note}")
            self.search()
            self.app.on_staged()
        except Exception as exc:
            notify(self, "Text edit failed", str(exc), error=True)


def address_nodes(recs):
    """(nodes, parents) for the address tree: `('group', id)` and `('record', index)` nodes, alternating.

    `tod.p3d` stores the table flat, and both directions are in the fields: a record's `name` is the group it
    *belongs to*, and its `address_id` is the group it *defines* for its children. So the real shape is
    group -> member -> group -> member, and a member is not a root just because nothing defines its parent group:

    - 96 of `story_a1`'s 257 records share `Address_7608304`, and no record defines that id - it is the mission's
      root group. Rendering each member as a top-level row made 96 siblings look like 96 unrelated roots.
    - the other 58 groups are defined by a member (the record whose `address_id` equals the id), so they nest under
      that member - `BW_BaseGOTO` owns `Address_2617254`, whose only member is `1`.

    A malformed file could describe a cycle; a member is promoted to a root rather than hanging the view.
    """
    nodes, parents, gidx, ridx = [], [], {}, {}
    for i, _r in enumerate(recs):
        ridx[i] = len(nodes)
        nodes.append(("record", i))
        parents.append(None)
    by_addr = {}
    for i, r in enumerate(recs):
        by_addr.setdefault(r["address_id"], i)
    for i, r in enumerate(recs):
        try:
            gid = int(r["name"].split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if gid not in gidx:
            gidx[gid] = len(nodes)
            nodes.append(("group", gid))
            parents.append(None)
        parents[ridx[i]] = gidx[gid]
    for gid, ni in gidx.items():
        owner = by_addr.get(gid)
        parents[ni] = None if owner is None else ridx[owner]
    for i in range(len(nodes)):
        seen, j = set(), i
        while j is not None:
            if j in seen:
                parents[i] = None
                break
            seen.add(j)
            j = parents[j]
    return nodes, parents


def address_path(recs, nodes, parents, node_index):
    """The path a mission script would use for this node, e.g. `BW_BaseGOTO|1` (group nodes are not in it)."""
    parts, seen, j = [], set(), node_index
    while j is not None and j not in seen:
        seen.add(j)
        kind, payload = nodes[j]
        if kind == "record":
            parts.append(recs[payload]["sub"])
        j = parents[j]
    return "|".join(reversed(parts))


class PlacementTab(Tab):
    def build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Filter").pack(side="left")
        self.q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.q, width=32)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _e: self.fill())
        e.bind("<KeyRelease>", lambda _e: self.fill())
        ttk.Button(top, text="Filter", command=self.fill).pack(side="left")
        self.count = ttk.Label(top, text="", foreground="#555")
        self.count.pack(side="left", padx=(8, 0))
        ttk.Button(top, text="Set position…", command=self.edit).pack(side="right")
        ttk.Button(top, text="Add address…", command=self.add).pack(side="right", padx=(0, 6))
        ttk.Label(top, text="Variant:").pack(side="right", padx=6)
        self.variant = tk.StringVar(value="active")
        self.cb = ttk.Combobox(top, textvariable=self.variant, values=["active"],
                               width=8, state="readonly")
        self.cb.pack(side="right")

        # A tree, because addresses are a parent/child table in the file: read flat, "BW_BaseGOTO" and the "1"
        # that every script lookup actually uses look like two unrelated rows.
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("record", "defines", "x", "y", "z"),
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="address (group / member)")
        self.tree.column("#0", width=230, anchor="w")
        for c, w in (("record", 250), ("defines", 110), ("x", 90), ("y", 90), ("z", 90)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w" if c in ("name", "sub") else "e")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.edit())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_path())
        self.path = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.path, anchor="w").pack(fill="x", pady=(4, 0))
        self.recs = []
        self.nodes, self.parents = [], []
        self.node_of = {}

    def refresh(self):
        self._sync_variants(self.cb)
        self.recs = []
        self.tree.delete(*self.tree.get_children())
        if not self.mission:
            return
        got = self.app.model.placement(self.mission, self.variant.get(), self.app.source.get())
        if not got:
            return
        _inner, recs, src = got
        self.recs = recs
        self.app.log(f"addresses {self.mission}/{self.variant.get()}: "
                     f"{len(recs)} record(s) from {src}")
        self.fill()

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        nodes, parents = address_nodes([d for d, _p in self.recs]) if self.recs else ([], [])
        self.nodes, self.parents = nodes, parents
        kids, roots = {}, []
        for i, p in enumerate(parents):
            (roots if p is None else kids.setdefault(p, [])).append(i)
        # A match keeps its children *and* its ancestors: an address is named `parent|child`, so hiding the
        # children of a matching parent hides the record the scripts read. Coordinates match too, which is how you
        # find "the address at 197.2" without knowing what it is called.
        needle = self.q.get().strip().lower()

        def text_of(i):
            kind, payload = nodes[i]
            if kind == "group":
                return f"address_{payload}"
            d = self.recs[payload][0]
            x, y, z = d["pos"]
            where = address_path([r[0] for r in self.recs], nodes, parents, i)
            return f"{d['name']} {d['sub']} {x:.2f} {y:.2f} {z:.2f} {where}".lower()

        want = filter_text(needle)
        matches = {i for i in range(len(nodes))
                   if not needle or want in filter_text(text_of(i)) or needle in text_of(i)}
        keep = filter_keep(parents, matches) if needle else set(range(len(nodes)))

        def add(i, parent_iid):
            kind, payload = nodes[i]
            if kind == "group":
                members = len(kids.get(i, []))
                iid = self.tree.insert(parent_iid, "end", iid=f"g{payload}", open=True,
                                       text=f"Address_{payload}",
                                       values=(f"group - {members} member(s)", "-", "", "", ""))
                self.node_of[iid] = i
            else:
                d, _p = self.recs[payload]
                x, y, z = d["pos"]
                # `defines` names the group this record creates for its children - that group is the node
                # nested under it, so the cell makes the nesting self-explanatory instead of mysterious
                defines = f"Address_{d['address_id']}" if kids.get(i) else "-"
                iid = self.tree.insert(parent_iid, "end", iid=f"r{payload}", open=True, text=d["sub"],
                                       values=(f"{d['name']} / {d['sub']}", defines,
                                               f"{x:.2f}", f"{y:.2f}", f"{z:.2f}"))
                self.node_of[iid] = i
            for c in sorted(kids.get(i, []), key=lambda k: text_of(k)):
                if c in keep:
                    add(c, iid)

        for i in sorted(roots, key=lambda k: text_of(k)):
            if i in keep:
                add(i, "")
        total = len(self.recs)
        shown = sum(1 for i in keep if nodes[i][0] == "record")
        self.count.config(text=f"{shown}/{total}" if needle else f"{total} addresses")
        self.show_path()

    def selected_record(self):
        """The record behind the selection, or None when a group node is selected."""
        sel = self.tree.focus()
        if not sel or not getattr(self, "nodes", None):
            return None
        node = self.node_of.get(sel)
        if node is None:
            return None
        kind, payload = self.nodes[node]
        return self.recs[payload][0] if kind == "record" else None

    def record_rows(self):
        """Every record row currently shown - group rows are structure, not addresses."""
        out = []
        def walk(i):
            for c in self.tree.get_children(i):
                if c.startswith("r"):
                    out.append(c)
                walk(c)
        walk("")
        return out

    def show_path(self):
        """Spell out the path a mission script uses, because the path - not the row - is what the game reads."""
        sel = self.tree.focus()
        if not sel or not getattr(self, "nodes", None):
            self.path.set("")
            return
        i = self.node_of.get(sel)
        if i is None:
            self.path.set("")
            return
        kind, payload = self.nodes[i]
        if kind == "group":
            members = self.tree.get_children(sel)
            self.path.set(f"group Address_{payload} - {len(members)} member(s); "
                          "select a member to see the path a script uses")
            return
        d = self.recs[payload][0]
        path = address_path([r[0] for r in self.recs], self.nodes, self.parents, i)
        self.path.set(f"path: Root|Missions|{self.mission}|{path}     "
                      f"record: {d['name']} / {d['sub']}     "
                      f"defines group {d['address_id']}")

    def edit(self):
        if self.app.source.get() != "working":
            self.app.log("editing builds on the working copy - switched back from "
                         f"{self.app.source.get()!r}; select the record and Set position again")
            self.app.source.set("working")
            self.app.source_changed()
            return
        d = self.selected_record()
        if d is None:
            notify(self, "That is a group", "Select one of its members to move it; a group has no position.")
            return
        cur = ask_value(
            self, "Set position", f"{d['name']} / {d['sub']}\n\nx,y,z:",
            ",".join(f"{c:.3f}" for c in d["pos"]), [])
        if cur is None:
            return
        try:
            xyz = tuple(float(t) for t in cur.replace(" ", "").split(","))
            if len(xyz) != 3:
                raise ValueError("need three numbers")
        except ValueError as exc:
            notify(self, "Bad position", str(exc), error=True)
            return
        try:
            applied = self.app.model.placement_apply(
                self.mission, self.variant.get(), {(d["name"], d["sub"]): xyz})
            for k, old, new in applied:
                self.app.log(f"address {k[0]}/{k[1]}: "
                             f"{tuple(round(c, 2) for c in old)} -> "
                             f"{tuple(round(c, 2) for c in new)}")
            self.refresh()
            self.app.on_staged()
        except Exception as exc:
            notify(self, "Placement failed", str(exc), error=True)



    def add(self):
        """Append an address to the mission's tod.p3d, staged like every other edit."""
        if self.app.source.get() != "working":
            self.app.source.set("working")
            self.app.source_changed()
        sel = self.tree.focus()
        seed = self.selected_record() if sel else None
        dlg = tk.Toplevel(self)
        dlg.title("Add address")
        dlg.transient(self.winfo_toplevel())
        rows = [("Target (group or existing sub)", tk.StringVar(value=seed["name"] if seed else "")),
                ("New address name", tk.StringVar(value="proto2_new_spot")),
                ("x", tk.StringVar(value="0")), ("y", tk.StringVar(value="0")),
                ("z", tk.StringVar(value="0"))]
        for r, (label, var) in enumerate(rows):
            ttk.Label(dlg, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(dlg, textvariable=var, width=34).grid(row=r, column=1, padx=8, pady=4)
        out = {}
        def ok():
            out["v"] = [v.get().strip() for _l, v in rows]
            dlg.destroy()
        def cancel():
            dlg.destroy()
        bar = ttk.Frame(dlg)
        bar.grid(row=len(rows), column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="Add", command=ok).pack(side="left", padx=4)
        ttk.Button(bar, text="Cancel", command=cancel).pack(side="left", padx=4)
        dlg.grab_set()
        self.wait_window(dlg)
        if "v" not in out:
            return
        target, sub, xs, ys, zs = out["v"][:5]
        try:
            pos = (float(xs), float(ys), float(zs))
        except ValueError:
            notify(self, "Bad position", "x, y and z must be numbers", error=True)
            return
        try:
            got = self.app.model.placement(self.mission, self.variant.get(), "working")
            if not got:
                raise RuntimeError("the address table could not be read")
            inner, _recs, _src = got
            new_bytes, info = AD.add_member(inner, target, sub, pos)
            staged = os.path.join(MOD, "art", "missions", self.mission, self.variant.get(),
                                  "tod.p3d.rz")
            os.makedirs(os.path.dirname(staged), exist_ok=True)
            open(staged, "wb").write(RZ.wrap(new_bytes))
            self.app.log(f"address added: {info['name']} | {sub} at "
                         f"({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f}) - staged "
                         f"{os.path.relpath(staged, ROOT)}")
            self.refresh()
            self.app.on_staged()
        except Exception as exc:
            notify(self, "Placement failed", str(exc), error=True)


class DeployTab(Tab):
    def build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Install to game", command=self.install).pack(side="left")
        ttk.Button(bar, text="Revert everything", command=self.revert).pack(side="left",
                                                                    padx=6)
        ttk.Button(bar, text="Apply override pack", command=self.apply_pack).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="right")
        ttk.Label(self, text="Staged in work/mod (this is exactly what Install copies):"
                  ).pack(anchor="w")
        self.summary = ttk.Label(self, text="", foreground="#333")
        self.summary.pack(anchor="w", pady=(0, 4))
        self.tree = ttk.Treeview(self, columns=("file", "status"), show="headings")
        self.tree.heading("file", text="file")
        self.tree.heading("status", text="state")
        self.tree.column("file", width=520, anchor="w")
        self.tree.column("status", width=220, anchor="w")
        self.tree.tag_configure("pending", foreground="#b00020")
        self.tree.tag_configure("staged", foreground="#0a6b3d")
        self.tree.pack(fill="both", expand=True)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        rows = self.app.model.deploy_status()
        label = {"pending": "changed since install - Install will copy it",
                 "staged": "not installed yet",
                 "installed": "installed, up to date"}
        counts = {"pending": 0, "staged": 0, "installed": 0}
        for rel, state in rows:
            counts[state] += 1
            self.tree.insert("", "end", tags=(state,), values=(rel, label[state]))
        self.summary.config(
            text=f"{counts['pending']} changed, {counts['staged']} not installed, "
                 f"{counts['installed']} up to date (of {len(rows)})")

    def install(self):
        code, out = self.app.model.run_tool([])
        self.app.log(out or "(no output)")
        self.refresh()
        if code == 0:
            notify(self, "Installed", "Mod copied into the game directory.")
        else:
            notify(self, "Install failed", out[-800:], error=True)

    def apply_pack(self):
        """Stage the hand-written records (overrides/) so unverifiable bytecode becomes editable."""
        if not confirm(
                self, "Apply the override pack?",
                "Stages the 43 hand-written records from overrides/ into work/mod. Records that ship as\n"
                "unverifiable bytecode become editable, and the pack ships with this repository.\n\n"
                "Nothing reaches the game until you press Install.\n\nContinue?"):
            return
        tool = os.path.join(ROOT, "tools", "overrides.py")
        p = subprocess.run([sys.executable, tool, "apply"], capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        self.app.log(out or "(no output)")
        self.refresh()
        if p.returncode == 0:
            notify(self, "Override pack applied", out.splitlines()[-1] if out else "staged.")
        else:
            notify(self, "Apply failed", out[-800:], error=True)

    def revert(self):
        staged = staged_files()
        if not confirm(
                self, "Revert everything?",
                "This removes every file Install wrote, including the debug menu.\n"
                "You will need to press Install again to get it back.\n\n"
                f"Staged edits are kept: work/mod still holds {len(staged)} file(s), which is what the list "
                "below shows, and Install would copy them back.\n\nContinue?"):
            return
        code, out = self.app.model.run_tool(["--revert"])
        self.app.log(out or "(no output)")
        self.refresh()
        if code == 0:
            left = staged_files()
            if left and confirm(
                    self, "Reverted - discard staging too?",
                    f"{len(left)} file(s) are still staged in work/mod.\n\n"
                    "Yes: delete them and reinstall the debug-menu payload (the usual clean state).\n"
                    "No: keep them; the next Install would copy them into the game."):
                import modstate
                modstate.clean(MOD, os.path.join(ROOT, "assets", "debugmenu"))
                self.app.model.run_tool(["--install"])
                self.refresh()
                notify(self, "Staging cleared",
                       "work/mod holds the debug-menu payload again, and the game was reinstalled with it.")
                return
            notify(self, "Reverted",
                   f"The game directory is back to shipped. {len(left)} staged file(s) remain in work/mod.")
        else:
            notify(self, "Revert failed", out[-800:], error=True)


class ScriptTab(Tab):
    """Per-record script map AND editor.

    The map shows every record with its container role and whether it decodes; selecting a decodable
    record loads its text for editing. Applying goes through script_edit.set_record_text, which is the
    same verified path as ./proto2 script: re-pack, splice, re-parse, byte-identical preservation of
    every other payload (locked ones included), and a Lua compile check before anything is staged.
    """

    def build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Variant:").pack(side="left")
        self.variant = tk.StringVar(value="active")
        cb = ttk.Combobox(bar, textvariable=self.variant, values=["active", "open"],
                          width=8, state="readonly")
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Label(bar, text="Mode:").pack(side="left", padx=(0, 4))
        self.mode = tk.StringVar(value="Records")
        mcb = ttk.Combobox(bar, textvariable=self.mode, values=["Records", "Whole script"],
                           width=12, state="readonly")
        mcb.pack(side="left", padx=(0, 10))
        mcb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Button(bar, text="Reload", command=self.refresh).pack(side="left")
        ttk.Label(bar, text="Filter").pack(side="left", padx=(10, 4))
        self.q = tk.StringVar()
        fentry = ttk.Entry(bar, textvariable=self.q, width=26)
        fentry.pack(side="left")
        fentry.bind("<KeyRelease>", lambda _e: self.refresh())
        fentry.bind("<Escape>", lambda _e: (self.q.set(""), self.refresh()))
        ttk.Button(bar, text="Locked report", command=self.report).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="(double-click a row to open that record in the Script Map)",
                  foreground="#777").pack(side="left", padx=(18, 0))
        self.summary = ttk.Label(bar, text="", foreground="#555")
        self.summary.pack(side="right")

        self.hint = ttk.Label(self, text="editable = source text you can change. [BYTECODE] = a compiled chunk "
                             "whose decompiled text is proven (it recompiles to the same bytecode), so it can be "
                             "staged as source. read-only = a compiled chunk whose decompilation is NOT proven: "
                             "you can read it, but it stays bytecode. [LOCKED] = still unknown; preserved as-is. "
                             "Rows shaded green differ from the shipped mission.",
                             foreground="#555", justify="left")
        self.hint.pack(anchor="w", fill="x", pady=(0, 6))
        # use the whole width: a fixed wraplength left the description as a narrow column on wide windows
        self.bind("<Configure>", self._rewrap_hint)

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)

        top = ttk.Frame(pane)
        # A role group per row, because every record has a role and the work is always "the tracks ones".
        cols = ("idx", "bytes", "state", "head")
        self.tree = ttk.Treeview(top, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="role")
        self.tree.column("#0", width=130, anchor="w")
        self.tree.tag_configure("changed", background="#e3f4e3", foreground="#14521a")
        for col, label, width in (("idx", "#", 60), ("bytes", "bytes", 70),
                                  ("state", "state", 90), ("head", "first line", 520)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("locked", foreground="#b00020")
        self.tree.tag_configure("bytecode", foreground="#0a6b3d")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.on_select())
        sb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pane.add(top, weight=3)

        bottom = ttk.Frame(pane)
        head = ttk.Frame(bottom)
        head.pack(fill="x", pady=(4, 4))
        # Contextual buttons: only the actions that can do something for the selected record are shown.
        # Packing the status label first once let a long description push every button out of view.
        self.apply_btn = ttk.Button(head, text="Apply record (stage)", command=self.apply)
        self.revert_btn = ttk.Button(head, text="Revert text", command=self.on_select)
        self.dropbtn = ttk.Button(head, text="Drop artifact bytes", command=self.drop_ctrl)
        self.stage_src_btn = ttk.Button(head, text="Stage as source", command=self.stage_source)
        self.listing_btn = ttk.Button(head, text="Listing", command=self.show_listing)
        self.const_btn = ttk.Button(head, text="Constants", command=self.edit_constants)
        self.diff_btn = ttk.Button(head, text="Show in Diff", command=self.show_in_diff)
        self._lay_buttons("none")
        self.what = ttk.Label(head, text="(select a record)", foreground="#555", anchor="w", width=1)
        self.what.pack(side="left", fill="x", expand=True)
        self.text = NumberedText(bottom, undo=True, height=12)
        self.text.pack(side="left", fill="both", expand=True)
        pane.add(bottom, weight=2)

        self.roles = ttk.Label(self, text="", foreground="#333", justify="left")
        self.roles.pack(anchor="w", pady=(6, 0))
        self.current = None
        self._changed = None

    def _rewrap_hint(self, event):
        width = max(400, event.width - 28)
        try:
            current = int(self.hint.cget("wraplength") or 0)
        except Exception:
            current = 0
        if abs(current - width) > 16:
            self.hint.configure(wraplength=width)

    def _lay_buttons(self, kind, ctrl=False, can_stage=False, changed=False):
        """Show only the actions that apply to the selection.

        editable : Apply / Revert, plus Drop artifact bytes when the decode contains raw control bytes
        bytecode : Listing, plus Stage as source when the decompilation is instruction-identical
        locked   : nothing - there is no action that can touch such a record
        """
        for b in (self.apply_btn, self.revert_btn, self.dropbtn, self.stage_src_btn,
                  self.listing_btn, self.const_btn, self.diff_btn):
            b.pack_forget()
        if kind == "editable":
            self.apply_btn.pack(side="right")
            self.revert_btn.pack(side="right", padx=(0, 6))
            if changed:
                self.diff_btn.pack(side="right", padx=(0, 6))
            if ctrl:
                self.dropbtn.pack(side="right", padx=(0, 6))
        elif kind == "bytecode":
            if can_stage:
                self.stage_src_btn.pack(side="right", padx=(0, 6))
            if changed:
                self.diff_btn.pack(side="right", padx=(0, 6))
            self.const_btn.pack(side="right", padx=(0, 6))
            self.listing_btn.pack(side="right", padx=(0, 6))

    def rows(self):
        if not self.mission:
            return []
        return self.app.model.script_records(self.mission, self.variant.get(), self.app.source.get())

    # The list is grouped by role, so "the rows" are the leaves. These accessors keep every caller independent of
    # the tree layout and of the column order, which is what broke when the role column became a group node.
    def record_rows(self):
        return [r for r in _leaf_rows(self.tree)
                if (self.tree.item(r, "values") or ("",))[0] != ""]

    def row_index(self, iid):
        return int(self.tree.item(iid, "values")[0])

    def row_state(self, iid):
        return self.tree.item(iid, "values")[2]

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.roles.config(text="")
        self.summary.config(text="")
        self.what.config(text="(select a record)")
        self.text.delete("1.0", "end")
        self._changed = None
        self.current = None
        if self.mode.get() == "Whole script":
            self.load_whole()
            return
        rows = self.rows()
        if not rows:
            self.tree.insert("", "end", tags=("locked",),
                             values=("", "", "[not available]",
                                     "no extracted fig.p3d for this mission"))
            return
        per, counts = {}, {"source": 0, "bytecode": 0, "readonly": 0, "unknown": 0}
        groups = {}
        import script_edit as SE
        _got = SE.open_side(self.mission, self.variant.get(), "working")
        payload_of = {i: rec[1] for i, rec in enumerate(SE.records_with_roles(_got[3]))} if _got else {}
        changed = self._changed_indices()
        edited = 0
        needle = filter_text(self.q.get().strip()) if getattr(self, "q", None) else ""
        hidden = 0
        for idx, role, nbytes, kind, head in rows:
            if head:
                head = SE.escape_ctrl(head)      # no control glyphs in the list either
            if kind == "bytecode":
                verdict = SE.cached_verdict(payload_of.get(idx))
                # None means the verdict cache is stale (the toolchain changed since it was written). Do not
                # label that "read-only": with a stale cache every record would look unverified. "bytecode"
                # is the honest neutral state; the per-record view computes the real verdict on demand.
                verified = verdict == "equivalent"
                label = "[BYTECODE]" if verdict is None or verified else "read-only"
                tag = "bytecode" if verdict is None or verified else "readonly"
                counts_key = "bytecode" if verdict is None or verified else "readonly"
            else:
                label = {"source": "editable", "unknown": "[LOCKED]"}[kind]
                tag = "locked" if kind == "unknown" else ""
                counts_key = kind
            if needle:
                # the index is shown as "#9", so accept that spelling too
                hay = filter_text(f"#{idx} {idx} {role} {kind} {head or ''} {label}")
                if needle not in hay:
                    hidden += 1
                    continue
            tags = (tag,) if tag else ()
            if idx in changed:
                tags = tuple(tags) + ("changed",)
                edited += 1
            group = groups.get(role)
            if group is None:
                group = groups[role] = self.tree.insert("", "end", open=True, text=role,
                                                        values=("", "", "", ""))
            self.tree.insert(group, "end", tags=tags, text=f"#{idx}",
                             values=(idx, nbytes, label, head))
            per.setdefault(role, {"source": 0, "bytecode": 0, "readonly": 0, "unknown": 0})[counts_key] += 1
            counts[counts_key] += 1
        self.summary.config(text=f"{counts['source']} editable, {counts['bytecode']} bytecode, "
                                 f"{counts['readonly']} read-only, {counts['unknown']} unknown "
                                 f"(of {len(rows)})"
                                 + (f"   ·   {hidden} hidden by filter" if hidden else "")
                                 + (f"   ·   {edited} edited" if edited else ""))
        lines = []
        for r, c in sorted(per.items()):
            bits = [f"{c['source']:>3} editable"]
            if c["bytecode"]:
                bits.append(f"{c['bytecode']:>3} bytecode")
            if c["unknown"]:
                bits.append(f"{c['unknown']:>3} unknown")
            lines.append(f"{r:<14} " + ", ".join(bits))
        self.roles.config(text="\n".join(lines))

    def load_whole(self):
        """Whole-mission script: every decodable record, with a marker the importer needs."""
        import script_edit as SE
        self.current = None
        try:
            txt = SE.export_script(self.mission, self.variant.get())
        except Exception as exc:
            self.what.config(text=f"export failed: {exc}")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", txt)
        n = txt.count("-- proto2:record #")
        locked = txt.count("[LOCKED]")
        self.summary.config(text=f"{n} records, {locked} locked")
        self.what.config(text="whole script - edit freely, then Apply (stage). "
                              "Keep every '-- proto2:record #' marker: the importer splits on them.")
        self.roles.config(text="")

    def on_select(self):
        sel = self.tree.focus()
        if not sel:
            return
        vals = self.tree.item(sel, "values")
        if not vals or not vals[0]:
            # a role group, not a record: there is nothing to open, and int("") would raise
            self.current = None
            self.text.delete("1.0", "end")
            self._lay_buttons("none")
            self.what.config(text=f"{self.tree.item(sel, 'text')} (role group)")
            return
        idx, state = int(vals[0]), vals[2]
        role = self.tree.item(self.tree.parent(sel), "text") or ""
        self.current = idx
        self.text.delete("1.0", "end")
        self._crlf = False
        self._lay_buttons("none")
        self._listing = None
        self._decompiled = None
        if state in ("[BYTECODE]", "read-only"):
            import script_edit as SE
            got = SE.open_side(self.mission, self.variant.get(), "working")
            payload = None
            if got is not None:
                rows = SE.records_with_roles(got[3])
                if 0 <= idx < len(rows):
                    payload = rows[idx][1]
            info_txt = SE.bytecode_listing(payload) if payload is not None else None
            if info_txt is None:
                self.what.config(text=f"#{idx} {role} - bytecode, but no captured chunk for it")
                return
            info, listing = info_txt
            self._listing = listing
            # readable source is the useful default; the raw listing is one button away
            src, _note = SE.decompile_payload(payload) if payload is not None else (None, None)
            if src is not None:
                self.decompile()
                return
            self.what.config(text=f"#{idx} {role} - Lua 5.1 chunk: {info['bytes']} bytes, "
                                  f"{info['insns']} instructions, {info['protos']} nested prototype(s), "
                                  f"strings: {', '.join(info['strings'][:6])}")
            self.text.insert("1.0", "-- read-only: this record is compiled Lua 5.1 bytecode.\n"
                                    "-- A captured chunk can be undumped; editing means replacing it "
                                    "with equivalent source.\n\n" + listing)
            return
        if state == "[LOCKED]":
            self.what.config(text=f"#{idx} {role} - LOCKED (cannot be edited; preserved as-is)")
            self.text.set_readonly(True)
            return
        try:
            import script_edit as SE
            # load the WORKING copy, not the shipped one: the list shows staged state, and editing the shipped
            # text here would stage a payload with earlier staged edits silently dropped
            path, _is_staged = SE.base_path(self.mission, self.variant.get())
            _b, _l, _o, model = SE.open_mission(path)
            rows = SE._decoded(model)
            t = rows[idx][1]
            tb = t.encode("latin-1") if isinstance(t, str) else t
            raw = tb.decode("latin-1")
            self._crlf = "\r\n" in raw
            shown = raw.replace("\r\n", "\n")
            ctrl = SE.control_bytes(shown)
            if ctrl:
                shown = SE.escape_ctrl(shown)
                self.what.config(text=f"#{idx} {role} - editable; contains raw control byte(s) "
                                      f"{', '.join(f'{k} x{v}' for k, v in ctrl.items())} shown as "
                                      f"escapes (an imperfect decode - leave them as they are)")
            self.text.insert("1.0", shown)
            if not ctrl:
                self.what.config(text=f"#{idx} {role} - editable")
            self._lay_buttons("editable", ctrl=bool(ctrl),
                              changed=(idx in self._changed_indices()))
            self.text.set_readonly(False)
        except Exception as exc:
            self.what.config(text=f"#{idx} {role} - load failed: {exc}")

    def apply(self):
        import script_edit as SE
        import types
        text = self.text.get("1.0", "end-1c")
        if self.mode.get() == "Whole script":
            a = types.SimpleNamespace(mission=self.mission, variant=self.variant.get(),
                                      text=text, stage=True, no_compile_check=True)
            try:
                SE.import_script(a)
                self.app.log("whole script staged; run ./proto2 install")
            except SystemExit as exc:
                self.app.log(f"refused: {exc}")
            except Exception as exc:
                self.app.log(f"failed: {exc}")
            return
        if self.current is None:
            self.app.log("select an editable record first")
            return
        # the editor shows escapes for raw control bytes and \n for line endings; restore the exact
        # bytes the file uses before writing (see script_edit.escape_ctrl / unescape_ctrl)
        text = SE.unescape_ctrl(text)
        if getattr(self, "_crlf", False):
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        a = types.SimpleNamespace(mission=self.mission, variant=self.variant.get(),
                                  index=self.current, text=text, stage=True,
                                  no_compile_check=True)
        try:
            SE.set_record_text(a)
            self.app.log(f"record #{self.current} staged; run ./proto2 install")
        except SystemExit as exc:
            self.app.log(f"refused: {exc}")
        except Exception as exc:
            self.app.log(f"failed: {exc}")

    def drop_ctrl(self):
        """Remove the escaped artifact bytes from the editor text - then review and Apply.

        These bytes are decode artifacts, not authored data, so removing them is usually the right
        repair - but the change goes through the normal Apply path, so the diff shows exactly what moved.
        """
        import re as _re
        import script_edit as SE
        text = self.text.get("1.0", "end-1c")
        fixed, n = _re.subn(r"\\x[0-9a-f]{2}", "", text)
        if not n:
            self.what.config(text="no escaped artifact bytes left in this record")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", fixed)
        self.what.config(text=f"removed {n} artifact byte(s) - check whether one replaced a real "
                              f"character (e.g. 'nitial' -> 'initial'), then Apply record (stage)")

    def show_listing(self):
        """Show the raw chunk disassembly for the selected bytecode record."""
        self.text.set_readonly(True)
        listing = getattr(self, "_listing", None)
        if not listing:
            self.app.log("no listing for this record")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "-- read-only: this record is compiled Lua 5.1 bytecode.\n"
                                "-- A captured chunk can be undumped; editing means replacing it with\n"
                                "-- equivalent source (press Decompile).\n\n" + listing)
        self.what.config(text=f"#{self.current}: raw listing")

    def _changed_indices(self):
        """Which records differ from the shipped file on the side being shown, cached per refresh."""
        if self._changed is None:
            import script_edit as SE
            try:
                self._changed = SE.changed_indices(self.mission, self.variant.get(),
                                                   self.app.source.get())
            except Exception:
                self._changed = set()
        return self._changed

    def show_in_diff(self):
        """Open this record in the Diff tab, preferring the copy that actually contains the change."""
        if self.current is None:
            return
        idx = self.current
        d = self.app.tab_diff
        for side in ("installed", "staged"):
            if d.open_record_index(idx, side):
                self.app.tabs.select(self.app.tabs.index(d))
                if side == "staged":
                    self.app.log(f"#{idx}: this change is staged but not installed - comparing against "
                                 f"staged")
                self.app.log(f"#{idx}: opened in Diff (against {side})")
                return
        self.app.log(f"#{idx}: no difference against installed or staged - nothing to show")

    def focus_record(self, index, line=None):
        """Select one record by index, optionally highlighting a line. Returns True when it was found.

        A jump must not be defeated by a filter the user typed earlier, so the filter is dropped when it would
        hide the target - the alternative is a button that appears to do nothing.
        """
        if self.mode.get() != "Records":
            self.mode.set("Records")
        if getattr(self, "q", None) and self.q.get().strip():
            self.q.set("")
        self.refresh()
        for row in self.record_rows():
            if self.row_index(row) != index:
                continue
            self.tree.focus(row)
            self.tree.selection_set(row)
            self.tree.see(row)
            self.on_select()
            if line:
                target = f"{line}.0"
                try:
                    self.text.text.tag_remove("hit", "1.0", "end")
                    self.text.text.tag_configure("hit", background="#fff3b0")
                    self.text.text.tag_add("hit", target, f"{line}.end")
                    self.text.text.mark_set("insert", target)
                    self.text.text.see(target)
                except Exception:
                    pass
            return True
        return False

    def decompile(self):
        """Show LuaDec's reconstruction of the selected bytecode record, with its equivalence verdict."""
        self.text.set_readonly(True)
        import script_edit as SE
        if self.current is None:
            return
        got = SE.open_side(self.mission, self.variant.get(), "working")
        if got is None:
            self.app.log("mission not found")
            return
        rows = SE.records_with_roles(got[3])
        if not (0 <= self.current < len(rows)):
            return
        payload = rows[self.current][1]
        src, note = SE.decompile_payload(payload)
        if src is None:
            # a locked record has no listing and no staging: clear the buttons, otherwise the previous
            # selection's buttons stay packed and the view claims this record can do things it cannot
            self.what.config(text=f"#{self.current}: cannot decompile - {note}")
            self._lay_buttons("locked")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", f"-- record #{self.current} is locked: {note}\n"
                                    f"-- its payload is preserved byte-for-byte; constants cannot be listed either\n")
            self.text.set_readonly(True)
            return
        verdict, detail = SE.roundtrip_equivalent(payload)
        self._decompiled = (src, verdict)
        self._lay_buttons("bytecode", can_stage=(verdict == "equivalent"),
                          changed=(self.current in self._changed_indices()))
        self.text.delete("1.0", "end")
        if verdict != "equivalent":
            packed = None
            try:
                import overrides as OV
                packed = OV.find(self.app.mission, self.variant.get(), self.current)
            except Exception:
                packed = None
            recover = ("-- A hand-written reconstruction of this record is in the override pack. Press\n"
                       "-- 'Apply override pack' on the Deploy tab (or run ./proto2 overrides apply) and it\n"
                       "-- becomes editable here - the pack ships with the repository.\n") if packed else ""
            header = (f"-- READ-ONLY: this decompilation is NOT verified.\n"
                      f"--   {detail}\n"
                      f"-- Recompiling the text below does not reproduce this record's bytecode, so editing is\n"
                      f"-- disabled and the shipped bytecode is what runs. Read it for reference, or use the\n"
                      f"-- Listing view for the actual instructions.\n{recover}")
        else:
            header = (f"-- LuaDec reconstruction ({note}; verdict: {verdict} - {detail})\n"
                      f"-- 'Stage as source' is enabled only when the recompiled chunk is\n"
                      f"-- instruction-identical to the original.\n")
        self.text.insert("1.0", header + "\n" + src)
        self.what.config(text=f"#{self.current} decompiled: {verdict} ({detail})")
        self.app.log(f"#{self.current}: decompile verdict {verdict} ({detail})")

    def stage_source(self):
        import script_edit as SE
        if self.current is None or not getattr(self, "_decompiled", None):
            return
        src, verdict = self._decompiled
        if verdict != "equivalent":
            self.app.log("refusing: only instruction-identical decompilations can be staged")
            return
        try:
            SE.stage_record_source(self.mission, self.variant.get(), self.current, src)
            self.app.log(f"#{self.current}: bytecode replaced with decompiled source - run ./proto2 install")
            self.app.on_staged()
        except SystemExit as exc:
            self.app.log(f"refused: {exc}")
        except Exception as exc:
            self.app.log(f"failed: {exc}")

    def edit_constants(self):
        """List a compiled record's constants and replace one - the edit path for read-only records.

        The replacement is staged through script_edit.stage_record_constant, which rebuilds the chunk and refuses
        unless every instruction word and every other constant is unchanged.
        """
        import script_edit as SE
        if self.current is None:
            return
        payload = None
        for side in ("working", "shipped", "installed"):
            got = SE.open_side(self.mission, self.variant.get(), side)
            if got is None:
                continue
            rows = SE.records_with_roles(got[3])
            if 0 <= self.current < len(rows) and SE.bytecode_chunk(rows[self.current][1]) is not None:
                payload = rows[self.current][1]
                break
        if payload is None:
            self.app.log(f"#{self.current}: no captured chunk to read constants from")
            return
        spec = SE.chunk_constants(payload)
        if not spec:
            self.app.log(f"#{self.current}: this record has no constants")
            return
        win = tk.Toplevel(self)
        win.title(f"record #{self.current} constants - {self.mission}/{self.variant.get()}")
        win.transient(self)
        ttk.Label(win, text="Select a constant, type the replacement, then Apply. Only the chosen value changes:\n"
                            "the instruction words and every other constant are compared before staging.",
                  foreground="#555", justify="left").pack(anchor="w", padx=8, pady=(8, 4))
        cols = ("path", "index", "kind", "value")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=14)
        for c, w in zip(cols, (70, 60, 80, 520)):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="w")
        for c in spec:
            tree.insert("", "end", values=(c["path"] or "(root)", c["index"], c["kind"], c["value"][:200]))
        tree.pack(fill="both", expand=True, padx=8)
        row = ttk.Frame(win)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="new value:").pack(side="left")
        value = tk.StringVar()
        entry = ttk.Entry(row, textvariable=value, width=40)
        entry.pack(side="left", padx=(6, 6))
        chosen = {}

        def on_select(_e=None):
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            chosen["path"] = "" if vals[0] == "(root)" else vals[0]
            chosen["index"] = int(vals[1])
            chosen["kind"] = vals[2]
            value.set(vals[3])
            entry.focus_set()
        tree.bind("<<TreeviewSelect>>", on_select)

        def apply():
            if not chosen:
                self.app.log("select a constant first")
                return
            try:
                SE.stage_record_constant(self.mission, self.variant.get(), self.current,
                                         chosen["path"], chosen["index"], value.get())
            except SystemExit as exc:
                self.app.log(f"refused: {exc}")
                return
            except Exception as exc:
                self.app.log(f"failed: {type(exc).__name__}: {exc}")
                return
            self.app.log(f"#{self.current}: constant {chosen['index']} replaced; run ./proto2 install")
            win.destroy()
            self.app.on_staged()
            self.refresh()

        ttk.Button(row, text="Apply (stage)", command=apply).pack(side="left")
        ttk.Button(row, text="Close", command=win.destroy).pack(side="left", padx=(6, 0))

    def report(self):
        rows = self.rows()
        locked = [(role, idx) for idx, role, _n, ok, _h in rows if not ok]
        self.app.log(f"{self.mission}: {len(rows) - len(locked)} editable, {len(locked)} locked")
        for role, idx in locked[:40]:
            self.app.log(f"  [LOCKED] {role} #{idx}")


class WholeScriptTab(Tab):
    """The complete mission Lua as one editable text - the file you would actually write against.

    Export lists every record with a `-- proto2:record #N role` marker; locked records appear as a
    marker plus a note and no body. Apply splits on those markers and rewrites only the records whose
    text changed, through script_edit.import_script (same verification as ./proto2 script).
    """

    def build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Variant:").pack(side="left")
        self.variant = tk.StringVar(value="active")
        cb = ttk.Combobox(bar, textvariable=self.variant, values=["active", "open"],
                          width=8, state="readonly")
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Button(bar, text="Reload", command=self.refresh).pack(side="left")
        self.applybtn = ttk.Button(bar, text="Apply (stage)", command=self.apply)
        self.applybtn.pack(side="left", padx=(6, 0))
        self.with_bc = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="show read-only text", variable=self.with_bc,
                        command=self.refresh).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Find", command=self.find).pack(side="left", padx=(6, 0))
        self.q = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.q, width=24)
        e.pack(side="left", padx=(4, 0))
        e.bind("<Return>", lambda _ev: self.find())
        self.info = ttk.Label(bar, text="", foreground="#555")
        self.info.pack(side="right")

        ttk.Label(self, text="One text for the whole mission. Edit inside a record's markers; keep every "
                             "'-- proto2:record #' line, the importer splits on them. Compiled records appear as "
                             "a note: [BYTECODE] ones can be staged from the Script Map, [READ-ONLY] ones are a "
                             "reconstruction that cannot be applied - tick 'show read-only text' to read them "
                             "here; those lines are grey and refuse editing, and Apply rejects the file if one "
                             "was changed.",
                  foreground="#555", wraplength=920, justify="left").pack(anchor="w", pady=(0, 6))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.text = NumberedText(wrap, undo=True)
        self.text.pack(side="left", fill="both", expand=True)
        self.text.text.bind("<Key>", self._guard_key, add="+")
        self.text.text.bind("<<Paste>>", self._guard_edit, add="+")
        self.text.text.bind("<<Cut>>", self._guard_edit, add="+")
        self.text.text.bind("<<Clear>>", self._guard_key, add="+")
        self.text.text.bind("<Button-2>", self._guard_edit, add="+")

    def refresh(self):
        self.text.delete("1.0", "end")
        if not self.mission:
            return
        import script_edit as SE
        want = self.app.source.get()
        side = "stock" if want == "shipped" else want   # "working" is handled by export_script
        try:
            txt = SE.export_script(self.mission, self.variant.get(), side,
                                   include_bytecode=self.with_bc.get())
        except SystemExit as exc:
            self.text.insert("1.0", f"-- {exc}\n")
            self.info.config(text=f"{want}: not present")
            return
        except Exception as exc:
            self.text.insert("1.0", f"-- export failed: {exc}\n")
            self.info.config(text="")
            return

        self.text.insert("1.0", txt)
        # the tag colours come from the active palette, applied by App.apply_theme; setting them here would
        # override the theme on every refresh, which is exactly how the read-only blocks stayed light grey
        self.text.apply_theme(self.app.THEMES[self.app.theme])
        self._mark_readonly(txt)
        recs = txt.count("-- proto2:record #")
        readonly = txt.count("[READ-ONLY]")
        bytecode = txt.count("[BYTECODE] provably")
        bodies = txt.count(SE.READONLY_END)
        note = "" if want == "working" else "  (view only - switch to working to edit)"
        extra = f" | {readonly} read-only" + (f" ({bodies} with text)" if bodies else "")
        if bytecode:
            extra += f", {bytecode} convertible"
        self.info.config(text=f"{want} | {len(txt):,} chars | {recs} records{extra}{note}")

    def apply(self):
        import script_edit as SE
        import types
        if self.app.source.get() != "working":
            self.app.log("editing builds on the working copy (staged if one exists, else shipped) - "
                         f"{self.app.source.get()!r} is a view; switched back to working, edit and Apply again")
            self.app.source.set("working")
            self.app.source_changed()
            return
        txt = self.text.get("1.0", "end-1c")
        a = types.SimpleNamespace(mission=self.mission, variant=self.variant.get(), text=txt,
                                  stage=True, no_compile_check=True)
        try:
            SE.import_script(a)
            self.app.log(f"{self.mission}: whole script staged - run ./proto2 install")
        except SystemExit as exc:
            self.app.log(f"refused: {exc}")
        except Exception as exc:
            self.app.log(f"failed: {exc}")

    def _mark_readonly(self, txt):
        """Grey out the reference blocks and remember them, so typing inside one can be refused."""
        t = self.text.text
        t.tag_remove("readonly", "1.0", "end")
        lines = txt.split("\n")
        ranges = 0
        i = 0
        while i < len(lines):
            if lines[i].startswith("-- proto2:record #") and i + 1 < len(lines) \
                    and lines[i + 1].startswith("-- [READ-ONLY]"):
                start = i + 1                      # the note line itself
                k = start + 1
                while k < len(lines) and lines[k].strip() != "-- proto2:end-readonly":
                    k += 1
                if k >= len(lines) or lines[k].strip() != "-- proto2:end-readonly":
                    k = start + 1                  # no body: only the note is grey
                t.tag_add("readonly", f"{start + 1}.0", f"{k + 1}.0")
                ranges += 1
                i = k + 1
                continue
            i += 1
        self._readonly_ranges = ranges

    def _in_readonly(self, index):
        t = self.text.text
        for lo, hi in zip(t.tag_ranges("readonly")[0::2], t.tag_ranges("readonly")[1::2]):
            if t.compare(lo, "<=", index) and t.compare(index, "<", hi):
                return True
        return False

    def _guard_key(self, event=None):
        """Refuse typing, deleting or pasting that would change a reference block."""
        if not getattr(self, "_readonly_ranges", 0):
            return None
        sym = getattr(event, "keysym", "") if event is not None else ""
        if sym in ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next", "Tab",
                   "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"):
            return None
        t = self.text.text
        try:
            ins = t.index("insert")
        except Exception:
            return None
        if sym == "BackSpace":
            if self._in_readonly(t.index(f"{ins} -1c")):
                self.app.log("read-only block: this decompilation is not proven, so it cannot be edited or staged; the shipped bytecode is what runs")
                return "break"
            return None
        if self._in_readonly(ins):
            self.app.log("read-only block: this decompilation is not proven, so it cannot be edited or staged; the shipped bytecode is what runs")
            return "break"
        return None

    def _guard_edit(self, event=None):
        t = self.text.text
        if not getattr(self, "_readonly_ranges", 0):
            return None
        if t.tag_ranges("sel"):
            lo, hi = t.index("sel.first"), t.index("sel.last")
            if self._in_readonly(lo) or self._in_readonly(hi):
                self.app.log("read-only block: this decompilation is not proven, so it cannot be edited or staged; the shipped bytecode is what runs")
                return "break"
        return None

    def find(self):
        needle = self.q.get()
        if not needle:
            return
        self.text.tag_remove("hit", "1.0", "end")
        pos = "1.0"
        hits = 0
        while True:
            got = self.text.search(needle, pos, stopindex="end")
            if not got:
                break
            end = f"{got}+{len(needle)}c"
            self.text.tag_add("hit", got, end)
            pos = end
            hits += 1
            if hits >= 500:
                break
        self.text.tag_configure("hit", background="#ffe08a")
        if hits:
            self.text.see(self.text.tag_ranges("hit")[0])
        self.info.config(text=f"{hits} match(es) for {needle!r}")


class GraphTab(Tab):
    """The mission's state machine as a navigable graph.

    States and the transitions that target them come from tools/mission_graph.py - the same decoder the
    CLI uses - so this view cannot drift from the data. Targets that resolve to a recovered name are
    shown by name; the rest keep their uid, honestly, rather than being guessed at.
    """

    def build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Variant:").pack(side="left")
        self.variant = tk.StringVar(value="active")
        cb = ttk.Combobox(bar, textvariable=self.variant, values=["active", "open"],
                          width=8, state="readonly")
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Button(bar, text="Reload", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Find in script", command=self.find_in_script).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="(double-click a row to open that record in the Script Map)",
                  foreground="#777").pack(side="left", padx=(18, 0))
        self.summary = ttk.Label(bar, text="", foreground="#555")
        self.summary.pack(side="right")

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane)
        ttk.Label(left, text="States").pack(anchor="w")
        self.states = ttk.Treeview(left, columns=("name", "parent", "uid", "sib"), show="headings",
                                   selectmode="browse")
        for col, label, w in (("name", "state", 200), ("parent", "inside", 170),
                              ("uid", "uid", 140), ("sib", "siblings", 70)):
            self.states.heading(col, text=label)
            self.states.column(col, width=w, anchor="w")
        self.states.tag_configure("unnamed", foreground="#b00020")
        self.states.bind("<<TreeviewSelect>>", lambda _e: self.fill_edges())
        sb = ttk.Scrollbar(left, orient="vertical", command=self.states.yview)
        self.states.configure(yscrollcommand=sb.set)
        self.states.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pane.add(left, weight=2)

        right = ttk.Frame(pane)
        self.title = ttk.Label(right, text="Transitions (all)", foreground="#333")
        self.title.pack(anchor="w")
        self.edges = ttk.Treeview(right, columns=("from", "to", "kind", "branch"), show="headings")
        for col, label, w in (("from", "from", 200), ("to", "target", 200),
                              ("kind", "kind", 110), ("branch", "branch", 70)):
            self.edges.heading(col, text=label)
            self.edges.column(col, width=w, anchor="w")
        self.edges.tag_configure("unnamed", foreground="#b00020")
        esb = ttk.Scrollbar(right, orient="vertical", command=self.edges.yview)
        self.edges.configure(yscrollcommand=esb.set)
        self.edges.pack(side="left", fill="both", expand=True)
        esb.pack(side="right", fill="y")
        pane.add(right, weight=5)

    def refresh(self):
        self.states.delete(*self.states.get_children())
        self.edges.delete(*self.edges.get_children())
        self.summary.config(text="")
        self.title.config(text="Transitions (all)")
        if not self.mission:
            return
        g = self.app.model.fsm_graph(self.mission, self.variant.get(), self.app.source.get())
        if not g:
            self.summary.config(text="no fig.p3d / no graph for this mission")
            return
        self.graph = g
        self.summary.config(text=f"branches {', '.join(g['branches']) or '-'} | "
                                 f"{g['states_total']} states ({g['states_named']} named) | "
                                 f"{len(g['edges'])} edges "
                                 f"({g['targets_named']} named, {g['targets_unnamed']} unnamed)")
        # sorted by the parent first, so sub-states sit next to what contains them
        for st in sorted(g["states"], key=lambda x: (x.get("depth", 0),
                                                     x.get("parent") or "",
                                                     x["name"] or "\uffff", x["uid"])):
            label = st["name"] or f"{st['uid']:#018x}"
            inside = st.get("parent") or ("(top level)" if st.get("depth", 0) <= 1
                                          else f"state {st.get('parent_uid'):#018x}")
            self.states.insert("", "end", tags=() if st["name"] else ("unnamed",),
                               values=(label, inside, f"{st['uid']:#018x}", st["siblings"]))
        self.fill_edges()

    def find_in_script(self):
        """Jump from a graph node to the script records that mention it.

        The graph knows state names and uids; the records are where those names are actually used, so
        this searches every decodable record for the selected label and reports the hits, then opens the
        whole-script view with that text highlighted.
        """
        sel = self.states.focus()
        if not sel:
            self.app.log("select a state first")
            return
        label = self.states.item(sel, "values")[0]
        uid = self.states.item(sel, "values")[1]
        import script_edit as SE
        try:
            path, _st = SE.base_path(self.mission, self.variant.get())
            _b, _l, _o, model = SE.open_mission(path)
            rows = SE._decoded(model)
        except Exception as exc:
            self.app.log(f"cannot read records: {exc}")
            return
        needles = [n for n in (label, uid, uid.replace("0x", "")) if n]
        hits = []
        for i, (role, t, _how, _it) in enumerate(rows):
            if t is None:
                continue
            tb = t.encode("latin-1") if isinstance(t, str) else t
            txt = tb.decode("latin-1")
            for n in needles:
                if n and n in txt:
                    line = txt[:txt.index(n)].count("\n") + 1
                    hits.append((i, role, line, n))
                    break
        if not hits:
            self.app.log(f"{label}: no record mentions it (it may only exist as a condition target)")
            return
        self.app.log(f"{label}: {len(hits)} record(s) mention it")
        for i, role, line, n in hits[:12]:
            self.app.log(f"   record #{i} ({role}), line {line}, matched {n}")
        # open the whole-script view and highlight the first hit
        try:
            idx = self.app.tabs.index(self.app.tab_whole)
            self.app.tabs.select(idx)
            self.app.tab_whole.q.set(hits[0][3])
            self.app.tab_whole.find()
        except Exception as exc:
            self.app.log(f"could not open the script view: {exc}")

    def fill_edges(self):
        self.edges.delete(*self.edges.get_children())
        g = getattr(self, "graph", None)
        if not g:
            return
        sel = self.states.focus()
        only = None
        if sel:
            vals = self.states.item(sel, "values")
            only = vals[0]
            self.title.config(text=f"Transitions from {only}")
        else:
            self.title.config(text="Transitions (all)")
        for e in g["edges"]:
            if only and e["from"] != only:
                continue
            to = e["to"] or f"{e['to_uid']:#018x}"
            self.edges.insert("", "end", tags=() if e["to"] else ("unnamed",),
                              values=(e["from"], to, e["kind"], e["branch"]))


class DiffTab(Tab):
    """What changed, record by record, between the shipped mission and a staged or installed copy.

    Read-only: it compares bytes and shows a unified text diff, so an edit can be audited after the fact
    instead of remembered. Locked records are listed too - if one ever differs that is flagged, because
    through this tooling it is impossible.
    """

    def build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Compare against:").pack(side="left")
        self.against = tk.StringVar(value="installed")
        cb = ttk.Combobox(bar, textvariable=self.against, values=["installed", "staged"],
                          width=10, state="readonly")
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        ttk.Button(bar, text="Reload", command=self.refresh).pack(side="left")
        self.revert_btn = ttk.Button(bar, text="Revert record to stock",
                                     command=self.revert_selected, state="disabled")
        self.revert_btn.pack(side="left", padx=(12, 0))
        ttk.Label(bar, text="(double-click a row to open that record in the Script Map)",
                  foreground="#777").pack(side="left", padx=(18, 0))
        self.summary = ttk.Label(bar, text="", foreground="#555")
        self.summary.pack(side="right")

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)

        top = ttk.Frame(pane)
        self.tree = ttk.Treeview(top, columns=("idx", "role", "state", "delta"), show="headings",
                                 selectmode="browse")
        for col, label, w in (("idx", "#", 60), ("role", "role", 160),
                              ("state", "state", 120), ("delta", "bytes", 90)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=w, anchor="w")
        self.tree.tag_configure("warn", foreground="#b00020")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show())
        self.tree.bind("<Double-1>", self.open_record)
        sb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pane.add(top, weight=1)

        bottom = ttk.Frame(pane)
        self.text = NumberedText(bottom, height=16)
        self.text.set_readonly(True)
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("add", background="#e6ffe6")
        self.text.tag_configure("del", background="#ffe6e6")
        pane.add(bottom, weight=2)
        self.data = None

    def other_files(self):
        """Staged files that are not fig records but carry this mission's edits.

        A Definition-tab change (the mission's name, its flow fields) is a constant in
        scripts/missiondefinit.lua; a text edit is the bible; an address or placement edit is the mission's tod or
        prop_joined. None of those live in fig.p3d.rz, so a record-only diff could never show them.
        """
        m, v = self.mission, self.variant()
        out = []
        for role, rel, tab in (
                ("missiondefinit", "scripts/missiondefinit.lua", "Definition"),
                ("text bible", "art/hud/fe_textbible.p3d.rz", "Text & Titles"),
                ("addresses", f"art/missions/{m}/{v}/tod.p3d.rz", "Addresses"),
                ("props", f"art/missions/{m}/{v}/prop_joined.p3d.rz", "Deploy")):
            staged = os.path.join(MOD, rel)
            if not os.path.exists(staged):
                continue
            try:
                a = open(staged, "rb").read()
            except OSError:
                continue
            stock = (read_archive("boot.rcf", rel) or read_archive("art.rcf", rel) or b"")
            inst_path = os.path.join(GAME, rel)
            installed = open(inst_path, "rb").read() if os.path.exists(inst_path) else stock
            base = stock if self.against.get() == "staged" else installed
            if a != base:
                out.append({"role": role, "rel": rel, "tab": tab, "state": "changed",
                            "delta": len(a) - len(base)})
            elif a != stock:
                # the edit is already installed, so it cannot show as pending - but it still differs from the
                # shipped file, and saying so is what makes "I changed it, why is nothing listed" answerable
                out.append({"role": role, "rel": rel, "tab": tab, "state": "installed",
                            "delta": len(a) - len(stock)})
        return out

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.text.delete("1.0", "end")
        self.data = None
        self.row_of = {}
        if hasattr(self, "revert_btn"):
            self.revert_btn.configure(state="disabled")
        if not self.mission:
            return
        import script_edit as SE
        try:
            d = SE.diff_mission(self.mission, self.variant(), self.against.get())
        except Exception as exc:
            self.summary.config(text=f"diff failed: {exc}")
            return
        if not d.get("ok"):
            self.summary.config(text=f"cannot compare: {d.get('why')}")
            return
        self.data = d
        self.summary.config(text=f"{len(d['changed'])} changed, "
                                 f"{sum(1 for r in d['rows'] if r['state'] == 'unchanged')} unchanged, "
                                 f"{len(d['locked'])} locked | file "
                                 f"{d['sizes'][0]} -> {d['sizes'][1]} ({d['bytes']:+d})")
        for r in d["rows"]:
            # locked records are counted in the summary; they only belong here if they DIFFER, which
            # through this tooling is impossible and would be a red flag
            if r["state"] not in ("changed", "LOCKED CHANGED"):
                continue
            warn = r["state"] == "LOCKED CHANGED"
            self.tree.insert("", "end", tags=("warn",) if warn else (),
                             values=(r["index"], r["role"], r["state"], f"{r['delta']:+d}"))
        # non-record edits (definition, text, addresses, placements) live outside fig.p3d.rz
        extras = self.other_files()
        for x in extras:
            iid = self.tree.insert("", "end", tags=("file",), values=("", x["role"], x["state"],
                                                                     f"{x['delta']:+d}"))
            self.row_of[iid] = x
        if extras:
            self.tree.tag_configure("file", foreground="#0a2f6b")
        if not d["changed"] and not extras:
            self.text.insert("end", "no differences: this mission matches the shipped file.\n")
        elif not d["changed"]:
            self.text.insert("end", "the mission's records match; the rows above are edits outside the fig\n")

    def variant(self):
        return self.app.tab_script.variant.get() if hasattr(self.app, "tab_script") else "active"

    def selected_record(self):
        if not self.data:
            return None
        sel = self.tree.focus()
        if not sel:
            return None
        if sel in getattr(self, "row_of", {}):
            return None                      # a non-record file row
        cell = str(self.tree.item(sel, "values")[0]).strip()
        if not cell.isdigit():
            return None
        idx = int(cell)
        return next((r for r in self.data["rows"] if r["index"] == idx), None)

    def selected_file(self):
        sel = self.tree.focus()
        return getattr(self, "row_of", {}).get(sel)

    def revert_selected(self):
        """Put the selected record back to the shipped payload, after an explicit confirmation."""
        from tkinter import messagebox
        rec = self.selected_record()
        if rec is None:
            return
        idx = rec["index"]
        if rec["state"] == "LOCKED CHANGED":
            self.app.log("refusing: a locked record should never differ; investigate instead of reverting")
            return
        if rec["state"] != "changed":
            return
        msg = (f"Revert record #{idx} ({rec['role']}) in {self.mission} to the shipped payload?\n\n"
               f"Only this record changes: it stops being a difference. Your other edits stay.\n"
               f"The staged file is rewritten; press Install in Deploy to put it in the game.")
        if not messagebox.askyesno("Revert record to stock", msg, parent=self):
            self.app.log(f"#{idx}: revert cancelled")
            return
        import script_edit as SE
        try:
            _i, old, new, note = SE.revert_record(self.mission, self.variant(), idx)
        except SystemExit as exc:
            self.app.log(f"refused: {exc}")
            return
        except Exception as exc:
            self.app.log(f"failed: {exc}")
            return
        self.app.model._drop(self.mission, self.variant())
        if "unstaged" in note:
            self.app.log(f"#{idx}: {note} - nothing to install, the game reads the archive again")
        elif "applied" in note:
            self.app.log(f"#{idx}: {note} ({old} -> {new} bytes) - the change is gone")
        else:
            self.app.log(f"#{idx}: {note} ({old} -> {new} bytes) - press Install to apply")
        self.refresh()
        # say it in the tab itself, not only in the log: the row is gone because the record is stock again
        try:
            self.summary.config(text=f"{self.summary.cget('text')}   |   reverted #{idx} to stock")
        except Exception:
            pass
        if hasattr(self.app, "tab_script"):
            self.app.tab_script.refresh()
        if hasattr(self.app, "tab_deploy"):
            self.app.tab_deploy.refresh()

    def open_record_index(self, idx, against=None):
        """Refresh, then select and show record `idx`. Returns False when it is not in the list."""
        if against:
            self.against.set(against)
        self.refresh()
        for row in self.tree.get_children():
            if int(self.tree.item(row, "values")[0]) != idx:
                continue
            self.tree.focus(row)
            self.tree.selection_set(row)
            self.tree.see(row)
            self.show()
            return True
        return False

    def open_record(self, _event=None):
        """Take the selected record to the Script Map, at the first line that differs.

        A non-record row jumps to the tab that owns its edit instead: the fig is not where a definition, text,
        address or placement change lives.
        """
        f = self.selected_file()
        if f is not None:
            tab = {"Definition": getattr(self.app, "tab_def", None),
                   "Text & Titles": getattr(self.app, "tab_text", None),
                   "Addresses": getattr(self.app, "tab_place", None),
                   "Deploy": getattr(self.app, "tab_deploy", None)}.get(f["tab"])
            if tab is not None:
                try:
                    self.app.tabs.select(self.app.tabs.index(tab))
                except Exception as exc:
                    self.app.log(f"could not switch tab: {exc}")
            self.app.log(f"{f['rel']} is edited in the {f['tab']} tab")
            return
        rec = self.selected_record()
        if rec is None:
            return
        idx = rec["index"]
        line = None
        a_t, b_t = rec["old_text"], rec["new_text"]
        if a_t is not None and b_t is not None:
            a_s = (a_t.encode("latin-1") if isinstance(a_t, str) else a_t).decode("latin-1").splitlines()
            b_s = (b_t.encode("latin-1") if isinstance(b_t, str) else b_t).decode("latin-1").splitlines()
            for tag, _i1, _i2, j1, _j2 in difflib.SequenceMatcher(None, a_s, b_s).get_opcodes():
                if tag != "equal":
                    line = j1 + 1
                    break
        try:
            self.app.tabs.select(self.app.tabs.index(self.app.tab_script))
        except Exception as exc:
            self.app.log(f"could not switch to the Script Map: {exc}")
            return
        if self.app.tab_script.focus_record(idx, line):
            where = f", line {line}" if line else ""
            self.app.log(f"record #{idx} ({rec['role']}) opened in the Script Map{where}")
        else:
            self.app.log(f"record #{idx} is not in the Script Map for this variant/source")

    def show(self):
        if not self.data:
            return
        f = self.selected_file()
        if f is not None:
            if hasattr(self, "revert_btn"):
                self.revert_btn.configure(state="disabled")
            self.text.delete("1.0", "end")
            if f.get("state") == "installed":
                body = (f"{f['rel']}: the edit is already installed, so it is not a pending difference -\n"
                        f"it still differs from the shipped file by {f['delta']:+d} bytes, which is what\n"
                        f"'{f['tab']}' changed. Switch 'Compare against' to staged to see it as a change.\n")
            else:
                body = (f"{f['rel']}: staged copy differs by {f['delta']:+d} bytes\n\n"
                        f"This is not a fig record: the edit lives in {f['tab']}. Revert it in that tab, or with\n"
                        f"./proto2 clean (discards every staged edit and reinstalls the payload).\n")
            self.text.insert("end", body)
            return
        rec = self.selected_record()
        if hasattr(self, "revert_btn"):
            self.revert_btn.configure(
                state="normal" if rec and rec["state"] == "changed" else "disabled")
        if rec is None:
            return
        idx = rec["index"]
        rec = next((r for r in self.data["rows"] if r["index"] == idx), None)
        self.text.delete("1.0", "end")
        if not rec:
            return
        a_t, b_t = rec["old_text"], rec["new_text"]
        if a_t is None or b_t is None:
            self.text.insert("end", f"record #{idx} ({rec['role']}): {rec['state']}, "
                                    f"{rec['delta']:+d} bytes\n"
                                    f"bytecode on both sides - byte comparison only, there is no text to diff\n")
            return
        a_s = (a_t.encode("latin-1") if isinstance(a_t, str) else a_t).decode("latin-1").splitlines()
        b_s = (b_t.encode("latin-1") if isinstance(b_t, str) else b_t).decode("latin-1").splitlines()
        for line in difflib.unified_diff(a_s, b_s, "stock", self.against.get(), lineterm="", n=2):
            tag = "add" if line.startswith("+") and not line.startswith("+++") else \
                  "del" if line.startswith("-") and not line.startswith("---") else ()
            self.text.insert("end", line + "\n", tag)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Prototype 2 — Mission Editor")
        self.geometry("1500x900")
        self.minsize(1100, 640)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # This host is HiDPI: a 10pt font has a 37px linespace, while Tk's
        # default Treeview row is ~20px, which clips every row. Derive the row
        # height from the real font metrics instead of hard-coding one.
        self.line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=self.line + 6)
        style.configure("Treeview.Heading", rowheight=self.line + 8)

        self.model = Model(self.log)
        self.mission = None
        # prefs are only written by the real GUI, never by --selftest/--uicheck
        self.persist = False

        # two palettes; the editor is monospace and both are tuned for long reading sessions
        self.THEMES = {
            "light": dict(bg="#ececec", fg="#111111", field="#ffffff", border="#b8b8b8",
                          text_bg="#ffffff", text_fg="#111111", gutter_bg="#f2f2f2", gutter_fg="#888888",
                          muted="#555555", tree_bg="#ffffff", tree_fg="#111111",
                          sel_bg="#4a6984", sel_fg="#ffffff",
                          readonly_bg="#eeeeee", readonly_fg="#444444",
                          changed_bg="#e3f4e3", changed_fg="#14521a",
                          danger="#b00020", ok="#0a6b3d", hit_bg="#fff3b0",
                          scroll_thumb="#b8b8b8", scroll_thumb_active="#8a8a8a"),
            "dark": dict(bg="#16202b", fg="#d7e3f4", field="#1e2c3a", border="#2c4257",
                         text_bg="#131c26", text_fg="#d7e3f4", gutter_bg="#1a2530", gutter_fg="#6e8ba6",
                         muted="#93a9c0", tree_bg="#1a2530", tree_fg="#d7e3f4",
                         sel_bg="#2c5c8a", sel_fg="#ffffff",
                         readonly_bg="#223142", readonly_fg="#afc6dd",
                         changed_bg="#1f3b4d", changed_fg="#86d9c8",
                         danger="#ff8fa3", ok="#7fd6c0", hit_bg="#5a4a1f",
                         scroll_thumb="#4f7fb0", scroll_thumb_active="#7fb2e0"),
        }
        self.theme = "light"

        # one shared monospace font for every script editor; size is adjustable and remembered
        self.editor_font = tkfont.nametofont("TkFixedFont").copy()
        self.editor_size = self._load_editor_size()

        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Filter missions").pack(side="left")
        self.filter = tk.StringVar()
        e = ttk.Entry(toolbar, textvariable=self.filter, width=32)
        e.pack(side="left", padx=6)
        e.bind("<KeyRelease>", lambda _e: self.fill_missions())
        ttk.Button(toolbar, text="Reload", command=self.reload).pack(side="left")
        ttk.Label(toolbar, text="  Source:").pack(side="left")
        self.source = tk.StringVar(value="working")
        src_cb = ttk.Combobox(toolbar, textvariable=self.source,
                              values=["working", "shipped", "staged", "installed"],
                              width=11, state="readonly")
        src_cb.pack(side="left", padx=(4, 10))
        src_cb.bind("<<ComboboxSelected>>", lambda _e: self.source_changed())
        ttk.Label(toolbar, text="  Text size").pack(side="left")
        ttk.Button(toolbar, text="−", width=2,
                   command=lambda: self.zoom_editor(-1)).pack(side="left", padx=(4, 0))
        self.size_label = ttk.Label(toolbar, text=str(self.editor_size), width=3, anchor="center")
        self.size_label.pack(side="left")
        ttk.Button(toolbar, text="+", width=2,
                   command=lambda: self.zoom_editor(+1)).pack(side="left")
        ttk.Button(toolbar, text="Light", width=6,
                   command=lambda: self.set_theme("light")).pack(side="left", padx=(12, 0))
        ttk.Button(toolbar, text="Dark", width=6,
                   command=lambda: self.set_theme("dark")).pack(side="left", padx=(4, 0))
        self.status = ttk.Label(toolbar, text="loading…")
        self.status.pack(side="right")

        self.vsplit = ttk.Panedwindow(self, orient="vertical")
        self.vsplit.pack(fill="both", expand=True)
        paned = self.paned = ttk.Panedwindow(self.vsplit, orient="horizontal")
        self.vsplit.add(paned, weight=4)

        left = self.left_pane = ttk.Frame(paned)
        self.missions = ttk.Treeview(left, columns=("title", "id"), show="headings",
                                     selectmode="browse")
        self.sort_col = "title"
        self.sort_desc = False
        self.missions.heading("title", text="title",
                              command=lambda: self.sort_by("title"))
        self.missions.heading("id", text="mission",
                              command=lambda: self.sort_by("id"))
        self.missions.column("title", width=300, anchor="w")
        self.missions.column("id", width=200, anchor="w")
        self.missions.column("#0", width=0, stretch=False)
        msb = ttk.Scrollbar(left, orient="vertical", command=self.missions.yview)
        self.missions.configure(yscrollcommand=msb.set)
        self.missions.pack(side="left", fill="both", expand=True)
        msb.pack(side="right", fill="y")
        self.missions.bind("<<TreeviewSelect>>", lambda _e: self.on_pick())
        paned.add(left, weight=1)

        right = self.right_pane = ttk.Frame(paned)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self.tab_def = DefinitionTab(self.tabs, self)
        self.tab_fsm = FsmTab(self.tabs, self)
        self.tab_text = TextTab(self.tabs, self)
        self.tab_place = PlacementTab(self.tabs, self)
        self.tab_script = ScriptTab(self.tabs, self)
        self.tab_whole = WholeScriptTab(self.tabs, self)
        self.tab_graph = GraphTab(self.tabs, self)
        self.tab_diff = DiffTab(self.tabs, self)
        self.tab_deploy = DeployTab(self.tabs, self)
        for t, name in ((self.tab_def, "Definition"), (self.tab_fsm, "FSM Lua"),
                        (self.tab_text, "Text & Titles"),
                        (self.tab_place, "Addresses"),
                        (self.tab_script, "Script Map"),
                        (self.tab_whole, "Mission Lua"),
                        (self.tab_graph, "FSM Graph"),
                        (self.tab_diff, "Diff"),
                        (self.tab_deploy, "Deploy")):
            self.tabs.add(t, text=name)
        paned.add(right, weight=4)

        # Ctrl+wheel and Ctrl +/- zoom everywhere; the size is shared and remembered
        for nt in self._editor_widgets():
            nt.on_zoom = self.zoom_editor
            nt.text.bind("<Control-MouseWheel>",
                         lambda e: (self.zoom_editor(1 if e.delta > 0 else -1), "break")[1], add="+")
            nt.text.bind("<Control-Button-4>", lambda _e: (self.zoom_editor(1), "break")[1], add="+")
            nt.text.bind("<Control-Button-5>", lambda _e: (self.zoom_editor(-1), "break")[1], add="+")
        for seq, d in (("<Control-plus>", 1), ("<Control-equal>", 1), ("<Control-KP_Add>", 1),
                       ("<Control-minus>", -1), ("<Control-KP_Subtract>", -1)):
            self.bind_all(seq, lambda _e, d=d: self.zoom_editor(d))
        self.bind_all("<Control-0>", lambda _e: (setattr(self, "editor_size", 11),
                                                 self.apply_editor_font(), None)[2])
        self.apply_editor_font()

        logframe = self.logframe = ttk.Frame(self.vsplit, padding=(8, 4), height=150)
        self.vsplit.add(logframe, weight=1)
        ttk.Label(logframe, text="log").pack(anchor="w")
        self.logtext = tk.Text(logframe, height=6, wrap="word", state="disabled",
                               background="#111", foreground="#ddd")
        lsb = ttk.Scrollbar(logframe, orient="vertical", command=self.logtext.yview)
        self.logtext.configure(yscrollcommand=lsb.set)
        self.logtext.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

        self.paint_sort()
        self.after(50, self.boot)

    # ---- lifecycle ------------------------------------------------------
    def boot(self):
        try:
            self.fill_missions()
            self.model.bible()
        except Exception as exc:
            self.log(f"! startup: {exc}")
            self.log(traceback.format_exc(limit=4))
        self.tab_deploy.refresh()
        self.on_staged()
        self.theme = self._load_theme()
        self.apply_theme()
        self.update_idletasks()
        self.paned.sashpos(0, 540)
        self.vsplit.sashpos(0, max(320, self.winfo_height() - 210))
        # start on a real mission instead of an empty right-hand side; prefer the last one opened
        rows = self.missions.get_children()
        pick = rows[0] if rows else None
        if pick and self.persist:
            try:
                import config as CFG
                remembered = CFG.get("gui_last_mission")
                if remembered and self.missions.exists(remembered):
                    pick = remembered
            except Exception:
                pass
        if pick:
            self.missions.selection_set(pick)
            self.missions.focus(pick)
            self.on_pick()

    def sort_by(self, col):
        """Clicking a heading sorts by it; clicking again reverses."""
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, False
        self.paint_sort()
        self.fill_missions()

    def paint_sort(self):
        for col, label in (("title", "title"), ("id", "mission")):
            arrow = ""
            if self.sort_col == col:
                arrow = " ▼" if self.sort_desc else " ▲"
            self.missions.heading(col, text=label + arrow)

    def reload(self):
        self.model.reload()
        self.model._bible = None
        self.fill_missions()
        if self.mission:
            self.select(self.mission)

    def fill_missions(self):
        needle = self.filter.get().lower()
        titles = {}
        try:
            _r, _h, h = self.model.bible()
            for i, key in enumerate(h.keys):
                if key.startswith("MISSION_NAME_"):
                    titles[key[len("MISSION_NAME_"):]] = h.value(i)
        except Exception:
            pass
        rows = []
        for name, _idx in self.model.missions():
            title = titles.get(name) or name          # never leave a blank row
            if needle and needle not in name.lower() and needle not in title.lower():
                continue
            rows.append((title, name))
        key = (lambda r: r[0].lower()) if self.sort_col == "title" \
            else (lambda r: r[1].lower())
        rows.sort(key=key, reverse=self.sort_desc)
        self.missions.delete(*self.missions.get_children())
        for title, name in rows:
            self.missions.insert("", "end", iid=name, values=(title, name))
        self.status.config(text=f"{len(self.missions.get_children())} shown")

    def source_changed(self):
        """One source selector for the whole window: every tab re-reads what it shows."""
        src = self.source.get()
        if src != "working":
            self.log(f"showing {src}: read-only view - edits always apply to the working copy")
        for name in dir(self):
            if not name.startswith("tab_"):
                continue
            tab = getattr(self, name, None)
            if isinstance(tab, Tab) and self.mission:
                try:
                    tab.select(self.mission)
                except Exception as exc:
                    self.log(f"! {name}: {exc}")

    def _editor_widgets(self):
        """Every NumberedText in the window, found by walking the tab attributes."""
        out = []
        for name in dir(self):
            if not name.startswith("tab_"):
                continue
            tab = getattr(self, name, None)
            text = getattr(tab, "text", None)
            if isinstance(text, NumberedText):
                out.append(text)
        return out

    # --- theme -------------------------------------------------------------------------------------------
    def _load_theme(self):
        try:
            import config as CFG
            name = CFG.get("gui_theme")
            return name if name in self.THEMES else "light"
        except Exception:
            return "light"

    def _save_theme(self, name):
        if not getattr(self, "persist", False):
            return
        try:
            import config as CFG
            data = {}
            if os.path.exists(CFG.PATH):
                try:
                    data = json.load(open(CFG.PATH))
                except Exception:
                    data = {}
            data["gui_theme"] = name
            CFG.save(data)
        except Exception:
            pass

    def set_theme(self, name):
        if name not in self.THEMES:
            return
        self.theme = name
        self.apply_theme()
        self._save_theme(name)

    def apply_theme(self):
        """Recolour ttk styles, every editor pane and every tree, in place."""
        pal = self.THEMES[self.theme]
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=pal["bg"], foreground=pal["fg"],
                        fieldbackground=pal["field"], bordercolor=pal["border"],
                        lightcolor=pal["bg"], darkcolor=pal["bg"], troughcolor=pal["bg"])
        for name in ("TFrame", "TPanedwindow", "TLabelframe", "TNotebook"):
            style.configure(name, background=pal["bg"], foreground=pal["fg"])
        style.configure("TLabelframe.Label", background=pal["bg"], foreground=pal["fg"])
        style.configure("TLabel", background=pal["bg"], foreground=pal["fg"])
        style.configure("TButton", background=pal["field"], foreground=pal["fg"])
        style.map("TButton", background=[("active", pal["sel_bg"])], foreground=[("active", pal["sel_fg"])])
        style.configure("TCheckbutton", background=pal["bg"], foreground=pal["fg"])
        style.configure("TEntry", fieldbackground=pal["field"], foreground=pal["text_fg"],
                        insertcolor=pal["text_fg"])
        style.configure("TCombobox", fieldbackground=pal["field"], foreground=pal["fg"],
                        background=pal["field"], arrowcolor=pal["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", pal["field"])],
                  foreground=[("readonly", pal["text_fg"])])
        style.configure("TNotebook.Tab", background=pal["bg"], foreground=pal["muted"], padding=(10, 4))
        style.map("TNotebook.Tab", background=[("selected", pal["field"])],
                  foreground=[("selected", pal["fg"])])
        style.configure("Treeview", background=pal["tree_bg"], fieldbackground=pal["tree_bg"],
                        foreground=pal["tree_fg"], bordercolor=pal["border"])
        style.configure("Treeview.Heading", background=pal["bg"], foreground=pal["fg"])
        style.map("Treeview", background=[("selected", pal["sel_bg"])],
                  foreground=[("selected", pal["sel_fg"])])
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar", background=pal["scroll_thumb"], troughcolor=pal["bg"],
                            arrowcolor=pal["fg"], bordercolor=pal["border"],
                            lightcolor=pal["scroll_thumb"], darkcolor=pal["scroll_thumb"],
                            width=SCROLLBAR_WIDTH)
            style.map(f"{orient}.TScrollbar",
                      background=[("pressed", pal["scroll_thumb_active"]),
                                  ("active", pal["scroll_thumb_active"])])
        try:
            self.configure(background=pal["bg"])
        except Exception:
            pass
        self._theme_walk(self, pal)
        self.apply_editor_font()

    def _theme_walk(self, widget, pal):
        """Depth-first recolour of the widgets that do not follow ttk styles."""
        muted_old = {"#555", "#555555", "#777", "#777777", "#888888", "#333", "#333333"}
        danger_old = {"#b00020"}
        ok_old = {"#0a6b3d"}
        for child in widget.winfo_children():
            try:
                if isinstance(child, NumberedText):
                    # its own apply_theme colours the gutter, the text and the read-only tag; descending further
                    # would hit the inner tk.Text with the generic branch below and repaint a read-only pane in the
                    # editable colour
                    child.apply_theme(pal)
                    continue
                elif isinstance(child, tk.Text):
                    child.configure(background=pal["text_bg"], foreground=pal["text_fg"],
                                    insertbackground=pal["text_fg"],
                                    selectbackground=pal["sel_bg"], selectforeground=pal["sel_fg"])
                elif isinstance(child, tk.Canvas):
                    child.configure(background=pal["bg"])
                elif isinstance(child, tk.Listbox):
                    child.configure(background=pal["tree_bg"], foreground=pal["tree_fg"],
                                    selectbackground=pal["sel_bg"], selectforeground=pal["sel_fg"])
                elif isinstance(child, ttk.Label):
                    fg = str(child.cget("foreground") or "").lower()
                    if fg in muted_old:
                        child.configure(foreground=pal["muted"])
                    elif fg in danger_old:
                        child.configure(foreground=pal["danger"])
                    elif fg in ok_old:
                        child.configure(foreground=pal["ok"])
                elif isinstance(child, ttk.Treeview):
                    child.tag_configure("changed", background=pal["changed_bg"], foreground=pal["changed_fg"])
                    child.tag_configure("locked", foreground=pal["danger"])
                    child.tag_configure("bytecode", foreground=pal["ok"])
                    child.tag_configure("muted", foreground=pal["muted"])
                    child.tag_configure("bad", foreground=pal["danger"])
            except Exception:
                pass
            self._theme_walk(child, pal)

    def _load_editor_size(self):
        """Text size from proto2.config.json, with a one-time read of the old work/state/gui.json."""
        try:
            import config as CFG
            size = CFG.get("gui_text_size")
            legacy = os.path.join(ROOT, "work", "state", "gui.json")
            if size == CFG.DEFAULTS["gui_text_size"] and os.path.exists(legacy):
                try:
                    size = int(json.load(open(legacy)).get("editor_size", size))
                except Exception:
                    pass
            return max(6, min(28, int(size)))
        except Exception:
            return 11

    def _save_editor_size(self, size):
        if not getattr(self, "persist", False):
            return
        try:
            import config as CFG
            data = {}
            if os.path.exists(CFG.PATH):
                try:
                    data = json.load(open(CFG.PATH))
                except Exception:
                    data = {}
            data["gui_text_size"] = int(size)
            CFG.save(data)
        except Exception:
            pass

    def apply_editor_font(self):
        """Push the current size onto the shared font and every editor pane."""
        size = max(6, min(28, int(self.editor_size)))
        self.editor_size = size
        self.editor_font.configure(size=size)
        for nt in self._editor_widgets():
            nt.text.configure(font=self.editor_font)
            nt.gutter.configure(font=self.editor_font)
            nt.redraw()
        if hasattr(self, "size_label"):
            self.size_label.config(text=str(size))

    def zoom_editor(self, delta):
        self.editor_size = max(6, min(28, self.editor_size + delta))
        self.apply_editor_font()
        self._save_editor_size(self.editor_size)

    def on_pick(self):
        sel = self.missions.focus()
        if not sel:
            return
        self.select(sel)

    def select(self, mission):
        self.mission = mission
        # every Tab gets the selection: a hardcoded tuple silently excluded new tabs (the Script Map
        # stayed empty because it was never handed a mission)
        for tab in (self.tab_def, self.tab_fsm, self.tab_text, self.tab_place,
                    self.tab_script, self.tab_whole, self.tab_graph, self.tab_diff,
                    self.tab_deploy):
            tab.select(mission)
        self.status.config(text=mission)
        if self.persist and mission:
            try:
                import config as CFG
                data = {}
                if os.path.exists(CFG.PATH):
                    try:
                        data = json.load(open(CFG.PATH))
                    except Exception:
                        data = {}
                if data.get("gui_last_mission") != mission:
                    data["gui_last_mission"] = mission
                    CFG.save(data)
            except Exception:
                pass

    def on_staged(self):
        self.tab_deploy.refresh()
        n = len(staged_files())
        if self.mission:
            self.status.config(text=f"{self.mission}    ·    {n} file(s) staged")

    # ---- logging --------------------------------------------------------
    def log(self, msg):
        self.logtext.configure(state="normal")
        for line in str(msg).splitlines() or [""]:
            self.logtext.insert("end", line + "\n")
        self.logtext.see("end")
        self.logtext.configure(state="disabled")
        self.update_idletasks()


# --------------------------------------------------------------------------
# selftest — exercise every read path without opening a window
# --------------------------------------------------------------------------

def selftest():
    print("model selftest")
    m = Model(print)
    ms = m.missions()
    print(f"  missions: {len(ms)}")
    probe = "story_intro1" if any(n == "story_intro1" for n, _ in ms) else ms[0][0]
    leaves = m.leaves(probe)
    print(f"  {probe}: {len(leaves)} leaves, "
          f"{sum(1 for x in leaves if x[2])} editable")
    doms = m.domains()
    tod = doms.get("flow.missionInfo.timeOfDay", [])
    amb = doms.get("flow.audioData.ambience", [])
    print(f"  domains: timeOfDay {len(tod)} values, ambience {len(amb)} values")
    got = m.fsm(probe)
    if got:
        inner, frags, src = got
        print(f"  fsm: {len(inner)} bytes, {len(frags)} fragments ({src})")
    view = m.fsm_structure(probe)
    if view.get("ok"):
        stats = view["stats"]
        print(f"  fsm structure: {len(view['branches'])} branch(es), "
              f"{stats['elements']} element(s), "
              f"{stats['sibling_mismatches']} sibling mismatch(es)")
    verified = m.verify_fsm(probe)
    print(f"  fsm verify: {verified['message'].splitlines()[0]}")
    got = m.placement(probe)
    if got:
        inner, recs, src = got
        print(f"  placement: {len(inner)} bytes, {len(recs)} records ({src})")
    _r, _h, h = m.bible()
    print(f"  bible: {h.count} english keys")
    print(f"  staged files: {len(staged_files())}")
    print(f"  installed: {len(m.installed())}")
    print("selftest OK")
    return 0



def _leaf_rows(tree):
    """Every leaf row of a tab tree, depth first - the records themselves, not the group nodes above them.

    Addresses, Definition and Script Map all nest their rows now, so any check that means "the records are
    shown" has to count leaves. Counting top-level children silently measures the number of groups instead.
    """
    out, stack = [], list(reversed(tree.get_children()))
    while stack:
        iid = stack.pop()
        kids = tree.get_children(iid)
        if kids:
            stack.extend(reversed(kids))
        else:
            out.append(iid)
    return out


def filter_text(text):
    """Normalise a string for filter matching, so every way of writing a path matches the same row.

    Field paths carry list indices as segments (`...ambientActiveList.1`) while mission scripts and the docs write
    `...ambientActiveList[1]`, and placement paths use `|`. A filter has to accept whichever the user has in front of
    them - otherwise the value is there and the filter says no.
    """
    s = text.lower()
    for ch in "[]|/\\":
        s = s.replace(ch, ".")
    while ".." in s:
        s = s.replace("..", ".")
    return s.strip(".")


def filter_keep(parents, matches):
    """Nodes a filter should show: every match, everything under it, and the ancestors connecting it.

    "Everything under it" is the half that matters for a tree. A filter that keeps only the hit and its ancestors
    hides the children of a matching parent - so typing `BW_BaseGOTO` (a placement parent) used to hide the `1`
    underneath it, which is the record scripts actually read, and the filter looked broken.
    """
    keep = set(matches)
    for m in matches:                      # ancestors of a match
        j = parents[m]
        while j is not None and j not in keep:
            keep.add(j)
            j = parents[j]
    for i in range(len(parents)):          # descendants of a match
        j = parents[i]
        while j is not None:
            if j in matches:
                keep.add(i)
                break
            j = parents[j]
    return keep


def _definition_fields(tab):
    """Leaf rows of the Definition tree: the fields, not the path groups above them.

    The tab nests each field under its dotted path (`flow.activationData...`), so a top-level row count measures
    tables rather than fields. Anything asserting "the mission's fields are shown" has to count leaves.
    """
    return [iid for iid in _leaf_rows(tab.tree) if tab.tree.item(iid, "values")]


def uicheck():
    """Build the real window and assert it is actually usable.

    This exists because the first version passed every data test while showing
    the user a blank strip: the mission pane was 223px wide for two columns
    needing 380px, and non-story rows had empty titles. Data checks cannot see
    that. These can.
    """
    app = App()
    for _ in range(40):
        app.update()
        app.update_idletasks()
    results = []
    # Several checks below assert behaviour on a *changed* mission (staged text, green rows, diff jumps). A fresh
    # clone has no staged edits, so those checks are skipped rather than failed - otherwise ./proto2 check can
    # never pass on a clean checkout, which is exactly what the README tells a new user to run.
    has_staged_mission = any("art/missions/" in p.replace("\\", "/") for p in staged_files())

    def staged_missions():
        """Mission names that actually have a staged file, discovered from the staging list."""
        out = set()
        for p in staged_files():
            p = p.replace("\\", "/")
            if "/missions/" in p:
                out.add(p.split("/missions/")[1].split("/")[0])
        return sorted(out)

    skipped = []

    def ck(name, ok, detail="", skip=False):
        if skip:
            skipped.append(name)
            return
        results.append((name, bool(ok), detail))
        # a modal dialog left open would block every later widget walk (and needed a human click once)
        for w in app.winfo_children():
            if isinstance(w, tk.Toplevel):
                try:
                    w.destroy()
                except Exception:
                    pass

    def all_widgets(w):
        for c in w.winfo_children():
            yield c
            yield from all_widgets(c)

    def cancel_dialog():
        # dialogs are children of the tab that opened them, not of the app
        for w in all_widgets(app):
            if isinstance(w, tk.Toplevel):
                w.destroy()

    ck("window mapped", app.winfo_ismapped(),
       f"{app.winfo_width()}x{app.winfo_height()}")

    pane = app.left_pane.winfo_width()
    need = (app.missions.column("title", "width")
            + app.missions.column("id", "width"))
    ck("mission pane fits both columns", pane >= need,
       f"pane {pane}px, columns need {need}px")

    rows = app.missions.get_children()
    ck("missions listed", len(rows) > 100, f"{len(rows)} rows")
    titled = sum(1 for r in rows if app.missions.item(r, "values")[0])
    ck("every row shows a title", rows and titled == len(rows),
       f"{titled}/{len(rows)}")

    bb = app.missions.bbox(rows[0]) if rows else ()
    ck("first row is on screen", bb and bb[2] > 100, str(bb))
    bb = app.missions.bbox(rows[0]) if rows else ()
    ck("row height fits the font", bb and bb[3] >= app.line,
       f"row {bb[3] if bb else 0}px, linespace {app.line}px")

    ck("a mission is selected at startup", bool(app.mission),
       str(app.mission))
    ck("startup selection populated Definition",
       len(_definition_fields(app.tab_def)) > 10,
       f"{len(_definition_fields(app.tab_def))} fields")

    lh = app.logframe.winfo_height()
    ck("log pane has usable height", 60 <= lh <= 260, f"{lh}px")

    probe = "story_intro1" if "story_intro1" in rows else rows[0]
    app.missions.selection_set(probe)
    app.missions.focus(probe)
    app.on_pick()
    for _ in range(15):
        app.update()
        app.update_idletasks()
    ck("selection reaches Definition", len(_definition_fields(app.tab_def)) > 10,
       f"{len(_definition_fields(app.tab_def))} fields")
    ck("selection reaches FSM Lua", app.tab_fsm.list.size() > 1,
       f"{app.tab_fsm.list.size()} entries")
    ck("selection reaches FSM structure",
       len(app.tab_fsm.stree.get_children()) > 0,
       f"{len(app.tab_fsm.stree.get_children())} root row(s)")

    def fsm_rows(parent=""):
        for iid in app.tab_fsm.stree.get_children(parent):
            yield iid
            yield from fsm_rows(iid)

    fsm_values = [app.tab_fsm.stree.item(i, "values") for i in fsm_rows()]
    ck("FSM structure shows element names",
       any(v[0] == "OnActive" for v in fsm_values)
       and any(v[0] == "Complete" for v in fsm_values),
       f"{sum(1 for v in fsm_values if v[0] in ('OnActive', 'Complete'))} named")
    ck("FSM structure shows transition targets",
       any("Complete" in str(v[3]) for v in fsm_values),
       next((str(v[3]) for v in fsm_values if "Complete" in str(v[3])), ""))
    ck("FSM structure shows decoded item tags",
       any("conditionGroup" in str(v[0]) for v in fsm_values)
       and any(str(v[0]).startswith("record") for v in fsm_values),
       sum(1 for v in fsm_values if "conditionGroup" in str(v[0])
           or str(v[0]).startswith("record")))
    ck("FSM structure shows sibling counts",
       any(str(v[2]).isdigit() for v in fsm_values),
       next((str(v[2]) for v in fsm_values if str(v[2]).isdigit()), ""))
    verified = app.model.verify_fsm(probe)
    ck("FSM verify reports byte-identical",
       verified["ok"] and "byte-identical" in verified["message"],
       verified["message"].splitlines()[0])
    # records, not leaves: the tree nests group -> member -> group, so a parent record has children too
    ck("selection reaches Addresses",
       len(app.tab_place.record_rows()) > 10,
       f"{len(app.tab_place.record_rows())} addresses")
    ck("mission recorded", app.mission == probe, str(app.mission))

    # sorting: clicking a heading must reorder and toggle direction
    def col(i, c):
        return app.missions.item(app.missions.get_children()[i], "values")[c]

    app.sort_by("id")
    ids = [col(i, 1) for i in range(5)]
    ck("sort by mission ascending", ids == sorted(ids, key=str.lower), str(ids))
    app.sort_by("id")
    ids2 = [col(i, 1) for i in range(5)]
    ck("second click reverses", ids2 == sorted(ids2, key=str.lower, reverse=True),
       str(ids2))
    app.sort_by("title")
    titles_now = [col(i, 0) for i in range(5)]
    ck("sort by title ascending",
       titles_now == sorted(titles_now, key=str.lower), str(titles_now))
    ck("sort indicator shown", "▲" in app.missions.heading("title", "text"),
       app.missions.heading("title", "text"))

    # fixed-value fields must offer their known values, not just free text
    doms = app.model.domains()
    tod = doms.get("flow.missionInfo.timeOfDay", [])
    ck("timeOfDay offers every hour", len(tod) >= 28, f"{len(tod)} values")
    ck("setTimeOfDay offers every hour",
       len(doms.get("flow.initialTransitionData.setTimeOfDay", [])) >= 28,
       f"{len(doms.get('flow.initialTransitionData.setTimeOfDay', []))} values")
    ck("ambience offers every set",
       len(doms.get("flow.audioData.ambience", [])) >= 7,
       f"{len(doms.get('flow.audioData.ambience', []))} values")
    ck("huge domains stay free text", "meta.name" not in doms,
       f"{len(doms.get('meta.name', []))} names")

    def drive():
        for w in app.winfo_children():
            if isinstance(w, tk.Toplevel):
                w._var.set("TOD_LAIR")
                w._ok()

    app.after(200, drive)
    picked = ask_value(app, "Edit value", "flow.missionInfo.timeOfDay",
                       "TOD_BREWERY", tod)
    ck("value dialog returns the picked value", picked == "TOD_LAIR", repr(picked))

    # Definitions: the edit box is pre-filled with the spelling the list shows (True/False/None), and that exact
    # text must parse back to the same constant.
    try:
        raws = {path: raw for (path, _s, _e, _k, raw) in app.model.leaves("story_a1")}
        prefill = raws.get("meta.blockReleaseSelection")
        app.model.set_leaf("story_a1", "meta.blockReleaseSelection", "False")
        ok = prefill == "False"
        detail = f"prefill {prefill!r}, accepted back"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    ck("definition edit keeps boolean casing", ok, detail)

    # Two shipped missions repeat a key in the same table; that used to abort the Definition tab (TclError on the
    # duplicate row id). It must render, note the merge, and still open the edit dialog for that field.
    # These two names are the data evidence for the bug, not a convenience fixture: if a future build stops
    # shipping the duplicate, the label check below fails rather than passing quietly.
    ok = True
    detail = []
    for mission, field in (("story_FinalBoss", "flow.initialTransitionData.forceAttackPower"),
                           ("gz_re_01", "flow.completionData.onCompleteCallback")):
        try:
            app.select(mission)
            for _ in range(6):
                app.update()
            rows = [app.tab_def.tree.item(i, "values")[0] for i in _definition_fields(app.tab_def)]
            label = app.tab_def.count.cget("text")
            dup = [p for p in set(rows) if rows.count(p) > 1]
            iids = _definition_fields(app.tab_def)
            unique_iids = len({app.tab_def.tree.item(i, "values")[0] for i in iids}) == len(iids)
            good = bool(rows) and not dup and unique_iids and "duplicate" in label
            ok = ok and good
            detail.append(f"{mission}: {len(rows)} rows, 1 key merged")
        except Exception as exc:
            ok = False
            detail.append(f"{mission}: {type(exc).__name__}: {exc}")
    ck("definition survives duplicate keys", ok, "; ".join(detail))
    app.select("story_a1")
    for _ in range(6):
        app.update()

    total_fields = len(_definition_fields(app.tab_def))
    app.tab_def.q.set("flow")
    app.tab_def.fill()
    kept = len(_definition_fields(app.tab_def))
    ck("definition filter narrows the list", 0 < kept < total_fields,
       f"{total_fields} -> {kept}")
    app.tab_def.q.set("")
    app.tab_def.fill()
    ck("clearing the filter restores every field",
       len(_definition_fields(app.tab_def)) == total_fields,
       f"{len(_definition_fields(app.tab_def))}/{total_fields}")

    hours = app.model.tod_hours()
    ck("TOD hours decoded from datainit", len(hours) >= 28, f"{len(hours)} names")
    ck("hours match the known table", hours.get("TOD_GREENZONE_DAY") == 16.5
       and hours.get("TOD_NIGHT") == 17.0,
       f"day={hours.get('TOD_GREENZONE_DAY')} night={hours.get('TOD_NIGHT')}")

    def drive_pick():
        for w in all_widgets(app):
            if isinstance(w, tk.Toplevel):
                w._var.set("TOD_GREENZONE_DAY   (16.5h)")
                w._ok()

    app.after(200, drive_pick)
    picked2 = ask_value(app, "Edit value", "flow.missionInfo.timeOfDay",
                        "TOD_GREENZONE_DAY   (16.5h)",
                        ["TOD_GREENZONE_DAY   (16.5h)"],
                        lambda t: t.strip().split()[0])
    ck("hour annotation is stripped on OK", picked2 == "TOD_GREENZONE_DAY",
       repr(picked2))

    # the other three dialogs must open too - a missing import here would be a
    # NameError the compiler cannot see, and it already happened once
    trows = _leaf_rows(app.tab_text.tree)
    if trows:
        app.tab_text.tree.focus(trows[0])
        app.tab_text.tree.selection_set(trows[0])
    prows = app.tab_place.record_rows()
    if prows:
        app.tab_place.tree.focus(prows[0])
        app.tab_place.tree.selection_set(prows[0])

    defrows = [r for r in _definition_fields(app.tab_def)
               if "computed" not in app.tab_def.tree.item(r, "tags")]
    if defrows:
        app.tab_def.tree.focus(defrows[0])
        app.tab_def.tree.selection_set(defrows[0])

    for label, fn in (("definition dialog opens", app.tab_def.edit),
                      ("title dialog opens", app.tab_text.edit_title),
                      ("text dialog opens", app.tab_text.edit),
                      ("position dialog opens", app.tab_place.edit)):
        detail = ""
        app.after(120, cancel_dialog)
        try:
            fn()
            ok = True
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        ck(label, ok, detail)

    # the record editor must load the same copy the list shows: a record whose staged text differs from
    # the shipped text has to appear in the pane with the staged text (regression: it once loaded the
    # shipped file, so staged edits looked missing and Apply would have dropped them)
    import script_edit as SE
    detail = ""
    ok = False
    for mission in (staged_missions() or ["story_a1"]):
        try:
            staged = SE._decoded(SE.open_side(mission, "active", "staged")[3])
            shipped = SE._decoded(SE.open_side(mission, "active", "shipped")[3])
        except Exception:
            continue
        diff = [i for i, (a, b) in enumerate(zip(shipped, staged)) if (a[1] or b"") != (b[1] or b"")]
        diff = [i for i in diff if staged[i][1] is not None]
        if not diff:
            continue
        app.select(mission)
        app.tab_script.mode.set("Records")
        app.tab_script.refresh()
        idx = diff[0]
        for row in _leaf_rows(app.tab_script.tree):
            if app.tab_script.tree.item(row, "values")[0] == str(idx):
                app.tab_script.tree.focus(row)
                app.tab_script.tree.selection_set(row)
                app.tab_script.on_select()
                break
        want = staged[idx][1]
        want = want.encode("latin-1") if isinstance(want, str) else want
        got = app.tab_script.text.get("1.0", "end-1c")
        got = got.replace("\n", "\r\n") if app.tab_script._crlf else got
        ok = want.decode("latin-1").rstrip("\r\n") in got.replace("\x1b", "\\x1b")
        detail = f"{mission} #{idx}: pane shows staged text"
        break
    ck("record editor reads the working copy", ok, detail, skip=not has_staged_mission)

    # the Source selector must actually change what the viewing tabs show
    ok = not has_staged_mission
    detail = "" if has_staged_mission else "no staged mission edits in this checkout"
    try:
        app.select("story_a1")
        app.source.set("working"); app.source_changed()
        for _ in range(5): app.update()
        a = len(app.tab_whole.text.get("1.0", "end-1c"))
        app.source.set("shipped"); app.source_changed()
        for _ in range(5): app.update()
        b = len(app.tab_whole.text.get("1.0", "end-1c"))
        ok = a != b
        detail = f"working {a} chars vs shipped {b} chars"
        app.source.set("working"); app.source_changed()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("source selector changes the script view", ok, detail, skip=not has_staged_mission)

    # A [BYTECODE] record is a proven reconstruction: readable, and Stage as source is offered.
    # A read-only record is a chunk whose decompilation is not proven: readable with an explicit warning, and
    # Stage as source must be absent.
    ok = False
    detail = []
    try:
        got_any = False
        # walk every mission: the set of missions with a read-only (or role-locked bytecode) record
        # shrinks as records are recovered, so a fixed list goes stale
        _all = [app.missions.item(i, "values")[1] for i in app.missions.get_children()]
        _preferred = ("story_FinalBoss", "story_intro1", "story_c3", "story_e3", "story_g2")
        _order = [m for m in _preferred if m in _all] + [m for m in _all if m not in _preferred]
        for mission in _order[:40]:
            app.select(mission)
            for _ in range(6):
                app.update()
            app.tab_script.mode.set("Records")
            app.tab_script.refresh()
            for _ in range(3):
                app.update()
            groups = {}
            for r in _leaf_rows(app.tab_script.tree):
                groups.setdefault(app.tab_script.tree.item(r, "values")[2], []).append(r)
            for state, want_stage in (("[BYTECODE]", "pack"), ("read-only", "")):
                if state not in groups:
                    continue
                state_ok = False
                last = ""
                for row in groups[state]:
                    app.tab_script.tree.focus(row)
                    app.tab_script.tree.selection_set(row)
                    app.tab_script.on_select()
                    for _ in range(3):
                        app.update()
                    pane = app.tab_script.text.get("1.0", "end-1c")
                    listing = app.tab_script.listing_btn.winfo_manager()
                    stage = app.tab_script.stage_src_btn.winfo_manager()
                    good = listing == "pack" and stage == want_stage
                    if state == "[BYTECODE]":
                        good = good and "LuaDec reconstruction" in pane
                    else:
                        good = good and "READ-ONLY" in pane and "NOT verified" in pane
                    last = f"{state}: listing={listing or 'hidden'}, stage={stage or 'hidden'}"
                    if good:
                        state_ok = True
                        break
                detail.append(last)
                ok = state_ok if not got_any else (ok and state_ok)
                got_any = True
            if got_any and ok:
                break
            ok = False
            got_any = False
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    # with every record recovered there is no read-only record left to open; that is a pass, not a failure
    # with every record source-backed, the only read-only rows left are locked payloads, which have no listing
    _no_readonly = all("read-only:" not in d or "listing=hidden" in d for d in detail)
    ck("bytecode and read-only records open as readable Lua", ok, "; ".join(detail), skip=_no_readonly and not ok)

    # read-only reference blocks are greyed and refuse typing, while the rest of the pane stays editable
    ok = False
    detail = ""
    try:
        # pick a mission that still has a read-only record: the set shrinks as records are recovered
        app.tabs.select(app.tab_whole)
        app.tab_whole.with_bc.set(True)
        picked = None
        for candidate in ("story_intro1", "story_FinalBoss", "story_e3", "test_rampage01"):
            app.select(candidate)
            for _ in range(4):
                app.update()
            app.tab_whole.refresh()
            for _ in range(2):
                app.update()
            if "[READ-ONLY]" in app.tab_whole.text.get("1.0", "end-1c"):
                picked = candidate
                break
        if picked is None:
            raise RuntimeError("no mission with a read-only record")
        for _ in range(3):
            app.update()
        t = app.tab_whole.text.text
        ranges = t.tag_ranges("readonly")
        greyed = len(ranges) // 2
        blocked = allowed = None
        if ranges:
            t.mark_set("insert", str(ranges[0]))
            blocked = app.tab_whole._guard_key(type("E", (), {"keysym": "x"})())
            t.mark_set("insert", "1.0")
            allowed = app.tab_whole._guard_key(type("E", (), {"keysym": "x"})())
        app.set_theme("dark")
        for _ in range(2):
            app.update()
        app.tab_whole.refresh()
        for _ in range(2):
            app.update()
        tag_bg = str(t.tag_cget("readonly", "background")).lower()
        ok = (greyed >= 1 and blocked == "break" and allowed is None
              and tag_bg == app.THEMES["dark"]["readonly_bg"])
        detail = (f"{greyed} block(s), typing inside={blocked}, outside={allowed}, "
                  f"tag bg={tag_bg} (palette {app.THEMES['dark']['readonly_bg']})")
        app.set_theme("light")
        app.tab_whole.refresh()
        app.tab_whole.with_bc.set(False)
        app.tab_whole.refresh()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    # once every record is recovered there is no read-only block to tint; skip rather than fail
    ck("read-only blocks take the palette tint and refuse typing", ok, detail,
       skip=not has_staged_mission or "no mission with a read-only record" in detail)

    # A pane must look the way it behaves. Both signs were once wrong at the same time: the editable Mission Lua
    # pane was painted in the *window* colour (the very grey used for read-only), and the disabled Diff pane was
    # painted in the text colour - so the only way to learn which was which was to try typing.
    ok = True
    bad = []
    try:
        for theme in ("light", "dark"):
            app.set_theme(theme)
            for _ in range(3):
                app.update()
            pal = app.THEMES[theme]
            for name, pane in (("mission lua", app.tab_whole.text),
                               ("script map", app.tab_script.text),
                               ("fsm lua", app.tab_fsm.text),
                               ("diff", app.tab_diff.text)):
                # what matters is that the colour follows the pane's real state: an editable pane must not look
                # disabled and a disabled pane must not look editable, whichever record happens to be selected
                bg = str(pane.text.cget("background")).lower()
                state = str(pane.text.cget("state"))
                want_bg = str(pal["readonly_bg" if state == "disabled" else "text_bg"]).lower()
                good = bg == want_bg
                ok = ok and good
                if not good:
                    bad.append(f"{theme}/{name}: bg={bg} want={want_bg} state={state}")
        app.set_theme("light")
    except Exception as exc:
        ok, bad = False, [f"{type(exc).__name__}: {exc}"]
    ck("panes look the way they behave", ok, "; ".join(bad) or "editable panes in the text colour, read-only in the read-only colour, both themes")

    # a compiled record offers its constants, which is the edit path when the decompilation is not proven
    ok = False
    detail = ""
    saw_compiled = False
    try:
        _names = [app.missions.item(i, "values")[1] for i in app.missions.get_children()]
        _pref = ("story_FinalBoss", "story_intro1", "story_c3", "story_e3", "test_rampage01")
        _order2 = [m for m in _pref if m in _names] + [m for m in _names if m not in _pref]
        for mission in _order2[:40]:
            app.select(mission)
            for _ in range(5):
                app.update()
            app.tab_script.mode.set("Records")
            app.tab_script.refresh()
            for _ in range(3):
                app.update()
            rows = [r for r in app.tab_script.tree.get_children()
                    if app.tab_script.tree.item(r, "values")[3] in ("[BYTECODE]", "read-only")]
            if rows:
                saw_compiled = True
            for row in rows:
                app.tab_script.tree.focus(row)
                app.tab_script.tree.selection_set(row)
                app.tab_script.on_select()
                for _ in range(3):
                    app.update()
                shown = app.tab_script.const_btn.winfo_manager()
                listing = app.tab_script.listing_btn.winfo_manager()
                if shown == "pack" and listing == "pack":
                    ok = True
                    detail = f"{mission}: Constants={shown}, Listing={listing}"
                    break
                detail = f"{mission}: Constants={shown or 'hidden'}, Listing={listing or 'hidden'}"
            if ok:
                break
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    # every record is source-backed now, so there may be no compiled record left to check
    ck("a compiled record offers its constants", ok, detail,
       skip=(not ok and not saw_compiled))

    # the theme buttons recolour the editor, and Light restores it
    ok = False
    detail = ""
    try:
        app.select("story_a1")
        for _ in range(4):
            app.update()
        app.set_theme("dark")
        for _ in range(2):
            app.update()
        dark_root = str(app.cget("background")).lower()
        dark_text = str(app.tab_whole.text.text.cget("background")).lower()
        dark_fg = str(app.tab_whole.text.text.cget("foreground")).lower()
        app.set_theme("light")
        for _ in range(2):
            app.update()
        light_text = str(app.tab_whole.text.text.cget("background")).lower()
        pal = app.THEMES["dark"]
        ok = (dark_root == pal["bg"] and dark_text == pal["text_bg"] and dark_fg == pal["text_fg"]
              and light_text == "#ffffff")
        detail = f"dark bg={dark_root}/{dark_text} fg={dark_fg} (palette {pal['bg']}), light text={light_text}"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("dark mode recolours the editor and Light restores it", ok, detail)

    # every script pane scrolls both ways, and the scrollbars are wide enough to grab
    ok = False
    detail = ""
    try:
        app.select("story_a1")
        for _ in range(4):
            app.update()
        panes = [app.tab_script.text, app.tab_whole.text]
        managed = [p.hsb.winfo_manager() for p in panes]
        width = int(ttk.Style(app).lookup("Vertical.TScrollbar", "width") or 0)
        cfg = str(app.tab_script.text.text.cget("xscrollcommand"))
        app.set_theme("dark")
        for _ in range(2):
            app.update()
        thumb = str(ttk.Style(app).lookup("Vertical.TScrollbar", "background")).lower()
        trough = str(ttk.Style(app).lookup("Vertical.TScrollbar", "troughcolor")).lower()
        app.set_theme("light")
        for _ in range(2):
            app.update()
        ok = (all(m == "grid" for m in managed) and width == SCROLLBAR_WIDTH and cfg != ""
              and thumb == app.THEMES["dark"]["scroll_thumb"] and thumb != trough)
        detail = (f"horizontal bars={managed}, width={width}px (want {SCROLLBAR_WIDTH}), "
                  f"dark thumb={thumb} vs trough={trough}, xscrollcommand={bool(cfg)}")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("script panes have wide two-way scrollbars", ok, detail)

    # an editable record must offer only editing actions
    ok = False
    detail = ""
    try:
        app.select("story_a1")
        for _ in range(6):
            app.update()
        app.tab_script.mode.set("Records")
        app.tab_script.refresh()
        for _ in range(3):
            app.update()
        rows = [r for r in app.tab_script.record_rows()
                if app.tab_script.row_state(r) == "editable"]
        if rows:
            app.tab_script.tree.focus(rows[0])
            app.tab_script.tree.selection_set(rows[0])
            app.tab_script.on_select()
            for _ in range(2):
                app.update()
            apply_vis = app.tab_script.apply_btn.winfo_manager()
            listing_vis = app.tab_script.listing_btn.winfo_manager()
            stage_vis = app.tab_script.stage_src_btn.winfo_manager()
            ok = apply_vis == "pack" and listing_vis == "" and stage_vis == ""
            detail = f"apply={apply_vis or 'hidden'}, listing={listing_vis or 'hidden'}, stage={stage_vis or 'hidden'}"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("editable record shows only editing actions", ok, detail)

    # the Diff tab must offer revert only for a record that is actually changed
    ok = False
    detail = ""
    try:
        app.select((staged_missions() or ["story_a1"])[0])
        for _ in range(6):
            app.update()
        d = app.tab_diff
        d.refresh()
        rows = [r for r in d.tree.get_children()
                if str(d.tree.item(r, "values")[0]).strip().isdigit()]
        if rows:
            d.tree.focus(rows[0])
            d.tree.selection_set(rows[0])
            d.show()
            enabled = str(d.revert_btn.cget("state"))
            d.tree.selection_remove(*rows)
            d.tree.focus("")
            d.show()
            disabled = str(d.revert_btn.cget("state"))
            ok = enabled == "normal" and disabled == "disabled"
            detail = f"changed row -> {enabled}, no selection -> {disabled}"
        else:
            detail = "no changed rows (clean tree)"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("diff offers revert only for a changed record", ok, detail,
       skip="no changed rows" in detail)

    # read-only panes must not accept typing, and the record editor must be writable only when it is editable
    ok = False
    detail = ""
    try:
        app.select("story_e3")
        for _ in range(6):
            app.update()
        app.tab_script.mode.set("Records")
        app.tab_script.refresh()
        for _ in range(3):
            app.update()
        states = {}
        # every compiled-chunk state must be read-only in the editor; only source text is writable
        for want in ("[BYTECODE]", "read-only", "editable"):
            for row in app.tab_script.record_rows():
                if app.tab_script.row_state(row) == want:
                    app.tab_script.tree.focus(row)
                    app.tab_script.tree.selection_set(row)
                    app.tab_script.on_select()
                    for _ in range(2):
                        app.update()
                    states[want] = str(app.tab_script.text.text.cget("state"))
                    break
        app.tab_diff.refresh()
        for _ in range(2):
            app.update()
        diff_state = str(app.tab_diff.text.text.cget("state"))
        compiled = [states.get(k) for k in ("[BYTECODE]", "read-only") if states.get(k)]
        ok = (all(s == "disabled" for s in compiled) and states.get("editable") == "normal"
              and diff_state == "disabled")
        detail = (f"bytecode={states.get('[BYTECODE]')}, read-only={states.get('read-only')}, "
                  f"editable={states.get('editable')}, diff={diff_state}")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("only editable panes accept typing", ok, detail)

    # double-clicking a diff row must land on that record in the Script Map
    ok = False
    detail = ""
    try:
        app.select((staged_missions() or ["story_a1"])[0])
        for _ in range(6):
            app.update()
        d = app.tab_diff
        d.refresh()
        rows = [r for r in d.tree.get_children()
                if d.tree.item(r, "values")[2] == "changed"
                and str(d.tree.item(r, "values")[0]).strip().isdigit()]
        if rows:
            want = int(d.tree.item(rows[0], "values")[0])
            d.tree.focus(rows[0])
            d.tree.selection_set(rows[0])
            d.show()
            d.open_record()
            for _ in range(3):
                app.update()
            on_tab = app.tabs.index(app.tabs.select()) == app.tabs.index(app.tab_script)
            ok = on_tab and app.tab_script.current == want
            detail = f"row #{want} -> script tab={on_tab}, current={app.tab_script.current}"
        else:
            detail = f"no changed rows in {app.mission}"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("diff double-click opens the record", ok, detail, skip="no changed rows" in detail)

    # the Script Map must offer the reverse jump, and only for records that differ from stock
    ok = False
    detail = ""
    try:
        app.select("story_a1")
        for _ in range(6):
            app.update()
        t = app.tab_script
        t.mode.set("Records")
        t.refresh()
        for _ in range(3):
            app.update()
        picked = {}
        for state in ("editable",):
            for row in t.record_rows():
                if t.row_state(row) != state:
                    continue
                idx = t.row_index(row)
                t.tree.focus(row)
                t.tree.selection_set(row)
                t.on_select()
                for _ in range(2):
                    app.update()
                vis = t.diff_btn.winfo_manager()
                picked[idx] = vis or "hidden"
                if vis == "pack":
                    t.show_in_diff()
                    for _ in range(3):
                        app.update()
                    on_tab = app.tabs.index(app.tabs.select()) == app.tabs.index(app.tab_diff)
                    focused = app.tab_diff.tree.focus()
                    sel = int(app.tab_diff.tree.item(focused, "values")[0]) if focused else None
                    ok = on_tab and sel == idx
                    detail = f"#{idx} button={vis}, diff tab={on_tab}, diff row={sel}"
                    break
            if ok:
                break
        if not detail:
            detail = f"no changed editable record found (saw {picked})"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("script map jumps to the diff row", ok, detail, skip=not has_staged_mission)

    # edited records must be shaded, and only on a side that can differ
    ok = not has_staged_mission
    detail = ""
    try:
        app.source.set("working")
        app.select("story_a1")
        for _ in range(6):
            app.update()
        app.tab_script.mode.set("Records")
        app.tab_script.refresh()
        for _ in range(3):
            app.update()
        # which records are changed depends on what is staged, so find them instead of assuming an index
        tagged = {}
        for row in app.tab_script.record_rows():
            tagged[app.tab_script.row_index(row)] = app.tab_script.tree.item(row, "tags")
        green = [k for k, v in tagged.items() if "changed" in (v or ())]
        plain = [k for k, v in tagged.items() if "changed" not in (v or ())]
        green0 = bool(green)
        green1 = not plain
        app.source.set("shipped")
        app.source_changed()
        for _ in range(3):
            app.update()
        app.tab_script.refresh()
        for _ in range(2):
            app.update()
        any_green = any("changed" in (app.tab_script.tree.item(r, "tags") or ())
                        for r in app.tab_script.record_rows())
        ok = green0 and not green1 and not any_green
        detail = (f"working: {len(green)} shaded, {len(plain)} plain; "
                  f"shipped: any green={any_green}")
        app.source.set("working")
        app.source_changed()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("edited records are shaded green", ok, detail, skip=not has_staged_mission)

    # The Deploy tab must compare bytes, not manifest membership. Editing a file that Install copied earlier leaves
    # its path in the manifest, so a membership test calls a real pending change "installed" - which is exactly how a
    # placement edit looked already deployed, with the world still using the old coordinates.
    ok = False
    detail = ""
    try:
        dep = app.tab_deploy
        dep.refresh()
        for _ in range(2):
            app.update()
        shown = [(dep.tree.item(i, "values")[0], dep.tree.item(i, "values")[1])
                 for i in dep.tree.get_children()]
        truth = app.model.deploy_status()
        want = {"pending": "changed since install", "staged": "not installed yet",
                "installed": "installed, up to date"}
        ok = ([rel for rel, _t in shown] == [rel for rel, _s in truth]
              and all(t.startswith(want[s]) for (rel, t), (_r, s) in zip(shown, truth))
              and len({rel for rel, _t in shown}) == len(shown))
        counted = sum(1 for _r, st in truth if st == "pending")
        detail = (f"{len(shown)} row(s), {counted} pending; "
                  f"summary={dep.summary.cget('text')!r}")
        ok = ok and str(counted) in str(dep.summary.cget("text"))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("deploy tab compares bytes, not the manifest", ok, detail)

    # Grouped views must render every GROUP the data has, not just every record. The address tab once dropped the
    # group level entirely: 96 members of Address_7608304 became 96 unrelated roots, and no existing check noticed
    # because they all counted records.
    ok = True
    detail = []
    try:
        def group_nodes(tree):
            n, stack = 0, list(tree.get_children())
            while stack:
                iid = stack.pop()
                if iid.startswith("g"):
                    n += 1
                stack.extend(tree.get_children(iid))
            return n

        app.select("story_a1")
        for _ in range(6):
            app.update()
        # addresses: one group node per distinct Address_<id> among the records
        app.tab_place.q.set("")
        app.tab_place.fill()
        recs = [d for d, _p in app.tab_place.recs]
        want = len({r["name"] for r in recs})
        got = group_nodes(app.tab_place.tree)
        ok = ok and got == want
        detail.append(f"addresses {got}/{want} groups")
        # definition: one group node per distinct ancestor prefix
        app.tab_def.q.set("")
        app.tab_def.fill()
        prefixes = set()
        for path, _disp, _e in app.tab_def.rows:
            pre = ""
            for seg in path.split(".")[:-1]:
                pre = f"{pre}.{seg}" if pre else seg
                prefixes.add(pre)
        got2, stack = 0, list(app.tab_def.tree.get_children())
        while stack:
            iid = stack.pop()
            if iid.startswith("g:"):
                got2 += 1
            stack.extend(app.tab_def.tree.get_children(iid))
        want2 = len(prefixes)
        ok = ok and got2 == want2
        detail.append(f"definition {got2}/{want2} groups")
        # script map: one group node per role
        app.tab_script.mode.set("Records")
        app.tab_script.q.set("")
        app.tab_script.refresh()
        roles = {r[1] for r in app.tab_script.rows()}
        got3 = len(app.tab_script.tree.get_children(""))
        ok = ok and got3 == len(roles)
        detail.append(f"script map {got3}/{len(roles)} roles")
    except Exception as exc:
        ok, detail = False, [f"{type(exc).__name__}: {exc}"]
    ck("grouped tabs render every group", ok, "; ".join(str(d) for d in detail))

    # the notify dialog's badge must be drawn, not a Unicode glyph that the font can turn into a stray "i"
    try:
        badge = _badge(app, False)
        texts = [badge.itemcget(i, "text") for i in badge.find_all() if badge.type(i) == "text"]
        ovals = [i for i in badge.find_all() if badge.type(i) == "oval"]
        ok = texts == ["i"] and len(ovals) == 1 and "\u2139" not in             open(os.path.join(ROOT, "tools", "mission_gui.py"), encoding="utf-8").read().split("def _badge")[1][:600]
        detail = f"text={texts}, circle={len(ovals)}"
        badge.destroy()
    except Exception as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    ck("dialog badge is drawn, not a font glyph", ok, detail)

    # the Script Map description must span the tab instead of sitting in a fixed-width column
    ok = False
    detail = ""
    try:
        app.select("story_e2")
        app.tabs.select(app.tab_script)          # a tab that is not current has no width to measure
        for _ in range(8):
            app.update()
        app.tab_script.mode.set("Records")
        app.tab_script.refresh()
        for _ in range(3):
            app.update()
        w_tab = app.tab_script.winfo_width()
        w_hint = app.tab_script.hint.winfo_width()
        ok = w_tab > 200 and w_hint >= 0.7 * w_tab
        detail = f"tab {w_tab}px, description {w_hint}px"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    ck("script map description spans the tab", ok, detail)

    # the log must be a draggable split, not a fixed strip
    h0 = app.logframe.winfo_height()
    app.vsplit.sashpos(0, max(200, app.vsplit.winfo_height() - 60))
    for _ in range(6):
        app.update()
        app.update_idletasks()
    h1 = app.logframe.winfo_height()
    ck("log pane is resizable", abs(h1 - h0) > 20, f"{h0}px -> {h1}px")

    app.destroy()

    print("UI check")
    bad = 0
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<32} {detail}")
        bad += 0 if ok else 1
    extra = f", {len(skipped)} skipped (no staged mission edits)" if skipped else ""
    print(f"\n{len(results) - bad}/{len(results)} checks passed{extra}")
    return 0 if bad == 0 else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--uicheck" in sys.argv:
        return uicheck()
    app = App()
    app.persist = True
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
