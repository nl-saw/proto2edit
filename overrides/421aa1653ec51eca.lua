-- params : ...
-- function num : 0
if not tracker then
  local l_0_0 = go_GetAllTagged("Story_e3_SSS") or {}
  if #l_0_0 == 0 then
    scenario_PostEventString("moveSStrackers")
    return 
  end
  tracker = {}
  for l_0_4,l_0_5 in ipairs(l_0_0) do
    local l_0_3 = go_FindGOHByName("SS_tracker_loc_0" .. l_0_4)
    tracker[l_0_5] = l_0_3
  end
end
local l_0_9 = false
dead_list = dead_list or {}
for l_0_4,l_0_5 in pairs(tracker) do
  if go_IsDead(l_0_4) then
    tracker[l_0_4] = nil
    table.insert(dead_list, l_0_5)
    print("SS died: ", l_0_4)
  else
    local l_0_15 = nil
    l_0_15 = go_GetPosition(l_0_4)
    go_Teleport(l_0_5, l_0_15, Vector(0, 0, 1))
    l_0_9 = true
  end
end
if l_0_9 then
  scenario_PostEventString("moveSStrackers")
  local l_0_16 = next(tracker, nil)
  local l_0_17 = nil
  l_0_17 = go_GetPosition(l_0_16)
  for l_0_21,l_0_22 in ipairs(dead_list) do
    go_Teleport(l_0_22, l_0_17, Vector(0, 0, 1))
  end
else
  tracker = nil
end
