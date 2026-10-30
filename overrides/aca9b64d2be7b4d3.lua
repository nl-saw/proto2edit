-- params : ...
-- function num : 0
lab_key_scientist = function(l_1_0)
  -- function num : 0_0
  go_AddTag(l_1_0, "I3_Assistant")
  local l_1_1 = go_FindGOHByName("I3_Assistant_Path")
  local l_1_2 = {path = l_1_1}
  ai_SetIdleTask(l_1_0, "patrol", l_1_2)
  ProtectSpawnedObject(l_1_0, 1)
  go_VariableIntSetGOH(l_1_0, "smartNodeSearchAllowed", 1)
  local l_1_3 = go_FindGOHByName("breweryLabTable003_036", "Cell_18")
  if go_IsValid(l_1_3) then
    pm_ActivateProp(l_1_3)
    ai_UseSmartNode(l_1_0, l_1_3, "typer_0")
  end
end

structureDef = {
COMPONENTS = {
CRITICAL_CHARACTERS = {lab_key = mms_CreateCriticalCharacterComponent("bw_scientist_2008", "STORY_I3_LAB_KEY", false, 1, Marker_Objective_Consume_Key, false, lab_key_scientist, nil, nil, 99999)}
, 
SECURE_DOORS = {
secureDoor = {
COMPONENTS = {
HAND_SCANNERS = {scanner1 = mms_CreateHandScanner(nil, BaseEntry.scanToEntryBranch, false, false, false, nil, nil, nil)}
}
, 
LOCKED_BY = {"scanner1"}
, 
ACCESS_LINK = {MISSION = "structure_red_group003", START_ADDRESS = "structure_red_blackwatch_253|accessLink_startPos"}
}
, 
LabDoor = {
COMPONENTS = {
HAND_SCANNERS = {scanner1 = mms_CreateHandScanner({"STORY_I3_LAB_KEY"}, BaseEntry.scanToEntryBranch, false, false, true, nil, nil, nil)}
}
, 
LOCKED_BY = {"scanner1"}
}
}
, 
PERIMETERS = {
interiorPerimeter = {
RESTRICTED_AREAS = {"brewery_restricted_area"}
, SHOW_ON_MINIMAP = false, 
PROPS = {iat_BreweryProps_BrewingRoom, iat_BreweryProps_GarageRoom, iat_BreweryProps_GarageSideRoom}
, 
ALERT_POSITIONS = {"brewery|brewingRoom1", "brewery|brewingRoom2", "brewery|garageRoom1", "brewery|garageRoom2", "brewery|garageSideRoom"}
, DEFAULT_ALERT_TREATMENT = "ON_WITH_AI_ALERT", ALERT_CHARACTERS = true}
}
}
}
comp_InitializeStructureMission(_Self)
