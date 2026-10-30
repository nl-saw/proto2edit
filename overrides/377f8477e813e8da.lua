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
  if l_0_4.target == "Stroll|Ambients|SmallDebris|fall3" then
  end
  local l_0_6, l_0_7 = go_GetCameraPositionOrientation()
  local l_0_8, l_0_9 = go_GetCoordinates(l_0_4.target)
  if type(l_0_8) ~= "Vector" then
    return 
  end
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
    do
      if l_0_4.fromCenterCheck then
        local l_0_12 = (vector.VectorDistance)(l_0_6, l_0_8)
        local l_0_13 = l_0_4.fromCenterVAngle or 11
        local l_0_14 = l_0_4.fromCenterVAngle or 15
        local l_0_15 = l_0_7
        local l_0_16 = l_0_8 - l_0_6
        local l_0_17 = (vector.VectorAngle)(Vector(l_0_16.x, 0, l_0_16.z), Vector(l_0_15.x, 0, l_0_15.z))
        local l_0_18 = (vector.VectorAngle)(Vector(l_0_16.x, 0, -l_0_16.y), Vector(l_0_15.x, 0, -l_0_15.y))
        if l_0_18 <= l_0_13 and l_0_17 <= l_0_14 then
          l_0_5 = true
        end
      else
        if l_0_4.radius == 0 or type(l_0_4.radius) ~= number or l_0_4.radius < 0 then
          l_0_5 = cm_IsPointInCameraView(l_0_8)
        else
          l_0_5 = cm_IsSphereInCameraView(l_0_8, l_0_4.radius)
        end
      end
    end
  end
  l_0_4.frameCount = l_0_4.frameCount or 0
  if l_0_4.target == "BrawlerRoad|Ambients|LargeDebris|fall1" then
    local l_0_17 = l_0_7
    local l_0_18 = l_0_8 - l_0_6
    local l_0_19 = (vector.VectorAngle)(Vector(l_0_18.x, 0, l_0_18.z), Vector(l_0_17.x, 0, l_0_17.z))
    local l_0_20 = (vector.VectorAngle)(Vector(l_0_18.x, 0, -l_0_18.y), Vector(l_0_17.x, 0, -l_0_17.y))
    MercerCamPos = (vector.VectorNew)(l_0_6)
    MercerPos = (vector.VectorNew)(l_0_8)
    if l_0_5 then
      print("In view: ", l_0_4.frameCount + 1, " LOS: ", cm_CheckLineOfSight(l_0_6, l_0_8))
    else
      print("Hidden: ", l_0_4.frameCount + 1, " LOS: ", cm_CheckLineOfSight(l_0_6, l_0_8))
    end
  end
  do
    local l_0_21 = cm_CheckLineOfSight(l_0_6, l_0_8)
    if l_0_5 and l_0_4.lineOfSight and l_0_21 == 3 then
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
    do
      do
        if l_0_5 and l_0_4.target == "BrawlerRoad|Ambients|LargeDebris|fall1" then
          local l_0_22 = nil
          print("Target in view and ready to go: ", l_0_4.frameCount + 1)
        end
        if l_0_5 then
          if type(l_0_4.inViewFunc) == "function" then
            schedule(0, l_0_4.inViewFunc, l_0_4)
          else
            if type(l_0_4.eventString) == "string" then
              scenario_PostEventString(l_0_4.eventString)
            end
          end
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
    end
  end
end
