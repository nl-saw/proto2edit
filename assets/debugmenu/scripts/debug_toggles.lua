-- I implemented this time of day stuff so entering the debug
-- menu doesn't fuck your current time of day up, which it does
-- with the default debug menu the game shipped with.
function gibbed_GenerateTimeOfDayEntries()
  local timeOfDays =
  {
      {Name = "@Lair",            Value = 1.0},
      {Name = "@Bunker",          Value = 2.0},
      {Name = "@Brewery",         Value = 2.5},
      {Name = "@Brewery (night)", Value = 3.0},
      {Name = "@Bunker (intro2)", Value = 3.5},
      {Name = "@Dawn (rain)",     Value = 15.0},
      {Name = "@Dawn",            Value = 15.5},
      {Name = "@Day (rain)",      Value = 16.0},
      {Name = "@Day",             Value = 16.5},
      {Name = "@Night",           Value = 17.0},
      {Name = "@Night (rain)",    Value = 17.5},
      {Name = "@Intro1",          Value = 20.0},
      {Name = "@Finalboss1",      Value = 21.5},
      {Name = "@Finalboss2",      Value = 22.0},
      {Name = "@Finalboss3",      Value = 22.5},
  }

  local entries = {}

  local lastTimeOfDay = 0.0
  for idx, tod in ipairs(timeOfDays) do
    while (lastTimeOfDay < tod.Value) do
      table.insert(entries, {Name = tostring(lastTimeOfDay), Value = tostring(lastTimeOfDay)})
      lastTimeOfDay = lastTimeOfDay + 0.5
    end
    table.insert(entries, {Name = tod.Name .. " (" .. tostring(tod.Value) .. ")", Value = tostring(tod.Value)})
    lastTimeOfDay = tod.Value + 0.5
  end

  -- tod_GetDayLength doesn't seem to return a sane value
  --[[local dayLength = tod_GetDayLength()
  while (lastTimeOfDay <= dayLength) do
    table.insert(entries, {Name = tostring(lastTimeOfDay), Value = tostring(lastTimeOfDay)})
    lastTimeOfDay = lastTimeOfDay + 0.5
  end]]--

  return entries
end

function gibbed_GetTimeOfDay()
  local tod = tod_GetTimeOfDay()

  -- hack, since I disabled the tod_GetDayLength stuff
  if (tod > 22.5) then
    return 22.5
  end

  local i, f = math.modf(tod)

  if (f < 0.25) then
    return tostring(i)
  elseif (f < 0.75) then
    return tostring(i) .. ".5"
  else
    return tostring(i + 1)
  end
end

-- Custom difficulty functions since the default ones
-- are broken / improperly done.

function gibbed_GetDifficultyOption()
  return tostring(db_GetInt("Proto_Save_Difficulty", 0, 0))
end

function gibbed_SetDifficultyOption(target)
  db_SetInt("Proto_Save_Difficulty", 0, tonumber(target), 0)
end

local gibbed_Dilation_Value = 1.0

function gibbed_Dilation_Get()
  return tostring(gibbed_Dilation_Value)
end

function gibbed_Dilation_Set(target)
  gibbed_Dilation_Value = tonumber(target)
  gameflow_DebugTimeDilation(gibbed_Dilation_Value)
end

return
{
  {
    Label = "Spawn Type",
    Function = "gibbed_Spawner_SetType",
    GetValueFunction = "gibbed_Spawner_GetType",
    Entries = gibbed_Spawner_GenerateTypeEntries(),
  },

  {
    Label = "Spawn Count",
    Function = "gibbed_Spawner_SetCount",
    GetValueFunction = "gibbed_Spawner_GetCount",
    Entries =
    {
      {Name = "@1",   Value = "1"},
      {Name = "@5",   Value = "5"},
      {Name = "@10",  Value = "10"},
      {Name = "@25",  Value = "25"},
      {Name = "@50",  Value = "50"},
      {Name = "@100", Value = "100"},
      {Name = "@200", Value = "200"},
      {Name = "@500", Value = "500"},
      {Name = "@1000", Value = "1000"},
    },
  },

  {
    Label = "Time Dilation",
    Function = "gibbed_Dilation_Set",
    GetValueFunction = "gibbed_Dilation_Get",
    Entries =
    {
      {Name = "@0.0", Value = "0"},
      {Name = "@0.1", Value = "0.1"},
      {Name = "@0.2", Value = "0.2"},
      {Name = "@0.3", Value = "0.3"},
      {Name = "@0.4", Value = "0.4"},
      {Name = "@0.5", Value = "0.5"},
      {Name = "@0.6", Value = "0.6"},
      {Name = "@0.7", Value = "0.7"},
      {Name = "@0.8", Value = "0.8"},
      {Name = "@0.9", Value = "0.9"},
      {Name = "@1.0", Value = "1"},
      {Name = "@1.1", Value = "1.1"},
      {Name = "@1.2", Value = "1.2"},
      {Name = "@1.3", Value = "1.3"},
      {Name = "@1.4", Value = "1.4"},
      {Name = "@1.5", Value = "1.5"},
      {Name = "@1.6", Value = "1.6"},
      {Name = "@1.7", Value = "1.7"},
      {Name = "@1.8", Value = "1.8"},
      {Name = "@1.9", Value = "1.9"},
      {Name = "@2.0", Value = "2"},
    },
  },

  --[[--
  -- Causes the game to freeze when set.
  {
    Label = "@Language",
    Function = "fes_SetLanguageDebug",
    GetValueFunction = "fe_DbgGetCurrLanguageStr",
    Entries =
    {
      {Name = "@ENGLISH", Value = "english"},
      {Name = "@FRENCH",  Value = "french"},
      {Name = "@ITALIAN", Value = "italian"},
      {Name = "@SPANISH", Value = "spanish"},
      {Name = "@GERMAN",  Value = "german"},
      {Name = "@RUSSIAN", Value = "russian"},
      {Name = "@POLISH",  Value = "polish"},
    },
  },
  --]]--

  {
    Label = "@Difficulty",
    --Function = "fes_SetDifficultyDebug",
    Function = "gibbed_SetDifficultyOption",
    --GetValueFunction = "fes_GetDifficultyOption",
    GetValueFunction = "gibbed_GetDifficultyOption",
    Entries =
    {
      {Name = "$DIFFICULTY_1", Value = "0"},
      {Name = "$DIFFICULTY_2", Value = "1"},
      {Name = "$DIFFICULTY_3", Value = "2"},
      {Name = "$DIFFICULTY_4", Value = "3"},
    },
  },

  {
    Label = "@Invert Camera X",
    Function = "fes_SetCameraInvertX",
    GetValueFunction = "fes_GetCameraInvertX",
    Entries =
    {
      {Name = "$OFF", Value = "0"},
      {Name = "$ON",  Value = "1"},
    },
  },

  {
    Label = "@Invert Camera Y",
    Function = "fes_SetCameraInvertY",
    GetValueFunction = "fes_GetCameraInvertY",
    Entries =
    {
      {Name = "$OFF", Value = "0"},
      {Name = "$ON",  Value = "1"},
    },
  },

  {
    Label = "@Toggle Targeting",
    Function = "fes_SetTargetToggle",
    GetValueFunction = "fes_GetTargetToggle",
    Entries =
    {
      {Name = "$OFF", Value = "0"},
      {Name = "$ON",  Value = "1"},
    },
  },

  {
    Label = "@Toggle SmartTargetCamera AutoSelection",
    Function = "fes_SetSmartTargetCameraOn",
    GetValueFunction = "fes_GetSmartTargetCameraOn",
    Entries =
    {
      {Name = "$OFF", Value = "0"},
      {Name = "$ON",  Value = "1"},
    },
  },

  --[[--
  -- Broken?
  {
    Label = "@New Jump Mechanic",
    Function = "fes_SetMarioJump",
    GetValueFunction = "fes_GetMarioJump",
    Entries =
    {
      {Name = "$OFF", Value = "0"},
      {Name = "$ON",  Value = "1"},
    },
  },
  --]]--

  -- I have no idea what this does.
  {
    Label = "@Special Attack Glow",
    Function = "fes_SetSpecialAttackGlow",
    GetValueFunction = "fes_GetSpecialAttackGlow",
    Entries =
    {
      {Name = "$OFF", Value = "0"},
      {Name = "$ON",  Value = "1"},
    },
  },

  {
    Label = "Time Of Day",
    Function = "fes_SetTimeOfDay",
    --GetValueFunction = "fes_GetTimeOfDay",
    GetValueFunction = "gibbed_GetTimeOfDay",
    Entries = gibbed_GenerateTimeOfDayEntries(),
  },

  {
    Label = "@Hard Coded Text Marking",
    Function = "fe_SetTextBibleDebugCheck",
    GetValueFunction = "fe_GetTextBibleDebugCheck",
    ParamIsNumber = true,
    Entries =
    {
      {Name = "$OFF", Value = "0"},
      {Name = "$ON",  Value = "1"},
    },
  },

  {
    Label = "@Instakill",
    Function = "fes_SetInstakill",
    GetValueFunction = "fes_GetInstakill",
    Entries =
    {
      {Name = "$OFF", Value = "0"},
      {Name = "$ON", Value = "1"},
    },
  },

  {
    Label = "@Award EP levels",
    Function = "fe_DebugSetAwardEPLevels",
    GetValueFunction = "fe_DebugGetAwardEPLevels",
    Entries = fes_GenerateLevelAwardEntriesDbg(),
  },

  -- I have no idea what this does.
  {
    Label = "@Set Current Consume Spec",
    Function = "fes_DebugConsumeSpecChosen",
    GetValueFunction = "fes_GetDebugConsumeSpecValue",
    Entries =
    {
      {Name = "@Min Spec", Value = false},
      {Name = "@Max Spec", Value = true},
    },
  },
}
