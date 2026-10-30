local gibbed_spawner_count = 1
local gibbed_spawner_type = "Soldier"
local gibbed_spawner_types =
{
  -- There are more to be added here but until I find a definite
  -- way to discover these names, I have to figure them out manually.
  "civilian_male", "civilian_generic", "civilian_female",
  "Soldier", "RocketSoldier", "SoldierHeadGearGasMask", "SoldierHeadGear",
  "SoldierHeadGearGasMaskNoSatchels",
  "Blackwatch", "BlackwatchCommander", "BlackwatchNoRifle",
  "BlackwatchCommanderNoRifle", "RocketBlackwatch", "GrenadeBlackwatch",
  "SawBlackwatch", "BlackwatchPilot", "BlackwatchPilotNoAI",
  "ConsumeEventBlackwatch", "SoldierPedestrian",
  "SoldierCollectibleRagdollTemplate", "BwOfficer", "TheHunter",
  "BlackwatchBaseDestruction", "KeyCommander",
  "apc_m2_marine", "apc_m2_blackwatch",
  "UndergroundBrawler", "BrawlerPackLeader",
  "bw_scientist_high_clearance",
  "heli_bh_marine_rocket", "heli_bh_marine_empty", "heli_bh_marine",
  "heli_bh_blackwatch_thermobaric_empty", "heli_bh_blackwatch_thermobaric",
  "heli_bh_blackwatch_supersoldier", "heli_bh_blackwatch_rocket",
  "heli_bh_blackwatch_empty", "heli_bh_blackwatch_empty_fix",
  "heli_bh_blackwatch",
}

function gibbed_Spawner_Spawn()
  for i = 1, gibbed_spawner_count, 1 do
    npcnum = npcnum or 0
    npcnum = npcnum + 1
    local pos = ai_GetCentrePos()
    local mob = ai_Spawn_CPP("gibbed_spawned_" .. npcnum, gibbed_spawner_type, pos, Vector(0, 0, 1))
  end
end

function gibbed_Spawner_Spawn_Safe()
  gibbed_Spawner_Spawn()
  --[[--
  local status, result = pcall(gibbed_Spawner_Spawn)
  table.insert(Pause_Debug_Options_Buttons, 1, {Label = "@" .. tostring(status), Function = "dummy_for_error"})
  table.insert(Pause_Debug_Options_Buttons, 2, {Label = "@" .. tostring(result), Function = "dummy_for_error"})
  --]]--
end

function gibbed_Spawner_SetCount(target)
  gibbed_spawner_count = tonumber(target)
end

function gibbed_Spawner_GetCount()
  return tostring(gibbed_spawner_count)
end

function gibbed_Spawner_SetType(target)
  gibbed_spawner_type = target
end

function gibbed_Spawner_GetType()
  return gibbed_spawner_type
end

function gibbed_Spawner_GenerateTypeEntries()
  local entries = {}
  for i, v in ipairs(gibbed_spawner_types)do
    table.insert(entries, {Name = v, Value = v})
  end
  return entries
end
