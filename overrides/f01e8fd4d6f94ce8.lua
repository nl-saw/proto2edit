-- params : ...
-- function num : 0
g2_beenHit = true
g2_playerDamaged = g2_playerDamaged + 1
if g2_playerDamaged == 4 then
  g2_playerDamaged = 0
end
local l_0_0 = go_GetMaxHealth(myChopper)
local l_0_1 = go_GetHealth(myChopper)
local l_0_2 = l_0_1 / l_0_0 * 100
if l_0_2 <= 10 then
else
  if l_0_2 <= 30 then
    ai_SetNpcDamageMultiplier(myChopper, 0.150000006)
  elseif l_0_2 <= 45 then
    ai_SetNpcDamageMultiplier(myChopper, 0.5)
  elseif l_0_2 <= 70 then
    ai_SetNpcDamageMultiplier(myChopper, 0.699999988)
  end
end
