-- params : ...
-- function num : 0
g3_objectiveID = fes_ShowMissionObjective("$G3_DESTROY_THERMO_0")
g3_restoreHelis = true
g3_heliDestroyed_02 = 0
g3_cam_helis = {}
local l_0_0 = mission_GetRestoreData()
if l_0_0 then
  if l_0_0.HeliRestoreBool == true then
    g3_spawnRestoreHelis = true
    scenario_PostEventString("g3_spawnRestoreHelis")
  elseif l_0_0.HeliRestoreBool == false then
  end
end
if mission_IsRetry() then
  scenario_PostEventString("g2_StartEnterThermoBonus")
end
if go_IsValid(CAMChopper1) == true then
  go_DestroyObject(CAMChopper1)
end
local l_0_1 = go_GetAllTagged("g3_cam_heli")
for l_0_5,l_0_6 in pairs(l_0_1) do
  go_KillCharacter(l_0_6)
end
go_SetBroadcastDamage(PLAYER, true)
g3_defended_helis = {}
g3_helis_downed = 0
g3_rocketHits = 0
local l_0_7 = function()
  -- function num : 0_0
  g3_cleanUpTable_0 = {}
  g3_cleanUpTable_0 = go_GetAllTagged("g3_thermo1_group1")
  if ai_IsPlayerInVehicle(PLAYER) == true then
    local l_1_0 = go_GetAttachedTo(PLAYER)
    if go_IsHelicopter(l_1_0) == true then
      for l_1_4,l_1_5 in pairs(g3_cleanUpTable_0) do

        if l_1_0 == l_1_5 then
          g3_cleanUpTable_0[l_1_4] = nil
        end
      end
    end
  end
  scenario_PostEventString("cleanUp_leftOverHelis")
end

schedule(0.699999988, l_0_7)
local l_0_8 = function()
  -- function num : 0_1
  scenario_PostEventString("g3_hurryDialogue_01")
end

schedule(20, l_0_8)
