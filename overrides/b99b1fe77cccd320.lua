-- params : ...
-- function num : 0
configData = {}

configData.encounterSetup = {}
mms_ConfigureAsDefeatEncounter(configData)
local l_0_0, l_0_1 = nil, nil
l_0_0 = enc_DeclareEncounter({name = "PackFodderEnc", positionSource = go_GetPosition(PLAYER), activeRadius = 150, waveSpawnCallback = function(l_1_0)
  -- function num : 0_0
end
, OnWaveFinishedInitialSpawn = function(l_2_0)
  -- function num : 0_1
  tanks = go_GetAllTagged("PackFodder")
  go_AddTag(tanks[2], "Tutorial_Target")
end
, markerType = Marker_Mission_Defeat})
l_0_1 = l_0_0:AddTemplate({template = TANK_BLACKWATCH_TEMPLATE, onSpawnCallback = function(l_3_0)
  -- function num : 0_2
  e3_pathIndx = e3_pathIndx or 1
  local l_3_1 = go_FindGOHByName("story_e3_death_march_path_" .. e3_pathIndx)
  ai_SetScriptedTask(l_3_0, "patrol", {path = l_3_1})
  e3_pathIndx = (e3_pathIndx == 1) and 2 or 1
  go_AddTag(l_3_0, "PackFodder")
end
, min = 2, max = 2, total = 2})
l_0_1:AddPositions({
address = {rootPath = activationData.rootPath .. "|Exterior|Tanks", type = AddressType.SpawnStreet, maxResults = 2}
})
table.insert(configData.encounterSetup, l_0_0)
