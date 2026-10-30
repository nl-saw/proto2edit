-- params : ...
-- function num : 0
consumableCounter = 0
ChurchHealth = 200
ChurchMaxHealth = 200
ChurchBarValue = 100
Church_Schedule = false
Church_Damage1 = false
Church_Damage1_2 = false
Church_Damage2 = false
Church_Damage3 = false
TrackingChurchHealth = true
DamageStage = 1
NumAttackers = 0
Repositioning = 0
AttackingHeller = 0
Damage = 0
ChurchPos = address_GetPosition(address_GetAddressByPath(activationData.rootPath .. "|apc_frontchurch|1"))
ChurchOri = address_GetOrientation(address_GetAddressByPath(activationData.rootPath .. "|apc_frontchurch|1"))
Attack_Church = function()
  -- function num : 0_0
  NumAttackers = 0
  AttackingHeller = 0
  Repositioning = 0
  Attacker_Table = {}
  Attacker_Table = go_GetAllTagged("C3_Attacker")
  Valid_Attackers = {}
  Non_Attackers = {}
  Player_Attackers = {}
  for l_1_3,l_1_4 in pairs(Attacker_Table) do
    if IsObjectInVolume(l_1_4, "Church_Boundary") == true then
      if IsObjectInVolume(PLAYER, "Church_Boundary") == true then
        local l_1_5 = (math.random)(100)
        if l_1_5 >= 50 then
          (table.insert)(Valid_Attackers, l_1_4)
        elseif disguise_IsInAlert(PLAYER) == true and go_GetDistance(l_1_4, PLAYER) <= 10 then
          (table.insert)(Player_Attackers, l_1_4)
        else
          (table.insert)(Valid_Attackers, l_1_4)
        end
      else
        (table.insert)(Valid_Attackers, l_1_4)
      end
    else
      if disguise_IsInAlert(PLAYER) == true and go_GetDistance(l_1_4, PLAYER) <= 10 then
        (table.insert)(Player_Attackers, l_1_4)
      else
        (table.insert)(Non_Attackers, l_1_4)
      end
    end
  end
  for l_1_9,l_1_10 in pairs(Valid_Attackers) do
    if go_IsValid(l_1_10) then
      TargetGOH = go_FindGOHByName("Church_Pustule")
      if go_IsValid(TargetGOH) then
        ai_ClearExclusiveTask(l_1_10)
        ai_ClearFixedTarget(l_1_10)
        ai_ClearBoostedTargets(l_1_10)
        ai_SetFixedTarget(l_1_10, TargetGOH)
        ai_BoostTarget(l_1_10, TargetGOH, 200)
        ai_BoostTarget(l_1_10, PLAYER, -200)
        ai_ForceSee(l_1_10, TargetGOH, 1)
        ai_SetExclusiveTask(l_1_10, "engage", {})
        NumAttackers = NumAttackers + 1
      else
        mms_Error(l_1_10 .. " could not find the door\nPlease tell Lars P. what you were doing when this occurred.")
      end
    end
  end
  Damage = NumAttackers * 2
  if NumAttackers >= 1 then
    if Damage <= 2 then
      Damage = 2
    end
    if Damage > 6 then
      Damage = 4
    end
    CurrentDamageValue = Damage / DamageStage
    ChurchHealth = ChurchHealth - CurrentDamageValue
  end
  for l_1_14,l_1_15 in pairs(Non_Attackers) do
    ai_ClearExclusiveTask(l_1_15)
    ai_ClearFixedTarget(l_1_15)
    ai_ClearBoostedTargets(l_1_15)
    ai_SetExclusiveTask(l_1_15, "goto", {pos = ChurchPos, ori = ChurchOri})
    go_VariableNameSetGOH(l_1_15, "movement_type", "soldier_run")
    Repositioning = Repositioning + 1
  end
  for l_1_23,l_1_24 in pairs(Player_Attackers) do
    if go_IsValid(l_1_24) and ai_GetTarget(l_1_24) ~= PLAYER then
      ai_ClearExclusiveTask(l_1_24)
      ai_ClearFixedTarget(l_1_24)
      ai_ClearBoostedTargets(l_1_24)
      ai_BoostTarget(l_1_24, TargetGOH, -200)
      ai_BoostTarget(l_1_24, PLAYER, 200)
      ai_SetExclusiveTask(l_1_24, "engage", {})
      AttackingHeller = AttackingHeller + 1
    end
  end
  if NumAttackers >= 1 then
    if ChurchHealth <= 160 and Church_Damage1 == false then
      Church_Damage1 = true
      DamageStage = 2
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectSmoke_1"), "default", "CLONED_cathedral_smoke_small")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectFire_1"), "default", "CLONED_cathedral_fire_small")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectSmoke_Level2Door"), "default", "CLONED_cathedral_smoke_small")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectFire_Level2Door"), "default", "CLONED_cathedral_fire_small")
    end
    if ChurchHealth <= 130 and Church_Damage1_2 == false then
      snd_PlayDialogueSequence("c3_log_240", "not_used")
      Church_Damage1_2 = true
    end
    if ChurchHealth <= 110 and Church_Damage2 == false then
      Church_Damage2 = true
      DamageStage = 3
      snd_PlayDialogueSequence("c3_log_250", "not_used")
      pm_SetPlaybackState(go_FindGOHByName("C3_ChurchSideFire_2"), "default", "CLONED_cathedral_fire_small")
      pm_SetPlaybackState(go_FindGOHByName("C3_ChurchSideSmoke1"), "default", "CLONED_cathedral_smoke_small")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectSmoke_2"), "default", "CLONED_cathedral_smoke_med")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectFire_2"), "default", "CLONED_cathedral_fire_small")
    end
    if ChurchHealth <= 70 and Church_Damage3 == false then
      Church_Damage3 = true
      DamageStage = 3.29999995
      snd_PlayDialogueSequence("c3_log_260", "not_used")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectFire_3"), "default", "CLONED_cathedral_fire_med")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectSmoke_3"), "default", "CLONED_cathedral_smoke_med")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectFire_4"), "default", "CLONED_cathedral_fire_med")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectSmoke_4"), "default", "CLONED_cathedral_smoke_med")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectSmoke_5"), "default", "CLONED_cathedral_smoke_med")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectFire_5"), "default", "CLONED_cathedral_fire_med")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectFire_Level3_BackDoor"), "default", "CLONED_cathedral_fire_small")
      pm_SetPlaybackState(go_FindGOHByName("C3_EffectSmoke_Level3_BackDoor"), "default", "CLONED_cathedral_smoke_small")
    end
    if ChurchHealth <= 0 and TrackingChurchHealth then
      TrackingChurchHealth = false
      scenario_PostEventString("Destroy_Church")
    else
      RubbleChance = (math.random)(100)
      RubbleSpot = (math.random)(3)
      GlassChance = (math.random)(100)
      RubbleGOH = "C3_EffectDebris_" .. RubbleSpot
      if RubbleChance >= 70 then
        pm_SetPlaybackState(go_FindGOHByName(RubbleGOH), "default", "spawn_rubble_interior")
      end
      if ChurchHealth < 0 then
        ChurchHealth = 0
      end
      ChurchBarValue = ChurchHealth / ChurchMaxHealth * 100
      fehudmeter_UpdatePercentage(ChurchBarValue)
    end
  end
end

CheckBoundary = function()
  -- function num : 0_1
  C3_Temp_Table = {}
  C3_Temp_Table = go_GetAllTagged("C3_Attacker")
  if (table.count)(C3_Temp_Table) >= 1 then
    Attack_Church()
  end
  Church_Schedule = schedule(2, CheckBoundary)
end
