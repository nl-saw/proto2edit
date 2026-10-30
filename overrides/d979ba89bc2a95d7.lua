-- params : ...
-- function num : 0
local l_0_0 = function()
  -- function num : 0_0
  go_SetVisibility(PLAYER, false, true)
end

local l_0_1 = function()
  -- function num : 0_1
  go_SetVisibility(PLAYER, true, true)
  b3_dudesInCam = {}
  b3_dudesInCam = go_GetAllTagged("final_round")
  for l_2_3,l_2_4 in pairs(b3_dudesInCam) do
    ai_SetPersistent(l_2_4, false)
    ai_SetPolicy(l_2_4, 1000, 250, 1, true)
  end
end

local l_0_2 = {searchCondition = AddressSearchCondition.Closest, origin = go_GetPosition(PLAYER), maxResults = 1, rootPath = "Root|Missions|story_b3|cleanupLocations"}
local l_0_3 = address_FindAddressList(l_0_2)
b3_myNISAddress = address_GetPosition(l_0_3[1])
b3_myNISOri = address_GetOrientation(l_0_3[1])
sequenceTable = {}

sequenceTable[1] = {}

sequenceTable[1].type = "Transition"
sequenceTable[1].data = {
transitionTable = {suppressFadeToBlack = false, suppressFadeFromBlack = true, position = b3_myNISAddress, orientation = b3_myNISOri}
}
sequenceTable[1].fadeData = {
fadeToBlackOnBegin = {soundMix = 0}
}
sequenceTable[1].name = "B3_TRANSITION_NIS_00"
sequenceTable[2] = {}
sequenceTable[2].type = "NIS"
sequenceTable[2].data = {name = "B3_NIS_010", sequenceFramerCam = false, keepPlayerInCurrentPowers = true}
sequenceTable[2].fadeData = {fadeFromBlackOnBegin = 0.666999996}
sequenceTable[2].name = "B3_NIS_010"
