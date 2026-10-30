-- The boot script is something I added to allow you to execute code
-- before debug_buttons.lua and debug_toggles.lua get executed.

dofile("scripts/debug_gibbed_spawner.lua")
pcall(dofile, "scripts/proto2_extra.lua")
pcall(dofile, "scripts/proto2_autospawn.lua")
