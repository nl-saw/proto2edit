-- Automatic spawn driven from the debug-menu Lua VM.
--
-- Why here and not in mission Lua: mission FSM snippets crash when they call
-- ai_Spawn_CPP (three attempts, docs/19). The debug VM spawns fine, so the trick is
-- to get *this* VM to run at the right moment instead of loading the spawn into the
-- mission.
--
-- The trigger is fe_RegisterEvent, whose signature is not documented. Three
-- plausible orderings are attempted, each inside pcall, so a wrong guess is an
-- inert failure rather than a crash. Whatever fires first wins; the rest are no-ops.

local ALEX = { -352.827, 19.333, 595.784 }   -- SpawnMercerGoto3, Address_4705433
local COUNT = 4
local fired = false

local function spawn_squad()
  if fired then return end
  fired = true
  local made = 0
  for i = 1, COUNT do
    local n = i - (COUNT + 1) / 2
    local ok = pcall(ai_Spawn_CPP, "proto2_auto_" .. i, "Soldier",
                     Vector(ALEX[1] + n * 2.0, ALEX[2], ALEX[3] + n * 2.0),
                     Vector(0, 0, 1))
    if ok then made = made + 1 end
  end
  if print then print("[proto2] autospawn made " .. made .. " soldier(s)") end
end

local EVENTS = { "cryForHelp", "Intro1_MercerOnScreen", "SkipABeat" }

for _, ev in ipairs(EVENTS) do
  pcall(fe_RegisterEvent, ev, spawn_squad)
  pcall(fe_RegisterEvent, spawn_squad, ev)
end

-- Also expose it manually, so the same code can be triggered from the menu if the
-- registration signature turns out to be wrong.
PROTO2 = PROTO2 or {}
PROTO2.SpawnAuto = spawn_squad
PROTO2.SpawnAroundAlex = PROTO2.SpawnAroundAlex or function() spawn_squad() end

return PROTO2
