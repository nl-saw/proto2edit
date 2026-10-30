-- params : ...
-- function num : 0
failText = "$A1_DEATH_HINT_TEXT_2"
ScientisWaveOneDone = false
ScientisWaveTwoDone = false
cageBreakLinePlayed = false
FieldOpsCamShown = false
PlayerinVolume = false
a1firstGroupSpawned = false
a2ShowHuntingHint1 = false
firstPulseSent = false
HellerHelpingLinePlayed = false
stealthScanWhite = false
WheresWilsonLinePlayed = false
tutorialframerSequenceUID = false
Consume_1_AlertDialoguePlayed = false
Consume_1_ExitAlertDialoguePlayed = false
HellerConsumeInAlertLinePlayed = false
cancelInfectionCam = false
HellerConsumeInAlertLine2Played = false
lineCount = 0
scientistCount = 0
infectedCameraPlayed = false
LastScientistConsumeLinePlayed = false
SecondScientistConsumeLinePlayed = false
FirstScientistConsumeLinePlayed = false
infectedOutbreak = false
BonusCompleted = false
scenario_SetTutorialMode(true)
FirstScientistGroupSpawned = function()
  -- function num : 0_0
  a1firstGroupSpawned = true
end

SetInfectedAmbientVariable = function()
  -- function num : 0_1
  infectedCameraPlayed = true
  infectedOutbreak = true
end

TellAmbientDudesToRun = function()
  -- function num : 0_2
  scenario_PostEventString("KnockPedsFromIdle")
end

if mission_IsRetry() then
  go_SetDesiredTransformation(PLAYER, HUD_TRANSFORM_SLOT.Attack, HUD_POWERS.Claws, false)
  disguise_SetAlert(PLAYER)
  ai_BlowDisguise(PLAYER, 13)
  scenario_PostEventString("CheckpointRestored")
  scenario_PostEventString("StartBonusObjective1Tracking")
else
  ExtraScientist = go_GetAllTagged("a1_ScientistGroup_1")
  scenario_PostEventString("StartOnFirstPlayThrough")
end
scenario_PostEventString("StartState")
streetProp_1 = go_FindGOHByName("Cell_145_detail_barrierConcrete002_002", "Cell_145_detail")
streetProp_2 = go_FindGOHByName("Cell_145_detailbarrierConcrete002", "Cell_145_detail")
streetProp_3 = go_FindGOHByName("Cell_145_detailBarrierOrange_001", "Cell_145_detail")
streetProp_6 = go_FindGOHByName("Cell_165_detailBarrierOrange_003", "Cell_165_detail")
streetProp_7 = go_FindGOHByName("Cell_165_detailBarrierOrange_001", "Cell_165_detail")
streetProp_8 = go_FindGOHByName("Cell_165_detail_barrierConcrete002_016", "Cell_165_detail")
streetProp_9 = go_FindGOHByName("Cell_165_detail_barrierConcrete002_015", "Cell_165_detail")
streetProp_10 = go_FindGOHByName("Cell_165_detailBarrierOrange_001", "Cell_165_detail")
streetProp_11 = go_FindGOHByName("Cell_145_002", "Cell_145")
if go_IsValid(streetProp_1) then
  go_AddTag(streetProp_1, "StreetProp")
end
if go_IsValid(streetProp_2) then
  go_AddTag(streetProp_2, "StreetProp")
end
if go_IsValid(streetProp_3) then
  go_AddTag(streetProp_3, "StreetProp")
end
if go_IsValid(streetProp_4) then
end
if go_IsValid(streetProp_5) then
end
if go_IsValid(streetProp_6) then
end
if go_IsValid(streetProp_7) then
  go_AddTag(streetProp_7, "StreetProp")
end
if go_IsValid(streetProp_8) then
  go_AddTag(streetProp_8, "StreetProp")
end
if go_IsValid(streetProp_9) then
  go_AddTag(streetProp_9, "StreetProp")
end
if go_IsValid(streetProp_10) then
  go_AddTag(streetProp_10, "StreetProp")
end
if go_IsValid(streetProp_11) then
  go_AddTag(streetProp_11, "StreetProp")
end
local l_0_0 = go_GetAllTagged("StreetProp")
for l_0_4,l_0_5 in pairs(l_0_0) do
  if go_IsValid(l_0_5) then
    pm_DeactivateProp(l_0_5)
  end
end
RooksSquad = go_GetAllTagged("RooksSquad2")
for l_0_9,l_0_10 in pairs(RooksSquad) do
  if go_IsValid(l_0_10) then
    ai_ClearExclusiveTask(l_0_10)
  end
end

if go_IsValid(RooksSquad[1]) then
  knownSchedules.a1_sch_12 = schedule(0.5, go_PlayMotionBranchByPath, RooksSquad[1], "//human", -10, true)
end

if go_IsValid(RooksSquad[2]) then
  knownSchedules.a1_sch_14 = schedule(1.5, go_PlayMotionBranchByPath, RooksSquad[2], "//human", -10, true)
end

if go_IsValid(RooksSquad[3]) then
  knownSchedules.a1_sch_13 = schedule(0, go_PlayMotionBranchByPath, RooksSquad[3], "//human", -10, true)
end
snd_KillAllDialogueSequences()
snd_PlayDialogueSequence("a1_log_025", "JustRun")
