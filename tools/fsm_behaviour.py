#!/usr/bin/env python3
"""Reconcile a runtime condition log against the decoded FSM.

Criterion 3's second half: the instrumentation records which conditions the engine evaluates
(`proto2_cond.asi` writes `tick | target state uid | caller`), and this tool checks that record against
the decode.

What it establishes:

  * every uid the engine evaluated exists in the decoded corpus (a uid that does not would mean the
    decode is missing a state);
  * which mission's condition set the log matches, so the playing session can be identified from the
    data rather than from memory;
  * which of that mission's transitions were observed and which were never touched.

    python3 tools/fsm_behaviour.py                      # default log path
    python3 tools/fsm_behaviour.py path/to/proto2_cond.log
    python3 tools/fsm_behaviour.py --mission story_a1   # compare against one mission explicitly
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_chunk as C
import fsm_edit as E
import config  # noqa: E402
import uid as U

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "tmp", "corpus")
DEFAULT_LOG = os.path.join(config.game_dir(), "proto2_cond.log") if config.game_dir() else ""

NAMES = {
    0xCA57F1F2F0F4C2DD: "Complete",
    0x004633DBC8C3FD98: "Fail",
    0x8F5AD09277139B57: "Failed",
    0x4569BDAF777A910D: "Success",
    0x2D9F03EF2328A7A5: "Outside",
    # recovered from a runtime session: two states the engine evaluates but no mission file defines
    0xE6B26F6008AF0FC5: "Activated",
    0x5CBEADA9DB46B707: "Stopped",
}


def mission_targets():
    """mission -> set of stateName uids its state-change conditions aim at"""
    out = {}
    for dirpath, _dirs, fns in os.walk(CORPUS):
        if "fig.p3d.rz" not in fns:
            continue
        mission = [p for p in dirpath.split("/") if p][-2]
        leaf = C.load(os.path.join(dirpath, "fig.p3d.rz"))
        try:
            model = E.parse(leaf)
        except Exception:
            continue
        targets = out.setdefault(mission, set())
        for el in E.walk_elements(model):
            for c in el["containers"]:
                for it in c["items"]:
                    f = it.get("fields")
                    if f and f.get("type") == "state-change":
                        targets.add(f["stateName"])
    return out


def read_log(path):
    seen = collections.Counter()
    callers = collections.defaultdict(set)
    for line in open(path, "rb"):
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            _tick, uid_s, caller = parts
            v = int(uid_s, 16)
        except ValueError:
            continue
        seen[v] += 1
        callers[v].add(caller.decode())
    return seen, callers


def usage():
    print(__doc__.strip())
    print("\n    --mission NAME   report that mission explicitly")
    print("    --save DIR       archive this trace as JSON (with a timestamp)")
    print("    --help           this text")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        return usage()
    path = DEFAULT_LOG
    explicit = None
    if "--mission" in argv:
        explicit = argv[argv.index("--mission") + 1]
    optvals = {explicit} | ({argv[argv.index("--save") + 1]} if "--save" in argv else set())
    rest = [a for a in argv if not a.startswith("--") and a not in optvals]
    if rest:
        path = rest[0]
    if not path:
        path = os.path.join(config.require_game_dir(), "proto2_cond.log")
    if not os.path.exists(path):
        print(f"no log at {path}")
        return 2
    seen, callers = read_log(path)
    if not seen:
        print(f"{path} has no evaluation lines yet")
        return 1

    print(f"evaluations logged : {sum(seen.values())}")
    print(f"distinct targets   : {len(seen)}")
    for v, c in seen.most_common(12):
        print(f"  {v:#018x} x{c:<5} {NAMES.get(v, '')}")

    targets = mission_targets()
    known = set()
    for s in targets.values():
        known |= s
    unknown = [v for v in seen if v not in known]
    print(f"\ntargets that appear in NO decoded mission file: {len(unknown)}")
    for v in unknown[:6]:
        nm = NAMES.get(v)
        tag = f"{nm!r} - named, but no mission file defines this state" if nm else "unnamed"
        print(f"  {v:#018x} (logged {seen[v]}x)  {tag}")

    # identification: a mission is identified only when its condition-target set matches EXACTLY and
    # no other mission shares that set. Most missions collapse into 13 distinct sets, so a partial or
    # common set cannot name a mission and is reported as ambiguous rather than guessed.
    # compare only targets that some mission actually declares: Activated/Stopped are runtime-only and
    # would otherwise make every trace look unmatched
    known_all = set()
    for t in targets.values():
        known_all |= set(t)
    obs = set(seen) & known_all
    exact = [m for m, t in targets.items() if t and frozenset(t) == frozenset(obs)]
    if len(exact) == 1:
        print(f"\nidentified mission : {exact[0]} (exact target-set match, unique)")
    elif len(exact) > 1:
        print(f"\nambiguous          : {len(exact)} missions share this target set: {sorted(exact)[:6]}")
    else:
        supersets = [m for m, t in targets.items() if t and obs <= set(t)]
        print(f"\nnot identified     : no mission's target set matches exactly "
              f"({len(supersets)} are supersets of what was observed)")

    print("\nbest-matching missions (share of their condition targets observed):")
    rows = []
    for m, s in targets.items():
        if not s:
            continue
        hit = len(s & set(seen))
        rows.append((hit / len(s), hit, len(s), m))
    for frac, hit, total, m in sorted(rows, reverse=True)[:6]:
        print(f"  {m:<28} {hit}/{total} = {frac*100:.0f}%")
    if explicit and explicit in targets:
        s = targets[explicit]
        missed = sorted(s - set(seen))
        print(f"\n{explicit}: observed {len(s & set(seen))}/{len(s)}; never evaluated {len(missed)}")
        for v in missed:
            print(f"  never evaluated {v:#018x} {NAMES.get(v, '')}")

    if "--save" in argv:
        save_dir = argv[argv.index("--save") + 1]
        os.makedirs(save_dir, exist_ok=True)
        best = sorted(rows, reverse=True)[0][3] if rows else "unknown"
        stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
        out = os.path.join(save_dir, f"{stamp}-{best}.json")
        import json
        with open(out, "w") as fh:
            json.dump({"log": path,
                       "evaluations": sum(seen.values()),
                       "targets": {f"{v:#018x}": {"count": c, "name": NAMES.get(v, ""),
                                                  "callers": sorted(callers.get(v, []))}
                                   for v, c in seen.items()},
                       "best_match": best,
                       "missions": {m: {"observed": len(s & set(seen)), "total": len(s)}
                                    for m, s in targets.items() if s}}, fh, indent=1)
        print(f"\nsaved trace -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
