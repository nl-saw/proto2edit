-- params : ...
-- function num : 0
for l_0_3,l_0_4 in pairs(go_GetAllTagged("MercerCloneTag")) do
  if go_IsValid(l_0_4) then
    go_DestroyObject(l_0_4, false)
  end
end
for l_0_8,l_0_9 in pairs(go_GetAllTagged("MercerTag")) do
  if go_IsValid(l_0_9) then
    go_DestroyObject(l_0_9, false)
  end
end
Wait(0.200000003)
local l_0_10 = "Root|Missions|story_FinalBoss|Opening|spawn"
local l_0_11 = "Root|Missions|story_FinalBoss|Opening|goto"
mercerShell = {}
mercerGOH = {}
for l_0_15 = 1, NUM_MERCERS do

  if l_0_15 == 1 then
    mercerGOH[l_0_15] = ai_SpawnAtAddress("mercer_clone" .. l_0_15, ALEX_BOSS_TEMPLATE, l_0_11 .. l_0_15)
  else

    mercerGOH[l_0_15] = ai_SpawnAtAddress("mercer_clone" .. l_0_15, ALEX_BOSS_TEMPLATE, l_0_11 .. l_0_15)
  end
  go_AddTag(mercerGOH[l_0_15], "MercerCloneTag")
  go_AddTag(mercerGOH[l_0_15], "MercerClone")
  ai_SetPersistent(mercerGOH[l_0_15], true)
  ai_SetScriptedTask(mercerGOH[l_0_15], "idle", {})
  local l_0_16, l_0_17 = address_GetCoordinates(l_0_11 .. l_0_15)
  ai_SetScriptedTask(mercerGOH[l_0_15], "dumb_goto", {pos = l_0_16, ori = l_0_17})
  go_SetBroadcastDamage(mercerGOH[l_0_15], true)
  ai_SetNpcDamageMultiplier(mercerGOH[l_0_15], 0.100000001)
  local l_0_18 = go_GetGrabSlot(mercerGOH[l_0_15], "Torso")
  debug_HackObjectScale(l_0_18, mercerScale[l_0_15])
  debug_HackObjectScale(mercerGOH[l_0_15], mercerScale[l_0_15])
  ProtectSpawnedObject(mercerGOH[l_0_15], 1)
  go_VariableNameSetGOH(mercerGOH[l_0_15], "tactic", "aggressive")
  go_VariableNameSetGOH(mercerGOH[l_0_15], "defense", "dodgy")
  go_SetTimeScale(mercerGOH[l_0_15], 0.800000012)
  ai_SetFixedTarget(mercerGOH[l_0_15], PLAYER)
  go_DisableTimeSlicing(mercerGOH[l_0_15])
  go_SetMaxHealth(mercerGOH[l_0_15], 1000)
  go_SetHealth(mercerGOH[l_0_15], 1000)
end
Wait(0.200000003)
for l_0_25,l_0_26 in pairs(mercerGOH) do
  go_PlayMotionBranchByPath(l_0_26, "//evolved/script/transform_for_the_camera", -10, true)
  local l_0_27 = go_GetGrabSlot(l_0_26, "Torso")
  Wait((math.random)())
end
Wait(0.100000001)
for l_0_31 = 1, NUM_MERCERS do
  local l_0_32 = go_GetGrabSlot(mercerGOH[l_0_31], "Torso")
  debug_HackObjectScale(mercerGOH[l_0_31], mercerScale[l_0_31])
  debug_HackObjectScale(l_0_32, mercerScale[l_0_31])
  Wait(0.100000001)
end
for l_0_36 = 1, NUM_MERCERS do
  local l_0_37, l_0_38 = go_GetCoordinates(mercerGOH[l_0_36])
  l_0_37.y = 15.0584002 + 1 * mercerScale[l_0_36]
  print("posy: ", l_0_37.y)
  Wait(0.100000001)
end
