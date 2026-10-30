-- params : ...
-- function num : 0
SpawnOfTheDead = function(l_1_0, l_1_1, l_1_2)
  -- function num : 0_0
  if l_1_2 then
    DestroyAllTagged("DeadBeef")
  end
  if l_1_0 and l_1_0 ~= "" then
    local l_1_3 = address_FindAddressList({rootPath = l_1_0, maxResults = 10, type = AddressType.DeadSpawn}) or {}
    for l_1_7,l_1_8 in pairs(l_1_3) do
      local l_1_9 = ai_NonManagedSpawnAtAddress("deadBeef_" .. util_GetUniqueName(), DEAD_CIVILIAN_TEMPLATE, l_1_8)
      MissionSchedule(0.5, go_ApplyDamageAndHit, l_1_9, 0, Vector(0, 0, 0), Vector(0, -0.00999999978, 0))
      go_AddTag(l_1_9, "DeadBeef")
    end
  end
  if l_1_1 and l_1_1 ~= "" then
    local l_1_3 = address_FindAddressList({rootPath = l_1_0, maxResults = 10, type = AddressType.DeadSpawn}) or {}
    for l_1_7,l_1_8 in pairs(l_1_3) do
      local l_1_9 = ai_NonManagedSpawnAtAddress("crawlingBeef_" .. util_GetUniqueName(), WOI_CIVILIAN_TEMPLATE, l_1_8)
      ProtectSpawnedObject(l_1_9, 2)
      go_SetHealth(l_1_9, 1)
      ai_SetScriptedTask(l_1_9, "flee", {})
      go_AddTag(l_1_9, "DeadBeef")
    end
  end
end

SpawnOfTheDeadII = function(l_2_0, l_2_1, l_2_2, l_2_3)
  -- function num : 0_1
  if not l_2_2 then
    l_2_2 = AddressType.DeadSpawn
  end
  if l_2_3 then
    DestroyAllTagged("DeadBeef")
  end
  if l_2_0 and l_2_0 == "" then
    return 
  end
  local l_2_4 = address_FindAddressList({rootPath = l_2_0, maxResults = 10, type = l_2_2}) or {}
  for l_2_8,l_2_9 in pairs(l_2_4) do
    local l_2_10 = nil
    if l_2_1 == "crawling" then
      l_2_10 = ai_NonManagedSpawnAtAddress("crawling_" .. util_GetUniqueName(), WOI_CIVILIAN_TEMPLATE, l_2_9)
      go_SetHealth(l_2_10, 1)
      ai_SetScriptedTask(l_2_10, "flee", {})
    else
      l_2_10 = ai_NonManagedSpawnAtAddress("body_" .. util_GetUniqueName(), DEAD_CIVILIAN_TEMPLATE, l_2_9)
      go_ApplyDamageAndHit(l_2_10, 0, Vector(0, 0, 0), Vector(0, -0.00999999978, 0))
    end
    go_AddTag(l_2_10, "DeadBeef")
  end
end

SpawnOfTheDeadIII = function(l_3_0, l_3_1, l_3_2, l_3_3, l_3_4)
  -- function num : 0_2
  if not l_3_3 then
    l_3_3 = AddressType.DeadSpawn
  end
  if l_3_4 then
    DestroyAllTagged("DeadBeef")
  end
  if l_3_1 and l_3_1 == "" then
    return 
  end
  if not l_3_2 then
    l_3_2 = "dead"
  end
  if not l_3_0 and l_3_2 ~= "crawling" then
    l_3_0 = DEAD_CIVILIAN_TEMPLATE
  end
  if not l_3_0 and l_3_2 == "crawling" then
    l_3_0 = WOI_CIVILIAN_TEMPLATE
  end
  local l_3_5 = address_FindAddressList({rootPath = l_3_1, maxResults = 10, type = l_3_3}) or {}
  for l_3_9,l_3_10 in pairs(l_3_5) do
    local l_3_11 = nil
    if l_3_2 == "crawling" then
      l_3_11 = ai_NonManagedSpawnAtAddress("crawling_" .. util_GetUniqueName(), l_3_0, l_3_10)
      go_SetHealth(l_3_11, 1)
      ai_SetScriptedTask(l_3_11, "flee", {})
    else
      l_3_11 = ai_NonManagedSpawnAtAddress("body_" .. util_GetUniqueName(), l_3_0, l_3_10)
      util_PlayMotionBranch(l_3_11, "//hitreaction/old/human/ragdoll/stopped/dead")
    end
    go_AddTag(l_3_11, "DeadBeef")
  end
end
