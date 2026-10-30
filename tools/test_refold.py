#!/usr/bin/env python3
"""The refold transform must only ever produce a candidate that the gate can then accept or reject.

It rewrites decompiled Lua text, so its two failure modes are "misses a fold" (harmless: the record stays as it
was) and "produces a candidate that is accepted wrongly" (impossible: acceptance requires an identical recompiled
signature). This pins the first half - the shapes it does and does not fold - on synthetic text, so a change to the
regexes cannot silently stop folding anything.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refold as RF

fails = []


def ck(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name:<52} {detail}")
    if not ok:
        fails.append(name)


simple = """local t = {}
t.someField = someValue
target = t
"""
cands = RF.fold(simple)
ck("folds a constructor into the assignment that consumes it",
   len(cands) == 1 and "target = { someField = someValue }" in cands[0], cands[:1])

returned = """local t = {}
t.alpha = 1
t.beta = "x"
return t
"""
cands = RF.fold(returned)
ck("folds into a return", len(cands) == 1 and "return { alpha = 1, beta = \"x\" }" in cands[0], cands[:1])

bracket = """local t = {}
t["k"] = 4
out = t
"""
ck("folds bracket keys", len(RF.fold(bracket)) == 1, RF.fold(bracket))

used_later = """local t = {}
t.x = 1
out = t
print(t)
"""
ck("does not fold when the temporary is used afterwards", RF.fold(used_later) == [], RF.fold(used_later))

no_consumer = """local t = {}
t.x = 1
t.y = 2
"""
ck("does not fold without a consuming statement", RF.fold(no_consumer) == [], RF.fold(no_consumer))

non_empty = """local t = { a = 1 }
t.b = 2
out = t
"""
ck("ignores a constructor that already has fields", RF.fold(non_empty) == [], RF.fold(non_empty))

interleaved = """local t = {}
t.x = 1
doSomething()
out = t
"""
cands = RF.fold(interleaved)
ck("keeps statements between the fields and the consumer",
   cands and "doSomething()" in cands[0] and "out = { x = 1 }" in cands[0], cands[:1])

nested = """local a = {}
a.x = 1
local b = {}
b.inner = a
out = b
"""
cands = RF.fold(nested)
ck("nests a second pass", len(cands) == 2 and "out = { inner = { x = 1 } }" in cands[-1], cands)



# --- the transforms added while recovering the last records -----------------------------------------------------

guarded = """if not State then
  State = rebuild()
  keep()
  also()
end
"""
cands = RF.guard_or(guarded)
ck("turns an if-guard with any assignment into `or`",
   len(cands) == 1 and "State = State or (rebuild())" in cands[0] and "keep()" in cands[0]
   and "  keep()" not in cands[0], cands[:1])

constructor_guard = """if not State then
  State = {}
  work()
end
"""
cands = RF.guard_or(constructor_guard)
ck("keeps the plain constructor form unchanged",
   len(cands) == 1 and "State = State or {}" in cands[0], cands[:1])

wrapped = """do (t[1]).data = build({ x = 1 })
"""
cands = RF.hoist(wrapped, do_wrapped=True)
ck("hoists from a do-wrapped field assignment",
   len(cands) == 1 and "local __ctor" in cands[0] and "build(__ctor" in cands[0], cands[:1])
ck("the plain hoist ignores the do-wrapped prefix", RF.hoist(wrapped) == [])

compound = """if a <= b and flag == 1 then
  work()
end
"""
cands = RF.comparison_swaps(compound)
ck("swaps a comparison inside an and-chain",
   any("b >= a and flag == 1" in c for c in cands), cands[:2])
ck("leaves the other term alone in the first candidate",
   "a <= b and 1 == flag" not in cands[0] and "flag == 1" in cands[0], cands[:1])

materialised = """local fn = doThing
local obj = target
fn = fn(obj)
out = fn
"""
cands = RF.collapse_call(materialised)
ck("collapses a call through materialised locals",
   bool(cands) and "out = doThing(target)" in cands[0], cands[:1])

hoisted = """boom = call({ a = 1 })
"""
cands = RF.hoist(hoisted)
earlier = RF.hoist_earlier("first()\nsecond()\n" + hoisted) if cands else []
ck("moves a hoisted local up a statement", bool(earlier) and earlier[0].index("local __ctor") < earlier[0].index("second()"),
   earlier[:1])

# --- recovery rules measured on the hand-reconstructed records (docs/32) ---------------------------------------

empty_else_src = """if cond then
  work()
end
"""
cands = RF.empty_else(empty_else_src)
ck("adds an empty else to a bare if block",
   len(cands) == 1 and "else\nend" in cands[0], cands[:1])
already_else = """if cond then
  work()
else
  other()
end
"""
ck("does not add an else to a block that has one", RF.empty_else(already_else) == [])

ck("fills LuaDec's empty assignment slot",
   RF.fill_empty_slots("local a, b = , f()\n") == "local a, b = nil, f()\n")
ck("leaves a normal assignment alone", RF.fill_empty_slots("local a, b = 1, 2\n") is None)

ck("inlines an atomic single-use local",
   RF.inline_single_use("local fn = doThing\nfn(x)\n") == "doThing(x)\n")
ck("does not inline a local that is used twice",
   RF.inline_single_use("local fn = doThing\nfn(x)\nfn(y)\n") is None)

ck("folds a constructor that has no consumer",
   RF.fold_dead_constructor("local t = {}\nt.a = one()\nt.b = 2\n") == "local t = {a = one(), b = 2}\n")

ck("merges a do-block copy-back",
   RF.merge_copyback("do\n  local a, b = f()\n  x = a\n  g = b\nend\n") == "x, g = f()\n")
ck("flattens an assignment-only do block", RF.flatten_do("do\n  x = 1\nend\n") == "  x = 1\n")

results = RF.materialise_results("f(1)\n")
ck("materialises a discarded call result", len(results) == 1 and results[0] == "local _r0 = f(1)\n")

pipeline = RF.recovery_candidates(empty_else_src)
ck("recovery candidates include the empty else", any("else\nend" in c for c in pipeline), pipeline[:2])

if fails:
    print("refold: FAILED " + ", ".join(fails))
    sys.exit(1)
print("refold: transform shapes agree")