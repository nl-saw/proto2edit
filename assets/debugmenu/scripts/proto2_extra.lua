-- proto2-extra: loaded from scripts/debug_boot.lua via pcall.
--
-- Must not call engine managers at load: it runs during startup, before the
-- world exists. Add helpers as named globals for the debug page to call, and
-- verify each one interactively before trusting it.
--
-- NOTE: mm_DebugSetMissionsUnlocked(true) was tried and tears down the world
-- (black screen, endless load). Use the debug page's "Go To Mission" instead,
-- which calls mms_DebugStartMission and works.

PROTO2 = { version = "0.4" }

-- Every helper below is wrapped in pcall. The debug page calls these globals by
-- name, so an uncompensated Lua error would propagate into the menu handler;
-- wrapping keeps each experiment recoverable.

local function safe(fn, ...)
  if type(fn) ~= "function" then
    return "missing: " .. tostring(fn)
  end
  local ok, err = pcall(fn, ...)
  return ok and "ok" or ("error: " .. tostring(err))
end

-- Zero-argument bindings are the signature-unambiguous ones. fe_ClearObjectives
-- and fe_MissionSuccess are registered with Method0<void()>, so calling them
-- needs no argument guesswork. Use these first to prove that mission state is
-- controllable from our own Lua at all: if the HUD's objective list disappears,
-- scripted mission control is confirmed and the argument-taking bindings become
-- worth reverse-engineering properly.

function PROTO2_ClearObjectives()
  return safe(fe_ClearObjectives)
end

function PROTO2_MissionSuccess()
  return safe(fe_MissionSuccess)
end

-- Argument-taking bindings: signatures are INFERRED from the mangled Method1/2/3
-- types, NOT verified. `safe` means a wrong guess returns an error string
-- instead of killing the session. Try this only after the zero-argument test.

function PROTO2_TryAddObjective()
  local tries = {
    function() return fe_AddObjectives("proto2_test", "PROTO2_TEST_OBJECTIVE") end,
    function() return fe_AddObjectives("PROTO2_TEST_OBJECTIVE", "proto2_test") end,
    function() return fe_AddObjectives("PROTO2_TEST_OBJECTIVE") end,
  }
  local log = {}
  for i, t in ipairs(tries) do
    local ok, err = pcall(t)
    log[#log + 1] = i .. ":" .. (ok and "ok" or tostring(err))
    if ok then break end
  end
  return table.concat(log, " | ")
end

-- Spawning is independent of the META decode: the engine exposes
-- ai_Spawn_CPP(name, template, position, forward) and ai_GetCentrePos().
-- Template names come from data, so a wrong name is expected to fail, which is
-- why this is a debug button and not something that runs automatically.
function PROTO2_SpawnNearby(template)
  template = template or "Soldier"
  return safe(function()
    return ai_Spawn_CPP("proto2_spawned", template, ai_GetCentrePos(), Vector(0, 0, 1))
  end)
end

-- Alex (the NPC) has no runtime lookup binding - there is no ai_GetEntityPos in the
-- exposed API - so this uses a recorded Address coordinate from
-- art/missions/story_intro1/active/tod.p3d.rz.
-- SpawnMercerGoto3 (-352.827, 19.333, 595.784), Address_4705433, is the marker the
-- owner identified as where Alex actually stands. SpawnMercer (-395.43, 16.28,
-- 603.24) is his opening marker ~43 units away and was the wrong one to use.
function PROTO2_SpawnAroundAlex(count)
  count = tonumber(count) or 4
  if count < 1 then count = 1 end
  if count > 12 then count = 12 end
  local ax, ay, az = -352.827, 19.333, 595.784
  local made = 0
  for i = 1, count do
    local n = i - (count + 1) / 2
    local ok = safe(function()
      return ai_Spawn_CPP("proto2_alex_" .. i, "Soldier",
                          Vector(ax + n * 2.0, ay, az + n * 2.0), Vector(0, 0, 1))
    end)
    if ok then made = made + 1 end
  end
  return made
end

-- ---------------------------------------------------------------------------
-- proto2: live XYZ readout in the bottom-left corner.
--
-- Implemented by proto2_lualog.asi (proto2_hud), which creates a layered, click-through window and paints
-- the numbers with GDI - no engine hooking, so it works in menus and in any mission. This driver lives in a
-- debug-menu script because those load at boot, before any mission: the readout is then available
-- everywhere rather than only in whichever mission happens to carry a driver.
--
-- Everything is defensive: PLAYER may not exist at boot, WaitFrames may not be available outside a mission
-- context, and calling into a missing binding would otherwise abort the script that the debug menu needs.
-- ---------------------------------------------------------------------------
PROTO2_XYZ_ENABLED = true

function PROTO2_PushXYZ()
  if not proto2_hud then return false end
  local ok, p = pcall(function() return go_GetPosition(PLAYER) end)
  if not ok or not p then return false end
  local ok2 = pcall(proto2_hud, p.x, p.y, p.z)
  return ok2
end

function PROTO2_StartXYZ()
  -- One push only. Looping here cannot work: a Lua 5.1 yield cannot cross a pcall boundary, so
  -- pcall(WaitFrames, 1) fails and any loop built on it dies after one frame. The real per-frame driver
  -- lives in the mission record (story_a1/active record 0), where Wait is called bare in the FSM coroutine.
  return PROTO2_PushXYZ() and "pushed once (per-frame loop lives in the mission record)" or "no player yet"
end


-- The bottom-left/objective text readout has been removed: the objective line renders with the mission's
-- stylised font and effects, which makes digits unreadable, and writing it hijacks the mission's own
-- objectives. The XYZ readout is now driven from the mission record (story_a1/active record 0), which loops
-- with a BARE Wait and calls proto2_hud -- the top-left overlay painted by proto2_lualog.asi.
-- PROTO2_XYZRetryText stays as a thin alias because the debug page still has a button for it.
function PROTO2_XYZRetryText()
  return PROTO2_XYZ_Retry()
end

-- Start any valid mission by name, bypassing the world marker. The call the third-party menu uses
-- (mms_DebugStartMission in datainit.lua), with its real signature: (name, unused, resetWorldCells).
-- Evidence: starting a mission whose content was transplanted from another one works this way, while its world
-- marker refuses to open it (docs/30).
function PROTO2_StartMission(name, reset_world)
    return pcall(mms_DebugStartMission, name, nil, reset_world == nil or reset_world)
end


return PROTO2
