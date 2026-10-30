#!/usr/bin/env python3
"""Stage an entity spawn into a mission's Lua, using the call shape proven in game.

What is proven (docs/19, 2026-10-27): from a normal mission chunk, at gameplay start,

    ai_Spawn(name, TEMPLATE, position, direction)

spawns a tank, an APC, a Blackwatch helicopter and a brawler. `TANK_BLACKWATCH_TEMPLATE` and friends resolve at
runtime even though no shipped Lua defines them. The old "mission Lua cannot spawn" result was `ai_Spawn_CPP` with a
bare string where the engine expects a template.

This command writes that call into a record and stages it, with the logger attached so the result is visible in
`proto2_lualog.log` instead of guessed at.

    ./proto2 spawn --mission story_intro1 --template TANK_BLACKWATCH_TEMPLATE
    ./proto2 spawn --mission story_intro1 --template BRAWLER_TEMPLATE --count 3 --ring 6
    ./proto2 spawn --mission story_intro1 --template TANK_BLACKWATCH_TEMPLATE --record 31 --offset 8,0,0

The insertion point is either `--record N` or the mission's gameplay-start record: the one that mentions
`skipOpeningCinematics` or `MakeHellerHuman(`, which is where the opening cinematics hand over to the player.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import script_edit as SE                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Templates the shipped missions pass to CHARACTER_TEMPLATE, which is where these names come from.
KNOWN_TEMPLATES = (
    "TANK_BLACKWATCH_TEMPLATE", "APC_MARINE_TEMPLATE", "APC_BLACKWATCH_TEMPLATE",
    "HELI_BH_TEMPLATE", "HELI_BH_CRASHED_TEMPLATE", "GUNSHIP_TEMPLATE", "GUNSHIP_EMPTY_TEMPLATE",
    "BRAWLER_TEMPLATE", "BLACKWATCH_TEMPLATE", "BW_SCIENTIST_TEMPLATE", "BLACKWATCH_COMMANDER_FULL_TEMPLATE",
    "SUPERSOLDIER_TEMPLATE", "BEHEMOTH_TEMPLATE", "ALEX_BOSS_TEMPLATE", "INFECTED3_SLAMMER_TEMPLATE",
    "EVOLVED_2ND_RUNNER_TEMPLATE", "UAV_TEMPLATE", "FLYER_TEMPLATE", "GUN_TURRET_TEMPLATE",
)

# Verified in game on 2026-10-27 (docs/19): every one of these spawned and `ai_Spawn` returned no error.
# BEHEMOTH_TEMPLATE spawns too but destabilises the run - it is a boss-tier actor with no encounter context.
PROVEN = ("TANK_BLACKWATCH_TEMPLATE", "APC_MARINE_TEMPLATE", "HELI_BH_TEMPLATE", "BRAWLER_TEMPLATE",
          "BLACKWATCH_TEMPLATE", "INFECTED3_SLAMMER_TEMPLATE", "SUPERSOLDIER_TEMPLATE")
WARNING_TEMPLATES = {"BEHEMOTH_TEMPLATE": "spawns, but destabilises the game (boss-tier actor without an encounter)"}


def gameplay_record(mission, variant):
    """Index of the record that runs when the opening cinematics hand over to gameplay, or None."""
    got = SE.open_side(mission, variant, "staged") or SE.open_side(mission, variant, "shipped")
    if got is None:
        return None
    dec = SE._decoded(got[3])
    both, either = None, None
    for i, (_role, text, _how, _it) in enumerate(dec):
        if text is None:
            continue
        body = text if isinstance(text, str) else text.decode("latin-1")
        has_skip = "skipOpeningCinematics" in body
        has_human = "MakeHellerHuman(" in body
        if has_skip and has_human and both is None:
            both = i            # the record that hands the opening cinematics over to gameplay
        elif (has_skip or has_human) and either is None:
            either = i
    return both if both is not None else either


def retry_records(mission, variant):
    """Indices of every record holding an `if mission_IsRetry() then` branch.

    Each checkpointed stage owns its restoration, so the resume path enters whichever stage's record matches the
    checkpoint the player resumes at. Measured on story_intro1 (probe run, 2026-10-27): the behemoth checkpoint
    re-ran records #132 and #138, while the retry branch in #31 never executed. Inserting into *all* guarded
    records is therefore the only way to spawn at "retry" without knowing which checkpoint the player will hit.
    """
    got = SE.open_side(mission, variant, "staged") or SE.open_side(mission, variant, "shipped")
    if got is None:
        return []
    out = []
    for i, (_role, text, _how, _it) in enumerate(SE._decoded(got[3])):
        if text is None:
            continue
        body = text if isinstance(text, str) else text.decode("latin-1")
        if "if mission_IsRetry() then" in body:
            out.append(i)
    return out


def insert_after_retry(body, snippet):
    """Text with `snippet` inserted as the first statements inside the retry branch."""
    marker = "if mission_IsRetry() then"
    at = body.index(marker) + len(marker)
    # keep the newline that follows the marker, then indent the snippet one level deeper
    while at < len(body) and body[at] in "\r\n":
        at += 1
    indented = "".join("    " + ln if ln.strip() else ln for ln in snippet.splitlines(True))
    return body[:at] + indented + body[at:]


def build_snippet(template, record_label, offset, count, ring, code=90, tag=None):
    lines = []
    lines.append('local proto2Spawned = nil')
    if ring:
        lines.append('local proto2SpawnPos = go_GetPosition(PLAYER)')
        lines.append('for proto2I = 1, %d do' % count)
        lines.append('    local proto2Offset = Vector(math.cos(proto2I) * %g, 0, math.sin(proto2I) * %g)'
                     % (ring, ring))
    else:
        lines.append('local proto2SpawnPos = go_GetPosition(PLAYER) + Vector(%g, %g, %g)' % tuple(offset))
        lines.append('for proto2I = 1, %d do' % count)
        lines.append('    local proto2Offset = Vector(0, 0, 0)')
    lines.append('    pcall(proto2_log, %d, "spawn %s #" .. proto2I)' % (code, record_label))
    lines.append('    local proto2Ok, proto2Err = pcall(function()')
    lines.append('        proto2Spawned = ai_Spawn("%s_" .. proto2I, %s, proto2SpawnPos + proto2Offset, Vector(0, 0, 1))'
                 % (record_label, template))
    lines.append('    end)')
    if tag:
        lines.append('    if proto2Ok and proto2Spawned then go_AddTag(proto2Spawned, "%s") end' % tag)
    lines.append('    pcall(proto2_log, %d, "spawn %s", proto2Ok and 1 or 0, proto2Err)' % (code, record_label))
    lines.append('end')
    return [ln + "\n" for ln in lines]


def encounter_record(mission, variant):
    """(index, text, variant) of the record that initialises a structure mission, or (None, None, None)."""
    for var in (variant, "open", "active"):
        for side in ("staged", "shipped"):
            got = SE.open_side(mission, var, side)
            if got is None:
                continue
            for i, (_role, text, _how, _it) in enumerate(SE._decoded(got[3])):
                if text is None:
                    continue
                body = text if isinstance(text, str) else text.decode("latin-1")
                if "comp_InitializeStructureMission" in body:
                    return i, body, var
    return None, None, None


def insert_encounter(body, entry_lua):
    """Insert one spawner entry into the ENCOUNTERS table of a structureDef, before its closing brace."""
    key = "ENCOUNTERS = {"
    best = None
    pos = body.find(key)
    while pos >= 0:
        i = pos + len(key) - 1        # at the opening brace
        depth = 0
        while i < len(body):
            c = body[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    block = body[pos:i]
                    # the spawner table is the one that carries spawner fields (PATROL_INFO / ACTIVE_RADIUS /
                    # CHARACTER_TEMPLATE with a template global); a nested component table often holds only an
                    # encounter1 stub, so prefer a block that looks like fielded spawners
                    score = block.count("PATROL_INFO") * 3 + block.count("CHARACTER_TEMPLATE") + block.count("ACTIVE_RADIUS")
                    if best is None or score > best[0]:
                        best = (score, i)
                    break
            i += 1
        pos = body.find(key, pos + 1)
    if best is None:
        return None
    at = best[1]
    return body[:at] + entry_lua + body[at:]


def add_template_record(mission, variant):
    """(index, text, variant, encounter_var) for a record that calls <enc>:AddTemplate(...), or Nones.

    This is the declarative spawner shape story missions use (story_f2 mole_enc:AddTemplate with
    TANK_BLACKWATCH_TEMPLATE, min/max/total and a SpawnStreet resume address). Unlike a structure ENCOUNTERS
    table it exists in ordinary story missions, which is what makes it testable in game.
    """
    for var in (variant, "open", "active"):
        for side in ("staged", "shipped"):
            got = SE.open_side(mission, var, side)
            if got is None:
                continue
            for i, (_role, text, _how, _it) in enumerate(SE._decoded(got[3])):
                if text is None:
                    continue
                body = text if isinstance(text, str) else text.decode("latin-1")
                if ":AddTemplate(" in body and "template =" in body:
                    import re
                    match = re.search(r"([A-Za-z_]\w*)\s*:\s*AddTemplate\s*\(", body)
                    if not match:
                        continue
                    return i, body, var, match.group(1)
    return None, None, None, None


def stage_add_template(a, template, label):
    index, body, variant, enc = add_template_record(a.mission, a.variant)
    if index is None:
        return None
    text = (f"\n\nlocal proto2_{label} = {enc}:AddTemplate( \n"
            f"    {{ \n"
            f"        template = {template}, \n"
            f"        name = \"proto2_{label}\", \n"
            f"        min = 1, \n"
            f"        max = 1, \n"
            f"        total = 1, \n"
            f"        tags = {{\"proto2_{label}\"}}, \n"
            f"        respawnPositions = {{ address = {{ type = AddressType.SpawnStreet, maxResults = 1, "
            f"searchCondition = AddressSearchCondition.Furthest, origin = PLAYER }} }}, \n"
            f"    }} \n"
            f")\n")
    # insert after the end of the last AddTemplate call in this record
    at = body.rfind(")\n")
    at = body.find(")", body.rfind(":AddTemplate("))
    depth = 0
    i = body.find("(", body.rfind(":AddTemplate("))
    while i < len(body):
        if body[i] == "(":
            depth += 1
        elif body[i] == ")":
            depth -= 1
            if depth == 0:
                at = i + 1
                break
        i += 1
    new_text = body[:at] + text + body[at:]
    print(f"record #{index}: adding {enc}:AddTemplate with {template}")
    if a.dry_run:
        print(text)
        return 0
    rc = SE.stage_record_source(a.mission, variant, index, new_text)
    print(f"staged (rc={rc}); run ./proto2 install, then load {a.mission}")
    return 0


def stage_encounter(a, template, label, offset):
    rc = stage_add_template(a, template, label)
    if rc is not None:
        return rc
    index, body, variant = encounter_record(a.mission, a.variant)
    if index is None:
        raise SystemExit(f"{a.mission} has no structure component (comp_InitializeStructureMission), so it has no "
                         f"ENCOUNTERS table to add to. Use the ai_Spawn timings (--when start/checkpoint) instead, or "
                         f"pick a mission that ships a structureDef.")
    entry = (f"\n    {label} = {{CHARACTER_TEMPLATE = {template}, ACTIVE_RADIUS = 150, DEFEND_RADIUS = 50}},"
             f"  -- added by proto2 spawn")
    new_text = insert_encounter(body, entry)
    if new_text is None:
        raise SystemExit("could not find an ENCOUNTERS = { ... } table in the structure record")
    a.variant = variant
    print(f"record #{index}: adding an ENCOUNTERS entry '{label}' with template {template}")
    print(entry.strip())
    if a.dry_run:
        return 0
    rc = SE.stage_record_source(a.mission, a.variant, index, new_text)
    print(f"staged (rc={rc}); run ./proto2 install, then load a mission that uses this structure")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="stage an entity spawn into a mission's Lua")
    ap.add_argument("--mission", default=None)
    ap.add_argument("--variant", default="active")
    ap.add_argument("--template", default=None,
                    help="a template global such as TANK_BLACKWATCH_TEMPLATE, or a quoted string")
    ap.add_argument("--record", type=int, default=None,
                    help="record to insert into (default: the gameplay-start record)")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--ring", type=float, default=0.0,
                    help="spread N spawns on a ring of this radius instead of one spot")
    ap.add_argument("--offset", default="0,0,0", help="x,y,z offset from the player")
    ap.add_argument("--code", type=int, default=90, help="log code so several tests are distinguishable")
    ap.add_argument("--when", default="start", choices=("start", "checkpoint", "both", "encounter"),
                    help="spawn when gameplay starts, when the player is restored at a checkpoint, or both")
    ap.add_argument("--tag", default=None,
                    help="tag each spawned entity (go_AddTag) so it can be found or cleaned up later")
    ap.add_argument("--list", action="store_true", help="show the template names this tool knows and their status")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if not a.list and not a.mission:
        raise SystemExit("--mission is required (use --list to see template names)")
    if a.list:
        print(f"{'template':34} status")
        for t in KNOWN_TEMPLATES:
            if t in WARNING_TEMPLATES:
                status = "spawns, but destabilises: " + WARNING_TEMPLATES[t]
            elif t in PROVEN:
                status = "spawn-verified in game"
            else:
                status = "known template name, not spawn-tested"
            print(f"{t:34} {status}")
        return 0

    template = a.template
    bare = template.strip('"')
    if not a.template:
        raise SystemExit("--template is required (use --list to see the ones this tool knows)")
    template = a.template
    if bare in WARNING_TEMPLATES:
        print(f"warning: {bare}: {WARNING_TEMPLATES[bare]}", file=sys.stderr)
    if bare in KNOWN_TEMPLATES and bare not in PROVEN:
        print(f"note: {bare} is a real template global but has not been spawn-tested yet (proven: "
              f"{', '.join(PROVEN)})", file=sys.stderr)
    if bare not in KNOWN_TEMPLATES and not template.startswith('"'):
        print(f"warning: {bare} is not among the template names the shipped missions use; if the global is nil the "
              f"spawn will throw and the logger will say so", file=sys.stderr)

    if a.when == "encounter":
        label0 = bare.replace('"', "").lower().replace("_template", "")
        return stage_encounter(a, template, label0, (0.0, 0.0, 0.0))

    index = a.record if a.record is not None else gameplay_record(a.mission, a.variant)
    if index is None:
        raise SystemExit("could not find a gameplay-start record in this mission; pass --record N "
                         "(./proto2 script --mission M list shows them)")

    got = SE.open_side(a.mission, a.variant, "staged") or SE.open_side(a.mission, a.variant, "shipped")
    rows = SE.records_with_roles(got[3])
    if not (0 <= index < len(rows)):
        raise SystemExit(f"record {index} out of range (mission has {len(rows)})")
    dec = SE._decoded(got[3])
    text = dec[index][1]
    if text is None:
        raise SystemExit(f"record {index} has no source text (bytecode or locked); pick a source record")

    body = text if isinstance(text, str) else text.decode("latin-1")
    label = bare.replace('"', "").lower().replace("_template", "")
    offset = tuple(float(x) for x in a.offset.split(",")) if a.offset else (0.0, 0.0, 0.0)
    if len(offset) != 3:
        raise SystemExit("--offset wants x,y,z")
    snippet = "".join(build_snippet(template, label, offset, a.count, a.ring, a.code, a.tag))

    # insert after the leading comment lines so the record still starts with its own header
    lines = body.splitlines(True)
    at = 0
    while at < len(lines) and lines[at].lstrip().startswith("--"):
        at += 1
    new_text = "".join(lines[:at]) + snippet + "".join(lines[at:])

    plan = []
    if a.when in ("start", "both"):
        plan.append((index, "gameplay start"))
    if a.when in ("checkpoint", "both"):
        guarded = retry_records(a.mission, a.variant)
        if not guarded:
            print("warning: no `if mission_IsRetry() then` branch found in this mission; skipping the retry spawn",
                  file=sys.stderr)
        for r_index in guarded:
            plan.append((r_index, "retry branch"))

    print(f"{a.count} x {template}; {len(plan)} insertion(s): "
          + ", ".join(f"#{i} ({why})" for i, why in plan))
    if a.dry_run:
        print("--- would insert ---")
        print(snippet)
        return 0
    # stage one edit at a time and re-read the record each time: the second edit must build on the first,
    # otherwise the retry insertion silently throws the gameplay-start insertion away
    for i, why in plan:
        got = SE.open_side(a.mission, a.variant, "staged") or SE.open_side(a.mission, a.variant, "shipped")
        body_now = SE._decoded(got[3])[i][1]
        body_now = body_now if isinstance(body_now, str) else body_now.decode("latin-1")
        if why == "retry branch":
            key = f"proto2_runs_{i}"
            latch = (f"{key} = ({key} or 0) + 1\n"
                     f"if {key} > 1 then\n")
            indented = "".join("    " + ln if ln.strip() else ln for ln in snippet.splitlines(True))
            lines_now = body_now.splitlines(True)
            at = 0
            while at < len(lines_now) and lines_now[at].lstrip().startswith("--"):
                at += 1
            text = ("".join(lines_now[:at]) + latch + indented + "end\n" + "".join(lines_now[at:]))
        else:
            lines_now = body_now.splitlines(True)
            at = 0
            while at < len(lines_now) and lines_now[at].lstrip().startswith("--"):
                at += 1
            text = "".join(lines_now[:at]) + snippet + "".join(lines_now[at:])
        rc = SE.stage_record_source(a.mission, a.variant, i, text)
        print(f"  staged record #{i} ({why}), rc={rc}")
    print("run ./proto2 install, then start the mission and read proto2_lualog.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
