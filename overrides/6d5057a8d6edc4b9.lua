-- params : ...
-- function num : 0
local l_0_0 = 0
local l_0_1 = 0
local l_0_2 = 0
UpdateWaveDisplay = function()
  -- function num : 0_0 , upvalues : l_0_0, l_0_1
  fe_ShowPassive({isLuaScriptControlled = true, priority = 0, line1 = "@Wave: " .. l_0_0 + 1 + l_0_1})
end

GetCurWave = function()
  -- function num : 0_1 , upvalues : l_0_0
  return l_0_0
end

GetPhantomWave = function()
  -- function num : 0_2 , upvalues : l_0_1
  return l_0_1
end

IncWave = function()
  -- function num : 0_3 , upvalues : l_0_0, l_0_2, l_0_1
  if l_0_0 < MAX_WAVES then
    l_0_0 = l_0_0 + 1
    l_0_2 = WAVE_BOUNDARY_TRIGGERS[MAX_WAVES] + PHANTOM_BOUNDARY
  else
    l_0_1 = l_0_1 + 1
    l_0_2 = l_0_2 + PHANTOM_BOUNDARY
  end
  UpdateWaveDisplay()
end

GenericWaveChecks = function(l_5_0)
  -- function num : 0_4
  if l_5_0 > 4 then
    return true
  end
  return false
end

ReadyForNextScriptedWave = function(l_6_0, l_6_1)
  -- function num : 0_5 , upvalues : l_0_0
  if GenericWaveChecks(l_6_1) and l_0_0 < MAX_WAVES and WAVE_BOUNDARY_TRIGGERS[l_0_0 + 1] <= l_6_0 then
    return true
  end
  return false
end

ReadyForNextPhantomWave = function(l_7_0, l_7_1)
  -- function num : 0_6 , upvalues : l_0_0, l_0_2
  if GenericWaveChecks(l_7_1) and l_0_0 >= MAX_WAVES and l_7_0 >= l_0_2 then
    return true
  end
  return false
end

ReadyForNextWave = function(l_8_0, l_8_1)
  -- function num : 0_7
  if ReadyForNextScriptedWave(l_8_0, l_8_1) or ReadyForNextPhantomWave(l_8_0, l_8_1) then
    return true
  end
  return false
end
