#!/usr/bin/env bash
# Build our own LuaDec + Lua 5.1 (32-bit, 4-byte floats) from viruscamp/luadec source.
#
# The prebuilt "p2 kit" binaries were convenient but we do not want to depend on them. This script pins the
# upstream source, applies the two changes Prototype 2 needs, and builds both tools locally:
#
#   1. numbers are 4-byte floats, not doubles  (P2 chunk header: 1b 4c 75 61 51 00 01 04 04 04 04 00)
#   2. build with -m32, so sizeof(size_t)==4 as that header requires
#
# Output: build/luadec-p2/bin/{luadec,luac,luac-novararg}
#
# Two compilers, and the difference is not cosmetic. The kit defines LUA_COMPAT_VARARG, so a function
# declared with '...' gets an implicit 'arg' local in its first register and is_vararg = 7. Mission record
# payloads match that codegen, but the boot chunks do not, so `scripts/missiondefinit.lua` cannot be
# reproduced by the canonical `luac` at all (every vararg proto comes out one register wider). `luac-novararg`
# is the same compiler built with that define cleared and is what `tools/mdef_decode.py` gates against.
# It is built from a scratch copy under tmp/ so the canonical kit keeps its recorded verdicts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:-$ROOT/reference/luadec-src}"
OUT="$ROOT/build/luadec-p2/bin"

if [ ! -d "$SRC/.git" ]; then
    echo "cloning viruscamp/luadec into $SRC"
    git clone --depth 1 https://github.com/viruscamp/luadec "$SRC"
    ( cd "$SRC" && git submodule update --init --depth 1 lua-5.1 )
fi

CONF="$SRC/lua-5.1/src/luaconf.h"
if grep -q "LUA_NUMBER_DOUBLE" "$CONF"; then
    echo "patching luaconf.h for 4-byte floats"
    python3 - "$CONF" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
s = s.replace('#define LUA_NUMBER_DOUBLE\n#define LUA_NUMBER\tdouble',
              '/* Prototype 2 ships Lua 5.1 with 4-byte numbers (float). */\n'
              '#define LUA_NUMBER_FLOAT\n#define LUA_NUMBER\tfloat')
s = s.replace('#define LUA_NUMBER_SCAN\t\t"%lf"\n#define LUA_NUMBER_FMT\t\t"%.14g"',
              # 9 significant digits, not 7: a 32-bit float needs FLT_DECIMAL_DIG digits to round-trip exactly.
              # With %.7g the decompiler printed 0.3141592 for a constant held as 0.31415924f and the recompiled
              # chunk got a *different* float, so the record failed the byte-identical gate (test_optimization).
              '#define LUA_NUMBER_SCAN\t\t"%f"\n#define LUA_NUMBER_FMT\t\t"%.9g"')
s = s.replace('#define lua_str2number(s,p)\tstrtod((s), (p))',
              '#define lua_str2number(s,p)\t((lua_Number)strtod((s), (p)))')
open(p, 'w').write(s)
PY
fi

# --- decompiler fixes for chunks that used to emit invalid Lua -------------------------------------------
# 1. statement.c: a statement beginning with '(' after another statement is ambiguous in Lua 5.1. luadec emitted
#    a lone ';' line before it, which is illegal at the start of a block ("unexpected symbol near ';'").
#    Wrapping it in do ... end is valid and keeps the statement's semantics.
python3 - "$SRC/luadec/statement.c" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='latin-1').read()
old = """\t// start with '(' and not the first statement of the block
\tif (parent->sub_print_count > 0 && stmt->code[0] == '(') {
\t\tPrintIndent(buff, indent);
\t\tStringBuffer_addPrintf(buff, ";\\n");
\t}"""
new = """\t// '(' after another statement is ambiguous in Lua 5.1; a lone ';' here is illegal, so wrap it.
\tif (parent->sub_print_count > 0 && stmt->code[0] == '(') {
\t\tPrintIndent(buff, indent);
\t\tStringBuffer_addPrintf(buff, "do %s end\\n", stmt->code);
\t\treturn;
\t}"""
if old in s:
    open(p, 'w', encoding='latin-1').write(s.replace(old, new))
    print("patched statement.c: ambiguous '(' statements")
PY

# 2. decompile.c: OP_SELF emitted `a:b` even when the method value was used as a plain expression, which does not
#    parse ("function arguments expected"). Keep ':' only when the next instruction calls that register.
python3 - "$SRC/luadec/decompile.c" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='latin-1').read()
old = "\t\t\tMakeIndex(F, str, cstr, SELF);\n\t\t\tTRY(AssignReg(F, a, StringBuffer_getRef(str), 0, 0));"
new = ("\t\t\t/* ':' only when this SELF result is called; otherwise it is a plain value and needs '.' */\n"
       "\t\t\tint self_called = 0;\n"
       "\t\t\tif (F->pc + 1 < F->f->sizecode) {\n"
       "\t\t\t\tInstruction next = F->f->code[F->pc + 1];\n"
       "\t\t\t\tint nop = GET_OPCODE(next);\n"
       "\t\t\t\tif ((nop == OP_CALL || nop == OP_TAILCALL) && GETARG_A(next) == a)\n"
       "\t\t\t\t\tself_called = 1;\n"
       "\t\t\t}\n"
       "\t\t\tMakeIndex(F, str, cstr, self_called ? SELF : DOT);\n"
       "\t\t\tTRY(AssignReg(F, a, StringBuffer_getRef(str), 0, 0));")
if old in s:
    open(p, 'w', encoding='latin-1').write(s.replace(old, new))
    print("patched decompile.c: SELF used as a value")
PY

# 3. decompile.c: a table constructor that is still being filled was materialised before its SETLIST when a local
#    was declared on that register - `{ f() }` became `local t = {}` plus a lost list, and every constructor with
#    declared content became `{}` plus field statements (NEWTABLE's size hint lost, constants reordered).
#    Defer the declaration when the constructor declares content or when a call fills it via SETLIST; the table then
#    closes on its own and prints as a real constructor. The skipped slot's release is defused so freeLocal stays
#    balanced.
python3 - "$SRC/luadec/decompile.c" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='latin-1').read()
old = """\t\t\tif (PENDING(r)) {
\t\t\t\tif (locals > 0) {
\t\t\t\t\tStringBuffer_add(str, ", ");
\t\t\t\t\tStringBuffer_add(rhs, ", ");
\t\t\t\t}
\t\t\t\tStringBuffer_add(str, LOCAL(i));
\t\t\t\tStringBuffer_add(rhs, GetR(F, r));
\t\t\t\tif (error) return;"""
new = """\t\t\tif (PENDING(r) && IS_TABLE(r)) {
\t\t\t\tDecTable* pending = (DecTable*)FindFromListTail(&(F->tables), (ListItemCmpFn)MatchTable, &r);
\t\t\t\tif (pending) {
\t\t\t\t\tint q, done = 0, saw_call = 0, saw_setlist = 0;
\t\t\t\t\tfor (q = pending->pc + 1; q < F->f->sizecode && q <= pending->pc + 64 && !done; q++) {
\t\t\t\t\t\tInstruction ins = F->f->code[q];
\t\t\t\t\t\tOpCode op = GET_OPCODE(ins);
\t\t\t\t\t\tif (op == OP_CALL || op == OP_TAILCALL) saw_call = 1;
\t\t\t\t\t\tif (op == OP_SETLIST && GETARG_A(ins) == r) { saw_setlist = 1; done = 1; }
\t\t\t\t\t\telse if (op == OP_SETTABLE && GETARG_A(ins) == r) done = 1;
\t\t\t\t\t\telse if (op == OP_SETGLOBAL || op == OP_SETUPVAL) done = 1;
\t\t\t\t\t}
\t\t\t\t\tif ((saw_call && saw_setlist) || pending->arraySize > 0 || pending->keyedSize > 0) {
\t\t\t\t\t\tF->f->locvars[i].endpc = 0x7fffffff;
\t\t\t\t\t\tcontinue;
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t\tif (PENDING(r)) {
\t\t\t\tif (locals > 0) {
\t\t\t\t\tStringBuffer_add(str, ", ");
\t\t\t\t\tStringBuffer_add(rhs, ", ");
\t\t\t\t}
\t\t\t\tStringBuffer_add(str, LOCAL(i));
\t\t\t\tStringBuffer_add(rhs, GetR(F, r));
\t\t\t\tif (error) return;"""
if old in s:
    open(p, 'w', encoding='latin-1').write(s.replace(old, new, 1))
    print("patched decompile.c: constructor kept open until it is complete")
PY

echo "building Lua 5.1 (32-bit, float)"
( cd "$SRC/lua-5.1/src" && rm -f ./*.o liblua.a lua luac && \
  make all MYCFLAGS="-m32 -DLUA_USE_POSIX" MYLDFLAGS="-m32" >/dev/null )

echo "building LuaDec (32-bit)"
( cd "$SRC/luadec" && make clean >/dev/null 2>&1 || true; \
  make LUAVER=5.1 MYCFLAGS="-m32 -DLUA_USE_POSIX" MYLDFLAGS="-m32" >/dev/null )

echo "building Lua 5.1 (32-bit, float, no compat vararg)"
VARSRC="$ROOT/tmp/luac-novararg/src"
rm -rf "$ROOT/tmp/luac-novararg"
mkdir -p "$(dirname "$VARSRC")"
cp -a "$SRC/lua-5.1/src" "$VARSRC"
sed -i 's/^#define LUA_COMPAT_VARARG$/#undef LUA_COMPAT_VARARG/' "$VARSRC/luaconf.h"
grep -qx '#undef LUA_COMPAT_VARARG' "$VARSRC/luaconf.h" || {
    echo "could not clear LUA_COMPAT_VARARG in the scratch copy of luaconf.h" >&2; exit 1; }
( cd "$VARSRC" && rm -f ./*.o liblua.a lua luac && \
  make all MYCFLAGS="-m32 -DLUA_USE_POSIX" MYLDFLAGS="-m32" >/dev/null )

mkdir -p "$OUT"
cp "$SRC/luadec/luadec" "$OUT/luadec"
cp "$SRC/lua-5.1/src/luac" "$OUT/luac"
cp "$VARSRC/luac" "$OUT/luac-novararg"
chmod +x "$OUT/luadec" "$OUT/luac" "$OUT/luac-novararg"

echo "built:"
"$OUT/luac" -v | head -1
mkdir -p "$ROOT/tmp"
printf 'print(1.5+2)\n' > "$ROOT/tmp/_p2check.lua"
"$OUT/luac" -o "$ROOT/tmp/_p2check.luac" "$ROOT/tmp/_p2check.lua"
printf 'header: '; xxd -l 12 -p "$ROOT/tmp/_p2check.luac"
echo "expected: 1b4c7561510001040404040000 (P2 flavour)"

# The two compilers must differ exactly where LUA_COMPAT_VARARG acts, so assert it: a build that silently
# ignored the define would otherwise look identical until a decode quietly failed its gate.
printf 'local f = function(...) return 1 end\nreturn f\n' > "$ROOT/tmp/_p2varg.lua"
"$OUT/luac" -s -o "$ROOT/tmp/_p2varg.a.luac" "$ROOT/tmp/_p2varg.lua"
"$OUT/luac-novararg" -s -o "$ROOT/tmp/_p2varg.b.luac" "$ROOT/tmp/_p2varg.lua"
if cmp -s "$ROOT/tmp/_p2varg.a.luac" "$ROOT/tmp/_p2varg.b.luac"; then
    echo "FAILED: luac-novararg produces the same chunk as luac; the variant build took no effect" >&2
    exit 1
fi
echo "variant check: vararg codegen differs from luac, as intended"
rm -f "$ROOT/tmp/_p2varg.lua" "$ROOT/tmp/_p2varg.a.luac" "$ROOT/tmp/_p2varg.b.luac"
