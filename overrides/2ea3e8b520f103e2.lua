-- params : ...
-- function num : 0
ResetSSTroops = function()
  -- function num : 0_0
  local l_0_0 = go_GetAllTagged("Story_e3_SSS")
  local l_0_1 = address_FindAddressList({rootPath = "Root|Missions|story_e3|SSSpawnPoints", type = AddressType.SpawnStreet, maxResults = #l_0_0})
  local l_0_2 = 1
  print("testspawner coord", (address_GetCoordinates)(l_0_1[1]))
  for l_0_6,l_0_7 in pairs(l_0_0) do
    print("urk: ", l_0_2 % #l_0_1 + 1)
    go_Teleport(l_0_7, (address_GetCoordinates)(l_0_1[l_0_2 % #l_0_1 + 1]))
    l_0_2 = l_0_2 + 1
  end
end
brawlerIdleTargets = {}
for l_0_3 = 1, 4 do
  brawlerIdleTargets[l_0_3] = {go_GetCoordinates("story_e3_brawler_idle_" .. l_0_3)}
end
IdlePack = function()
  -- function num : 0_1
  local l_2_0 = ai_GetAllSummoned() or {}
  for l_2_5,l_2_6 in ipairs(l_2_0) do
    ai_SetScriptedTask(l_2_6, "goto", {pos = brawlerIdleTargets[l_2_5][1], ori = brawlerIdleTargets[l_2_5][2]})
  end
end
ReleasePack = function()
  -- function num : 0_2
  local l_3_0 = ai_GetAllSummoned() or {}
  for l_3_5,l_3_6 in ipairs(l_3_0) do
    ai_RemoveScriptedTask(l_3_6)
  end
end
obstructionDefn = {
  isDynamic = true,
  volumeWidth = 1,
  volumeHeight = 1,
  fadeTypes = {peds = true, vehicles = true, air = false, walkers = true, hunters = true, tanks = true, dead = true, props = true, displacedProps = true},
  hideTypes = {},
  destroyTypes = {}
}
