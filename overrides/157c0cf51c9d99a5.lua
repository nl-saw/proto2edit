-- params : ...
-- function num : 0
print("Flyer Interface Initiated")
SpawnFlyerSquad = function(l_1_0)
  -- function num : 0_0
  local l_1_1 = {
    name = "notOptional",
    flockSize = "notOptional",
    path = {"notOptional", "optionA"},
    circle = {"notOptional", "optionA"},
    dumbPatrol = "optional",
    radius = "optional",
    clockwise = "optional",
    dontTarget = "optional",
    leader = "optional",
    leaderScale = "optional",
    leaderOnSpawnCallBack = "optional",
    onSpawnCallBack = "optional",
    flockOnSpawnCallBack = "optional",
    flockDeathCallBack = "optional",
    onDeathCallBack = "optional",
    animSpeed = "optional",
    speed = "optional",
    startAt = "optional",
    scaleRange = "optional",
    initialPositions = "optional",
    wayPointCommands = "optional",
    termination = "optional",
    spawnDelay = "optional"
  }
  local l_1_2 = checkFields(l_1_0, l_1_1)
  if l_1_2 then
    cprint("error", "Bad SpawnFlyerSquad Definition:", l_1_2)
    return 
  end
  mm_PostEvent({string = "FlyerVignettePathFollow", toFSM = missionMachine, userTable = (table.copy)(l_1_0)})
end

FlyersOnPath = function(l_2_0, l_2_1, l_2_2, l_2_3, l_2_4, l_2_5, l_2_6)
  -- function num : 0_1
  mm_PostEvent({string = "FlyerVignettePathFollow", toFSM = missionMachine, 
userTable = {startAt = l_2_2, name = l_2_0, speed = l_2_4, flockSize = l_2_1, path = l_2_3, wayPointCommands = l_2_5, termination = l_2_6}
})
end

CreateFrozenLeaderAt = function(l_3_0, l_3_1, l_3_2)
  -- function num : 0_2
  local l_3_3, l_3_4 = go_GetCoordinates(l_3_1, l_3_2)
  local l_3_5 = go_FindGOHByName(l_3_0)
  if go_IsDead(l_3_5) then
    l_3_5 = ai_Spawn(l_3_0, FLYER_TEMPLATE, l_3_3, l_3_4)
  else
    go_TeleportTo(l_3_5, l_3_3, l_3_4)
  end
  go_AddTag(l_3_5, l_3_0)
  util_PlayMotionBranch(l_3_5, "//flyer/scripted/frozen")
  ai_SetExclusiveTask(l_3_5, "idle", {})
  go_SetScale(l_3_5, Vector(0.0500000007, 0.0500000007, 0.0500000007))
  return l_3_5
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
