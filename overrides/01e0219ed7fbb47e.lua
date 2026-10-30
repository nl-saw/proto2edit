-- params : ...
-- function num : 0
if not PlayerViewPointTrackingData or not next(PlayerViewPointTrackingData, nil) then
  alreadyTrackingPlayerView = false
  return 
end
alreadyTrackingPlayerView = true
for l_0_3,l_0_4 in pairs(PlayerViewPointTrackingData) do
  local l_0_5 = false
  if l_0_4.chaseCamOnly and not cm_IsGameCameraActive() then
    break
  end
  local l_0_6, l_0_7 = go_GetCameraPositionOrientation()
  local l_0_8, l_0_9 = go_GetCoordinates(l_0_4.target)
  if l_0_4.useProximityTest then
    local l_0_10 = go_GetPosition(PLAYER)
    if not l_0_4.useXYZDistance then
      l_0_10.y = l_0_8.y
    end
    local l_0_11 = (vector.VectorDistance)(l_0_8, l_0_10)
    local l_0_12 = l_0_4.radius or l_0_4.maxRadius or 1
    local l_0_13 = l_0_4.minRadius or 0
    if l_0_13 < l_0_11 and l_0_11 <= l_0_12 then
      l_0_5 = true
    end
  else
    if l_0_4.fromCenterCheck then
      local l_0_12 = ((vector.VectorDistance)(l_0_6, l_0_8))
      local l_0_13 = nil
      if l_0_4.radius == 0 then
        l_0_13 = 4
      else
        l_0_13 = l_0_4.radius
      end
      if RaySphereIntersection(l_0_8, l_0_13, l_0_6, l_0_7) then
        if l_0_4.radius == 0 then
          l_0_5 = cm_IsPointInCameraView(l_0_8)
        else
          l_0_5 = cm_IsSphereInCameraView(l_0_8, l_0_4.radius)
        end
      end
    else
      if l_0_4.radius == 0 then
        l_0_5 = cm_IsPointInCameraView(l_0_8)
      else
        l_0_5 = cm_IsSphereInCameraView(l_0_8, l_0_4.radius)
      end
    end
  end
  l_0_4.frameCount = l_0_4.frameCount or 0
  if l_0_5 and l_0_4.lineOfSight and not cm_CheckLineOfSight(l_0_6, l_0_8) then
    l_0_5 = false
  end
  if l_0_5 then
    l_0_4.frameCount = l_0_4.frameCount + 1
  else
    if l_0_4.frameCount > 0 then
      print("GONE")
    end
    l_0_4.frameCount = 0
  end
  if l_0_5 and type(l_0_4.framesInView) == "number" and l_0_4.framesInView > 0 then
    if l_0_4.frameCount < l_0_4.framesInView then
      l_0_5 = false
    else
      l_0_4.frameCount = 0
    end
  end
  if l_0_5 then
    if type(l_0_4.inViewFunc) == "function" then
      (l_0_4.inViewFunc)(l_0_4)
    else
      if type(l_0_4.eventString) == "string" then
        scenario_PostEventString(l_0_4.eventString)
      end
    end
    print("Target In View: ", l_0_4.name, " : ", l_0_4.target)
    if not l_0_4.continueTracking then
      RemoveViewPointTrackingTarget(l_0_4.name)
    else
      if type(l_0_4.continueTracking) == number then
        l_0_4.continueTracking = l_0_4.continueTracking - 1
        if l_0_4.continueTracking <= 0 then
          RemoveViewPointTrackingTarget(l_0_4.name)
        end
      end
    end
  end
end
