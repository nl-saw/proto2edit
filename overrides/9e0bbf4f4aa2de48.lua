-- params : ...
-- function num : 0
local l_0_0 = (context.event).object
if not go_IsValid(l_0_0) then
  return 
end
if not l_0_0.flyerFlockGOH and not l_0_0.flockData then
  return 
end
local l_0_1 = l_0_0.flyerFlockGOH or group_GetDirector(l_0_0)
if not go_IsValid(l_0_1) then
  return 
end
local l_0_2 = group_GetMembers(l_0_0)
local l_0_3 = l_0_1.flockData
if l_0_3 then
  local l_0_4 = l_0_3.wayPointCommands or {}
  for l_0_8,l_0_9 in ipairs(l_0_4) do
    if l_0_9[1] == (context.event).data and type(l_0_9[2]) == "function" then
      l_0_9[2](l_0_0, l_0_1, l_0_3, l_0_9[2])
    end
  end
end
if (context.event).isLastPoint then
  print("Last Waypoint for:", l_0_0)
  if type(l_0_3.termination) == "function" then
    l_0_3.termination(l_0_1, l_0_3)
  else
    if l_0_3.termination then
      if go_IsValid(l_0_1) then
        go_DestroyObject(l_0_1, false)
      end
    end
  end
  for l_0_8,l_0_9 in ipairs(l_0_2) do
    if type(l_0_3.termination) == "function" then
    else
      if l_0_3.termination then
        if not go_IsDead(l_0_9) then
          go_DestroyObject(l_0_9, false)
        end
      end
    end
  end
  return 
end
