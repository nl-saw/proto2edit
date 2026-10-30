#!/usr/bin/env python3
"""Compute the 64-bit id the engine uses for Lua name arguments.

Objectives are not identified by their string. `fe_SetCurrentObjectives` and
friends take `"$SOME_NAME"`, strip the leading `$`, check the name against a
registry, and then hash it to a 64-bit id which is what the objective table
actually compares.

Algorithm, read from `FUN_1063be80` in prototype2engine.dll (evidence
ev_5ccd731b...): a 64-bit accumulator, multiplied by 0x1003F and XORed with the
sign-extended byte of each character:

    h = 0
    for each byte c:  h = (h * 0x1003F) ^ sign_extend(c)

The multiplier is a literal in the code (`PUSH 0x0 / PUSH 0x1003f` feeding a
64x64 multiply), not a decompiler artefact.

This is not the hash used for META class ids or RCF entry hashes - those are
32-bit and none of the common algorithms match them, so they are assigned rather
than computed.

    python3 tools/uid.py INTRO1_MOVE_TUTORIAL4 '$INTRO1_MOVE_TUTORIAL4' UID_TEST
"""

import sys

PRIME = 0x1003F
MASK = (1 << 64) - 1


def uid(name):
    """64-bit id for a Lua name the way the engine computes it."""
    h = 0
    for byte in name.encode("latin-1"):
        h = (h * PRIME) & MASK
        c = byte if byte < 0x80 else byte - 0x100       # sign-extend like MOVSX
        h ^= c & MASK
    return h


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    strip = "--keep-dollar" not in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    for a in args:
        name = a[1:] if (strip and a.startswith("$")) else a
        print(f"{uid(name):#018x}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
