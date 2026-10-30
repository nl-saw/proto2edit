#!/usr/bin/env python3
"""Measured totals for the shipped corpus, in one place, so checks do not carry bare numbers.

These are not settings: they are the size of the mission corpus that the shipped archives contain, measured once
and used as a canary. A different edition or patch changes all of them together, and the checks that compare
against them say "the corpus changed" instead of failing on a literal.

Regenerate by running `./proto2 progress` (records, bytecode) and `./proto2 coverage` (variants) on the shipped
archives; nothing here is written at run time.
"""
MISSION_VARIANTS = 430     # mission/variant pairs across art/*.rcf
RECORDS = 4793             # Lua payload records in those mission files
BYTECODE = 994             # records whose payload is a Lua 5.1 chunk rather than source text
