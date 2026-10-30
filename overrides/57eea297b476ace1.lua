-- params : ...
-- function num : 0
local l_0_0 = enc_DeclareEncounter({name = MISSION_NAME .. "_enc_2_" .. util_GetUniqueName(), markerType = false})
local l_0_1 = l_0_0:AddTemplate({template = PUSTULE_TEMPLATE})
pustule_field_pustule_encSetup = {l_0_0}
structureDef = {
COMPONENTS = {
SECURE_DOORS = {
lair_exit = {
LOCKED_BY = {}
, 
POWERED_BY = {}
, 
ACCESS_LINK = {MISSION = "yz_sc_02", START_ADDRESS = "accessLink_StartPos"}
, FORCE_LOCKED = true}
}
}
}
comp_InitializeStructureMission(_Self)
