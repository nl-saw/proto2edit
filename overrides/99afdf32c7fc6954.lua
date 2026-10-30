-- params : ...
-- function num : 0
SPAWN_OMEGA_FREE = false
SpawnOfTheDeadOmega = function(l_0)
  -- function num : 0_0
  local l_1, l_2, l_3, l_4, l_5, l_6
  local l_7 = l_0.rootName
  local l_8 = {}
  local l_9 = function(l_10)
    -- function num : 0_0_0
    for l_13,l_14 in pairs(l_10) do
      ai_SetUnDowngradable(l_13, false)
    end
  end
  l_4 = l_0.addressType or AddressType.DeadSpawn
  if l_0.purge then
    DestroyAllTagged(l_0.tag or "DeadBeef")
  end
  if l_0.deadPath and l_0.deadPath == "" then
    return 
  end
  if not l_0.deadType then
    l_3 = "dead"
  end
  l_1 = l_0.template
  if not l_1 then
    if l_0.deadType ~= "crawling" then
      l_1 = DEAD_CIVILIAN_TEMPLATE
    end
  end
  if not l_1 and l_3 == "crawling" then
    l_1 = WOI_CIVILIAN_TEMPLATE
  end
  local l_10 = address_FindAddressList({rootPath = l_0.deadPath, maxResults = l_0.maxSpawn or 3, type = l_4}) or {}
  local l_11 = ai_CanReserveActiveSlotBudget(PLAYER, #l_10, 2)
  for l_15,l_16 in pairs(l_10) do
    local l_17, l_18, l_19 = nil, nil, nil
    local l_20 = l_0.initMotionBranch or "//alex_regular/scripted/actions/KillAfterSpawn/die"
    if l_0.rootName then
      l_19 = l_15
    else
      l_19 = util_GetUniqueName()
    end
    if l_3 == "crawling" then
      l_18 = l_0.rootName or "crawling_"
      l_17 = ai_NonManagedSpawnAtAddress(l_18 .. l_19, l_1, l_16)
      go_SetHealth(l_17, 1)
      ai_SetScriptedTask(l_17, "flee", {})
    else
      l_18 = l_0.rootName or "body_"
      l_17 = ai_NonManagedSpawnAtAddress(l_18 .. l_19, l_1, l_16)
      l_8[l_17] = true
      ai_CharacterizeFromReserved(PLAYER, l_17)
      ai_SetUnDowngradable(l_17, true)
      if l_1 ~= DEAD_CIVILIAN_TEMPLATE or l_0.initMotionBranch then
        util_PlayMotionBranch(l_17, l_20)
      else
        go_ApplyDamageAndHit(l_17, 999999, Vector(0, 0, 0), Vector(0, -0.00999999978, 0), {hitType = l_0.hitDescription})
      end
    end
    go_AddTag(l_17, l_0.tag or "DeadBeef")
    go_AddTag(l_17, "DeadBeef")
    local l_21 = go_GetGrabbedObject(l_17, "Holding")
    if go_IsValid(l_21) then
      go_DestroyObject(l_21, false)
    end
  end
  MissionSchedule(2, l_9, l_8)
end
