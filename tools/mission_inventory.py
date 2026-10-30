#!/usr/bin/env python3
"""Corpus-wide inventory of the mission scripting / placement vocabulary.

Regenerates every number quoted in ``analysis/mission-vocabulary.md`` so none of
them is hand-typed.  Pure data extraction: it never writes to the game install
and it does not touch the REA session.

Subcommands
  list      enumerate every ``art/missions/...`` entry in every shipped archive
            (index only, no payload reads) -> analysis/mission-files.tsv
  sample    print the exact sample set the content analysis uses, and why
  chunks    distinct chunk types inside the sampled mission .p3d files
  vocab     meta-object classes, META entry/attribute names, combinations
  names     placed-object name families
  all       chunks + vocab + names, writes the .md and .json deliverables

Sets
  --set all (default)     every mission asset directory in the archives
  --set sample            the fixed 20-mission sample listed in SAMPLE, written to
                          analysis/mission-vocabulary-sample.{md,json}

Everything extracted lands under ./tmp/corpus/ (git-ignored); analysis caches
land under ./tmp/work/.  Re-runs reuse the extracted files.
"""

import argparse
import collections
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from p3d import parse, walk, ROOT_ID                      # noqa: E402
from rcf import Rcf, unwrap_rz                            # noqa: E402
from textbible import read_aligned_u8                     # noqa: E402
import config  # noqa: E402

GAME = config.game_dir()
ARCHIVES = ["art.rcf", "boot.rcf", "cells.rcf", "woi.rcf", "story_a_to_h.rcf",
            "story_i_to_z.rcf", "00audio.rcf", "01audio.rcf", "audio_german.rcf",
            "movies.rcf", "scripts.rcf", "shaders.rcf", "enable_debug.rcf"]
MISSIONS_PREFIX = "art\\missions\\"

CORPUS = os.path.join(ROOT, "tmp", "corpus")
CACHE = os.path.join(ROOT, "tmp", "work")
ANALYSIS = os.path.join(ROOT, "analysis")

META_OBJECT = 0x07F00000
META_DATA = 0x07F00001
TEXTBIBLE_HOLDER = 0x00018202
TEXTBIBLE_STORAGE = 0x00018201

# Chunk ids whose meaning is already documented somewhere in this repo.  Any
# other id is reported as unclassified, with its observed count.
KNOWN_CHUNK_TYPES = {
    ROOT_ID: "P3D root",
    META_OBJECT: "meta-object: placed object; aligned_u8 display/name/class header",
    META_DATA: "meta-object data block ('META' payload)",
    TEXTBIBLE_HOLDER: "TextBibleHolder (tools/textbible.py)",
    TEXTBIBLE_STORAGE: "TextBibleStorage (tools/textbible.py)",
}

# Observed type ids above this bound are registry ids; anything smaller is a
# length/count word that a coincidental string match picked up (see
# analysis/mission-vocabulary.md, "How the parse was validated").
MIN_TYPE_ID = 0x01000000

PACKAGE_VALUE = re.compile(r"^[a-z0-9_]+_(active|open)$")

# ---------------------------------------------------------------- sample set
# Fixed, explicit sample: 20 missions stratified over the prefixes the shipped
# mission-definition file uses, deliberately spanning both archives that carry
# mission assets (boot.rcf holds story_intro1, art.rcf holds the rest).
SAMPLE = [
    ("story_intro1", "opening playable mission; only mission asset in boot.rcf"),
    ("story_a1", "story mission, chapter 1"),
    ("story_a2", "story mission, chapter 1"),
    ("story_FinalBoss", "story mission, end of campaign"),
    ("story_i1", "story mission, mid campaign"),
    ("story_m3", "story mission, late campaign"),
    ("gz_la_01", "green-zone lair (interior)"),
    ("rz_la_02", "red-zone lair (interior)"),
    ("yz_la_01", "yellow-zone lair (interior)"),
    ("gz_sp_01", "green-zone side activity"),
    ("rz_sc_01", "red-zone side activity"),
    ("yz_cd_01", "yellow-zone side activity"),
    ("gz_ra_01", "green-zone side activity"),
    ("location_green", "location container asset, not a scripted mission"),
    ("location_red", "location container asset, not a scripted mission"),
    ("meta_green", "meta/lair container asset"),
    ("meta_red", "meta asset whose asset only exists for the open variant in art.rcf"),
    ("ow_green_black_net_hunt_target", "open-world black-net hunt target"),
    ("hex_test", "engine test asset"),
    ("ms8_character_test", "engine test asset"),
]

# Mission directories that ship assets but have no entry in
# scripts/missiondefinit.lua (checked against missions/ALL-MISSIONS.txt).
NON_MISSION_DIRS = {
    "Red_Zone_Press", "gz_m001", "hex_test", "meta_green", "meta_red", "meta_yellow",
    "ms8_character_test", "rz_m001", "structure_green_lair_interior_test_s_c_4",
    "structure_green_lair_interior_test_v_c_1", "test_autotest_green_zone",
    "test_autotest_red_zone", "test_autotest_yellow_zone", "test_black_box_manager",
    "test_combat_3way", "test_combat_infected", "test_combat_military", "test_jump",
    "test_loco", "test_minimal", "test_optimization", "test_scientist",
    "test_scientist_consume_5", "test_vc1", "test_vip_hunting",
}


def archive_path(name):
    return os.path.join(GAME, name)


def human(n):
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if x < 1024:
            return f"{x:,.1f} {unit}"
        x /= 1024.0
    return f"{x:,.1f} TiB"


def kind_of(fname):
    base = fname[:-3] if fname.endswith(".rz") else fname
    return base[:-4] if base.endswith(".p3d") else base


# --------------------------------------------------------------- RCF listing
def iter_mission_entries():
    """Yield (archive, mission, variant, filename, size) for art/missions/**.

    Reads only the archive index; no payload is touched.
    """
    for arc_name in ARCHIVES:
        path = archive_path(arc_name)
        if not os.path.exists(path):
            continue
        with Rcf(path) as arc:
            if arc.unnamed_entries:
                print(f"warning: {arc_name}: {len(arc.unnamed_entries)} entries have no name",
                      file=sys.stderr)
            for e in arc.entries:
                if not e.name or not e.name.startswith(MISSIONS_PREFIX):
                    continue
                parts = e.name.replace("\\", "/").split("/")
                if len(parts) != 5:
                    print(f"warning: unexpected mission path {e.name}", file=sys.stderr)
                    continue
                yield arc_name, parts[2], parts[3], parts[4], e.size


def mission_index():
    """mission -> variant -> [(file, size, archive)]."""
    idx = collections.OrderedDict()
    for arc, mission, variant, fname, size in iter_mission_entries():
        idx.setdefault(mission, collections.OrderedDict()) \
           .setdefault(variant, []).append((fname, size, arc))
    for m in idx:
        for v in idx[m]:
            idx[m][v].sort()
    return idx


def package_index():
    """Every art/packages/** entry name and size, from the archive indexes."""
    out = []
    for arc_name in ARCHIVES:
        path = archive_path(arc_name)
        if not os.path.exists(path):
            continue
        with Rcf(path) as arc:
            for e in arc.entries:
                if not e.name:
                    continue
                low = e.name.lower()
                if low.startswith("art\\packages\\"):
                    out.append((e.name.replace("\\", "/"), e.size, arc_name))
    out.sort()
    return out


def select_sample():
    idx = mission_index()
    out, missing = [], []
    for mission, why in SAMPLE:
        if mission in idx:
            out.append((mission, why))
        else:
            missing.append(mission)
    if missing:
        raise SystemExit(f"sample missions not present in the archives: {missing}")
    return out, idx


def resolve_set(kind):
    idx = mission_index()
    if kind == "all":
        return [(m, "every mission asset directory in the archives") for m in idx], idx
    return select_sample()


# ------------------------------------------------------------- extraction
def corpus_path(arc, name):
    return os.path.join(CORPUS, arc, name.replace("\\", "/"))


def fetch(arc, name):
    """Extract one entry (unwrapping .rz) into ./tmp/corpus and return the path."""
    dst = corpus_path(arc, name)
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with Rcf(archive_path(arc)) as rcf:
        data = rcf.read(rcf.find(name))
    raw, _info = unwrap_rz(data)
    with open(dst, "wb") as fh:
        fh.write(raw)
    return dst


def corpus_files(selections, idx):
    """Yield (mission, variant, fname, archive, local_path) for the chosen set."""
    for mission, _why in selections:
        for variant, files in idx[mission].items():
            for fname, size, arc in files:
                if not fname.endswith((".p3d", ".p3d.rz")):
                    continue
                full = f"{MISSIONS_PREFIX}{mission}\\{variant}\\{fname}"
                yield mission, variant, fname, arc, fetch(arc, full)


# ------------------------------------------------------------- p3d parsing
def _printable(raw):
    return bool(raw) and all(32 <= b < 127 for b in raw) and b"\0" not in raw


def _u32(body, at):
    return struct.unpack_from("<I", body, at)[0]


def meta_records(chunk):
    """Decode one 0x07f00001 META body into entry records.

    Byte layout, established from the shipped bytes (documented in
    analysis/mission-vocabulary.md):

        u32   len(body) - 4          self describing length
        "META"
        u32   named_count            matches the number of records that carry an
                                     explicit name in most bodies (see below)
        then, repeated:
            u32 name_len, name       OPTIONAL explicit name of the attribute
            u32 type_len, type       the component / behaviour / attribute type
            u16 1                    constant in every record observed
            u32 type_id              32-bit registry id, stable per type name
            <type specific parameter bytes>

    There is no per-record length, so records are found by their anchor: a
    ``u32 len`` + printable string immediately followed by ``u16 1`` + a u32.
    A record that carries an explicit name is recognised because the bytes
    immediately before the type's length prefix are ``u32 len`` + printable
    name (e.g. ``transform`` in front of ``MatrixAttribute``).

    Returns (records, named_count, self_consistent); a record is
    ``{"name": str|None, "type": str, "type_id": int}``.
    """
    body = chunk.data[12:]
    if len(body) < 12:
        return [], None, False
    declared = _u32(body, 0)
    ok = declared == len(body) - 4 and body[4:8] == b"META"
    named_count = _u32(body, 8)
    out = []
    i, n = 12, len(body)
    while i + 4 <= n:
        ln = _u32(body, i)
        if 1 <= ln <= 64 and i + 4 + ln + 6 <= n and _printable(body[i + 4:i + 4 + ln]):
            if body[i + 4 + ln:i + 4 + ln + 2] == b"\x01\x00":
                name = None
                for sl in range(1, 65):
                    j = i - 4 - sl
                    if j < 12:
                        break
                    if _u32(body, j) == sl and _printable(body[j + 4:j + 4 + sl]):
                        name = body[j + 4:j + 4 + sl].decode("latin-1")
                        break
                out.append({"name": name,
                            "type": body[i + 4:i + 4 + ln].decode("latin-1"),
                            "type_id": _u32(body, i + 4 + ln + 2)})
                i = i + 4 + ln + 6
                continue
        i += 1
    return out, named_count, ok


TEMPLATE_CLASSES = {"GameObjectTemplateBuilder", "PropTemplate"}


def u8_strings(body):
    """Sequential `u8 len` + bytes strings, as used by the template classes.

    `GameObjectTemplateBuilder` starts the list with a `u32` count;
    `PropTemplate` starts immediately.  Both are tried and the longer parse wins.
    """
    best = []
    for start in (12, 8):
        out, p = [], start
        while p < len(body):
            n = body[p]
            if 1 <= n <= 200 and p + 1 + n <= len(body) and _printable(body[p + 1:p + 1 + n]):
                out.append(body[p + 1:p + 1 + n].decode("latin-1"))
                p += 1 + n
            else:
                break
        if len(out) > len(best):
            best = out
    return best


def analyse_file(path):
    """Chunk-type counts and meta-object facts for one .p3d file."""
    blob = open(path, "rb").read()
    rec = {"size": len(blob), "chunk_types": collections.Counter(), "chunks": 0,
           "objects": [], "meta_blocks": 0, "meta_ok": 0, "meta_bad": 0,
           "named_count_ok": 0, "named_count_ok_nopkg": 0, "named_count_checked": 0,
           "header_fail": 0, "error": None}
    try:
        root = parse(blob)
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec
    for c in walk(root):
        rec["chunks"] += 1
        rec["chunk_types"][c.type] += 1
    for c in walk(root):
        if c.type != META_OBJECT:
            continue
        obj = {"name": None, "klass": None, "display": None, "trailer": None,
               "class_tag": None, "records": [], "named_count": None, "strings": []}
        body = c.data[12:]
        try:
            obj["display"], p = read_aligned_u8(body, 0)
            obj["name"], p = read_aligned_u8(body, p)
            obj["klass"], p = read_aligned_u8(body, p)
        except Exception:
            rec["header_fail"] += 1
            continue
        tail = body[p:]
        obj["trailer"] = tail[:8].hex()
        obj["class_tag"] = None
        if len(tail) >= 8:
            obj["class_tag"] = (struct.unpack_from("<H", tail, 0)[0],
                                struct.unpack_from("<H", tail, 2)[0],
                                struct.unpack_from("<I", tail, 4)[0])
        for k in c.children:
            if k.type != META_DATA:
                continue
            rec["meta_blocks"] += 1
            records, named_count, ok = meta_records(k)
            if ok:
                rec["meta_ok"] += 1
            else:
                rec["meta_bad"] += 1
            obj["records"].extend(records)
            if obj["klass"] in TEMPLATE_CLASSES:
                obj["strings"] = u8_strings(k.data[12:])
            obj["named_count"] = named_count
            if named_count is not None and records:
                rec["named_count_checked"] += 1
                named = sum(1 for r in records if r["name"])
                named_nopkg = sum(1 for r in records
                                  if r["name"] and not PACKAGE_VALUE.match(r["name"]))
                if named == named_count:
                    rec["named_count_ok"] += 1
                if named_nopkg == named_count:
                    rec["named_count_ok_nopkg"] += 1
        rec["objects"].append(obj)
    return rec


# ------------------------------------------------------------ collect/cache
def collect(set_name):
    selections, idx = resolve_set(set_name)
    records = {}
    for mission, variant, fname, arc, path in corpus_files(selections, idx):
        if not os.path.exists(path) or os.path.getsize(path) < 12:
            continue
        rec = analyse_file(path)
        rec.update({"mission": mission, "variant": variant, "file": fname,
                    "archive": arc, "kind": kind_of(fname)})
        rec["chunk_types"] = {f"0x{t:08x}": n for t, n in rec["chunk_types"].items()}
        records[f"{mission}/{variant}/{fname}"] = rec
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, f"analysed-{set_name}.json"), "w") as fh:
        json.dump({"set": set_name, "files": records}, fh)
    return records, selections, idx


# ------------------------------------------------------------- aggregation
def build(records, selections, idx, set_name):
    out = {}
    files = sorted(records)
    out["set"] = set_name
    out["missions"] = [{"mission": m, "why": w,
                        "variants": sorted(idx[m]),
                        "files": sum(len(v) for v in idx[m].values()),
                        "index_bytes": sum(s for v in idx[m].values() for _f, s, _a in v)}
                       for m, w in selections]
    out["files_analysed"] = len(files)
    out["analyzed_bytes"] = sum(records[f]["size"] for f in files)
    out["parse_errors"] = [{"file": f, "error": records[f]["error"]}
                           for f in files if records[f]["error"]]

    # -- per (variant, file kind) emptiness
    fill = collections.defaultdict(lambda: {"files": 0, "root_only": 0, "bytes": 0,
                                            "max_bytes": 0})
    for f in files:
        rec = records[f]
        key = (rec["variant"], rec["kind"])
        d = fill[key]
        d["files"] += 1
        d["bytes"] += rec["size"]
        d["max_bytes"] = max(d["max_bytes"], rec["size"])
        if sum(rec["chunk_types"].values()) <= 1:
            d["root_only"] += 1
    open_pop = collections.defaultdict(set)
    for f in files:
        rec = records[f]
        if rec["variant"] == "open" and sum(rec["chunk_types"].values()) > 1:
            open_pop[rec["kind"]].add(rec["mission"])
    out["open_layer"] = {k: sorted(v) for k, v in open_pop.items()}
    out["variant_fill"] = [
        {"variant": k[0], "kind": k[1], "files": v["files"], "root_only": v["root_only"],
         "bytes": v["bytes"], "max_bytes": v["max_bytes"]}
        for k, v in sorted(fill.items())]

    # -- chunk types
    types = collections.Counter()
    tfiles = collections.defaultdict(set)
    tkinds = collections.defaultdict(collections.Counter)
    errors = []
    for f in files:
        rec = records[f]
        if rec["error"]:
            errors.append(f)
            continue
        for t, n in rec["chunk_types"].items():
            types[t] += n
            tfiles[t].add(f)
            tkinds[t][rec["kind"]] += n
    out["chunk_types"] = [
        {"type": t, "count": n, "files": len(tfiles[t]),
         "known": KNOWN_CHUNK_TYPES.get(int(t, 16)),
         "by_kind": dict(tkinds[t].most_common())}
        for t, n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))]
    out["chunks_total"] = sum(types.values())
    pref = collections.defaultdict(lambda: {"types": 0, "chunks": 0})
    for t, n in types.items():
        key = t[:6] + "xx"        # 0x0001xxxx -> 0x0001xx
        pref[key]["types"] += 1
        pref[key]["chunks"] += n
    out["chunk_type_families"] = [
        {"prefix": k, "types": v["types"], "chunks": v["chunks"]}
        for k, v in sorted(pref.items(), key=lambda kv: (-kv[1]["chunks"], kv[0]))]
    out["chunk_type_count"] = len(types)
    out["chunk_type_unclassified"] = sum(1 for t in types if int(t, 16) not in KNOWN_CHUNK_TYPES)
    out["nonzero_chunk_files"] = sum(1 for f in files if not records[f]["error"]
                                     and sum(records[f]["chunk_types"].values()) > 1)

    # -- meta objects
    classes = collections.Counter()
    class_files = collections.defaultdict(set)
    class_records = collections.Counter()
    class_norecords = collections.Counter()
    header_fail = 0
    meta_blocks = 0
    for f in files:
        rec = records[f]
        header_fail += rec["header_fail"]
        meta_blocks += rec["meta_blocks"]
        for o in rec["objects"]:
            if o["klass"]:
                classes[o["klass"]] += 1
                class_files[o["klass"]].add(f)
                if o["records"]:
                    class_records[o["klass"]] += 1
                else:
                    class_norecords[o["klass"]] += 1
    class_tags = collections.defaultdict(collections.Counter)
    tag_kind = collections.defaultdict(collections.Counter)
    for f in files:
        for o in records[f]["objects"]:
            if o["klass"] and o.get("class_tag"):
                a, b, cid = o["class_tag"]
                key = f"{a},{b},0x{cid:08x}"
                class_tags[o["klass"]][key] += 1
                tag_kind[key][records[f]["kind"]] += 1
    out["class_tags"] = {k: dict(v.most_common()) for k, v in class_tags.items()}
    out["class_tags_by_kind"] = {k: dict(v.most_common()) for k, v in tag_kind.items()}
    out["meta_objects"] = sum(classes.values())
    out["meta_blocks"] = meta_blocks
    out["meta_header_failures"] = header_fail
    out["meta_classes"] = [
        {"class": k, "objects": n, "files": len(class_files[k]),
         "objects_with_records": class_records[k],
         "objects_without_records": class_norecords[k]}
        for k, n in sorted(classes.items(), key=lambda kv: (-kv[1], kv[0]))]

    # -- entry types (the core vocabulary)
    tcount = collections.Counter()
    tids = collections.defaultdict(collections.Counter)
    tkind = collections.defaultdict(collections.Counter)
    tmission = collections.defaultdict(set)
    tid_names = collections.defaultdict(collections.Counter)
    rejected = collections.Counter()
    named = collections.Counter()
    named_type = collections.Counter()
    combos = collections.Counter()
    combos_set = collections.Counter()
    for f in files:
        rec = records[f]
        for o in rec["objects"]:
            names = []
            for r in o["records"]:
                if r["type_id"] >= MIN_TYPE_ID:
                    tcount[r["type"]] += 1
                    tids[r["type"]][f"0x{r['type_id']:08x}"] += 1
                    tid_names[f"0x{r['type_id']:08x}"][r["type"]] += 1
                    tkind[r["type"]][rec["kind"]] += 1
                    tmission[r["type"]].add(rec["mission"])
                    names.append(r["type"])
                    if r["name"]:
                        named[r["name"]] += 1
                        named_type[(r["name"], r["type"])] += 1
                else:
                    rejected[r["type"]] += 1
            if names:
                combos[tuple(names)] += 1
                combos_set[tuple(sorted(set(names)))] += 1
    out["entry_types"] = [
        {"name": k, "count": n, "type_ids": dict(tids[k].most_common()),
         "files": 0,
         "missions": sorted(tmission[k]),
         "by_kind": dict(tkind[k].most_common())}
        for k, n in sorted(tcount.items(), key=lambda kv: (-kv[1], kv[0]))]
    type_files = collections.defaultdict(set)
    for f in files:
        for o in records[f]["objects"]:
            for r in o["records"]:
                if r["type_id"] >= MIN_TYPE_ID:
                    type_files[r["type"]].add(f)
    for item in out["entry_types"]:
        item["files"] = len(type_files[item["name"]])
    out["entry_type_count"] = len(tcount)
    out["entry_type_observations"] = sum(tcount.values())
    out["type_ids"] = [{"id": k, "names": dict(v.most_common()),
                        "count": sum(v.values())}
                       for k, v in sorted(tid_names.items())]
    out["rejected_candidates"] = [{"name": k, "count": n}
                                  for k, n in sorted(rejected.items(), key=lambda kv: (-kv[1], kv[0]))]
    out["named_attributes"] = [
        {"name": k, "count": n,
         "looks_like_package_value": bool(PACKAGE_VALUE.match(k)),
         "types": dict(named_type[k].most_common()) if False else None}
        for k, n in sorted(named.items(), key=lambda kv: (-kv[1], kv[0]))]
    for item in out["named_attributes"]:
        del item["types"]
        item["pairs"] = {f"{nm} -> {tp}": c
                         for (nm, tp), c in named_type.items() if nm == item["name"]}
    out["combinations"] = [{"types": list(k), "count": n}
                           for k, n in sorted(combos.items(), key=lambda kv: (-kv[1], kv[0]))]
    out["combination_count_ordered"] = len(combos)
    out["combination_count_asset"] = len(combos_set)
    out["named_count_checks"] = {
        "bodies_with_records": sum(1 for f in files for o in records[f]["objects"] if o["records"]),
        "bodies": sum(records[f]["named_count_checked"] for f in files),
        "agree": sum(records[f]["named_count_ok"] for f in files),
        "agree_nopkg": sum(records[f]["named_count_ok_nopkg"] for f in files),
    }

    # -- placed object names
    placed = collections.Counter()
    placed_class = collections.defaultdict(collections.Counter)
    placed_kind = collections.defaultdict(collections.Counter)
    for f in files:
        rec = records[f]
        for o in rec["objects"]:
            if not o["name"]:
                continue
            placed[o["name"]] += 1
            placed_class[o["klass"]][o["name"]] += 1
            placed_kind[rec["kind"]][o["name"]] += 1
    out["placed"] = {"total": sum(placed.values()), "distinct": len(placed)}
    out["placed_classes"] = [
        {"class": k, "objects": sum(v.values()), "distinct": len(v),
         "examples": [n for n, _ in v.most_common(5)]}
        for k, v in sorted(placed_class.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))]
    out["placed_kinds"] = [
        {"kind": k, "objects": sum(v.values()), "distinct": len(v)}
        for k, v in sorted(placed_kind.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))]
    out["placed_families"] = families(placed, {n: c for c, v in placed_class.items() for n in v})
    out["placed_derived"] = derived_names(records, files)
    out["placed_top"] = [{"name": k, "count": n,
                          "class": next((c for c, v in placed_class.items() if k in v), None)}
                         for k, n in placed.most_common(40)]
    out["_placed_names"] = sorted(placed)      # kept out of the json dump
    return out


TEMPLATE_SUFFIX = re.compile(r"^(?P<stem>.+?)[_ ](?P<suffix>High|Low|Med|MedLOD|LowLOD|RenderOnly|"
                             r"Body|Crushed|Broken|Destroyed|Intact|Damaged|Static|Dynamic|"
                             r"LOD\d?|lod\d?|Proxy|Collision)$")


def families(names, klass_of=None):
    klass_of = klass_of or {}
    """Naming families for placed-object names (data driven, no hand lists)."""
    fam = collections.Counter()
    ex = collections.defaultdict(list)
    digit = re.compile(r"^(?P<stem>[A-Za-z_\-./ ]*?)(?P<num>\d+)(?P<tail>.*)$")
    for n in names:
        m = TEMPLATE_SUFFIX.match(n)
        if klass_of.get(n) and n.endswith(klass_of[n]):
            key = "stem glued to its own class name (<stem><Class>)"
        elif m:
            key = "template name + disposition suffix (_High/_Low/_RenderOnly/...)"
        elif n.startswith("//"):
            key = "asset path reference (//...)"
        elif re.match(r"^Address_\d+$", n):
            key = "engine-generated id name (Address_<decimal>)"
        elif digit.match(n):
            key = "stem + digits (+ optional tail)"
        elif re.search(r"[A-Z]", n) and re.search(r"[a-z]", n):
            key = "CamelCase / glued word name"
        else:
            key = "other"
        fam[key] += 1
        if len(ex[key]) < 4:
            ex[key].append(n)
    stems = collections.Counter()
    for n in names:
        m = digit.match(n)
        if m and m.group("stem"):
            stems[m.group("stem")] += 1
    return {"buckets": [{"family": k, "count": v, "examples": ex[k]}
                        for k, v in sorted(fam.items(), key=lambda kv: (-kv[1], kv[0]))],
            "stems": [{"stem": k, "count": v} for k, v in stems.most_common(40)]}


def derived_names(records, files):
    """Names that are another placed object's name plus a suffix, per file.

    The engine synthesises helper objects from a template: `<name>_High`,
    `<name>_RenderOnly`, `<name>_LODs`, `<name>SmartNodeArray`, ...  Each such
    name resolves to a *stem* that is itself a placed object in the same file,
    which is what makes this a measured relationship rather than a guess.
    """
    suffix = collections.Counter()
    examples = collections.defaultdict(list)
    resolved = total = 0
    for f in files:
        names = sorted({o["name"] for o in records[f]["objects"] if o["name"]})
        pool = set(names)
        for n in names:
            total += 1
            for i in range(3, len(n) - 1):
                if n[:i] in pool:
                    sfx = n[i:]
                    if len(sfx) > 40:
                        continue
                    suffix[sfx] += 1
                    resolved += 1
                    if n not in examples[sfx] and len(examples[sfx]) < 3:
                        examples[sfx].append(n)
                    break
    return {"names_examined": total, "resolved_to_a_sibling_object": resolved,
            "suffixes": [{"suffix": k, "count": v, "examples": examples[k]}
                         for k, v in suffix.most_common(40)]}


def cross_reference(records, files, out):
    """Attempt the archetype/package joins; report only what is provable."""
    xr = {}
    # (1) packageName attribute value vs the mission package it lives in.
    # A NameAttribute's value is a u32-length-prefixed string that sits exactly
    # where a record's explicit name would sit, so the recorded "name" of the
    # next record is that value whenever it is package-shaped.
    hits = miss = 0
    samples = []
    other = collections.Counter()
    values = collections.Counter()
    for f in files:
        rec = records[f]
        pkg = f"{rec['mission']}_{rec['variant']}"
        for o in rec["objects"]:
            for r in o["records"]:
                if r["name"] and PACKAGE_VALUE.match(r["name"]):
                    values[r["name"]] += 1
                    if r["name"] == pkg:
                        hits += 1
                    else:
                        miss += 1
                        other[r["name"]] += 1
                        if len(samples) < 10:
                            samples.append({"file": f, "value": r["name"], "package": pkg})
    xr["package_name_values"] = {
        "observed": hits + miss, "match_own_package": hits, "other": miss,
        "other_examples": samples,
        "distinct": len(values),
        "cross_package_values": dict(other.most_common()),
        "values": dict(values.most_common(40)),
        "method": ("the string recovered immediately before a component record when it "
                   "matches '^[a-z0-9_]+_(active|open)$' is the value of the preceding "
                   "packageName:NameAttribute record; it is compared against "
                   "'<mission dir>_<variant>'"),
    }

    # (2) template / builder children vs placed object names in the same file.
    # PropTemplate + GameObjectTemplateBuilder bodies hold a u8-length string
    # list; each entry names a GameObject placed in the same file.
    tmpl_hits = tmpl_total = 0
    tmpl_examples = []
    tmpl_names = collections.Counter()
    tmpl_by_class = collections.Counter()
    for f in files:
        rec = records[f]
        names = {o["name"] for o in rec["objects"] if o["name"]}
        for o in rec["objects"]:
            if o["klass"] not in TEMPLATE_CLASSES:
                continue
            for child in o["strings"]:
                tmpl_total += 1
                tmpl_names[child] += 1
                tmpl_by_class[o["klass"]] += 1
                if child in names:
                    tmpl_hits += 1
                    if len(tmpl_examples) < 10:
                        tmpl_examples.append({"file": f, "template": o["name"],
                                              "child": child})
    xr["template_references"] = {
        "observed": tmpl_total, "resolved_to_a_named_object_in_same_file": tmpl_hits,
        "by_class": dict(tmpl_by_class),
        "distinct_children": len(tmpl_names),
        "examples": tmpl_examples,
        "top_children": dict(tmpl_names.most_common(15)),
        "method": ("u8-length child strings inside GameObjectTemplateBuilder/PropTemplate "
                   "META bodies, compared with the 0x07f00000 object names of the same file"),
    }

    # (3) 32-bit ids vs datainit.lua string constants
    lua = os.path.join(ROOT, "work", "lua")
    strings = set()
    for name in ("datainit.strings.txt", "datainit.globals.txt"):
        p = os.path.join(lua, name)
        if os.path.exists(p):
            for line in open(p, encoding="latin-1"):
                s = line.rstrip("\n")
                if s:
                    strings.add(s)
    id_set = {i["id"] for i in out["type_ids"]}
    hits3 = []
    if strings:
        from rcf import hash_filename
        for s in strings:
            h = f"0x{hash_filename(s):08x}"
            if h in id_set:
                hits3.append({"id": h, "lua_string": s})
    xr["id_vs_datainit"] = {
        "lua_strings_tested": len(strings),
        "entry_type_ids_tested": len(id_set),
        "matches": hits3,
        "method": "tools/rcf.hash_filename over every datainit string constant vs every observed entry type id",
        "result": "no join established" if not hits3 else "join found",
    }

    # (4) do the same type ids recur across files (are they global constants)?
    per_id = collections.Counter()
    for f in files:
        seen = {r["type_id"] for o in records[f]["objects"] for r in o["records"]
                if r["type_id"] >= MIN_TYPE_ID}
        for t in seen:
            per_id[f"0x{t:08x}"] += 1
    xr["id_recurrence"] = {
        "ids": len(per_id),
        "ids_in_more_than_one_file": sum(1 for v in per_id.values() if v > 1),
        "max_files_per_id": max(per_id.values()) if per_id else 0,
        "detail": dict(per_id.most_common(15)),
    }
    return xr


# ------------------------------------------------------------------ reports
def write_json(report, path):
    clean = {k: v for k, v in report.items() if not k.startswith("_")}
    with open(path, "w") as fh:
        json.dump(clean, fh, indent=1, sort_keys=False)
        fh.write("\n")


def mdcell(value):
    return str(value).replace("|", "\\|")


def table_md(rows, header):
    out = ["| " + " | ".join(mdcell(h) for h in header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(mdcell(c) for c in r) + " |")
    return "\n".join(out)


def write_md(report, path):
    r = report
    L = []
    A = L.append
    A("# Mission scripting / placement vocabulary — corpus inventory\n")
    A("Generated by `tools/mission_inventory.py` (`all`). Every number below is")
    A("computed from the shipped archives; nothing is hand-typed.\n")
    A(f"- analysis set: **{r['set']}** = {len(r['missions'])} mission asset directories, "
      f"{r['files_analysed']} `.p3d` files, {human(r['analyzed_bytes'])} of p3d payload")
    if r.get("sample_crosscheck"):
        sc = r["sample_crosscheck"]
        A(f"- the fixed {sc['missions']}-mission sample (see §1.1) covers "
          f"**{100*sc['entry_type_coverage']:.0f}%** of the distinct entry types found in the "
          f"whole corpus — {len(sc['entry_types_missing_from_sample'])} types would have been "
          "missed by a sample-only study, which is why the tables below are corpus-wide")
    A(f"- chunks parsed: **{r['chunks_total']}**, distinct types **{r['chunk_type_count']}** "
      f"({r['chunk_type_unclassified']} unclassified)")
    A(f"- meta-objects: **{r['meta_objects']}** in {len(r['meta_classes'])} classes, "
      f"{r['meta_blocks']} META data blocks")
    A(f"- entry types: **{r['entry_type_count']}** distinct "
      f"({r['entry_type_observations']} observations), "
      f"**{r['combination_count_ordered']}** distinct ordered combinations "
      f"({r['combination_count_asset']} distinct as sets)")
    A(f"- placed objects: **{r['placed']['total']}** ({r['placed']['distinct']} distinct names)\n")

    A("## 1. Mission archive inventory\n")
    A("`analysis/mission-files.tsv` lists every entry under `art/missions/` in every shipped")
    A("archive (index read only, no extraction). Shape of the tree:\n")
    A("```")
    A("art/missions/<mission>/<variant>/<file>")
    A("  <variant> = active | open")
    A("  <file>    = fig.p3d.rz | prop_joined.p3d.rz | prop_sound.p3d | sound.p3d | tod.p3d.rz")
    A("```\n")
    A(f"- mission directories: **{r['mission_files']['missions']}** "
      f"({r['mission_files']['in_missiondef']} appear in `scripts/missiondefinit.lua`, "
      f"{r['mission_files']['not_in_missiondef']} do not)")
    A(f"- variant directories: **{r['mission_files']['variant_dirs']}** "
      f"({r['mission_files']['variant_sets']})")
    A(f"- files: **{r['mission_files']['files']}**, total **{human(r['mission_files']['bytes'])}** "
      f"of stored payload")
    A(f"- archives holding mission assets: {r['mission_files']['archives']}\n")
    A("Per-mission file kinds (count, stored bytes, mean per mission):\n")
    A(table_md([[k, v["files"], human(v["bytes"]), f"{v['files']/max(1,r['mission_files']['missions']):.2f}"]
                for k, v in r["mission_files"]["per_kind"].items()],
               ["file kind", "files", "stored bytes", "per mission"]))
    A("")
    A("Related assets that are *not* mission-dirs but hold the archetype/package definitions:")
    A("\n")
    A(table_md([[f"`{p['dir']}`", p["files"], human(p["bytes"])]
                for p in r["mission_files"]["packages"]],
               ["package directory", "files", "bytes"]))
    A("")
    A(f"{r['mission_files']['package_files']} package files, "
      f"{human(r['mission_files']['package_bytes'])} — counted from the archive indexes only,")
    A("never extracted (they are several hundred MiB). Sections 7.2 and 6 show how mission")
    A("objects reference them.\n")
    A("How much of each variant actually carries chunks (a `root_only` file is the 12-byte")
    A("P3D root and nothing else, i.e. an unpopulated layer):\n")
    A(table_md([[f"`{v['variant']}`", f"`{v['kind']}`", v["files"], v["root_only"],
                 v["files"] - v["root_only"], human(v["bytes"]), human(v["max_bytes"])]
                for v in r["variant_fill"]],
               ["variant", "file kind", "files", "root only", "with chunks", "bytes",
                "largest"]))
    A("")
    ol = r["open_layer"]
    struct_only = [m for m in ol.get("prop_joined", []) if not m.startswith("structure_")]
    A(f"The populated layer is `active/`. `open/` is empty for most missions, but not all: "
      f"**{len(ol.get('prop_joined', []))}** missions carry objects in "
      f"`open/prop_joined`, **{len(ol.get('tod', []))}** in `open/tod` and "
      f"**{len(ol.get('prop_sound', []))}** in `open/prop_sound`.")
    A(f"Those are overwhelmingly the `structure_*` (city block) assets — only "
      f"{len(struct_only)} of the {len(ol.get('prop_joined', []))} "
      f"`open/prop_joined` files belong to anything else "
      f"({', '.join('`' + m + '`' for m in struct_only)}). So `open/` is the shared")
    A("open-world placement layer, not a per-mission layer: an editor must treat it as")
    A("world state that several missions can share. Missions without a")
    A("`missiondefinit.lua` entry:")
    A(", ".join(f"`{m}`" for m in r["mission_files"]["not_in_missiondef_list"]) + ".\n")

    if r.get("sample_crosscheck"):
        sc = r["sample_crosscheck"]
        A("### 1.1 The fixed sample and what it would have missed\n")
        A(f"The tool ships a fixed {sc['missions']}-mission sample so that the shape of the")
        A("corpus can be re-checked quickly (`--set sample`, ~5 s instead of ~50 s). It is:")
        A("\n")
        A(table_md([[f"`{m['mission']}`", m["why"]] for m in r["sample_definition"]],
                   ["mission", "why it is in the sample"]))
        A("")
        A(table_md([["missions", sc["missions"], len(r["missions"])],
                    ["p3d files", sc["files"], r["files_analysed"]],
                    ["chunks", sc["chunks"], r["chunks_total"]],
                    ["distinct chunk types", sc["chunk_types"], r["chunk_type_count"]],
                    ["meta-objects", sc["meta_objects"], r["meta_objects"]],
                    ["distinct entry types", sc["entry_types"], r["entry_type_count"]],
                    ["distinct ordered combinations", sc["combinations"],
                     r["combination_count_ordered"]]],
                   ["metric", "20-mission sample", "whole corpus"]))
        A("")
        if sc["entry_types_missing_from_sample"]:
            A("Entry types absent from the sample (found only by scanning the whole corpus):")
            A(", ".join(f"`{t}`" for t in sc["entry_types_missing_from_sample"]) + ".\n")

    A("## 2. Distinct chunk types inside mission `.p3d` files\n")
    A(f"This is the complete set observed over the '{r['set']}' set "
      f"({r['chunks_total']} chunks). `files` counts distinct `.p3d` files containing the")
    A("type; `by_kind` splits the")
    A("occurrences over the five mission file kinds.\n")
    A("Classified types: `0xFF443350` = P3D root, `0x07F00000` = placed meta-object,")
    A("`0x07F00001` = its META data block. Everything else is **unclassified**: the ids are")
    A("reported exactly as they appear, with no guessed meanings. Grouped by their high")
    A("bytes they form a handful of families (counted below), which is where the engine's")
    A("subsystems separate — naming them is left to whoever owns the chunk taxonomy.\n")
    A(table_md([[f"`{f['prefix']}`", f["types"], f["chunks"]]
                for f in r["chunk_type_families"]],
               ["type prefix", "distinct types", "chunks"]))
    A("")
    A("Full set, highest count first:\n")
    A(table_md([[f"`{t['type']}`", t["count"], t["files"],
                 ", ".join(f"{k}×{v}" for k, v in list(t["by_kind"].items())[:3]),
                 t["known"] or "**unclassified**"] for t in r["chunk_types"]],
               ["chunk type", "count", "files", "top file kinds", "status"]))
    A("")

    A("## 3. Meta-object classes (the placed-object containers)\n")
    A("Each placed object is chunk `0x07F00000` whose header is three `aligned_u8`")
    A("strings — `Class(slot):InstanceName`, `InstanceName`, `Class` — followed by an")
    A("eight byte class tag `u16 1, u16 flag, u32 class id`. `objects_without_records`")
    A("marks classes whose")
    A("META block uses a class-specific layout instead of the record grammar below, so")
    A("their internals are not part of the entry vocabulary.\n")
    A(table_md([[f"`{c['class']}`", c["objects"], c["files"],
                 c["objects_with_records"], c["objects_without_records"]]
                for c in r["meta_classes"]],
               ["class", "objects", "files", "with records", "without records"]))
    A("")
    A("The eight bytes after the class string are a stable class tag")
    A("`u16 1, u16 layer, u32 class id` — the id is constant per class across the whole")
    A("corpus, so it is the value an editor must write when creating an object of that")
    A("class. The middle word is `1` only for 931 `GameObject` instances, all of them in")
    A("`prop_joined.p3d`; `GameObject` is the only class present in both placement layers, so")
    A("for it (and only it) that word separates the layers with no exceptions in this corpus:\n")
    rows = []
    for c in r["meta_classes"]:
        tags = r["class_tags"].get(c["class"], {})
        for tag, n in tags.items():
            layer = r["class_tags_by_kind"].get(tag, {})
            rows.append([f"`{c['class']}`", f"`{tag}`", n,
                         ", ".join(f"{k}×{v}" for k, v in layer.items())])
    A(table_md(rows, ["class", "class tag", "objects", "placement layer"]))
    A("")
    A(f"Meta-object header parse failures: {r['meta_header_failures']} "
      f"(all three `aligned_u8` strings present; none observed in this set)"
      if r["meta_header_failures"] == 0 else
      f"Meta-object header parse failures: {r['meta_header_failures']}")
    A("")

    A("## 4. Entry / attribute type vocabulary\n")
    A(f"**{r['entry_type_count']} distinct type names**, "
      f"{r['entry_type_observations']} observations. Frequency is the number of META")
    A("records carrying that type across every object and file in the analysis set. `type id` is the")
    A("32-bit registry id written after the name (stable per name across all files).\n")
    A(table_md([[f"`{e['name']}`", e["count"], e["files"], len(e["missions"]),
                 ", ".join(f"`{i}`" for i in e["type_ids"]),
                 ", ".join(f"{k}×{v}" for k, v in list(e["by_kind"].items())[:2])]
                for e in r["entry_types"]],
               ["type name", "count", "files", "missions", "type id", "file kinds"]))
    A("")
    multi = [t for t in r["type_ids"] if len(t["names"]) > 1]
    A(f"Ids carrying more than one name: **{len(multi)}**. " +
      ("They are: " + "; ".join(f"`{m['id']}` → " +
                                ", ".join(f"`{n}`×{c}" for n, c in m["names"].items())
                                for m in multi) + ". " if multi else "") +
      "The names are all real behaviour classes; the shared id means the id identifies a")
    A("serializer/registry entry, not a unique class name (a derived behaviour is written")
    A("with its base behaviour's id). This is reported, not explained.\n")
    A("### Rejected candidates\n")
    A("Strings that matched the record grammar but whose trailing word is too small to be a")
    A("registry id (`< 0x01000000`) — these are class-specific string tables, i.e. *values*,")
    A("so they are excluded from the vocabulary but listed for completeness:\n")
    A(table_md([[f"`{c['name']}`", c["count"]] for c in r["rejected_candidates"]],
               ["rejected string", "count"]))
    A("")

    A("### Named attributes (explicit attribute names)\n")
    A("A record may carry an explicit attribute name in front of its type, e.g.")
    A("`transform` + `MatrixAttribute`, `packageName` + `NameAttribute`. These are listed")
    A("separately because the grammar cannot always separate such a name from a *value* of")
    A("the preceding attribute: `NameAttribute`'s value is itself a `u32`-length string, so")
    A("it sits in the same position. `looks_like_package_value` marks the strings that")
    A("equal the mission package name (`<mission>_<variant>`) and are therefore provably")
    A("values, not names.\n")
    named_rows = [n for n in r["named_attributes"] if not n["looks_like_package_value"]]
    common = [n for n in named_rows if n["count"] >= 3][:40]
    A(table_md([[f"`{n['name']}`", n["count"],
                 ", ".join(f"`{k}`" for k in list(n["pairs"])[:2])]
                for n in common],
               ["explicit name", "count", "type(s)"]))
    A("")
    A(f"Names occurring at least three times are listed above ({len(common)} rows); the")
    A(f"other {len(named_rows) - len(common)} distinct names occur once or twice each — they")
    A("are mostly per-instance smart-node spot names (`cage_back_0`, `listener_1`, ...). The")
    A("full list is in `analysis/mission-vocabulary.json` under `named_attributes`.\n")
    A(f"Strings that look like package values (excluded): "
      f"{sum(n['count'] for n in r['named_attributes'] if n['looks_like_package_value'])} "
      f"observations over "
      f"{sum(1 for n in r['named_attributes'] if n['looks_like_package_value'])} distinct strings.\n")
    c = r["named_count_checks"]
    A(f"Grammar cross-check: the `u32` after `META` equals the number of explicitly named")
    A(f"records in **{c['agree_nopkg']} of {c['bodies']}** META blocks with records "
      f"({100*c['agree_nopkg']/max(1,c['bodies']):.1f}%) once the provable package values are")
    A(f"excluded, and in {c['agree']} of {c['bodies']} "
      f"({100*c['agree']/max(1,c['bodies']):.1f}%) counting every recovered name. The residue is")
    A("`NameAttribute` values (package and object names) that occupy the same byte position")
    A("as an explicit attribute name, plus a few `EndTriggerMessageRef` style records that")
    A("carry names the count field does not include. This is why the named-attribute table")
    A("is marked provisional and the type vocabulary — which does not depend on it — is the")
    A("load-bearing result.\n")

    A("## 5. Distinct entry-name combinations\n")
    A(f"Ordered type lists per META block: **{r['combination_count_ordered']}** distinct")
    A(f"({r['combination_count_asset']} distinct as unordered sets), over "
      f"{sum(c['count'] for c in r['combinations'])} objects with records.\n")
    def compress(types):
        out, i = [], 0
        while i < len(types):
            j = i
            while j < len(types) and types[j] == types[i]:
                j += 1
            n = j - i
            out.append(f"`{types[i]}`" + (f" ×{n}" if n > 1 else ""))
            i = j
        return " + ".join(out)

    A(table_md([[i + 1, c["count"], compress(c["types"])]
                for i, c in enumerate(r["combinations"][:60])],
               ["#", "objects", "entry types (serialization order, runs collapsed)"]))
    if len(r["combinations"]) > 60:
        A(f"\n_Showing 60 of {len(r['combinations'])}; the full list is in "
          "`analysis/mission-vocabulary.json`._")
    A("")

    A("## 6. Placed-object naming\n")
    A(f"**{r['placed']['total']}** placed objects carry a name, **{r['placed']['distinct']}** "
      "distinct. Distribution over classes:\n")
    A(table_md([[f"`{c['class']}`", c["objects"], c["distinct"],
                 ", ".join(f"`{e}`" for e in c["examples"])]
                for c in r["placed_classes"]],
               ["class", "objects", "distinct", "examples"]))
    A("")
    A(table_md([[f"`{k['kind']}`", k["objects"], k["distinct"]]
                for k in r["placed_kinds"]], ["file kind", "objects", "distinct names"]))
    A("")
    A("Naming families:\n")
    A(table_md([[f["family"], f["count"], ", ".join(f"`{e}`" for e in f["examples"])]
                for f in r["placed_families"]["buckets"]],
               ["family", "names", "examples"]))
    A("")
    A("Names that resolve to a *sibling* placed object in the same file (engine-synthesised")
    A("helper objects, measured, not guessed):\n")
    A(f"{r['placed_derived']['resolved_to_a_sibling_object']} of "
      f"{r['placed_derived']['names_examined']} distinct names examined resolve to one. Suffixes:\n")
    A(table_md([[f"`{s['suffix']}`", s["count"],
                 ", ".join(f"`{e}`" for e in s["examples"])]
                for s in r["placed_derived"]["suffixes"][:30]],
               ["suffix appended to a sibling object name", "names", "examples"]))
    A("")
    A("Most frequent name stems (text before the first digit):\n")
    A(table_md([[f"`{s['stem']}`", s["count"]] for s in r["placed_families"]["stems"][:25]],
               ["stem", "names"]))
    A("")

    A("## 7. Archetype / package references\n")
    xr = r["cross_reference"]
    pn = xr["package_name_values"]
    A("### 7.1 `packageName` → mission package (established)\n")
    A(f"- distinct package-name values recovered: **{pn['distinct']}**")
    A(f"- records whose value equals `<mission dir>_<variant>` of the file it sits in: "
      f"**{pn['match_own_package']} of {pn['observed']}**")
    A(f"- records whose value names a *different* package — a genuine cross-package "
      f"reference: **{pn['observed'] - pn['match_own_package']}**")
    A(f"- method: {pn['method']}")
    if pn["cross_package_values"]:
        A("- the cross-package values: " +
          ", ".join(f"`{v}`×{n}" for v, n in pn["cross_package_values"].items()))
    A("")
    A("### 7.2 Template / builder → placed object (established)\n")
    tr = xr["template_references"]
    A(f"- child names recovered from `GameObjectTemplateBuilder`/`PropTemplate` META bodies: "
      f"**{tr['observed']}** observations, **{tr['distinct_children']}** distinct "
      f"({', '.join(f'{k}×{v}' for k, v in tr['by_class'].items())})")
    A(f"- resolved to a named object in the same file: "
      f"**{tr['resolved_to_a_named_object_in_same_file']} of {tr['observed']}**")
    A(f"- method: {tr['method']}")
    if tr["examples"]:
        A("- examples: " + ", ".join(f"`{e['template']}` → `{e['child']}`"
                                     for e in tr["examples"][:6]))
    A("")
    A("### 7.3 32-bit ids vs `datainit.lua` string constants (NOT established)\n")
    iv = xr["id_vs_datainit"]
    A(f"- datainit string constants tested: {iv['lua_strings_tested']}")
    A(f"- entry type ids tested: {iv['entry_type_ids_tested']}")
    A(f"- matches: **{len(iv['matches'])}** — {iv['result']}")
    A(f"- method: {iv['method']}")
    A("- no u32 seen inside the META payloads could be joined to a `datainit.lua` string")
    A("  constant by that hash, and `datainit.lua` contains no numeric id table, so no")
    A("  numeric archetype-id join is claimed here.\n")
    ir = xr["id_recurrence"]
    A(f"### 7.4 Are the type ids global constants?\n")
    A(f"- ids observed: {ir['ids']}; ids appearing in more than one file: "
      f"**{ir['ids_in_more_than_one_file']}**; max files per id: {ir['max_files_per_id']}.")
    A("  The same id always accompanies the same name, so the ids are global registry")
    A("  constants, not per-file indices.\n")

    A("## 8. Unknowns\n")
    for u in r["unknowns"]:
        A(f"- {u}")
    A("")
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")


# --------------------------------------------------------------- commands
def cmd_list(args):
    idx = mission_index()
    os.makedirs(ANALYSIS, exist_ok=True)
    rows = [(m, v, f, s, a) for m in sorted(idx) for v in sorted(idx[m])
            for f, s, a in idx[m][v]]
    with open(os.path.join(ANALYSIS, "mission-files.tsv"), "w") as fh:
        fh.write("mission\tvariant\tfile\tsize\tarchive\n")
        for row in rows:
            fh.write("\t".join(str(x) for x in row) + "\n")
    total = sum(r[3] for r in rows)
    per_kind = collections.Counter()
    per_kind_bytes = collections.Counter()
    for _m, _v, f, s, _a in rows:
        per_kind[kind_of(f)] += 1
        per_kind_bytes[kind_of(f)] += s
    archives = collections.Counter(r[4] for r in rows)
    print(f"archives scanned          {len(ARCHIVES)}")
    print(f"mission directories       {len(idx)}")
    print(f"variant directories       {sum(len(idx[m]) for m in idx)}")
    print(f"mission files             {len(rows)}")
    print(f"total size                {human(total)}")
    print(f"archives holding missions {dict(archives)}")
    print("per-mission file kinds:")
    for k, n in per_kind.most_common():
        print(f"  {k:10} {n:5} files {human(per_kind_bytes[k]):>11}")
    print(f"wrote {os.path.join(ANALYSIS, 'mission-files.tsv')} ({len(rows)} rows)")
    return 0


def cmd_sample(args):
    # always the fixed sample: that is what this command exists to make auditable
    selections, idx = select_sample()
    print(f"fixed sample set   ({len(selections)} of {len(idx)} mission directories; "
          f"--set all analyses all {len(idx)})")
    tf = tb = 0
    for mission, why in selections:
        files = sum(len(v) for v in idx[mission].values())
        size = sum(s for v in idx[mission].values() for _f, s, _a in v)
        tf += files
        tb += size
        print(f"  {mission:34} {'+'.join(sorted(idx[mission])):12} {files:3} files "
              f"{human(size):>11}  {why}")
    print(f"  {'':34} {'':12} {tf:3} files {human(tb):>11}  (index sizes, pre-unwrap)")
    return 0


def cmd_chunks(args):
    records, selections, idx = collect(args.set)
    report = build(records, selections, idx, args.set)
    print(f"set={args.set}  {len(selections)} missions  {report['files_analysed']} p3d files  "
          f"{report['chunks_total']} chunks  {report['chunk_type_count']} distinct types")
    print(f"{'chunk type':12} {'count':>8} {'files':>6}  status")
    for t in report["chunk_types"]:
        print(f"{t['type']:12} {t['count']:8} {t['files']:6}  "
              f"{t['known'] or 'UNCLASSIFIED'}")
    print(f"\nunclassified: {report['chunk_type_unclassified']} of {report['chunk_type_count']}")
    for e in report["parse_errors"]:
        print(f"parse error: {e['file']}: {e['error']}")
    return 0


def cmd_names(args):
    records, selections, idx = collect(args.set)
    report = build(records, selections, idx, args.set)
    print(f"placed objects named {report['placed']['total']}  distinct {report['placed']['distinct']}")
    for c in report["placed_classes"]:
        print(f"  {c['class']:26} {c['objects']:6} objects {c['distinct']:6} distinct  "
              f"{', '.join(c['examples'][:3])}")
    print("families:")
    for f in report["placed_families"]["buckets"]:
        print(f"  {f['count']:6}  {f['family']}  e.g. {f['examples'][:2]}")
    return 0


UNKNOWNS = [
    "The `u32` after `META` and the `u16 1` before each type id are not explained: they "
    "behave like a named-record count and a version/flags word (the count matches in the "
    "majority of blocks) but this was not proven against engine code.",
    "Per-type parameter layouts are *not* decoded here. Every record's parameters are "
    "skipped by scanning to the next anchor, so transform matrices, patrol points, trigger "
    "volumes and string values are located but not interpreted. Decoding them is the "
    "separate attribute-value problem.",
    "Classes whose META block uses a class-specific layout (`objects_without_records` in "
    "table 3) are opaque: their body holds no record grammar, so no vocabulary is "
    "extracted from them.",
    "The 32-bit type ids are stable global constants, but the hash/registry that produces "
    "them is unknown: neither `tools/rcf.hash_filename`, CRC32, FNV1/1a, djb2, sdbm nor "
    "poly31/33/131 reproduce them, and two distinct names share one id.",
    "No numeric archetype/package id join to `datainit.lua` was established (table 7.3). "
    "The only proven references are string-valued: `packageName` and template child names.",
    "The `open/` layer is shared world state: 35 missions carry objects there, 33 of them "
    "`structure_*` city-block assets. Nothing here says which mission ultimately loads a "
    "given `open/` file at runtime, so the ownership of that layer is still open.",
]


def cmd_all(args):
    records, selections, idx = collect(args.set)
    report = build(records, selections, idx, args.set)
    report["cross_reference"] = cross_reference(records, sorted(records), report)
    report["mission_files"] = summarize_index(idx)
    report["unknowns"] = UNKNOWNS
    report["sample_definition"] = [{"mission": m, "why": w} for m, w in SAMPLE]
    if args.set == "all":
        # also run the fixed 20-mission sample so the headline document states
        # both the complete numbers and the sample numbers, side by side.
        srec, ssel, sidx = collect("sample")
        srep = build(srec, ssel, sidx, "sample")
        report["sample_crosscheck"] = {
            "missions": len(ssel), "files": srep["files_analysed"],
            "analyzed_bytes": srep["analyzed_bytes"],
            "chunks": srep["chunks_total"], "chunk_types": srep["chunk_type_count"],
            "meta_objects": srep["meta_objects"], "meta_classes": len(srep["meta_classes"]),
            "entry_types": srep["entry_type_count"],
            "entry_type_observations": srep["entry_type_observations"],
            "combinations": srep["combination_count_ordered"],
            "combinations_as_sets": srep["combination_count_asset"],
            "placed": srep["placed"]["total"], "placed_distinct": srep["placed"]["distinct"],
            "entry_type_coverage": len(srep["entry_types"]) / max(1, report["entry_type_count"]),
            "entry_types_missing_from_sample": sorted(
                {e["name"] for e in report["entry_types"]} -
                {e["name"] for e in srep["entry_types"]}),
        }
    suffix = "" if args.set == "all" else "-" + args.set
    os.makedirs(ANALYSIS, exist_ok=True)
    json_path = os.path.join(ANALYSIS, f"mission-vocabulary{suffix}.json")
    md_path = os.path.join(ANALYSIS, f"mission-vocabulary{suffix}.md")
    write_json(report, json_path)
    write_md(report, md_path)
    print(f"set={args.set} missions={len(selections)} files={report['files_analysed']} "
          f"chunk_types={report['chunk_type_count']} "
          f"entry_types={report['entry_type_count']} combinations={report['combination_count_ordered']}")
    print(f"wrote {os.path.relpath(json_path, ROOT)}")
    print(f"wrote {os.path.relpath(md_path, ROOT)}")
    return 0


def _mission_names():
    """The mission list, read from missiondefinit in the archive - no data file in the repository.

    `missions/ALL-MISSIONS.txt` used to be the source; the names there are exactly the top-level protos of
    scripts/missiondefinit.lua whose first constant is a mission name, which the archive already holds.
    """
    import lua51
    import mission as MISSION_MOD
    try:
        with Rcf(os.path.join(GAME, "boot.rcf")) as arc:
            for e in arc.entries:
                if (e.name or "").replace("\\", "/").lower().endswith("scripts/missiondefinit.lua"):
                    root = lua51.load(arc.read(e))
                    return [name for _p, name, _proto in MISSION_MOD.mission_protos(root)]
    except Exception:
        pass
    return []


def summarize_index(idx):
    files = sum(len(idx[m][v]) for m in idx for v in idx[m])
    per_kind = collections.OrderedDict()
    for m in idx:
        for v, entries in idx[m].items():
            for f, s, _a in entries:
                k = kind_of(f)
                d = per_kind.setdefault(k, {"files": 0, "bytes": 0})
                d["files"] += 1
                d["bytes"] += s
    names = _mission_names()
    archives = collections.Counter()
    for m in idx:
        for v in idx[m]:
            for _f, _s, a in idx[m][v]:
                archives[a] += 1
    not_def = sorted(set(idx) - set(names))
    pkgs = package_index()
    pkg_by_dir = collections.Counter()
    pkg_bytes = collections.Counter()
    for name, size, _arc in pkgs:
        parts = name.split("/")
        pkg_by_dir["/".join(parts[:3])] += 1
        pkg_bytes["/".join(parts[:3])] += size
    return {
        "packages": [{"dir": k, "files": pkg_by_dir[k], "bytes": pkg_bytes[k]}
                     for k in sorted(pkg_by_dir)],
        "package_files": len(pkgs),
        "package_bytes": sum(s for _n, s, _a in pkgs),
        "missions": len(idx),
        "in_missiondef": len(set(idx) & set(names)),
        "not_in_missiondef": len(not_def),
        "not_in_missiondef_list": not_def,
        "variant_dirs": sum(len(idx[m]) for m in idx),
        "variant_sets": "every mission has both `active` and `open` once the two archives "
                        "are merged (story_intro1 comes from boot.rcf, the rest from art.rcf)",
        "files": files,
        "bytes": sum(s for m in idx for v in idx[m].values() for _f, s, _a in v),
        "per_kind": per_kind,
        "archives": dict(archives),
    }


def main(argv=None):
    config.require_game_dir()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["list", "sample", "chunks", "vocab", "names", "all"])
    ap.add_argument("--set", default="all", choices=["all", "sample"],
                    help="`all` = every mission asset directory (the deliverable), "
                         "`sample` = the fixed 20-mission sample, written to *-sample.*")
    args = ap.parse_args(argv)
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(CORPUS, exist_ok=True)
    return {"list": cmd_list, "sample": cmd_sample, "chunks": cmd_chunks,
            "vocab": cmd_all, "names": cmd_names, "all": cmd_all}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
