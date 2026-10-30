-- params : ...
-- function num : 0
mms_SpawnOfTheDeadIV = function(l_1_0, l_1_1, l_1_2, l_1_3, l_1_4)
  -- function num : 0_0
  if not l_1_3 then
    l_1_3 = AddressType.DeadSpawn
  end
  if l_1_1 and l_1_1 == "" then
    return 
  end
  if not l_1_2 then
    l_1_2 = "dead"
  end
  if not l_1_0 and l_1_2 ~= "crawling" then
    l_1_0 = DEAD_CIVILIAN_TEMPLATE
  end
  if not l_1_0 and l_1_2 == "crawling" then
    l_1_0 = WOI_CIVILIAN_TEMPLATE
  end
  local l_1_5 = address_FindAddressList({rootPath = l_1_1, maxResults = 10, type = l_1_3}) or {}
  for i_2,l_1_10 in pairs(l_1_5) do
    local l_1_11 = nil
    if l_1_2 == "crawling" then
      l_1_11 = ai_NonManagedSpawnAtAddress("crawling_" .. util_GetUniqueName(), l_1_0, l_1_10)
      go_SetHealth(l_1_11, 1)
      ai_SetScriptedTask(l_1_11, "flee", {})
    else
      ai_SetPolicy(l_1_11, 10000, 10000, 1, true)
      l_1_11 = ai_NonManagedSpawnAtAddress("body_" .. util_GetUniqueName(), l_1_0, l_1_10)
      go_ApplyDamageAndHit(l_1_11, 999999, Vector(0, 0, 0), Vector(0, -0.00999999978, 0), {hitType = "AlexClawSlash1Charged"})
    end
    go_AddTag(l_1_11, "DeadBeef")
  end
end

mms_SpawnOfTheDeadIV(WOI_CIVILIAN_TEMPLATE, "spawnofthedead", "dead", AddressType.DeadSpawn, "AlexClawSlash1Charged")
