-- params : ...
-- function num : 0
ss_defend = function(l_1_0)
  -- function num : 0_0
  ai_SetDefendArea(l_1_0, Vector(-268.200012, 6.1500001, 16.1000004), Vector(1, 0, 0), 75)
  go_AddTag(l_1_0, "b2_superSoldier")
  go_AddTag(l_1_0, "b2_notStunned")
  go_SetBroadcastDamage(l_1_0, true)
  go_VariableIntSetGOH(l_1_0, "NoEarlyConsume", 1)
  ProtectSpawnedObject(l_1_0)
  if ai_IsCharacterFrozen(l_1_0) then
    local l_1_1 = ai_CanReserveActiveSlotBudget(PLAYER, 1, 0.5)
    if l_1_1 then
      ai_CharacterizeFromReserved(PLAYER, l_1_0)
      ai_SetUnDowngradable(l_1_0, true)
    end
    local l_1_2 = scheduleRealTime(0.25, scenario_PostEventString, "ss_spawned02", l_1_0)
    table.insert(scheduleTable, l_1_2)
    fe_FadeToBlack({fadeTime = 0})
  else
    scenario_PostEventString("ss_spawned02", l_1_0)
  end
  Consumables.Manager.AttachConsumable(l_1_0, nil, nil, {((Consumables.Clients).WebOfIntrigue).ProviderAPI})
end

configData = {noCleanup = true, 
encounterSetup = {
{name = "SS_Attack_base_" .. util_GetUniqueName(), positionSource = PLAYER, activeRadius = 1000, markerType = Marker_Mission_Defeat, 
templates = {
{template = SUPERSOLDIER_TEMPLATE, onSpawnCallback = ss_defend, gohName = "super_01", min = 1, max = 1, total = 1, 
respawnPositions = {
address = {rootPath = activationData.rootPath .. "|ssgroup01", maxResults = 2}
}
}
}
}
}
}
mms_ConfigureAsDefeatEncounter(configData)
