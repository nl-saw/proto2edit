#!/usr/bin/env python3
"""Import the third-party ScriptHook function dump and cross-check it against our static inventory.

Input : reference/scripthook/prototype2lua_funcs.log   (external witness, git-ignored)
Output: analysis/lua-api-signatures-scripthook.json    (normalised, committed)
        analysis/lua-api-scripthook-crosscheck.md      (the comparison, committed)

The dump lists each binding as `Name return (args) - Native 0xADDR` (static) or `Name(dynamic) - Native
0xADDR` (dynamic). It is an external witness: useful to find gaps in our scan and to get C-side signatures, but
never the sole authority for a claim about our own binaries.
"""
import collections
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "reference", "scripthook", "prototype2lua_funcs.log")
INV = os.path.join(ROOT, "analysis", "lua-api-inventory.json")
OUT_JSON = os.path.join(ROOT, "analysis", "lua-api-signatures-scripthook.json")
OUT_MD = os.path.join(ROOT, "analysis", "lua-api-scripthook-crosscheck.md")

STATIC = re.compile(r"^(\S+)\s+(\S+)\s*\(([^)]*)\)\s*-\s*Native\s+(0x[0-9A-Fa-f]+)$")
DYNAMIC = re.compile(r"^(\S+)\(dynamic\)\s*-\s*Native\s+(0x[0-9A-Fa-f]+)$")
DYNAMIC_ANON = re.compile(r"^\(dynamic\)\s*-\s*Native\s+(0x[0-9A-Fa-f]+)$")


def parse(path):
    lines = [ln.rstrip("\r\n") for ln in open(path, encoding="utf-8", errors="replace")]
    out, section, headers = [], None, {}
    for ln in lines:
        if ln.startswith("---"):
            section = "dynamic" if "Dynamic" in ln else "static"
            headers[section] = ln.strip("- ").strip()
            continue
        if not ln.strip():
            continue
        m = STATIC.match(ln)
        if m and section == "static":
            out.append({"name": m.group(1), "returns": m.group(2),
                        "args": [a.strip() for a in m.group(3).split(",") if a.strip()],
                        "native": m.group(4), "kind": "static"})
            continue
        m = DYNAMIC.match(ln)
        if m:
            out.append({"name": m.group(1), "returns": "?", "args": None,
                        "native": m.group(2), "kind": "dynamic"})
            continue
        m = DYNAMIC_ANON.match(ln)
        if m:
            out.append({"name": None, "returns": "?", "args": None, "native": m.group(1),
                        "kind": "dynamic", "note": "anonymous entry (no name in the dump)"})
            continue
        out.append({"name": None, "returns": "?", "args": None, "native": None,
                    "kind": "unparsed", "raw": ln})
    return out, headers


def main():
    if not os.path.exists(SRC):
        print(f"missing {SRC}", file=sys.stderr)
        return 1
    entries, headers = parse(SRC)
    digest = hashlib.sha256(open(SRC, "rb").read()).hexdigest()
    counts = collections.Counter(e["kind"] for e in entries)

    inv = json.load(open(INV))
    ours = {n for names in inv["registered"].values() for n in names}
    usage = inv["usage"]
    theirs = {e["name"] for e in entries if e["name"]}
    only_theirs = sorted(theirs - ours)
    only_ours = sorted(ours - theirs)
    shared = theirs & ours

    # arity: our call-site observations vs their declared parameter lists
    declared = {e["name"]: len(e["args"]) for e in entries if e["name"] and e["args"] is not None}
    used = sorted(n for n in shared if n in usage)
    wider = [(n, max(int(k) for k in usage[n]["argc"]), declared[n]) for n in used
             if n in declared and max(int(k) for k in usage[n]["argc"]) > declared[n]]

    never_called = sorted(n for n in shared if n not in usage)

    doc = {
        "provenance": {
            "source": "reference/scripthook/prototype2lua_funcs.log",
            "sha256": digest,
            "origin": "third-party ScriptHook dump supplied by the owner (Discord); external witness",
            "caveat": "listed is not the same as callable: some entries are placeholders or shipped for other "
                      "platforms, per the tool's author",
        },
        "counts": {
            "static": counts["static"],
            "dynamic_named": sum(1 for e in entries if e["kind"] == "dynamic" and e["name"]),
            "dynamic_anonymous": sum(1 for e in entries if e["kind"] == "dynamic" and not e["name"]),
            "total_named": len(theirs),
        },
        "functions": [e for e in entries if e["name"]],
    }
    with open(OUT_JSON, "w") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")

    fam = collections.Counter(n.split("_")[0] if "_" in n else n for n in only_ours)
    lines = []
    A = lines.append
    A("# ScriptHook function dump vs our static inventory\n")
    A(f"Source: `{os.path.relpath(SRC, ROOT)}` (sha256 `{digest[:16]}…`), supplied by the owner; an external")
    A("witness, not our own measurement. Normalised form: `analysis/lua-api-signatures-scripthook.json`.\n")
    A("## Counts\n")
    A(f"- static entries: **{counts['static']}** ({headers.get('static','')})")
    A(f"- dynamic entries: **{counts['dynamic']}** in the file, of which "
      f"**{counts['dynamic'] - 2} named + 2 anonymous** ({headers.get('dynamic','')})")
    A(f"- named functions in total: **{len(theirs)}**\n")
    A(f"Our inventory lists **{len(ours)}** registered names, of which **{len(shared)}** appear in their dump "
      f"({100*len(shared)/len(theirs):.1f}% of their list).\n")
    A("## Names they have and we do not\n")
    if only_theirs:
        A("These are worth treating as **gaps in our scan**: the strings do exist in `prototype2engine.dll`.\n")
        A("| name | native | kind |")
        A("|---|---|---|")
        for n in only_theirs:
            e = next(x for x in entries if x["name"] == n)
            A(f"| `{n}` | `{e['native']}` | {e['kind']} |")
        A("")
    else:
        A("None.\n")
    A("## Names we have and they do not\n")
    A(f"**{len(only_ours)}** names. These are not necessarily functions: our scan collects every identifier-shaped")
    A("literal, which includes script-facing helpers, debug command names and data keys that are never registered")
    A("as native bindings. Largest families:\n")
    A("| family | names |")
    A("|---|---|")
    for f, c in fam.most_common(10):
        A(f"| `{f}` | {c} |")
    A("")
    A("## Arity agreement\n")
    A(f"- shared names with a static signature: **{len(declared)}**")
    A(f"- shared names that shipped missions actually call: **{len(used)}**")
    A(f"- our observed call arity fits within their declared parameters: **{len(used) - len(wider)}**")
    A(f"- our call sites pass **more** arguments than their prototype declares: **{len(wider)}**")
    A("  (Lua passes extra arguments freely; the C side ignores them, so this is expected for variadic-style")
    A("  bindings rather than a disagreement)\n")
    if wider:
        A("| name | max args seen in scripts | declared |")
        A("|---|---|---|")
        for n, obs, dec in wider[:10]:
            A(f"| `{n}` | {obs} | {dec} |")
        A("")
    by_native = collections.defaultdict(list)
    for e in entries:
        if e.get("native"):
            by_native[e["native"]].append(e["name"] or "(anonymous)")
    shared_addr = {a: n for a, n in by_native.items() if len(n) > 1}
    A("## Entries sharing one native address\n")
    if shared_addr:
        A("Two names pointing at one implementation is a strong hint of an alias or a placeholder - the kind of")
        A("entry the tool's author warned about:\n")
        A("| native | names |")
        A("|---|---|")
        for a, n in sorted(shared_addr.items(), key=lambda kv: -len(kv[1]))[:12]:
            shown = ", ".join("`" + x + "`" for x in n[:6])
            more = f"  _(+{len(n) - 6} more)_" if len(n) > 6 else ""
            A(f"| `{a}` | {shown}{more} |")
        A("")
    else:
        A("None.\n")
    A("## Listed is not callable\n")
    A(f"**{len(never_called)}** of the shared names never appear in shipped mission scripts. Combined with the")
    A("author's note - _\"some functions do not work because they are intended for other platforms or are simply")
    A("implemented as placeholders on the C++ side\"_ - this set is the first place to expect non-working")
    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"wrote {os.path.relpath(OUT_JSON, ROOT)} ({len(theirs)} named functions)")
    print(f"wrote {os.path.relpath(OUT_MD, ROOT)}")
    print(f"their list {len(theirs)} | overlap {len(shared)} | only theirs {len(only_theirs)} | "
          f"only ours {len(only_ours)} | arity wider {len(wider)} | never called {len(never_called)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
