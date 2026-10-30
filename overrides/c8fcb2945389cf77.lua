-- params : ...
-- function num : 0
configData = {}

configData.encounterSetup = {}
mms_ConfigureAsDefeatEncounter(configData)
local l_0_0, l_0_1 = nil, nil
l_0_0 = enc_DeclareEncounter({name = "jakTestEncounter", positionSource = go_GetPosition(PLAYER), activeRadius = 10, waveSpawnCallback = function(l_1_0)
  -- function num : 0_0
end
})
l_0_1 = l_0_0:AddTemplate({template = BRAWLER_TEMPLATE, onSpawnCallback = function(l_2_0)
  -- function num : 0_1
  ai_SetScriptedTask(l_2_0, "idle", {})
  go_SetHealth(l_2_0, go_GetHealth(l_2_0) / 10)
  local l_2_1, l_2_2 = nil, nil
  l_2_1, l_2_2 = go_GetCoordinates(l_2_0)
end
, min = 1, max = 1, total = 1})
l_0_1:AddPositions({cloud = CloudParams.brawler})
table.insert(configData.encounterSetup, l_0_0)
