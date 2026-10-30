-- params : ...
-- function num : 0
local l_0_0 = go_GetAllTagged("VS_EndCrowd")
for l_0_4,l_0_5 in pairs(l_0_0) do
  go_DestroyObject(l_0_5, false)
end
WaitFrames(1)
local l_0_6 = address_FindAddressList({rootPath = activationData.rootPath .. "|EndCrowd", maxResults = 10000}) or {}
for l_0_11,i_2 in pairs(l_0_6) do
  local l_0_7 = ai_SpawnAtAddress("VS_EndCrowd" .. util_GetUniqueName(), INFECTED1_1_TEMPLATE, i_2)
  ai_SetScriptedTask(l_0_7, "idle", {})
  go_SetHealth(l_0_7, 10)
  go_AddTag(l_0_7, "VS_EndCrowd")
end
