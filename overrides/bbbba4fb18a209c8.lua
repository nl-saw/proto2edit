-- params : ...
-- function num : 0
if (context.event).attackType == AttackType.Missile and g2_killedWithRockets < g2_totalKilledWithRockets then
  g2_killedWithRockets = g2_killedWithRockets + 1
  local l_0_0 = fe_GetFormattedLocalizedString("$G2_BONUS_02", g2_killedWithGuns, g2_totalKilledWithGuns, g2_killedWithRockets, g2_totalKilledWithRockets)
  mms_UpdateBonusObjective(l_0_0)
else
  if ((context.event).attackType == AttackType.Bullet or (context.event).attackType == AttackType.BulletChainGun) and g2_killedWithGuns < g2_totalKilledWithGuns then
    g2_killedWithGuns = g2_killedWithGuns + 1
    local l_0_1 = fe_GetFormattedLocalizedString("$G2_BONUS_02", g2_killedWithGuns, g2_totalKilledWithGuns, g2_killedWithRockets, g2_totalKilledWithRockets)
    mms_UpdateBonusObjective(l_0_1)
  end
end
if g2_killedWithRockets >= g2_totalKilledWithGuns and g2_killedWithGuns >= g2_totalKilledWithRockets then
  mms_CompleteBonusObjective()
  g2_bonusComplete = true
  g2_bonusObjStarted = false
end
