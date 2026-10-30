-- params : ...
-- function num : 0
if not ChopTracker then
  local l_0_0 = go_GetAllTagged("Story_e3_chopper") or {}
  if #l_0_0 == 0 then
    scenario_PostEventString("moveChopperTracker")
    return 
  end
  ChopTracker = {}
  for l_0_4,l_0_5 in ipairs(l_0_0) do
    local l_0_3 = go_FindGOHByName("story_e3_chopper_tracker_loc_0" .. l_0_4)
    ChopTracker[l_0_5] = l_0_3
  end
end
local l_0_9 = false
dead_list = dead_list or {}
for l_0_4,l_0_5 in pairs(ChopTracker) do
  if go_IsDead(l_0_4) then
    ChopTracker[l_0_4] = nil
    table.insert(dead_list, l_0_5)
    print("SS died: ", l_0_4)
  else
    local l_0_15 = nil
    l_0_15 = go_GetPosition(l_0_4)
    go_Teleport(l_0_5, l_0_15, Vector(0, 0, 1))
    l_0_9 = true
  end
end
