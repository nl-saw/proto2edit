-- params : ...
-- function num : 0
userTable = (context.event).userTable
local l_0_0 = ((context.event).userTable).goh
local l_0_1 = ((context.event).userTable).address
local l_0_2 = ((context.event).userTable).totalTime
local l_0_3 = ((context.event).userTable).height
local l_0_4, l_0_5 = go_GetCoordinates(l_0_1)
local l_0_6 = 30
local l_0_7 = l_0_2 * l_0_6
local l_0_8 = 1 / l_0_7
local l_0_9, l_0_10 = go_GetCoordinates(l_0_0)
local l_0_11, l_0_12, l_0_13, l_0_14, l_0_15, l_0_16, l_0_17, l_0_18 = nil, nil, nil, nil, nil, nil, nil, nil
local l_0_19 = (vector.VectorDistance)(l_0_9, l_0_4)
if not l_0_2 then
  l_0_2 = 1
end
if not l_0_3 then
  l_0_3 = 1
end
MyWait = function(l_1_0)
  -- function num : 0_0
  if not l_1_0 then
    return 
  end
  local l_1_1 = 0
  while l_1_1 < l_1_0 do
    l_1_1 = l_1_1 + time_GetSimulationDelta()
    coroutine.yield()
  end
  return l_1_1
end

if l_0_19 > 0.100000001 then
  local l_0_20 = 0
  local l_0_21 = nil
  local l_0_22 = 0
  if go_IsValid(l_0_0) then
    ai_ClearExclusiveTask(l_0_0)
    ai_SetScriptedTask(l_0_0, "idle", {})
    l_0_12 = l_0_3 * 4
    l_0_11 = (l_0_4.y - l_0_9.y) / l_0_19
    while l_0_22 <= l_0_2 do
      local l_0_23 = l_0_22 / l_0_2
      l_0_13 = l_0_9 + l_0_23 * (l_0_4 - l_0_9)
      l_0_14 = l_0_10 + l_0_23 * (l_0_5 - l_0_10)
      l_0_16 = (vector.VectorDistance)(l_0_13, l_0_9)
      l_0_17 = l_0_11 * l_0_16
      l_0_18 = l_0_12 * (l_0_16 / l_0_19) * ((l_0_19 - l_0_16) / l_0_19)
      l_0_13.y = l_0_9.y + l_0_17 + l_0_18
      if go_IsValid(l_0_0) then
        go_Teleport(l_0_0, l_0_13, l_0_14)
      end
      l_0_21 = MyWait(1 / l_0_6)
      l_0_20 = l_0_20 + l_0_21
      l_0_22 = l_0_20
    end
    if go_IsValid(l_0_0) then
      go_Teleport(l_0_0, l_0_4, l_0_5)
    end
  end
end
if go_IsValid(missionMachine) then
  mm_PostEvent({string = "HellerJumpDone", toFSM = missionMachine, userTable = userTable})
end
