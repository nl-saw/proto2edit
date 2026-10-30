-- params : ...
-- function num : 0
local l_0_0 = (table.copy)((context.event).userTable)
local l_0_1, l_0_2 = nil, nil
local l_0_3 = {}
local l_0_4 = nil
patrolType = "patrol"
if type(l_0_0.path) == "string" then
  l_0_4 = go_FindGOHByName(l_0_0.path)
else
  l_0_4 = l_0_0.path
end
if l_0_0.startAt then
  l_0_1, l_0_2 = go_GetCoordinates(l_0_0.startAt)
else
  l_0_1, l_0_2 = go_GetCoordinates(l_0_4)
end
if l_0_0.useTemplate then
  if type(l_0_0.useTemplate) == "string" then
    l_0_3[1] = ai_Spawn(l_0_0.name .. "_1", l_0_0.useTemplate, l_0_1, l_0_2)
    l_0_0.useTemplate.total = 1
  elseif type(l_0_0.useTemplate) == "table" then
    l_0_0.useTemplate.total = l_0_0.useTemplate.total or 1
    local l_0_5 = l_0_0.useTemplate.template
    if type(l_0_5) ~= "table" then
      l_0_5 = {l_0_5}
    end
    for l_0_9 = 1, l_0_0.useTemplate.total do
      l_0_3[l_0_9] = ai_Spawn(l_0_0.name .. "_" .. l_0_9, l_0_5[MOD1(l_0_9, #l_0_5)], l_0_1, l_0_2)
      if l_0_0.spawnOffset and type(l_0_0.spawnOffset) == "Vector" then
        l_0_1 = l_0_1 + l_0_0.spawnOffset
      else
        l_0_1 = l_0_1 - l_0_2 * -1
      end
    end
  else
    cprint("error", "Cannot Spawn character", (table.show)(l_0_0))
    return 
  end
elseif l_0_0.useGOH then
  l_0_3 = l_0_0.useGOH
else
  cprint("error", "Cannot Spawn character", (table.show)(l_0_0))
  return 
end
if type(l_0_3) ~= "table" then
  l_0_3 = {l_0_3}
end
wayPointData = wayPointData or {}
for l_0_8,l_0_9 in ipairs(l_0_3) do
  if not go_IsValid(l_0_9) then
    cprint("error", "FollowPath: invalid object:", l_0_9)
  else
    if l_0_0.useGOH and l_0_0.startAt then
      go_TeleportTo(l_0_9, l_0_1, l_0_2)
      if l_0_0.spawnOffset and type(l_0_0.spawnOffset) == "Vector" then
        l_0_1 = l_0_1 + l_0_0.spawnOffset
      else
        l_0_1 = l_0_1 - l_0_2 * -1
      end
    end
    ai_ClearExclusiveTask(l_0_9)
    if l_0_0.aiReset then
      ai_Reset(l_0_9)
    end
    if l_0_0.dumbPatrol then
      patrolType = "dumb_patrol"
    end
    if l_0_0.setExclusiveTask then
      ai_SetScriptedTask(l_0_9, patrolType, {path = l_0_4, speed = l_0_0.speed or 1})
    else
      ai_SetTask(l_0_9, patrolType, {path = l_0_4, speed = l_0_0.speed or 1})
    end
    if l_0_0.onSpawnCallback and type(l_0_0.onSpawnCallback) == "function" then
      l_0_0.onSpawnCallback(l_0_9, l_0_0)
    end
    if l_0_0.tag then
      go_AddTag(l_0_9, l_0_0.tag)
    end
    wayPointData[l_0_9] = {commands = (table.copy)(l_0_0.wayPointCommands), termination = l_0_0.termination, setExclusiveTask = l_0_0.setExclusiveTask}
  end
  if l_0_0.spawnDelay then
    if type(l_0_0.spawnDelay) == "number" then
      Wait(l_0_0.spawnDelay)
    else
      Wait(1)
    end
  end
end
