#!/usr/bin/env python3
"""Convert every provably equivalent bytecode record, mission by mission, aborting on the first failure.

This is the bulk smoke test: each mission goes through `script_edit`'s existing gates (decompile -> recompile ->
instruction-identical, untouched payloads byte-identical), and any gate failure stops the whole run rather than
leaving a half-converted corpus.

Take a snapshot first:  ./proto2 snapshot create --label pre-bulk
Then:                    ./proto2 convert-all
and install afterwards:  ./proto2 install
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import script_edit as SE  # noqa: E402


def missions_with_bytecode():
    if not os.path.isdir(SE.CORPUS) or not any(
            "fig.p3d.rz" in names for _dp, _d, names in os.walk(SE.CORPUS)):
        raise SystemExit("no extracted corpus under tmp/corpus - run ./proto2 corpus to rebuild it")
    out = []
    for dp, _d, names in os.walk(SE.CORPUS):
        if "fig.p3d.rz" not in names:
            continue
        m = os.path.basename(os.path.dirname(dp))
        v = os.path.basename(dp)
        if (m, v) in [(x[0], x[1]) for x in out]:
            continue
        got = SE.open_side(m, v, "shipped")
        if got is None:
            continue
        if any(SE.bytecode_chunk(p) is not None
               for (it, p, _r), (_rl, t, _h, _x) in zip(SE.records_with_roles(got[3]), SE._decoded(got[3]))
               if t is None):
            out.append((m, v))
    return sorted(out)


def staged_missions():
    """(mission, variant) for every mission file that is actually staged.

    Deriving the report from the staging directory rather than from `missions_with_bytecode()` avoids a
    resolution quirk that silently dropped a mission (rz_cr_02) from the counts.
    """
    root = os.path.join(SE.MOD, "art", "missions")
    out = []
    for dp, _dirs, names in os.walk(root):
        if "fig.p3d.rz" not in names:
            continue
        parts = os.path.relpath(dp, root).split(os.sep)
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return sorted(out)


def refresh_report(todo, path):
    """Recompute the report from the staging directory.

    The report used to record what a conversion run did, which went stale as soon as a record was reverted (the
    three `conditions` records that must stay compiled). This derives the numbers from the files staged now, and
    separately counts the records the role rule deliberately leaves as bytecode.
    """
    report, total, kept_compiled = [], 0, 0
    for mission, variant in staged_missions():
        try:
            shipped = SE.open_side(mission, variant, "shipped")
            staged = SE.open_mission(SE._staged_path(mission, variant))
        except Exception:
            continue
        if shipped is None or staged is None:
            continue
        rows_s = SE.records_with_roles(staged[3])
        rows_h = SE.records_with_roles(shipped[3])
        n = 0
        for (a_it, ps, role), (b_it, ph, _r) in zip(rows_s, rows_h):
            if role in SE.NON_CONVERTIBLE_ROLES:
                if SE.bytecode_chunk(ps) is not None:
                    kept_compiled += 1
                continue
            if SE.L.stream_of(ps) != SE.L.stream_of(ph):
                n += 1
        total += n
        if n:
            report.append({"mission": mission, "variant": variant, "status": "ok", "converted": n})
    json.dump({"missions": report, "total_converted": total,
               "role_locked_kept_compiled": kept_compiled}, open(path, "w"), indent=1)
    print(f"report refreshed: {total} converted record(s) in {len(report)} mission(s)")
    print(f"kept compiled by the role rule: {kept_compiled} record(s)")
    print(f"wrote {path}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default=os.path.join(ROOT, "artifacts", "convert-all-report.json"))
    ap.add_argument("--refresh", action="store_true",
                    help="rewrite the report from what is staged right now, converting nothing")
    a = ap.parse_args(argv)

    todo = missions_with_bytecode()
    if a.refresh:
        return refresh_report(todo, a.report)
    print(f"missions with bytecode records: {len(todo)}")
    if a.dry_run:
        for mission, variant in todo:
            print(f"  would convert: {mission}/{variant}")
        return 0
    total = 0
    report = []
    for mission, variant in todo:
        cmd = [sys.executable, os.path.join(ROOT, "tools", "script_edit.py"),
               "--mission", mission, "--variant", variant, "decompile", "--stage-equivalent"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ABORT  {mission}/{variant}: gate failure\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
            report.append({"mission": mission, "variant": variant, "status": "abort",
                           "detail": (r.stdout + r.stderr)[-2000:]})
            with open(a.report, "w") as fh:
                json.dump(report, fh, indent=1)
            return 1
        m = re.search(r"(\d+) record\(s\) proven equivalent", r.stdout)
        n = int(m.group(1)) if m else 0
        total += n
        if n:
            print(f"  {mission}/{variant}: {n} converted")
        report.append({"mission": mission, "variant": variant, "status": "ok", "converted": n})
    with open(a.report, "w") as fh:
        json.dump({"missions": report, "total_converted": total}, fh, indent=1)
    changed = [x for x in report if x.get("converted")]
    print(f"\nconverted {total} record(s) in {len(changed)} mission(s)")
    print(f"report: {os.path.relpath(a.report, ROOT)}")
    if not a.dry_run:
        print("next: ./proto2 install, then ./proto2 check and ./proto2 dupes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
