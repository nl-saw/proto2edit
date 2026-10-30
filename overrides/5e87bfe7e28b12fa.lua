-- params : ...
-- function num : 0
configData = {}

configData.encounterSetup = {}
mms_ConfigureAsDefeatEncounter(configData)
local l_0_0, l_0_1 = nil, nil
l_0_0 = enc_DeclareEncounter({name = "SSSquad1", positionSource = go_GetPosition(PLAYER), activeRadius = 150, waveSpawnCallback = function(l_1_0)
  -- function num : 0_0
end
, markerType = false})
l_0_1 = l_0_0:AddTemplate({template = SUPERSOLDIER_TEMPLATE, onSpawnCallback = function(l_2_0)
  -- function num : 0_1
  ai_SetScriptedTask(l_2_0, "idle", {})
  go_SetHealth(l_2_0, 999)
  ai_SetExclusiveTask(l_2_0, "stand_attack", {})
  ai_SetPolicy(l_2_0, 999, 999, 1, true)
  ai_SetNpcDamageMultiplier(l_2_0, 100)
  go_AddTag(l_2_0, "Story_e3_SSS")
  go_VariableIntSetGOH(goh, "hunterNoStun", 1)
end
, min = 1, max = 1, total = 1})
l_0_1:AddPositions({
address = {rootPath = activationData.rootPath .. "|SSSpawnPoints", type = AddressType.SpawnStreet, maxResults = 3}
})
table.insert(configData.encounterSetup, l_0_0)
