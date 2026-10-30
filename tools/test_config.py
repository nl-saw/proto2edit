#!/usr/bin/env python3
"""Config file: defaults, validation, provenance and the PROTO2_GAME override.

Runs against a temporary path via PROTO2_CONFIG, so the real proto2.config.json is never touched.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="cfg-", dir=os.path.join(ROOT, "tmp"))
os.environ["PROTO2_CONFIG"] = os.path.join(TMP, "proto2.config.json")
os.environ.pop("PROTO2_GAME", None)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import config  # noqa: E402

fails = []


def ck(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {detail}" if detail else ""))


data, sources = config.load()
ck("defaults apply with no file", data["capture_version"] == "v6" and data["gui_text_size"] == 10,
   f"{data['capture_version']}, {data['gui_text_size']}")
ck("game_dir falls back to the default", config.game_dir() == config.DEFAULT_GAME, config.game_dir())

try:
    config.validate("game_dir", "/nonexistent/path")
    ck("game_dir rejects a missing directory", False, "accepted /nonexistent/path")
except ValueError:
    ck("game_dir rejects a missing directory", True)

try:
    config.validate("gui_text_size", "99")
    ck("gui_text_size range enforced", False, "accepted 99")
except ValueError:
    ck("gui_text_size range enforced", True)

try:
    config.require_game_dir()
    ck("require_game_dir explains when unset", False, "no SystemExit")
except SystemExit as exc:
    ck("require_game_dir explains when unset", "config set game_dir" in str(exc), str(exc))

try:
    config.validate("capture_version", "v9")
    ck("capture_version restricted", False, "accepted v9")
except ValueError:
    ck("capture_version restricted", True)

config.save({"gui_text_size": 14, "gui_last_mission": "story_e2"})
ck("values persist", config.get("gui_text_size") == 14 and config.get("gui_last_mission") == "story_e2",
   f"{config.get('gui_text_size')}, {config.get('gui_last_mission')!r}")

data, sources = config.load()
ck("provenance reported", sources["gui_text_size"] == "config", str(sources["gui_text_size"]))

os.environ["PROTO2_GAME"] = "/fake/override"
data, sources = config.load()
ck("PROTO2_GAME overrides the file", data["game_dir"] == "/fake/override" and sources["game_dir"] == "PROTO2_GAME",
   f"{data['game_dir']} [{sources['game_dir']}]")
os.environ.pop("PROTO2_GAME", None)

try:
    config.get("no_such_key")
    ck("unknown keys rejected", False, "no exception")
except KeyError:
    ck("unknown keys rejected", True)

import shutil
shutil.rmtree(TMP, ignore_errors=True)
if fails:
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("config: all checks agree")
