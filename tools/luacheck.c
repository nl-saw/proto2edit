/* luacheck.c - syntax-check Lua source with LuaJIT (Lua 5.1), the language version the game uses.
 *
 * The game compiles mission Lua at load time and aborts the mission on a script error, so an edit
 * should be syntax-checked before it is installed. Nothing is executed: luaL_loadfile only compiles.
 *
 *   build/luacheck file.lua [more.lua ...]
 */
#include <lua.h>
#include <lauxlib.h>
#include <stdio.h>

int main(int argc, char **argv)
{
    lua_State *L;
    int i, rc = 0;
    if (argc < 2) {
        fprintf(stderr, "usage: luacheck file.lua [...]\n");
        return 2;
    }
    L = luaL_newstate();
    if (!L) {
        fprintf(stderr, "cannot create lua state\n");
        return 2;
    }
    for (i = 1; i < argc; i++) {
        if (luaL_loadfile(L, argv[i]) != 0) {
            fprintf(stderr, "%s: %s\n", argv[i], lua_tostring(L, -1));
            rc = 1;
        } else {
            printf("%s: syntax OK\n", argv[i]);
        }
        lua_pop(L, 1);
    }
    lua_close(L);
    return rc;
}
