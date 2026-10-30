-- params : ...
-- function num : 0
ObjectTrackingData = ObjectTrackingData or {}
TrackObjectWith = function(l_0_0)
  local l_0_1 = {
    name = "notOptional",
    target = "notOptional",
    trackWith = "notOptional",
    positionOnly = "optional",
    worldOffset = "optional",
    useRail = "optional"
  }
  local l_0_2 = l_0_0 or {}
  for l_0_6,l_0_7 in ipairs(l_0_2) do
    local l_0_8 = checkFields(l_0_7, l_0_1)
    if l_0_8 then
      cprint("error", "Bad TrackPlayerViewPoint Definition:", l_0_8)
    end
    if l_0_8 then
      return 
    end
  end
  for l_0_6,l_0_7 in pairs(l_0_2) do
    if type(l_0_7.target) == "GOH" then
      l_0_7.targetGOH = l_0_7.target
    elseif type(l_0_7.target) == "string" then
      l_0_7.targetGOH = go_FindGOHByName(l_0_7.target)
    else
      cprint("error", "TrackObjectWith: Bad object to track: " .. l_0_7.target)
      return 
    end
    if type(l_0_7.trackWith) == "GOH" then
      l_0_7.trackWithGOH = l_0_7.trackWith
    elseif type(l_0_7.trackWith) == "string" then
      l_0_7.trackWithGOH = go_FindGOHByName(l_0_7.trackWith)
    else
      cprint("error", "TrackObjectWith: Bad object to trackWith: " .. l_0_7.trackWith)
      return 
    end
    if l_0_7.useRail then
      if type(l_0_7.useRail.railStart) ~= "Vector" then
        local l_0_8 = go_GetCoordinates(l_0_7.useRail.railStart)
        if type(l_0_8) ~= "Vector" then
          cprint("error", "TrackObjectWith: Bad railStart " .. l_0_7.useRail.railStart)
          return 
        end
        l_0_7.useRail.railStart = l_0_8
      end
      if type(l_0_7.useRail.railEnd) ~= "Vector" then
        local l_0_8 = go_GetCoordinates(l_0_7.useRail.railEnd)
        if type(l_0_8) ~= "Vector" then
          cprint("error", "TrackObjectWith: Bad railEnd " .. l_0_7.useRail.railEnd)
          return 
        end
        l_0_7.useRail.railEnd = l_0_8
      end
      ai_DrawLine(l_0_7.useRail.railStart, l_0_7.useRail.railEnd)
    end
  end
  if go_IsValid(missionMachine) then
    mm_PostEvent({string = "RequestToTrackObject", toFSM = missionMachine, userTable = (table.copy)(l_0_2)})
  end
end
RemoveTrackObject = function(l_2_0)
-- function num : 0_1
local l_2_1 = type(l_2_0)
local l_2_2 = function(l_3_0)
  -- function num : 0_1_0

  if type(l_3_0) == "string" then
    ObjectTrackingData[l_3_0] = nil
  else

    if type(l_3_0) == "table" then
      ObjectTrackingData[l_3_0.name] = nil
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
  if #ObjectTrackingData <= 0 then
    alreadyTrackingObject = false
  end
end
end

PurgeObjectTrackingTargets = function()
-- function num : 0_2
ObjectTrackingData = {}
alreadyTrackingObject = false
end

