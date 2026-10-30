#!/usr/bin/env python3
"""Decompile `scripts/missiondefinit.lua` and gate every prototype against the shipped bytes.

The chunk is the mission-side prelude: 237 prototypes, 13,747 instructions, fully stripped (no source
name, no line info, no local names) and compiled with 4-byte floats. It is the one boot script the
project could not regenerate (docs/29), so this tool answers, per prototype, "is the source we can
print actually the source that produced these bytes?".

The gate is the same idea as `script_edit` uses for records: decompile, recompile, and require the
instruction stream to come back identical. Two details decide whether that gate is even reachable:

  * the compiler must be `luac-novararg`, not `luac`. The kit's default `luac` defines
    LUA_COMPAT_VARARG, which reserves an implicit `arg` register in every function declared with
    '...'; 210 of the 237 prototypes here are vararg, so the default kit shifts every temporary up
    one register and fails the whole file for a reason that has nothing to do with the decompiler.
  * the chunk is stripped, so local names are invented. A prototype whose original source kept a
    literal in a local that the decompiler inlines cannot be byte-identical, and is reported as
    `code-differs` rather than silently counted as wrong.

    python3 tools/mdef_decode.py                  # full report, writes work/lua/
    python3 tools/mdef_decode.py --decompiler luadec
    python3 tools/mdef_decode.py --json OUT       # machine-readable per-prototype status
"""
import argparse
import collections
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import config  # noqa: E402
import lua51  # noqa: E402
import rcf  # noqa: E402

CHUNK_NAME = "scripts\\missiondefinit.lua"
KIT = os.path.join(ROOT, "build", "luadec-p2", "bin")
DECOMPILERS = {
    "unluac-rs": os.path.join(ROOT, "build", "unluac-rs", "unluac-cli"),
    "luadec": os.path.join(KIT, "luadec"),
}
OUT_DIR = os.path.join(ROOT, "work", "lua")
EXTRACTED = os.path.join(ROOT, "work", "extracted", "boot", "scripts", "missiondefinit.lua")


def chunk_bytes():
    """The shipped chunk, extracted from boot.rcf if the loose copy is not there yet."""
    if os.path.exists(EXTRACTED):
        return open(EXTRACTED, "rb").read(), EXTRACTED
    game = config.game_dir()
    if not game:
        raise SystemExit("no game directory configured; run ./proto2 config set game_dir ...")
    with rcf.Rcf(os.path.join(game, "boot.rcf")) as archive:
        for entry in archive.entries:
            if (entry.name or "").lower() == CHUNK_NAME.lower():
                data = archive.read(entry)
                os.makedirs(os.path.dirname(EXTRACTED), exist_ok=True)
                open(EXTRACTED, "wb").write(data)
                return data, EXTRACTED
    raise SystemExit(f"{CHUNK_NAME} not found in boot.rcf")


def decompile(chunk, impl, src_path, temp_dir):
    if impl == "unluac-rs":
        tool = DECOMPILERS[impl]
        if not os.access(tool, os.X_OK):
            raise SystemExit("unluac-rs is not built (bash tools/build_unluac_rs.sh)")
        via_file = True
    else:
        tool = DECOMPILERS[impl]
        if not os.access(tool, os.X_OK):
            raise SystemExit("LuaDec kit is not built (bash tools/build_luadec.sh)")
        via_file = False
    chunk_path = os.path.join(temp_dir, "chunk.luac")
    open(chunk_path, "wb").write(chunk)
    cmd = ([tool, "-D", "lua51", "-i", chunk_path] if via_file
           else [tool, chunk_path])
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    text = proc.stdout.decode("latin-1")
    if proc.returncode != 0 or not text.strip():
        raise SystemExit(f"{impl} failed: {proc.stderr.decode('latin-1')[:400]}")
    open(src_path, "w", encoding="latin-1").write(text)
    return text


def recompile(src_path, out_path, luac):
    proc = subprocess.run([luac, "-s", "-o", out_path, src_path],
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise SystemExit(f"{os.path.basename(luac)} rejected the decompiled source: "
                         f"{(proc.stderr or proc.stdout).strip()[:400]}")
    return open(out_path, "rb").read()


def opcode_mix(proto, opcodes):
    c = collections.Counter()
    for ins in proto.code:
        c[opcodes[lua51.decode(ins)[0]]] += 1
    return c


def classify(orig, back, opcodes):
    """Per-prototype verdict, over that prototype's own code, constants and metadata.

    Deliberately not recursive. `lua51.signature()` includes the subtree, so a prototype whose own instructions
    match would be marked as differing only because one of its children does - and that child already has its
    own row. Counting the parent too would overstate the remaining work.
    """
    own_same = (tuple(orig.code) == tuple(back.code)
                and list(orig.constants) == list(back.constants)
                and len(orig.protos) == len(back.protos))
    meta_same = (orig.num_params, orig.is_vararg) == (back.num_params, back.is_vararg)
    if own_same and meta_same:
        return "identical", {}
    if not tuple(orig.code) == tuple(back.code):
        a, b = opcode_mix(orig, opcodes), opcode_mix(back, opcodes)
        delta = {k: b[k] - a[k] for k in set(a) | set(b) if b[k] != a[k]}
        if not delta:
            return "operands", {}
        structural = {"FORLOOP", "FORPREP", "TFORLOOP", "JMP", "EQ", "LT", "LE", "TEST", "TESTSET"} & set(delta)
        return ("control-flow" if structural else "codegen"), delta
    if own_same and not meta_same:
        return "metadata", {"num_params": [orig.num_params, back.num_params],
                            "is_vararg": [orig.is_vararg, back.is_vararg]}
    return "constants", {}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--decompiler", default="unluac-rs", choices=sorted(DECOMPILERS))
    ap.add_argument("--json", metavar="PATH", help="where to write the machine-readable status")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    chunk, chunk_path = chunk_bytes()
    os.makedirs(OUT_DIR, exist_ok=True)
    temp_dir = os.path.join(ROOT, "tmp", "mdef_decode")
    os.makedirs(temp_dir, exist_ok=True)

    src_path = os.path.join(OUT_DIR, f"missiondefinit.{a.decompiler}.lua")
    decompile(chunk, a.decompiler, src_path, temp_dir)

    luac = os.path.join(KIT, "luac-novararg")
    if not os.access(luac, os.X_OK):
        raise SystemExit("luac-novararg is missing; run bash tools/build_luadec.sh "
                         "(the default luac cannot reproduce this chunk - see docs/29)")
    back_chunk = recompile(src_path, os.path.join(temp_dir, "recompiled.luac"), luac)

    orig_protos = list(lua51.walk(lua51.load(chunk)))
    back_protos = list(lua51.walk(lua51.load(back_chunk)))
    if len(orig_protos) != len(back_protos):
        raise SystemExit(f"prototype count changed {len(orig_protos)} -> {len(back_protos)}; "
                         "the decompiled source does not describe the same file")

    status = collections.Counter()
    rows = []
    for index, (orig, back) in enumerate(zip(orig_protos, back_protos)):
        verdict, delta = classify(orig, back, lua51.OPCODES)
        status[verdict] += 1
        if verdict != "identical":
            rows.append({
                "proto": index,
                "verdict": verdict,
                "instructions": [len(orig.code), len(back.code)],
                "vararg": orig.is_vararg,
                "params": orig.num_params,
                "opcode_delta": delta,
            })

    total = len(orig_protos)
    report = {
        "chunk": os.path.relpath(chunk_path, ROOT),
        "chunk_bytes": len(chunk),
        "chunk_sha1": __import__("hashlib").sha1(chunk).hexdigest(),
        "decompiler": a.decompiler,
        "source": os.path.relpath(src_path, ROOT),
        "gate": os.path.relpath(luac, ROOT),
        "protos": total,
        "instructions": sum(len(p.code) for p in orig_protos),
        "status": dict(status),
        "failures": rows,
    }
    json_path = a.json or os.path.join(OUT_DIR, f"missiondefinit.{a.decompiler}.status.json")
    open(json_path, "w").write(json.dumps(report, indent=1, sort_keys=True) + "\n")

    if not a.quiet:
        print(f"chunk        {report['chunk']}  {len(chunk)} bytes  sha1 {report['chunk_sha1'][:12]}")
        print(f"decompiler   {a.decompiler} -> {report['source']}")
        print(f"gate         {report['gate']}")
        print(f"prototypes   {total}  {report['instructions']} instructions")
        for verdict in ("identical", "metadata", "constants", "operands", "codegen", "control-flow"):
            if status[verdict]:
                print(f"  {verdict:12s} {status[verdict]:4d}")
        recovered = status["identical"]
        print(f"  recompiles to the shipped instructions: {recovered}/{total}")
        if rows:
            print(f"  first failures: {[(r['proto'], r['verdict'], r['instructions']) for r in rows[:8]]}")
        print(f"status json  {os.path.relpath(json_path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
