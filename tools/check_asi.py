#!/usr/bin/env python3
"""Verify a built .asi can actually load: every imported DLL must exist in the game directory or be a
Windows component.

A thread-local variable silently pulled `libgcc_s_dw2-1.dll` into the recorder, the game refused to load it
with error 126 (module not found), and the failure only showed up at launch - after the instrument had
already cost a playthrough. This check runs the same analysis the loader does.

    python3 tools/check_asi.py build/*.asi
"""
import os
import struct
import sys
import config

SYSTEM = {
    "kernel32.dll", "user32.dll", "gdi32.dll", "advapi32.dll", "msvcrt.dll", "shell32.dll",
    "ole32.dll", "oleaut32.dll", "ws2_32.dll", "winmm.dll", "version.dll", "ntdll.dll",
    "d3d9.dll", "d3dx9_43.dll", "xinput1_3.dll", "binkw32.dll",
}
GAME = config.game_dir()
# Proton supplies the VC runtime inside the prefix, not the game folder
_PREFIX = config.proton_prefix()
SEARCH_DIRS = [GAME]
if _PREFIX:
    SEARCH_DIRS += [os.path.join(_PREFIX, "drive_c", "windows", "system32"),
                    os.path.join(_PREFIX, "drive_c", "windows", "syswow64")]


def _listing(d):
    """lowercase filenames in a directory (this filesystem is case-sensitive; Windows is not)."""
    try:
        return {n.lower() for n in os.listdir(d)}
    except OSError:
        return set()


def present(dll):
    low = dll.lower()
    return any(low in _listing(d) for d in SEARCH_DIRS)


def imports(path):
    data = open(path, "rb").read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    optsz = struct.unpack_from("<H", data, pe + 20)[0]
    sec = pe + 24 + optsz
    rva = {}
    for i in range(nsec):
        o = sec + i * 40
        vs, va, rs, ra = struct.unpack_from("<IIII", data, o + 8)
        rva[va] = (ra, vs)
    # data directories: import table is entry 1
    dd = pe + 24 + 96
    imp_rva, imp_sz = struct.unpack_from("<II", data, dd + 8)
    out = []

    def off(addr):
        for va, (ra, vs) in rva.items():
            if va <= addr < va + max(vs, 0x1000):
                return ra + (addr - va)
        return None

    p = off(imp_rva)
    while p and p + 20 <= len(data):
        name_rva = struct.unpack_from("<I", data, p + 12)[0]
        if not name_rva:
            break
        q = off(name_rva)
        if q is None:
            break
        end = data.index(b"\0", q)
        out.append(data[q:end].decode("latin-1"))
        p += 20
    return out


def main(argv=None):
    paths = (argv or sys.argv[1:]) or ["build/proto2_inflate2.asi"]
    bad = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"{path}: missing")
            bad += 1
            continue
        missing = []
        for dll in imports(path):
            low = dll.lower()
            if low in SYSTEM or low.startswith("api-ms-win-"):   # API sets resolve via the OS/Wine
                continue
            if present(dll):
                continue
            missing.append(dll)
        status = "OK" if not missing else "WILL NOT LOAD (error 126)"
        print(f"{os.path.basename(path):<28} {status}" + (f"  missing: {', '.join(missing)}" if missing else ""))
        bad += 1 if missing else 0
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
