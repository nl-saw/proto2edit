-- params : ...
-- function num : 0
TurnAllOff = function(l_0, l_1, l_2, l_3)
  if l_0 then
    for l_4 = l_1, l_2 do
      local l_5 = go_FindGOHByName((string.format)(l_0 .. "%03d", l_4), l_3)
      if go_IsValid(l_5) then
        pm_SetPlaybackState(l_5, "switch", "initial")
      end
    end
  else
    for l_8,l_9 in ipairs(propList) do
      pm_SetPlaybackState(l_9, "switch", "initial")
    end
  end
end
obstructionDef = {isDynamic = true, volumeWidth = 4, volumeHeight = 4, volumeZBias = 2, fadeAlpha = 0.5, fadeTypes = {}, hideTypes = {props = true}, destroyTypes = {}}
local l_0 = PROJECTILE_NAME
dodgeTutorialActive = true
local l_1 = go_FindGOHByName(l_0)
local l_2, l_3
StopPhys = function()
  phys_EnablePhysics(l_1, false)
  go_SetUninterruptible(PLAYER, true)
  scenario_PostEventString("Intro1_PhysicsStopped")
end
RestorePhys = function()
end
BreakProjectile = function()
  pm_SetPlaybackState(l_1, "intro1_final_break")
end
dodgeCamFramerDef = {sequenceMethod = "prompted", freezePlayer = false, firstPromptedView = "Incoming!", letterBox = false, adaptGameCamera = true, cleanCut = true, isRealtime = true, obstructionHelper = obstructionDef, funcOnStart = function()
  TurnAllOff("Cell_324_rubblePile001_", 5, 12, "Cell_324")
  go_TeleportTo("Intro1_OldLookAt_loc", "BrawlerRoad|HellerGetsTargeted|cam1Look")
  go_VariableIntSetGOH(l_1, "initialized", 1)
  go_VariableIntSetGOH(l_1, "waitingForScriptedDestruction", 1)
  pm_SetPlaybackState(l_1, "intro1_burning")
  PlaceMercer("RunToAlley|Spawns|Behemoth_entrance")
  snd_Trigger("intro1_helicrash_p1", "Play", "IgnoreIfPlaying", "")
  go_ResetPose(l_1)
end, funcOnEnd = function()
  if go_IsValid(l_1) then
  end
  ShowCinematicMessage()
  TurnAllOn("Cell_324_rubblePile001_", 5, 8, "Cell_324")
  go_SlowMotion(1)
  RestorePhys()
  dodgeTutorialActive = false
  go_TeleportTo(l_1, "BrawlerRoad|HellerGetsTargeted|propEnd")
  go_VariableIntSetGOH(PLAYER, "injured_walk", 0)
  camera_SetCameraSettings("story_intro1_ChaseCamSettings")
  if not nis_IsPlayerLocked() then
    nis_LockPlayer(true)
    nisScriptLock = nisScriptLock + 1
  end
end, viewDefs = {{name = "Incoming!", holdWhenComplete = true, cleanCut = true, posRelObject = "BrawlerRoad|HellerGetsTargeted|cam3Pos", focusObjects = {"BrawlerRoad|HellerGetsTargeted|cam1Look", "Intro1_OldLookAt_loc", "BrawlerRoad|HellerGetsTargeted|cam3Look"}, shotTimes = {0, 9999}, focusObjectMasks = {2, 4}, transitionTimes = {1}, funcOnStart = function()
  ShowCinematicMessage()
  go_TeleportObjectTo(PLAYER, "BrawlerRoad|HellerGetsTargeted")
  util_PlayMotionBranch(PLAYER, "//alex_regular/scripted/intro1_dodge_husk")
  schedule(0.13333334, fe_FadeFromBlack, {fadeTime = 0.600000024})
end, fovs = {45, 38}}, {name = "thePause", posRelObject = "BrawlerRoad|HellerGetsTargeted|cam3Pos", focusObjects = {"BrawlerRoad|HellerGetsTargeted|cam3Look"}, funcOnStart = function()
  go_SlowMotion(0.2)
end, funcOnEnd = function()
  go_SlowMotion(1)
end, tutorialParams = {dof = "interact"}, fovs = {38}}}}
mms_TutorializeFramerCam(dodgeCamFramerDef)
