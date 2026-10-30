-- params : ...
-- function num : 0
local l_0_0 = function()
  -- function num : 0_0
  go_SetVisibility(PLAYER, false, true)
  fe_HideHUDElements(HUD_ELEMENT.ALL)
  fe_FadeFromBlack()
end

local l_0_1 = function()
  -- function num : 0_1
  snd_PlayDialogueSequence("g1_log_230", "g1_log_230_over")
end

local l_0_3 = function()
  -- function num : 0_2 , upvalues : l_0_1
  g1_schedule_13 = schedule(1.29999995, l_0_1)
end

local l_0_4 = function()
  -- function num : 0_3
  mms_PlayerFaceObject(whip_evo_01)
  go_SetVisibility(PLAYER, true, true)
end

local l_0_5 = address_GetPosition(address_GetAddressByPath(activationData.rootPath .. "|focuscam_02|focus01"))
local l_0_6 = address_GetPosition(address_GetAddressByPath(activationData.rootPath .. "|focuscam_02|focus02"))
local l_0_7 = address_GetPosition(address_GetAddressByPath(activationData.rootPath .. "|focuscam_02|focus02a"))
local l_0_8 = whip_evo_01
local l_0_9 = address_GetPosition(address_GetAddressByPath(activationData.rootPath .. "|focuscam_02|camera01"))


local l_0_10 = address_GetPosition(address_GetAddressByPath(activationData.rootPath .. "|focuscam_02|camera02"))
sequenceTable = {}
sequenceTable[1] = {}
sequenceTable[1].type = "FramerCam"
sequenceTable[1].data = {blendOutTime = 0, ignoreHUDElements = HUD_ELEMENT.STATUS_ALERT, shakyCam = true, funcOnStart = function()
  -- function num : 0_4
  if go_IsValid(whip_evo_01) == true then
    local l_5_0 = ai_CanReserveActiveSlotBudget(PLAYER, 1, 0.5)
    if l_5_0 then
      ai_CharacterizeFromReserved(PLAYER, whip_evo_01)
      ai_SetUnDowngradable(whip_evo_01, true)
    else
      ai_SetPersistent(whip_evo_01, true)
      ai_SetPolicy(whip_evo_01, 1000, 250, 1, true)
    end
  end
end
, 
obstructionHelper = {isDynamic = true, volumeWidth = 1, volumeHeight = 1, 
hideTypes = {peds = true}
}
, 
viewDefs = {
{cleanCut = true, streamingPointOfInterest = "LookAtPosition", 
posRelObject = {l_0_9}
, 
focusObjects = {l_0_5}
, 
fovs = {45, 55}
, 
shotTimes = {1.79999995, 0}
, 
transitionTimes = {1.70000005}
, funcOnStart = l_0_0}
, 
{cleanCut = true, streamingPointOfInterest = "LookAtPosition", 
posRelObject = {l_0_10}
, 
focusObjects = {l_0_6, l_0_8}
, 
fovs = {35, 75}
, 
shotTimes = {1.14999998, 4.0999999}
, 
transitionTimes = {0.699999988}
, funcOnStart = l_0_1, funcOnEnd = l_0_4}
}
}
  sequenceTable[1].name = "Placeholder_g1_02a"

