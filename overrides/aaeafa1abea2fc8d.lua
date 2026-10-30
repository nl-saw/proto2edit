-- params : ...
-- function num : 0
for l_0_3,l_0_4 in pairs(g1_destroy_props) do
  if l_0_4.goh == (context.event).object then
    l_0_4.destroyed = true
  end
end
g1_propsKilled = g1_propsKilled + 1
local l_0_5 = (context.event).originator
if g1_propsKilled == 1 and not g1_firstDestroyed then
  if disguise_IsInRed(PLAYER) == true then
    if go_GetFaction(PLAYER) == UID("alex") or go_GetFaction(PLAYER) == UID("powered") then
      if ai_IsPlayerInVehicle(PLAYER) == false then
        snd_PlayDialogueSequence("g1_log_050", "g1_log_050_over")
      end
    else
      snd_PlayDialogueByName("virtual", "g1_log_100_010_cantrell_blackwatch_01", "none", "cantrell_blackwatch")
    end
  else
    snd_PlayDialogueByName("virtual", "g1_log_100_030_soldier_blackwatch_a2_1_01", "none", "soldier_blackwatch_a2_1")
  end
elseif g1_propsKilled == 2 and not g1_rocketEntered then
  g1_rocketEntered = true
  scenario_PostEventString("halfway")
  if disguise_IsInRed(PLAYER) == true then
    snd_PlayDialogueSequence("g1_log_070", "g1_log_070_over")
  else
    if go_GetFaction((context.event).giver) == UID("infectedSummon") then
      snd_PlayDialogueSequence("g1_log_063", "g1_log_063_over")
    else
      snd_PlayDialogueSequence("g1_log_090", "g1_log_090_over")
    end
  end
end
