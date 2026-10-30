#!/usr/bin/env python3
"""Minimal PE32/PE32+ reader: headers, sections, imports, exports, version info.

Dependency-free on purpose so the repository tooling runs anywhere Python does.
Used here to inventory prototype2.exe / prototype2engine.dll and to resolve
RVA<->file-offset for REA address targets.
"""
import argparse
import json
import struct
import sys

MACHINES = {0x14c: "i386", 0x8664: "amd64", 0xaa64: "arm64", 0x1c0: "arm"}


class PE:
    def __init__(self, path):
        self.path = path
        self.data = open(path, "rb").read()
        d = self.data
        if d[:2] != b"MZ":
            raise ValueError("not an MZ image")
        (self.e_lfanew,) = struct.unpack_from("<I", d, 0x3C)
        if d[self.e_lfanew:self.e_lfanew + 4] != b"PE\0\0":
            raise ValueError("missing PE signature")
        pe = self.e_lfanew + 4
        (self.machine, self.nsections, self.timestamp, _, _,
         self.opt_size, self.characteristics) = struct.unpack_from("<HHIIIHH", d, pe)
        opt = pe + 20
        (self.magic,) = struct.unpack_from("<H", d, opt)
        self.is64 = self.magic == 0x20B
        if self.is64:
            (self.image_base,) = struct.unpack_from("<Q", d, opt + 24)
            self.dd = opt + 112
        else:
            (self.image_base,) = struct.unpack_from("<I", d, opt + 28)
            self.dd = opt + 96
        (self.entry_rva,) = struct.unpack_from("<I", d, opt + 16)
        (self.subsystem,) = struct.unpack_from("<H", d, opt + 68)
        self.sections = []
        sec = opt + self.opt_size
        for i in range(self.nsections):
            base = sec + i * 40
            name = d[base:base + 8].rstrip(b"\0").decode("latin-1")
            vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", d, base + 8)
            chars, = struct.unpack_from("<I", d, base + 36)
            self.sections.append(dict(name=name, vsize=vsize, vaddr=vaddr,
                                      raw_size=rsize, raw_ptr=raddr, characteristics=chars))

    # -- address translation -------------------------------------------
    def rva_to_offset(self, rva):
        for s in self.sections:
            span = max(s["vsize"], s["raw_size"])
            if s["vaddr"] <= rva < s["vaddr"] + span:
                delta = rva - s["vaddr"]
                if delta >= s["raw_size"]:
                    return None
                return s["raw_ptr"] + delta
        return None

    def va_to_offset(self, va):
        return self.rva_to_offset(va - self.image_base)

    def read_rva(self, rva, size):
        off = self.rva_to_offset(rva)
        if off is None:
            return b""
        return self.data[off:off + size]

    def cstring(self, rva, limit=4096):
        off = self.rva_to_offset(rva)
        if off is None:
            return None
        end = self.data.find(b"\0", off, off + limit)
        if end < 0:
            return None
        return self.data[off:end].decode("latin-1")

    # -- directories ----------------------------------------------------
    def _dd(self, idx):
        rva, size = struct.unpack_from("<II", self.data, self.dd + idx * 8)
        return rva, size

    def imports(self):
        rva, size = self._dd(1)
        out = []
        if not rva:
            return out
        off = self.rva_to_offset(rva)
        if off is None:
            return out
        p = off
        while True:
            try:
                ilt, ts, fc, name_rva, iat = struct.unpack_from("<IIIII", self.data, p)
            except struct.error:
                break
            if not (ilt or name_rva or iat):
                break
            dll = self.cstring(name_rva) or "?"
            funcs = []
            thunk_rva = ilt or iat
            toff = self.rva_to_offset(thunk_rva)
            if toff is not None:
                step = 8 if self.is64 else 4
                fmt = "<Q" if self.is64 else "<I"
                flag = (1 << 63) if self.is64 else (1 << 31)
                i = 0
                while True:
                    (val,) = struct.unpack_from(fmt, self.data, toff + i * step)
                    if val == 0:
                        break
                    if val & flag:
                        funcs.append(f"#{val & 0xFFFF}")
                    else:
                        nm = self.cstring((val & 0x7FFFFFFF) + 2)
                        funcs.append(nm or f"0x{val:x}")
                    i += 1
                    if i > 20000:
                        break
            out.append({"dll": dll, "functions": funcs})
            p += 20
        return out

    def exports(self):
        rva, size = self._dd(0)
        out = []
        if not rva:
            return out
        off = self.rva_to_offset(rva)
        if off is None:
            return out
        (chars, ts, maj, mnr, name_rva, base, nfuncs,
         nnames, funcs_rva, names_rva, ords_rva) = struct.unpack_from("<IIHHIIIIIII", self.data, off)
        dll_name = self.cstring(name_rva) or ""
        foff = self.rva_to_offset(funcs_rva)
        noff = self.rva_to_offset(names_rva)
        ooff = self.rva_to_offset(ords_rva)
        if None in (foff, noff, ooff):
            return out
        for i in range(nnames):
            (nm_rva,) = struct.unpack_from("<I", self.data, noff + i * 4)
            (ordinal,) = struct.unpack_from("<H", self.data, ooff + i * 2)
            (frva,) = struct.unpack_from("<I", self.data, foff + ordinal * 4)
            out.append({"name": self.cstring(nm_rva) or f"ord{ordinal}",
                        "ordinal": base + ordinal,
                        "rva": frva, "va": self.image_base + frva})
        return out

    def summary(self):
        return {
            "path": self.path,
            "machine": MACHINES.get(self.machine, hex(self.machine)),
            "is_64": self.is64,
            "timestamp": self.timestamp,
            "image_base": f"0x{self.image_base:x}",
            "entry_rva": f"0x{self.entry_rva:x}",
            "entry_va": f"0x{self.image_base + self.entry_rva:x}",
            "sections": self.sections,
            "imports": self.imports(),
            "exports_count": len(self.exports()),
            "exports": self.exports(),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("cmd", choices=["summary", "sections", "imports", "exports"])
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    pe = PE(a.path)
    if a.cmd == "summary":
        s = pe.summary()
        s["exports"] = s["exports"][:a.limit or None] if a.limit else s["exports"]
        print(json.dumps(s, indent=1))
        return
    if a.cmd == "sections":
        for s in pe.sections:
            print(f"{s['name']:10} va=0x{s['vaddr']:08x} vsize=0x{s['vsize']:08x} "
                  f"raw=0x{s['raw_ptr']:08x}+0x{s['raw_size']:08x}")
    elif a.cmd == "imports":
        for m in pe.imports():
            print(f"{m['dll']}  ({len(m['functions'])})")
            for f in m["functions"][:a.limit or None]:
                print("   ", f)
    elif a.cmd == "exports":
        ex = pe.exports()
        for e in sorted(ex, key=lambda e: e["rva"])[:a.limit or None]:
            print(f"0x{e['va']:08x}  {e['name']}")


if __name__ == "__main__":
    sys.exit(main())
