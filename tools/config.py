#!/usr/bin/env python3
"""Project configuration: one JSON file in the repository root.

    proto2.config.json      machine-local, git-ignored (see .gitignore)
    proto2.config.example.json  committed, documents every key and default

Keys (all optional; a missing file means defaults):

    game_dir         path to the Prototype 2 install (PROTO2_GAME overrides it)
    capture_version  payload recorder to install: v2..v6 (default v6)
    gui_text_size    editor text size, 6..28 (default 10)
    gui_last_mission mission id the GUI opens with (default: first in the list)

    ./proto2 config               show everything, with provenance
    ./proto2 config set KEY VAL   validate and store one value
    ./proto2 config unset KEY     drop an override
    ./proto2 config path          print the file path

The module is also the single accessor for the game directory: every tool calls config.game_dir() instead of
hard-coding a path, so PROTO2_GAME, the config file and the default agree everywhere.
"""
import argparse
import json
import os
import shlex
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("PROTO2_CONFIG") or os.path.join(ROOT, "proto2.config.json")
LEGACY_GAME = os.path.join(ROOT, ".proto2-game")
DEFAULT_GAME = ""      # set per machine: ./proto2 config set game_dir "/path/to/Prototype 2"

DEFAULTS = {
    "game_dir": DEFAULT_GAME,
    "capture_version": "v6",
    "gui_text_size": 10,
    "gui_theme": "light",          # "light" or "dark"
    "gui_last_mission": "",
    "proton_prefix": "",       # Wine/Proton prefix for the ASI verifier; optional
}
CAPTURE_VERSIONS = ["v2", "v3", "v4", "v5", "v6"]


def load():
    """Every key with its effective value, plus where each came from."""
    data, sources = {}, {}
    if os.path.exists(PATH):
        try:
            file_data = json.load(open(PATH))
        except Exception as exc:
            print(f"warning: {PATH}: {type(exc).__name__}: {exc}", file=sys.stderr)
            file_data = {}
        for key, value in file_data.items():
            if key in DEFAULTS:
                data[key] = value
                sources[key] = "config"
    if "game_dir" not in data and os.path.exists(LEGACY_GAME):
        try:
            legacy = open(LEGACY_GAME).read().strip()
            if legacy:
                data["game_dir"] = legacy
                sources["game_dir"] = ".proto2-game (legacy)"
        except Exception:
            pass
    env = os.environ.get("PROTO2_GAME", "").strip()
    if env:
        data["game_dir"] = env
        sources["game_dir"] = "PROTO2_GAME"
    for key, value in DEFAULTS.items():
        if key not in data:
            data[key] = value
            sources.setdefault(key, "default")
    return data, sources


def get(key):
    data, _src = load()
    if key not in DEFAULTS:
        raise KeyError(f"unknown config key {key!r} (known: {', '.join(DEFAULTS)})")
    return data[key]


GAME_HINT = ('game directory not set: ./proto2 config set game_dir "/path/to/Prototype 2" '
             '(or set PROTO2_GAME)')


def game_dir():
    """The game directory, honouring PROTO2_GAME and the config file (may be empty when unset)."""
    return get("game_dir")


def require_game_dir():
    """The game directory, or a SystemExit with the one-line reminder of how to set it."""
    gd = get("game_dir")
    if not gd:
        raise SystemExit(GAME_HINT)
    return gd


def proton_prefix():
    """Wine/Proton prefix used by the ASI verifier (PROTO2_PREFIX overrides)."""
    env = os.environ.get("PROTO2_PREFIX", "").strip()
    return env or get("proton_prefix")


def save(data):
    known = {k: v for k, v in data.items() if k in DEFAULTS}
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(known, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, PATH)


def validate(key, value):
    """Return the typed value, or raise ValueError with a useful message."""
    if key == "game_dir":
        value = value.strip()
        if not os.path.isdir(value):
            raise ValueError(f"not a directory: {value}")
        if not os.path.exists(os.path.join(value, "prototype2engine.dll")):
            raise ValueError(f"no prototype2engine.dll in {value} - is that the game directory?")
        return value
    if key == "capture_version":
        if value not in CAPTURE_VERSIONS:
            raise ValueError(f"capture_version must be one of {', '.join(CAPTURE_VERSIONS)}")
        asi = os.path.join(ROOT, "build", f"proto2_inflate{value[1:]}.asi")
        if not os.path.exists(asi):
            print(f"warning: {os.path.relpath(asi, ROOT)} is not built yet", file=sys.stderr)
        return value
    if key == "gui_text_size":
        try:
            n = int(value)
        except ValueError:
            raise ValueError("gui_text_size must be an integer")
        if not 6 <= n <= 28:
            raise ValueError("gui_text_size must be between 6 and 28")
        return n
    if key == "gui_theme":
        if value not in ("light", "dark"):
            raise ValueError("gui_theme must be light or dark")
        return value
    if key == "gui_last_mission":
        return value.strip()
    if key == "proton_prefix":
        return value.strip()
    raise ValueError(f"unknown config key {key!r} (known: {', '.join(DEFAULTS)})")


def main(argv=None):
    ap = argparse.ArgumentParser(description="project configuration")
    ap.add_argument("cmd", nargs="?", default="show",
                    choices=["show", "get", "set", "unset", "path", "shell"])
    ap.add_argument("key", nargs="?")
    ap.add_argument("value", nargs="?")
    a = ap.parse_args(argv)
    data, sources = load()

    if a.cmd == "path":
        print(PATH)
        return 0
    if a.cmd == "shell":
        # consumed by the ./proto2 wrapper: two shell-quoted assignments
        print(f"GAME={shlex.quote(data['game_dir'])}")
        print(f"GAME_FROM={shlex.quote(sources.get('game_dir', 'default'))}")
        return 0
    if a.cmd == "get":
        if not a.key:
            raise SystemExit("config get KEY")
        print(data[a.key])
        return 0
    if a.cmd == "unset":
        if not a.key:
            raise SystemExit("config unset KEY")
        file_data = json.load(open(PATH)) if os.path.exists(PATH) else {}
        file_data.pop(a.key, None)
        save(file_data)
        print(f"unset {a.key}")
        return 0
    if a.cmd == "set":
        if not a.key or a.value is None:
            raise SystemExit("config set KEY VALUE")
        try:
            typed = validate(a.key, a.value)
        except ValueError as exc:
            raise SystemExit(f"config: {exc}")
        file_data = json.load(open(PATH)) if os.path.exists(PATH) else {}
        file_data[a.key] = typed
        save(file_data)
        print(f"{a.key} = {typed!r}  (stored in {os.path.relpath(PATH, ROOT)})")
        return 0

    print(f"config file : {os.path.relpath(PATH, ROOT)}"
          f"{'' if os.path.exists(PATH) else '  (not created yet)'}")
    for key in DEFAULTS:
        value = data[key]
        shown = value if value != "" else "(empty)"
        print(f"  {key:<17} {str(shown):<58} [{sources.get(key, 'default')}]")
    if os.path.exists(LEGACY_GAME):
        print("\nnote: .proto2-game exists; `./proto2 config set game_dir \"<path>\"` then delete it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
