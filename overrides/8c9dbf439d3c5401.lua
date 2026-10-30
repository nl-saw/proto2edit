-- params : ...
-- function num : 0
ObjectTrackingData = ObjectTrackingData or {}
TrackObjectWith = function(l_0_0)
  -- function num : 0_0
  local l_0_1 = {
    name = "notOptional",
    target = "notOptional",
    trackWith = "notOptional",
    positionOnly = "optional",
    worldOffset = "optional"
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
  end
  mm_PostEvent({string = "RequestToTrackObject", toFSM = missionMachine, userTable = (table.copy)(l_0_2)})
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

checkFields = function(l_4_0, l_4_1)
  -- function num : 0_3
  local l_4_2 = (table.copy)(l_4_0)
  local l_4_3 = "Missing: "
  local l_4_4 = {}
  for l_4_8,l_4_9 in pairs(l_4_1) do
    if type(l_4_9) == "table" then
      local l_4_10 = l_4_9[2]
      l_4_4[l_4_10] = l_4_4[l_4_10] or {keys = {}, count = 0}
      table.insert((l_4_4[l_4_10]).keys, l_4_8)
      if l_4_2[l_4_8] then
        (l_4_4[l_4_10]).count = (l_4_4[l_4_10]).count + 1
      end
    else
      if l_4_9 == "notOptional" and not l_4_2[l_4_8] then
        l_4_3 = l_4_3 .. "|keyWord" .. " : " .. l_4_8
      end
    end
    l_4_2[l_4_8] = nil
  end
  for l_4_8,l_4_9 in pairs(l_4_4) do
    if l_4_9.count > 1 then
      l_4_3 = l_4_3 .. "Use only one of:" .. (table.show)(l_4_9.keys)
    end
  end
  if #l_4_2 ~= 0 then
    l_4_3 = l_4_3 .. "Extra parameters in definition" .. (table.show)(l_4_2)
  end
  if l_4_3 == "Missing: " then
    return nil
  else
    return l_4_3
  end
end
