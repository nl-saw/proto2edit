-- params : ...
-- function num : 0
PlayerViewPointTrackingData = PlayerViewPointTrackingData or {}
TrackPlayerViewPoint = function(l_1_0)
  -- function num : 0_0
  local l_1_1 = {
    name = "notOptional",
    target = "notOptional",
    radius = "notOptional",
    useProximityTest = "optional",
    useXZDistance = "optional",
    minRadius = "optional",
    chaseCamOnly = "optional",
    lineOfSight = "optional",
    fromCenterCheck = "optional",
    framesInView = "optional",
    inViewFunc = {"notOptional", "optionA"},
    eventString = {"notOptional", "optionA"}
  }
  local l_1_2 = l_1_0 or {}
  for l_1_6,l_1_7 in ipairs(l_1_2) do
    local l_1_8 = checkFields(l_1_7, l_1_1)
    if l_1_8 then
      cprint("error", "Bad TrackPlayerViewPoint Definition:", l_1_8)
    end
    if l_1_8 then
      return 
    end
  end
  mm_PostEvent({string = "RequestToTrackPlayerViewPoint", toFSM = missionMachine, userTable = (table.copy)(l_1_2)})
end
RemoveViewPointTrackingTarget = function(l_2_0)
-- function num : 0_1
local l_2_1 = type(l_2_0)
local l_2_2 = function(l_3_0)
  -- function num : 0_1_0

  if type(l_3_0) == "string" then
    PlayerViewPointTrackingData[l_3_0] = nil
  else

    if type(l_3_0) == "table" then
      PlayerViewPointTrackingData[l_3_0.name] = nil
    end
  end
end

if l_2_1 ~= "table" then
  l_2_2(l_2_0)
else
  for l_2_6,l_2_7 in pairs(l_2_0) do
    l_2_2(l_2_7)
  end
end
do
  if #PlayerViewPointTrackingData <= 0 then
    alreadyTrackingPlayerView = false
  end
end
end

PurgeViewPointTrackingTargets = function()
-- function num : 0_2
PlayerViewPointTrackingData = {}
alreadyTrackingPlayerView = false
end

