
-- params : ...
-- function num : 0
evolvedTemplates = {"evolved_lancer_generic", "evolved_basic_generic", "evolved_tornado_generic"}
tmpltCount = 1
evolvedShell = function(l_1_0)
  -- function num : 0_0
  local l_1_1 = nil
  local l_1_2, l_1_3 = go_GetCoordinates(l_1_0)
  l_1_1 = ai_Spawn(MISSION_NAME .. "_EvolvedSoldier_" .. util_GetUniqueName(), evolvedTemplates[tmpltCount], l_1_2, l_1_3)
  ai_SetEvolvedShell(l_1_1, l_1_0)
  ai_SetFixedTarget(l_1_1, PLAYER)
  go_VariableNameSetGOH(l_1_1, "tactic", "aggressive")
  fes_AddMarker(l_1_1, Marker_Mission_Defeat)
  tmpltCount = tmpltCount + 1
end


