-- params : ...
-- function num : 0
a2ArriveAtbaseStringStarted = false
hellerHasBeenInAlert = false
stealthConsumeLinePlayed = false
FrontGateUsed = false
if a2customize01 == false then
  local l_0_0 = address_GetAddressByPath(activationData.rootPath .. "|key_commander|enc|spawnPos")
  keyCommander = ai_SpawnAtAddress("Commander", BLACKWATCH_COMMANDER_FULL_TEMPLATE, l_0_0)
  local l_0_1 = go_FindGOHByName("structure_yellow_facility_94_soldiers_interior_patrol")
  local l_0_2 = {path = l_0_1}
ai_SetTask(keyCommander, "patrol", l_0_2)
  go_AddTag(keyCommander, "KeyCommander")
  do ((Consumables.Manager).AttachConsumable)(keyCommander) end
  comp_AddStructureToCriticalList(MISSION_NAME, "structure_yellow_group005|structure_yellow_facility_94")
  a2customize01 = true
end
do
  if mission_IsRetry() then
  end
  if alertExitedinBase == false then
    scenario_PostEventString("ShowObjective")
  else
    Wait(0.300000012)
    scenario_PostEventString("A2FrontScannerUsed")
  end
end
