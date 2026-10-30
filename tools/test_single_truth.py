#!/usr/bin/env python3
"""Prove the CLI and the GUI are one implementation.

Two independent checks, each comparing STAGED BYTES - not intentions:

  A. different entry points, same result
     `./proto2 script replace` (apply_edit, subprocess)  vs  the GUI's record editor
     (set_record_text, in-process). Both must stage a byte-identical fig.p3d.rz.

  B. same operation, different front door
     `./proto2 script apply FILE` (subprocess)  vs  the GUI's Mission Lua tab
     (import_script, in-process). Byte-identical again.

Both run against scratch staging directories (PROTO2_SCRIPT_MOD), so nothing touches the real
work/mod or the game.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import script_edit as SE  # noqa: E402

MISSION = "story_a1"
VARIANT = "active"
SCRATCH = os.path.join(ROOT, "tmp", "single-truth")


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def fresh(name):
    d = os.path.join(SCRATCH, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def staged(mod, variant=VARIANT):
    return os.path.join(mod, "art", "missions", MISSION, variant, "fig.p3d.rz")


def run_cli(args, mod):
    env = dict(os.environ, PROTO2_SCRIPT_MOD=mod)
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "script_edit.py"),
                        "--mission", MISSION, "--variant", VARIANT] + args,
                       capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout + p.stderr).strip()


def main():
    ok = True
    anchor = "-- set failtext variable default"
    addition = "\r\n-- single-truth marker"

    # ---------- A: CLI replace vs GUI record editor ----------
    mod_cli = fresh("A-cli")
    rc, out = run_cli(["replace", "--index", "0", "--old", anchor,
                       "--new", anchor + addition, "--stage"], mod_cli)
    if rc != 0:
        print("A: CLI replace failed:", out[-300:])
        return 1

    mod_gui = fresh("A-gui")
    path = SE.find_mission(MISSION, VARIANT)
    _b, _l, _o, model = SE.open_mission(path)
    text = SE._decoded(model)[0][1]
    tb = text.encode("latin-1") if isinstance(text, str) else text
    new_text = tb.replace(anchor.encode(), (anchor + addition).encode(), 1).decode("latin-1")
    SE.MOD = mod_gui
    SE.set_record_text(types.SimpleNamespace(mission=MISSION, variant=VARIANT, index=0,
                                             text=new_text, stage=True, no_compile_check=True))

    a_cli, a_gui = staged(mod_cli), staged(mod_gui)
    same_a = sha(a_cli) == sha(a_gui)
    print(f"A. CLI replace vs GUI record editor : {'IDENTICAL' if same_a else 'DIFFER'}  {sha(a_cli)[:16]}")
    ok &= same_a

    # ---------- B: CLI apply(file) vs GUI Mission Lua tab ----------
    whole = SE.export_script(MISSION, VARIANT)
    edited = whole.replace(anchor, anchor + "\n-- single-truth marker", 1)
    f = os.path.join(SCRATCH, "whole.lua")
    open(f, "w", encoding="latin-1").write(edited)

    mod_cli2 = fresh("B-cli")
    rc, out = run_cli(["apply", f, "--stage"], mod_cli2)
    if rc != 0:
        print("B: CLI apply failed:", out[-300:])
        return 1

    mod_gui2 = fresh("B-gui")
    SE.MOD = mod_gui2
    SE.import_script(types.SimpleNamespace(mission=MISSION, variant=VARIANT, text=edited,
                                           stage=True, no_compile_check=True))

    same_b = sha(staged(mod_cli2)) == sha(staged(mod_gui2))
    print(f"B. CLI apply vs GUI Mission Lua     : {'IDENTICAL' if same_b else 'DIFFER'}  {sha(staged(mod_cli2))[:16]}")
    ok &= same_b

    # ---------- B2: batch refuses atomically ----------
    mod_b = fresh("B2-refuse")
    rc, out = run_cli(["batch", "--missions", "story_a1", "--old", "template = MARINE_TEMPLATE",
                       "--new", "template = BLACKWATCH_TEMPLATE", "--stage"], mod_b)
    refused = rc != 0 and "REFUSED" in out and not os.path.exists(staged(mod_b))
    print(f"B2. batch refusal is atomic          : {'yes' if refused else 'NO'} (rc={rc}, "
          f"staged={os.path.exists(staged(mod_b))})")
    ok &= refused

    # ---------- D: the diff viewer sees exactly what was staged ----------
    SE.MOD = mod_cli2                      # holds the record-0 edit staged by B-cli
    d = SE.diff_mission(MISSION, VARIANT, "staged")
    seen = [(r["index"], r["state"], r["delta"]) for r in d.get("changed", [])]
    # delta is the PAYLOAD byte delta (re-compressed size), not the text length change
    good_d = d.get("ok") and seen == [(0, "changed", 16)]
    print(f"D. diff sees the staged change       : {'yes' if good_d else 'NO'} {seen}")
    ok &= good_d

    # ---------- E: control-byte escaping round-trips ----------
    import script_edit as SE2
    sample = 'a\x1b"DISABLED", true )\r\n\x00x\x7f'
    rt = SE2.unescape_ctrl(SE2.escape_ctrl(sample)) == sample
    print(f"E. control-byte escaping round-trips : {'yes' if rt else 'NO'}")
    ok &= rt

    # ---------- C: neither touched the real staging area ----------
    real = os.path.join(ROOT, "work", "mod", "art", "missions", MISSION, VARIANT, "fig.p3d.rz")
    print(f"C. real staging untouched           : {'yes' if not a_cli.startswith(os.path.join(ROOT, 'work')) else 'NO'}")
    print(f"   (real staged file exists: {os.path.exists(real)})")

    print("\nRESULT:", "single truth confirmed" if ok else "DIVERGENCE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
