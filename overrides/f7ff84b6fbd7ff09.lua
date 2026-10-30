-- params : ...
-- function num : 0
consumeNumber = consumeNumber + 1
if not ConsumedMessageDone then
  ShowPassiveMessage("$INTRO2_CONSUMING_INFO", true)
  ConsumedMessageDone = true
end
if not burnMessageDone then
  if consumeNumber == 2 then
  end
end
if not timedOut and consumeNumber == 4 then
  timedOut = true
  scenario_PostEventString("ConsumeTimeout")
end
WaitFrames(3)
go_SetHealth(PLAYER, go_GetHealth(PLAYER) + EXTRA_INFECTED_HEALTH)
local l_0_0 = go_GetHealth(PLAYER) / go_GetMaxHealth(PLAYER)
if l_0_0 > 0.5 and not defeatMessageDone then
  ShowObjectiveMessage("$INTRO2_OBJECTIVE_2")
  defeatMessageDone = true
  consumeMessageDone = false
end
