-- params : ...
-- function num : 0
local l_0_0 = (context.event).object
if l_0_0 ~= jumpingJackGOH or RETRY_STATE_FOR_CAM_TESTING then
  return 
end
jackJumpedCount = jackJumpedCount or 0
jackJumpedCount = jackJumpedCount + 1
if jackJumpedCount == 1 and go_IsValid(jumpingJackGOH) then
else
  go_DestroyObject(jumpingJackGOH, true)
  local l_0_1 = go_FindGOHByName("crawlingBeef")
  if go_IsValid(l_0_1) then
    go_ApplyDamageAndHit(l_0_1, 99999, Vector(0, 0, 0), Vector(0, 0.00999999978, 0))
  end
end
