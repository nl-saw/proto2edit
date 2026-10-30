#!/usr/bin/env python3
"""Extract the engine's Lua-facing API surface from prototype2engine.dll.

The Cement engine exposes engine singletons to the game's Lua scripts. Each
exposed C++ method is registered with a short Lua name that follows a family
prefix:

    fe_*    front-end / HUD / mission-objective bindings
    mm_*    mission manager
    mms_*   mission manager, script-side helpers (the shipped .lua files)
    mcs_*   mission "current state" helpers

The names are plain ASCII literals in `.rdata`, so they can be recovered
directly from the shipped binary with no disassembler. This tool does that,
groups them by prefix, and can dump the raw literal cluster a name came from
(so every claim in the docs is re-derivable from the shipped artifact).

This is an inventory, not a decompilation: it reports the *names* the engine
registers. Argument order and types are established separately (see
docs/15-mission-scripting-api.md for what is verified and what is not).

Usage:
  python3 tools/luaapi.py list                    # all names, grouped
  python3 tools/luaapi.py list --prefix mm_       # one family
  python3 tools/luaapi.py list --mission          # mission/objective subset
  python3 tools/luaapi.py strings --start 0xa8e400 --end 0xa8f000
  python3 tools/luaapi.py json analysis/lua-api.json
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from peinfo import PE  # noqa: E402
import config  # noqa: E402

DEFAULT_DLL = os.path.join(config.game_dir(), "prototype2engine.dll")

# Families the engine registers. Order is the order they are reported in.
PREFIXES = ("fe_", "mm_", "mms_", "mcs_")


def strings(pe, min_len=4):
    """Yield (virtual_address, ascii text) for every printable run in the image."""
    for m in re.finditer(rb"[ -~]{%d,}" % min_len, pe.data):
        off = m.start()
        for s in pe.sections:
            if s["raw_ptr"] <= off < s["raw_ptr"] + s["raw_size"]:
                yield pe.image_base + s["vaddr"] + (off - s["raw_ptr"]), m.group().decode("latin-1")
                break


def api_names(pe):
    """All registered Lua names, as {prefix: [(va, name), ...]} in address order."""
    out = {p: [] for p in PREFIXES}
    for va, text in strings(pe):
        for p in PREFIXES:
            if text.startswith(p) and re.fullmatch(r"[A-Za-z0-9_]+", text):
                out[p].append((va, text))
                break
    for p in out:
        out[p].sort()
    return out


# The mission/objective core: names that directly control mission state.
MISSION_CORE = {
    # mission lifecycle
    "mm_RegisterMissions", "mm_ConfigureMission", "mm_ActivateMission",
    "mm_DeactivateMission", "mm_CompleteMission", "mm_AssociateStateMachine",
    "mm_SetLocationDependency", "mm_GetMissionCompletionCount",
    "mm_IsMissionUnlockedOrUnlockable", "mm_PostUnlockKey", "mm_CancelUnlockKey",
    "mm_HasUnlockKeyBeenReceived", "mm_SetCurrentLocation", "mm_GetCurrentLocation",
    "mm_GetActiveMissions", "mm_GetOpenMissions", "mm_GetTriggerOverlap",
    "mm_LineOfSightTest", "mm_PostEvent", "mm_AddMissionObjectTags",
    "mm_SetObjectAlpha", "mm_ResetGameDatabase", "mm_ResetMissionVars",
    "mm_DestroyMissionVars", "mm_MissionVarReset", "mm_MissionVarDestroy",
    # streaming / packages
    "mm_ConfigureStreamSlotGroup", "mm_PurgeStreamSlotGroups",
    "mm_GetActiveStreamSlotGroupCount", "mm_IsStreamSlotPurgeInProgress",
    "mm_GetFreeMemoryInDynamicSlots",
    # collectibles
    "mm_RegisterCollectibles", "mm_SetCollectibleCollected",
    "mm_IsCollectibleCollected", "mm_GetCollectibleStatus",
    "mm_SetCollectibleDiscovered", "mm_SetCollectibleUnlocked",
    "mm_SetCollectibleTempUnlocked", "mm_ClearCollectibleTempUnlocked",
    "mm_ClearCollectibleRecord", "mm_CountCollectibleTotalCollected",
    # objectives (what a "mission aspect" editor needs)
    "fe_AddObjectives", "fe_AddObjectivesCount", "fe_RemoveObjectives",
    "fe_ClearObjectives", "fe_SetCurrentObjectives", "fe_UpdateObjectivesStatus",
    "fe_UpdateObjectivesCount", "fe_IncreaseObjectivesCount",
    "fe_SetObjectivesIconType", "fe_MissionSuccess", "fe_MissionFail",
    "fe_ShowObjective", "fe_CloseObjective", "fe_ShowBonusObjective",
    "fe_CloseBonusObjective", "fe_GetLastMissionObjectiveStringID",
    # mission/HUD text + dialogue
    "fe_GetMissionName", "fe_GetNumMissions", "fe_ShowHint", "fe_CloseHint",
    "fe_Dialogue", "fe_PromptDialogue", "fe_GetLocalizedString",
    "fe_GetFormattedLocalizedString", "fe_StartTypeWriter", "fe_StopTypeWriter",
    # debug
    "mm_DebugResetMission", "mm_DebugSetMissionStateDisplay",
    "mm_DebugSetMissionsUnlocked", "mm_DebugSetMissionUnlocked",
    "mm_DebugGetStreamSlotGroupLists", "mm_DebugGetStreamSlotFreeMemory",
    "mm_DebugGetFreeMemoryStats",
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["list", "strings", "json"])
    ap.add_argument("out", nargs="?")
    ap.add_argument("--dll", default=None)
    ap.add_argument("--prefix", default=None)
    ap.add_argument("--mission", action="store_true")
    ap.add_argument("--start", default=None, help="hex VA, for `strings`")
    ap.add_argument("--end", default=None, help="hex VA, for `strings`")
    a = ap.parse_args(argv)
    if not a.dll:
        a.dll = os.path.join(config.require_game_dir(), "prototype2engine.dll")

    pe = PE(a.dll)
    if a.command == "strings":
        lo = int(a.start, 16) if a.start else 0
        hi = int(a.end, 16) if a.end else 1 << 62
        for va, text in strings(pe):
            if lo <= va < hi:
                print(f"{va:08x}\t{text}")
        return 0

    api = api_names(pe)
    if a.command == "json":
        blob = {p: [{"va": f"0x{va:08x}", "name": n} for va, n in v] for p, v in api.items()}
        blob["missionCore"] = sorted(MISSION_CORE)
        dest = a.out or "analysis/lua-api.json"
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w") as f:
            json.dump(blob, f, indent=1)
        print(f"wrote {dest}: " + ", ".join(f"{p}={len(v)}" for p, v in api.items()))
        return 0

    # list
    fams = [a.prefix] if a.prefix else list(PREFIXES)
    total = 0
    for p in fams:
        rows = api.get(p, [])
        if a.mission:
            rows = [(va, n) for va, n in rows if n in MISSION_CORE]
        if not rows:
            continue
        print(f"\n## {p}  ({len(rows)})")
        for va, n in rows:
            print(f"  {va:08x}  {n}")
        total += len(rows)
    print(f"\n-- {total} name(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
