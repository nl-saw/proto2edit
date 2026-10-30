#!/usr/bin/env python3
"""Clone a mission under a new name, by renaming rather than by adding a table entry.

Registration lives in `scripts/missiondefinit.lua`, a boot script that ships as stripped bytecode and that the engine
loads as a chunk (a loose modified copy already boots - see docs/30). Adding an entry means emitting new bytecode.
A **rename** does not: pick a new name of the **same length** and every replacement is a byte substitution, so no size
field, string table or prototype has to be rebuilt. That is what this tool does, and why it insists on equal length.

It stages (never installs):

    work/mod/art/missions/<dst>/<variant>/{fig.p3d.rz, tod.p3d.rz, prop_joined.p3d.rz, sound.p3d, prop_sound.p3d}
    work/mod/scripts/missiondefinit.lua        (the existing staged copy if there is one, else the shipped chunk)

then prints `./proto2 install` as the next step, which is the owner's decision.

    python3 tools/clone_mission.py --src test_vehicles --dst test_clone_01 --dry-run
    python3 tools/clone_mission.py --src test_vehicles --dst test_clone_01

What it does not do: text-bible titles and unlock keys, which name the mission in `fe_textbible`; the clone will
therefore show the source's title in the menu. That is deliberate for a first test - identity of the *mission* is what
the launch is testing, not cosmetic labels.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import rz  # noqa: E402
import mission_gui as G  # noqa: E402

MOD = os.path.join(ROOT, "work", "mod")
FILES = ("fig.p3d.rz", "tod.p3d.rz", "prop_joined.p3d.rz", "sound.p3d", "prop_sound.p3d")


def model():
    return G.Model(lambda _msg: None)


def read_shipped(m, mission, variant, filename):
    inner, src = m.payload(mission, variant, filename, "shipped")
    if inner is None:
        return None, None
    return inner, src


def replace_in_asset(blob, old, new, filename):
    """Swap the name in an asset and re-pack .rz files the way tools/address.py writes them.

    `Model.payload` already unwraps .rz, so the container flag has to come from the filename, not from the magic:
    reading `blob[:2] == b"RZ"` here would be False for every mission asset and the raw p3d would be written under a
    .rz name.
    """
    packed = filename.endswith(".rz")
    hits = blob.count(old)
    data = blob.replace(old, new) if hits else blob
    return (rz.wrap(data) if packed else data), hits, packed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--variant", default="active")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if len(a.dst) != len(a.src):
        raise SystemExit(f"--dst must be the same length as --src ({len(a.src)}), because every replacement is a "
                         f"length-neutral substitution; got {len(a.dst)}")
    if a.dst == a.src:
        raise SystemExit("--dst must differ")
    for bad in ("/", "\\"):
        if bad in a.dst:
            raise SystemExit("--dst must be a plain name")

    m = model()
    old, new = a.src.encode("latin-1"), a.dst.encode("latin-1")
    plan, failures = [], []
    for filename in FILES:
        blob, src = read_shipped(m, a.src, a.variant, filename)
        if blob is None:
            failures.append(f"{filename}: not shipped for {a.src}/{a.variant}")
            continue
        out, hits, packed = replace_in_asset(blob, old, new, filename)
        plan.append({"file": filename, "bytes": len(blob), "hits": hits, "packed": packed,
                     "data": out, "source": src})

    # registration: prefer the staged chunk (it may already carry edits), else the shipped one
    staged_def = os.path.join(MOD, "scripts", "missiondefinit.lua")
    if os.path.exists(staged_def):
        reg_blob, reg_src = open(staged_def, "rb").read(), "staged"
    else:
        # not a mission asset: read the boot chunk directly out of the archive
        reg_blob = G.read_archive("boot.rcf", "scripts/missiondefinit.lua")
        if reg_blob is None:
            raise SystemExit("no staged missiondefinit and none in boot.rcf")
        reg_src = "boot.rcf"
    reg_hits = reg_blob.count(old)
    reg_out = reg_blob.replace(old, new) if reg_hits else reg_blob

    print(f"clone {a.src} -> {a.dst}  (variant {a.variant}{', dry run' if a.dry_run else ''})")
    for item in plan:
        print(f"  {item['file']:20s} {item['bytes']:8d} B  name replaced {item['hits']}x"
              f"  {'rz' if item['packed'] else 'raw'}")
    print(f"  {'missiondefinit':20s} {len(reg_blob):8d} B  name replaced {reg_hits}x  ({reg_src})")
    for line in failures:
        print(f"  ! {line}")

    if not any(item["hits"] for item in plan) and not reg_hits:
        raise SystemExit("nothing to replace: the source name does not appear in these assets - pick another source")
    if reg_hits == 0:
        print("  ! the mission name is not in missiondefinit, so no new mission will be registered")

    if a.dry_run:
        print("\ndry run: nothing written")
        return 0

    for item in plan:
        dst = os.path.join(MOD, "art", "missions", a.dst, a.variant, item["file"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "wb").write(item["data"])
        print(f"  wrote {os.path.relpath(dst, ROOT)}")
    # never lose the staged registration we are editing on top of
    import shutil
    import time
    keep = os.path.join(ROOT, "work", "state", "clone-backup")
    os.makedirs(keep, exist_ok=True)
    shutil.copy2(staged_def, os.path.join(keep, f"missiondefinit-{time.strftime('%Y%m%d-%H%M%S')}.lua"))
    open(staged_def, "wb").write(reg_out)
    print(f"  wrote {os.path.relpath(staged_def, ROOT)} (previous copy replaced; ./proto2 install ships it)")

    # verify the patched fig the way the engine will see it
    fig = os.path.join(MOD, "art", "missions", a.dst, a.variant, "fig.p3d.rz")
    inner = rz.unwrap(open(fig, "rb").read())
    left = inner.count(old)
    print(f"\ncheck: patched fig mentions '{a.src}' {left}x, '{a.dst}' {inner.count(new)}x "
          f"(header expects {a.dst}_{a.variant})")
    print("next: ./proto2 install, launch, and look for the mission in the list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
