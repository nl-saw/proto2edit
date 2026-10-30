#!/usr/bin/env python3
"""Rebuild the extracted mission corpus under ./tmp/corpus from the game's .rcf archives.

`tmp/` is scratch and may be deleted at any time. Several corpus-wide tools (coverage, dupes, convert-all,
fsm verify) read the extracted trees, so this is the one command that puts them back:

    ./proto2 corpus          # extract every mission fig.p3d.rz from every archive that has one

Files are stored as tmp/corpus/<archive>/<entry path>, unpacked from their .rz wrapper, which is the layout
script_edit.find_mission expects (.../missions/<mission>/<variant>/fig.p3d.rz). Existing files are skipped, so it
is cheap to re-run after adding an archive.
"""
import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import rz      # noqa: E402
import config  # noqa: E402
from rcf import Rcf  # noqa: E402

GAME = config.game_dir()
CORPUS = os.path.join(ROOT, "tmp", "corpus")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=None)
    ap.add_argument("--force", action="store_true", help="overwrite files that already exist")
    a = ap.parse_args(argv)
    a.game = a.game or config.require_game_dir()

    archives = sorted(glob.glob(os.path.join(a.game, "*.rcf")))
    if not archives:
        raise SystemExit(f"no .rcf archives in {a.game} (set PROTO2_GAME)")
    written = skipped = 0
    archives_used = []
    for path in archives:
        stem = os.path.splitext(os.path.basename(path))[0]
        n_here = 0
        with Rcf(path) as rcf:
            for entry in rcf.entries:
                name = entry.name
                if not name or not name.lower().endswith("fig.p3d.rz"):
                    continue
                flat = name.replace("\\", "/")
                if "/missions/" not in "/" + flat.lower():
                    continue
                dst = os.path.join(CORPUS, stem, flat)
                if os.path.exists(dst) and not a.force:
                    skipped += 1
                    continue
                data = rcf.read(entry)
                if data[:2] == b"RZ":
                    data = rz.unwrap(data)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(data)
                written += 1
                n_here += 1
        if n_here:
            archives_used.append((stem, n_here))
    print(f"corpus: wrote {written}, skipped {skipped} ({len(archives_used)} archive(s))")
    for stem, n in archives_used:
        print(f"  {stem}: {n}")
    print(f"  under {os.path.relpath(CORPUS, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
