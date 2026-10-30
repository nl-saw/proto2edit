-- params : ...
-- function num : 0
if restoreData then
  D3_BonusFailed = restoreData.BonusFailed
end
do ((Consumables.Manager).Disable)(MISSION_NAME, ((Consumables.Clients).PowerUpgrades).ProviderAPI) end
smrt_IgnoreStickUse(false)
unlockable_SetIsActivated(0, Unlockable.Hunting, true)
Play_Random_Audio = function()
  -- function num : 0_0
  snd_PlayDialogueByName("virtual", "d3_log_215_010_commander_evolved_01", "OptionalCompletionTag", "commander_evolved")
  Schedule_Fight_Audio()
end

Schedule_Fight_Audio = function()
  -- function num : 0_1
  RandomDelay = (math.random)(18) + 10
  Audio_Schedule = schedule(RandomDelay, Play_Random_Audio)
end

go_SetBroadcastDamage(PLAYER, true)
fes_RemoveMissionObjective(D3_objectiveID)
if go_IsValid(gerrardShell) then
  go_DestroyObject(gerrardShell, false)
end
if go_IsValid(gerrardGOH) then
  go_DestroyObject(gerrardGOH, false)
  WaitFrames(1)
end
if not mission_IsRetry() then
  local l_0_2 = {addressPath = MISSION_NAME .. "|Checkpoints|FightGerrard", waitForEventString = MISSION_NAME .. "_ChopperCrashCamReady", forceAttackPower = HUD_POWERS.Claws, justWaitForForcedCells = true, 
fadeToParams = {soundMix = 0}
}
  mms_StartTransition(l_0_2)
  mms_WaitForTransitionComplete(true)
  stream_ForceCellsLoaded({reset = true})
else
  go_SetDesiredTransformation(PLAYER, HUD_TRANSFORM_SLOT.Attack, HUD_POWERS.Claws, false)
end
chopper_frontGOH = go_FindGOHByName("VS_heli_bh_husk_front_001")
chopper_tailGOH = go_FindGOHByName("VS_heli_bh_husk_tail_001")
local l_0_3, l_0_4 = address_GetCoordinates("Root|Missions|story_d3|Chopper_propspawn")
if not go_IsValid(chopper_frontGOH) then
  chopper_frontGOH = ai_NonManaged_Spawn("VS_heli_bh_husk_front_001", "heli_bh_husk_front", l_0_3, l_0_4)
else
  go_Teleport(chopper_frontGOH, l_0_3, l_0_4)
end
if go_IsValid(chopper_frontGOH) then
  pm_SetPlaybackState(chopper_frontGOH, "switch", "on")
  go_SetDamageable(chopper_frontGOH, false)
end
if go_IsValid(chopper_frontGOH) then
  go_PlayMotionBranchByPath(chopper_frontGOH, "//helicopter/script/VS_helicopter_crash", -10, true)
end
local l_0_5 = "Root|Missions|story_d3|offcam_spawn"
gerrardShell = ai_SpawnAtAddress("VS_CaptainGerrard" .. "_shell", "mission_disguise_gerrard", l_0_5)
gerrardGOH = ai_SpawnAtAddress("VS_CaptainGerrard", "mission_evolved_gerrard", l_0_5)
ai_SetPersistent(gerrardGOH, true)
ai_SetEvolvedShell(gerrardGOH, gerrardShell)
ai_SetScriptedTask(gerrardGOH, "idle", {})
ai_SetScriptedTask(gerrardShell, "idle", {})
go_SetBroadcastDamage(gerrardGOH, true)
ai_SetNpcDamageMultiplier(gerrardGOH, 0.100000001)
ProtectSpawnedObject(gerrardGOH, 1)
go_VariableNameSetGOH(gerrardGOH, "evolved_name", "gerrard")
go_SetMaxHealth(gerrardGOH, 4000)
go_SetHealth(gerrardGOH, 4000)
ai_SetFixedTarget(gerrardGOH, PLAYER)
go_DisableTimeSlicing(gerrardGOH)
go_DisableTimeSlicing(gerrardShell)
go_AddTag(gerrardShell, "D3_Gerrard")
go_AddTag(gerrardGOH, "D3_Gerrard")
go_AddTag(gerrardGOH, "D3_Gerrard_Encounter")
Battle_Hits = 0
Gerrard_Hits = 0
gerrardStunned = false
go_PlayMotionBranchByPath(gerrardGOH, "//evolved/script/VS/GerrardCombatIntro", -10, true)
local l_0_8 = {collectibleID = "d3_woi_010", noMarker = true, endOnBlack = true}
local l_0_9 = (((Consumables.Clients).WebOfIntrigue).ProviderAPI).CreateConsumable(l_0_8)
Consumables.Manager.AttachConsumable(gerrardGOH, l_0_9)
fsm_PostEventString("VS_StartFinalFight")
FailMessage = "$D3_FAIL_MESSAGEDEATH"
WaitFrames(20)
