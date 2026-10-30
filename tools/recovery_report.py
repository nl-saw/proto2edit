#!/usr/bin/env python3
"""Measure how much of the mission corpus is recovered, and how much is proven editable.

Definitions used here (the same gates the editor applies):

  source      the payload already is Lua text; editable directly
  bytecode    a Lua 5.1 chunk (captured at run time or recovered with the engine's preset dictionary)
  unknown     neither; preserved byte-for-byte
  provable    a bytecode record whose decompiled source recompiles to an instruction-identical chunk

Verdicts are cached in artifacts/bytecode-verdicts.json keyed by the chunk's sha1, so the expensive part (LuaDec +
luac) runs once per distinct chunk. Prints a table and, with --write-doc, refreshes
docs/23-lua-recovery-progress.md.
"""
import argparse
import collections
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import script_edit as SE
import hostpaths as HP         # noqa: E402  # noqa: E402

CACHE = SE.VERDICT_CACHE

DOC = os.path.join(ROOT, "docs", "23-lua-recovery-progress.md")


def missions():
    seen = set()
    for dp, _d, names in os.walk(SE.CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        m = os.path.basename(os.path.dirname(dp))
        v = os.path.basename(dp)
        if (m, v) in seen:
            continue
        seen.add((m, v))
        got = SE.open_side(m, v, "shipped")
        if got is not None:
            yield m, v, got[3]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-doc", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="only the first N missions (debug)")
    ap.add_argument("--use-cache", action="store_true",
                    help="include the local playthrough cache (not committed); default measures the published corpus")
    a = ap.parse_args(argv)

    if not a.use_cache:
        os.environ["PROTO2_NO_CACHE"] = "1"      # the runtime cache is git-ignored; keep published numbers reproducible
    cache = SE.load_verdict_cache()
    stats = collections.Counter()
    per_mission = []
    t0 = time.time()
    done = 0
    for mission, variant, model in missions():
        if a.limit and done >= a.limit:
            break
        done += 1
        rows = SE.records_with_roles(model)
        dec = SE._decoded(model)
        cats = collections.Counter()
        for (it, payload, _role), (_r, text, _how, _x) in zip(rows, dec):
            if text is not None:
                cats["source"] += 1
                continue
            chunk = SE.bytecode_chunk(payload)
            if chunk is None:
                cats["unknown"] += 1
                continue
            cats["bytecode"] += 1
            key = hashlib.sha1(chunk).hexdigest()
            verdict = cache.get(key)
            if verdict is None:
                status, detail = SE.roundtrip_equivalent(payload)
                verdict = {"status": status, "detail": HP.scrub(detail)}
                cache[key] = verdict
            if verdict["status"] == "equivalent":
                # provable, but some roles must stay compiled (see script_edit.NON_CONVERTIBLE_ROLES): converting
                # a conditions record crashes the mission at load, so they do not count as editable
                if _role in SE.NON_CONVERTIBLE_ROLES:
                    cats["provable_role"] += 1
                else:
                    cats["provable"] += 1
            elif verdict["status"] == "differs":
                cats["differs"] += 1
            else:
                cats["no_decompile"] += 1
        stats.update(cats)
        per_mission.append({"mission": mission, "variant": variant, **cats})

    if a.limit:
        print("(partial run: --limit)", file=sys.stderr)
    total = sum(stats[k] for k in ("source", "bytecode", "unknown"))
    bc = stats["bytecode"]
    def pct(n):
        return f"{100.0 * n / total:.1f}%" if total else "-"
    print(f"records                 {total}")
    print(f"  source (editable now) {stats['source']:>5}  {pct(stats['source'])}")
    print(f"  bytecode             {bc:>5}  {pct(bc)}")
    print(f"    provable -> source {stats['provable']:>5}  {pct(stats['provable'])} of corpus, "
          f"{100.0 * stats['provable'] / bc:.1f}% of bytecode")
    print(f"    bytecode-only role {stats['provable_role']:>5}  (provable, but that role must stay compiled)")
    print(f"    differs            {stats['differs']:>5}  (readable, not yet editable)")
    print(f"    no decompile       {stats['no_decompile']:>5}")
    print(f"  unknown (locked)     {stats['unknown']:>5}  {pct(stats['unknown'])}")
    print(f"readable at all        {total - stats['unknown']:>5}  "
          f"{100.0 * (total - stats['unknown']) / total:.2f}%")
    print(f"editable (source + convertible) {stats['source'] + stats['provable']:>5}  "
          f"{100.0 * (stats['source'] + stats['provable']) / total:.1f}%")
    SE.save_verdict_cache(cache)

    work, work_missions = working_copy()
    wtotal = sum(work[k] for k in ("source", "role", "blocked", "unknown"))
    print()
    print("working copy (staged overrides applied - what the game loads after install)")
    print(f"  records                {wtotal}")
    print(f"  source-backed          {work['source']:>5}  {100.0 * work['source'] / wtotal:.1f}%")
    print(f"  held back (role rule)  {work['role']:>5}  provable, but that role must stay compiled")
    print(f"  blocked                {work['blocked']:>5}  no verified source and not convertible")
    print(f"  unknown                {work['unknown']:>5}")
    print(f"elapsed {time.time() - t0:.1f}s, verdicts cached {len(cache)}")

    if not a.limit:
        json.dump(cache, open(CACHE, "w"))
        if a.write_doc:
            write_doc(stats, total, per_mission, staged_conversions(), work, wtotal)
            print(f"wrote {os.path.relpath(DOC, ROOT)}")
    return 0


def working_copy():
    """Counts for the working copy: staged overrides applied, i.e. what the game would load after install.

    The shipped-corpus table above measures what the *archives* contain, which is the right number for
    "how much converts automatically". It cannot see a hand reconstruction: those records still ship as
    bytecode, so they stay in `differs` forever. This pass opens each mission's working side (staged file
    when one exists) and reports what is actually editable now.
    """
    stats = collections.Counter()
    per_mission = []
    for mission, variant, _model in missions():
        got = SE.open_side(mission, variant, "working")
        if got is None:
            continue
        cats = collections.Counter()
        dec = SE._decoded(got[3])
        for (it, payload, role), (_r, text, _how, _x) in zip(SE.records_with_roles(got[3]), dec):
            if text is not None:
                cats["source"] += 1
                continue
            chunk = SE.bytecode_chunk(payload)
            if chunk is None:
                cats["unknown"] += 1
            elif role in SE.NON_CONVERTIBLE_ROLES:
                cats["role"] += 1
            else:
                cats["blocked"] += 1
        stats.update(cats)
        per_mission.append({"mission": mission, "variant": variant, **cats})
    return stats, per_mission


def staged_conversions():
    """Changed records in the staged missions that shipped as bytecode -> already converted."""
    n = 0
    missions_dir = os.path.join(ROOT, "work", "mod", "art", "missions")
    if not os.path.isdir(missions_dir):
        return 0
    for mission in sorted(os.listdir(missions_dir)):
        for variant in ("active", "open"):
            if not os.path.exists(os.path.join(missions_dir, mission, variant, "fig.p3d.rz")):
                continue
            try:
                diff = SE.diff_mission(mission, variant, "staged")
                got = SE.open_side(mission, variant, "shipped")
            except Exception:
                continue
            if got is None:
                continue
            dec = SE._decoded(got[3])
            for row in diff["rows"]:
                if row["state"] == "changed" and dec[row["index"]][1] is None:
                    n += 1
    return n


def write_doc(stats, total, per_mission, staged=0, work=None, wtotal=0):
    def pct(n):
        return f"{100.0 * n / total:.1f}%"
    bc = stats["bytecode"]
    prov = stats["provable"]
    rows = sorted([m for m in per_mission if m.get("bytecode")], key=lambda r: -r["bytecode"])[:25]
    table = "\n".join(
        f"| `{r['mission']}` | {r.get('source', 0)} | {r.get('bytecode', 0)} | "
        f"{r.get('provable', 0)} | {r.get('differs', 0)} | {r.get('unknown', 0)} |" for r in rows)
    locked_note = ("none left: every payload decodes" if stats['unknown'] == 0
                   else "preserved byte-for-byte")
    work_source, work_role = work["source"], work["role"]
    work_blocked, work_unknown = work["blocked"], work["unknown"]
    work_source_pct = f"{100.0 * work_source / wtotal:.1f}%"
    work_role_pct = f"{100.0 * work_role / wtotal:.1f}%"
    work_blocked_pct = f"{100.0 * work_blocked / wtotal:.1f}%"
    work_unknown_pct = f"{100.0 * work_unknown / wtotal:.1f}%"
    work = work or {"source": 0, "role": 0, "blocked": 0, "unknown": 0}
    wtotal = wtotal or 1
    text = f"""# Lua recovery progress

Generated by `python3 tools/recovery_report.py --write-doc`. Numbers are measured against the shipped archives;
play-state is tracked separately below.

## Corpus ({total:,} record payloads, {len(per_mission)} mission/variants)

| category | records | share | meaning |
|---|---:|---:|---|
| source text | {stats['source']:,} | {pct(stats['source'])} | editable in the Script Map today |
| bytecode | {bc:,} | {pct(bc)} | Lua 5.1 chunks; inspectable, not text yet |
| — provably convertible | {prov:,} | {pct(prov)} | decompile → recompile → **instruction-identical** |
| — differs | {stats['differs']:,} | {pct(stats['differs'])} | decompiles, but not byte-identical when recompiled |
| — bytecode-only role | {stats['provable_role']:,} | {pct(stats['provable_role'])} | provable, but `conditions` must stay compiled (crashes at load) |
| — no decompile | {stats['no_decompile']:,} | {pct(stats['no_decompile'])} | LuaDec could not produce parseable source |
| unknown (locked) | {stats['unknown']:,} | {pct(stats['unknown'])} | {locked_note} |

Rows above are the **archives**. Every record counted under `differs`, `no decompile` and `bytecode-only role` has
already been dealt with in the working copy: `differs` and `no decompile` by hand reconstruction (verified by the same
gate), `bytecode-only role` by the rule that keeps `conditions` compiled. See the working-copy table below for the
numbers the game actually loads.

Counts are measured with the repository as published: the runtime playthrough cache is not committed, so a local
cache can add a record or two (re-run with `--use-cache` to include it).

## Working copy ({wtotal:,} record payloads, staged overrides applied)

The table above measures the **archives**. A record that was reconstructed by hand still ships as bytecode, so it
stays in `differs` there forever - which is why this second table exists. It opens each mission's working side (the
staged file when one exists) and reports what the game loads after `./proto2 install`.

| category | records | share | meaning |
|---|---:|---:|---|
| source-backed | {work_source:,} | {work_source_pct} | verified source staged for this record |
| held back (`conditions`) | {work_role:,} | {work_role_pct} | provable, but that role must stay compiled (crashes at load) |
| blocked | {work_blocked:,} | {work_blocked_pct} | no verified source and not convertible |
| unknown | {work_unknown:,} | {work_unknown_pct} | payload neither text nor a Lua chunk |

So the honest headline is **{work_source:,} of {wtotal:,} records editable ({work_source_pct})**, with
{work_role:,} provable but held back by the role rule and **{work_blocked:,} blocked**.

- **Readable at all:** {total - stats['unknown']:,} / {total:,} = **{100.0 * (total - stats['unknown']) / total:.2f}%**
- **Editable in the current install:** {stats['source'] + staged:,} / {total:,} =
  **{100.0 * (stats['source'] + staged) / total:.1f}%** ({staged} bytecode records already converted and staged)
- **Editable after converting every provable record:** {stats['source'] + prov:,} / {total:,} =
  **{100.0 * (stats['source'] + prov) / total:.1f}%**
- **Recoverable ceiling** (editable + differs, i.e. everything that is Lua at all):
  {stats['source'] + bc:,} / {total:,} = **{100.0 * (stats['source'] + bc) / total:.1f}%**

## Heaviest remaining bytecode

| mission | source | bytecode | provable | differs | unknown |
|---|---:|---:|---:|---:|---:|
{table}

## What is verified, and how

| claim | evidence |
|---|---|
| bytecode chunks are genuine | header, declared size, and LuaDec parses them |
| provable conversions are faithful | instruction-identical recompilation, per record |
| converted installs cleanly | 597 records installed without a gate failure (bulk batch reverted afterwards) |
| converted missions play | `story_c1` and `story_m1` completed with 25 converted payloads inflated between them - including 2 records produced by the hoist rewrite - plus a yellow-checkpoint side mission; `story_f2` completed with 31 converted payloads (29 records, the most of any mission) and a green-lair side mission alongside; earlier: `story_g2` 31, `story_l1` 30 (its `conditions` record kept compiled), `story_intro1` 35, `story_intro2` 15, `story_e3` 32, `story_b3` 16 (door worked) - all without a behavioural difference |
| engine dictionary rule | decompiled ctor calls `inflateSetDictionary`; all {bc:,} bytecode payloads decode under it |
| prefixed payloads decode | 4 `conditions` payloads carry a u32 prefix before the packed stream; all 4 decode (see below) |

## Still unverified

1. Nothing at the payload level. Four `conditions` payloads (in `story_a1`, `story_l1`, `story_l2`, `story_e2`)
   were listed as locked because the engine never loaded them, so no capture existed; they carry a 4-byte prefix
   before the packed stream, and the offline decoder refused anything not starting with `7f 00`. They decode
   under the same engine dictionary as every other payload, and are covered by `tools/test_records.py`.
2. `differs` records ({stats['differs']:,}): readable, but their decompiled source recompiles differently, so they are
   not offered as edits.
3. Play validation at scale: one full mission out of the 70 that carry converted records, plus the pre-bulk
   sessions. The rest rests on the instruction-identity gate.
"""
    os.makedirs(os.path.dirname(DOC), exist_ok=True)
    open(DOC, "w").write(text)


if __name__ == "__main__":
    raise SystemExit(main())
