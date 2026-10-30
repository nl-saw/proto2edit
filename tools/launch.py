#!/usr/bin/env python3
"""Record-keeping around a launch, so an experiment's outcome is attributable.

The project has twice paid for experiments whose result could not be read: `prototype_fix.log` holds **one log per
launch and overwrites it**, so "the game crashed" could not be traced to the file we installed, and an offline proof
saying "byte-identical" says nothing about whether the engine accepts a shape it has never loaded. This wraps a launch
in a session record:

    ./proto2 launch --begin --label text-boot --marker "text boot ok"
    <start the game however you normally do: Steam, Proton, or ./proto2 launch --run>
    ./proto2 launch --end

`--end` archives the game's logs into `artifacts/traces/<stamp>/` and prints what the engine actually did: the files it
opened in order, the last few before the log stopped, whether the marker text appeared, and whether each staged file was
requested at all. A missing marker with a log that stops early localises the failure; a log that never mentions the file
means the override was never consumed.

Nothing here launches the game unless you ask with `--run`, and nothing is reverted automatically: the debug menu stays
installed, and a revert is printed as a command rather than performed.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import config  # noqa: E402

STATE = os.path.join(ROOT, "work", "state", "launch-session.json")
TRACES = os.path.join(ROOT, "artifacts", "traces")
MOD = os.path.join(ROOT, "work", "mod")

LOGS = ("prototype_fix.log", "proto2_lualog.log", "proto2_ping.txt",
        "proto2_inflate6.log", "proto2_inflate6.in")


def game():
    g = config.game_dir()
    if not g or not os.path.isdir(g):
        raise SystemExit("no game directory configured; ./proto2 config set game_dir ...")
    return g


def _sha(path):
    try:
        return hashlib.sha1(open(path, "rb").read()).hexdigest()
    except OSError:
        return None


def staged_hashes():
    out = {}
    if not os.path.isdir(MOD):
        return out
    for dp, dirs, files in os.walk(MOD):
        dirs[:] = [d for d in dirs if d != "backup"]
        for fn in files:
            p = os.path.join(dp, fn)
            out[os.path.relpath(p, MOD).replace(os.sep, "/")] = _sha(p)
    return out


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")


ARGS_BACKUP = os.path.join(ROOT, "work", "state", "args-backup.txt")


def valid_missions():
    """(valid, configured) mission names, read from the shipped boot chunks.

    `Level=<name>` is an engine option that checks the name against `AddValidMission` in scripts/init.lua, so that
    list is what validates a quick start. A name that is valid but has no config in scripts/missiondefinit.lua only
    gets the engine's fallback config, which does not start (measured: `Level=test_jump` requested no mission
    files). The AddValidMission names are read from the decompiled copy of init.lua when one exists; otherwise the
    configured names are used, which are always a safe subset.
    """
    import re
    valid, configured = set(), set()
    try:
        import lua51
        import mission_gui as G
        md = lua51.load(G.read_archive("boot.rcf", "scripts/missiondefinit.lua"))
        configured = {p.constants[0].decode("latin-1") for p in md.protos
                      if p.constants and isinstance(p.constants[0], bytes)}
        dec = os.path.join(ROOT, "work", "lua", "init.unluac.lua")
        if os.path.exists(dec):
            text = open(dec, encoding="latin-1", errors="replace").read()
            valid = set(re.findall(r'AddValidMission\("([A-Za-z0-9_]+)"', text))
    except Exception:
        pass
    return (valid or set(configured)), configured


def write_args(level, game_dir):
    """Write <game>/args.txt with one Level token, backing up a pre-existing file."""
    path = os.path.join(game_dir, "args.txt")
    had = None
    if os.path.exists(path):
        had = open(path, "rb").read()
        os.makedirs(os.path.dirname(ARGS_BACKUP), exist_ok=True)
        open(ARGS_BACKUP, "wb").write(had)
    open(path, "w").write(f"Level={level}\n")
    return had is not None


def clear_args(game_dir, had_backup):
    path = os.path.join(game_dir, "args.txt")
    if had_backup and os.path.exists(ARGS_BACKUP):
        shutil.copy2(ARGS_BACKUP, path)
        os.remove(ARGS_BACKUP)
    elif os.path.exists(path):
        os.remove(path)


def begin(args):
    g = game()
    when = stamp()
    trace = os.path.join(TRACES, f"session-{when}")
    os.makedirs(trace, exist_ok=True)
    # keep the pre-launch logs: these are what the previous launch wrote, and they are about to be overwritten
    for name in LOGS:
        src = os.path.join(g, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(trace, f"pre-{name}"))
    level = getattr(args, "level", None)
    args_backup = False
    if level:
        valid, configured = valid_missions()
        if valid and level not in valid:
            raise SystemExit(f"{level!r} is not in init.lua's AddValidMission list - Level= would be ignored")
        if configured and level not in configured:
            print(f"  warning    : {level} has no config in missiondefinit (fallback only; it will not start)")
        args_backup = write_args(level, g)
        print(f"  quick start: Level={level} written to args.txt"
              f" ({'previous file backed up' if args_backup else 'new file'})")
    record = {
        "level": level,
        "args_backup": args_backup,
        "label": args.label or "experiment",
        "marker": args.marker,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "started_epoch": time.time(),
        "game": g,
        "trace": os.path.relpath(trace, ROOT),
        "staged": staged_hashes(),
        "log_mtimes": {n: os.path.getmtime(os.path.join(g, n))
                       for n in LOGS if os.path.exists(os.path.join(g, n))},
    }
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(record, open(STATE, "w"), indent=1)
    print(f"session started: {record['label']}")
    print(f"  trace      : {record['trace']}")
    print(f"  staged     : {len(record['staged'])} file(s) recorded by hash")
    if args.marker:
        print(f"  marker     : {args.marker!r} (looked for in every log)")
    print("  next       : start the game (or re-run with --run), then ./proto2 launch --end")
    if args.run:
        return run(record, args)
    return 0


def run(record, args):
    g = record["game"]
    exe = os.path.join(g, "prototype2.exe")
    cmd = args.command
    if not cmd:
        if shutil.which("protontricks-launch"):
            cmd = f'protontricks-launch --appid 115320 "{exe}"'
        else:
            cmd = f'"{exe}"'
    print(f"  launching  : {cmd}")
    code = subprocess.run(cmd, shell=True, cwd=g).returncode
    print(f"  exited     : {code}")
    return finish(args)


def fix(session_file):
    try:
        return json.load(open(session_file))
    except (OSError, ValueError):
        raise SystemExit("no session in progress; run ./proto2 launch --begin first")


def opened(g):
    """Ordered FileOpenRequest list from prototype_fix.log, with duplicates collapsed in place."""
    path = os.path.join(g, "prototype_fix.log")
    out = []
    try:
        for line in open(path, "r", errors="replace"):
            if "FileOpenRequest::DoRequest(" not in line:
                continue
            name = line.split("DoRequest(", 1)[1].split(")", 1)[0].strip()
            out.append(name)
    except OSError:
        return None
    return out


def finish(args):
    rec = fix(STATE)
    g = rec["game"]
    trace = os.path.join(ROOT, rec["trace"])
    fresh = {}
    for name in LOGS:
        src = os.path.join(g, name)
        if os.path.exists(src):
            dst = os.path.join(trace, name)
            shutil.copy2(src, dst)
            fresh[name] = dst
    ops = opened(g)
    marker = rec.get("marker")
    hit = {}
    for name in ("prototype_fix.log", "proto2_lualog.log", "proto2_ping.txt", "proto2_inflate6.log"):
        p = os.path.join(g, name)
        try:
            text = open(p, "r", errors="replace").read()
        except OSError:
            continue
        if marker and marker in text:
            hit[name] = text.count(marker)

    if rec.get("level"):
        clear_args(g, rec.get("args_backup"))
        print(f"  quick start : args.txt restored (Level={rec['level']} removed)")
    print(f"session {rec['label']} (#{os.path.basename(trace)})")
    print(f"  trace        : {rec['trace']}")
    # a log left over from an earlier launch must never be read as this session's result
    log_path = os.path.join(g, "prototype_fix.log")
    stale = None
    if os.path.exists(log_path):
        mtime = os.path.getmtime(log_path)
        if mtime <= rec["started_epoch"] + 1:
            stale = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
    if stale:
        print(f"  engine log   : STALE - last written {stale}, before this session began.")
        print("                 The game did not run since --begin; nothing can be concluded from this run.")
        json.dump({"label": rec["label"], "marker": rec.get("marker"), "verdict": "stale",
                   "started": rec["started"], "ended": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "note": "prototype_fix.log was not rewritten after --begin"},
                  open(os.path.join(trace, "session.json"), "w"), indent=1)
        os.remove(STATE)
        return 1
    if ops is None:
        print("  engine log   : MISSING - the game never wrote prototype_fix.log, so it did not get as far as the loader")
        verdict = "no-log"
    else:
        new = [o for o in ops]
        print(f"  files opened : {len(new)}")
        print(f"  last opened  : {new[-3:] if new else '(none)'}")
        total = {n: new.count(n) for n in set(new)}
        staged = rec.get("staged", {})
        consumed = [rel.replace("/", "\\") for rel in staged
                    if any(rel.replace("/", "\\") in o for o in new)]
        print(f"  staged files : {len(staged)} installed, {len(consumed)} requested by the engine")
        missing = [r for r in staged if r.replace("/", "\\") not in consumed]
        if missing and len(missing) <= 6:
            print(f"  not requested: {missing}")
        verdict = "log"
    if marker:
        if hit:
            print(f"  marker       : FOUND {marker!r} {hit}")
            verdict += "+marker"
        else:
            print(f"  marker       : not found ({marker!r}) - the code did not run, or did not reach the logger")
    summary = {
        "label": rec["label"], "marker": marker, "verdict": verdict,
        "started": rec["started"], "ended": time.strftime("%Y-%m-%d %H:%M:%S"),
        "opened": ops, "marker_hits": hit,
        "staged": rec.get("staged", {}),
    }
    json.dump(summary, open(os.path.join(trace, "session.json"), "w"), indent=1)
    print(f"  wrote        : {os.path.relpath(os.path.join(trace, 'session.json'), ROOT)}")
    print("\nRead it as: marker FOUND = our change ran. Log present, marker absent = it loaded and did not reach our")
    print("code (or the script failed). No log = the game did not boot far enough to use the loader.")
    print("Revert the experiment with the specific command for what was installed, e.g. ./proto2 fsm --revert")
    print("or ./proto2 clean - never ./proto2 revert unless you mean to remove the debug menu too.")
    os.remove(STATE)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--begin", action="store_true", help="record the baseline and archive the previous logs")
    ap.add_argument("--end", action="store_true", help="collect the logs and print what the engine did")
    ap.add_argument("--run", action="store_true", help="launch the game between begin and end")
    ap.add_argument("--command", default=None, help="launch command to use with --run")
    ap.add_argument("--label", default=None, help="name this experiment (shown in the trace path)")
    ap.add_argument("--marker", default=None, help="text to look for in the logs to prove the change ran")
    ap.add_argument("--level", default=None, metavar="MISSION",
                    help="quick-start this mission: writes Level=<mission> into the game's args.txt (removed on --end)")
    a = ap.parse_args(argv)
    if a.begin:
        return begin(a)
    if a.end:
        return finish(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
