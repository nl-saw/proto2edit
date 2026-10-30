#!/usr/bin/env python3
"""Self-check: every mission in the corpus must round-trip byte-identically.

`fsm_write.verify()` parses a mission, rebuilds the containers it understands from their
parsed items, splices them back and compares. Running it over the whole corpus is the
strongest offline statement this project can make about the writer's byte mechanics:

    if the parse covers the file and the rebuild puts every byte back where it was,
    the writer is not corrupting anything it does not mean to touch.

It is NOT a statement that the engine accepts new shapes. Those are different claims, and
conflating them is what produced the crash this project is investigating.

Usage:
    python3 tools/selfcheck_corpus.py [--corpus DIR] [-v]
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_graph as F
import fsm_write as W


def corpus_root(explicit=None):
    if explicit:
        return explicit
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "tmp", "corpus")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    verbose = "-v" in argv or "--verbose" in argv
    root = None
    if "--corpus" in argv:
        root = argv[argv.index("--corpus") + 1]
    root = corpus_root(root)
    if not os.path.isdir(root):
        print(f"no corpus at {root}")
        return 2

    table = F.name_table()
    ok = differ = failed = 0
    failures = []
    for dirpath, _dirs, names in os.walk(root):
        if "fig.p3d.rz" not in names:
            continue
        path = os.path.join(dirpath, "fig.p3d.rz")
        rel = path.split(os.sep + "missions" + os.sep)[-1]
        try:
            # verify() prints one line per file; keep that only under -v
            sink = sys.stdout if verbose else io.StringIO()
            with contextlib.redirect_stdout(sink):
                result = W.verify(path, table)
        except Exception as exc:  # a raise is a failure, not a skip
            failed += 1
            failures.append((rel, f"raised {exc!r}"))
            continue
        if result is True:
            ok += 1
        else:
            differ += 1
            failures.append((rel, f"verify returned {result!r}"))

    total = ok + differ + failed
    print(f"missions checked : {total}")
    print(f"  byte-identical : {ok}")
    print(f"  differ         : {differ}")
    print(f"  failed         : {failed}")
    for rel, why in failures[:10]:
        print(f"  {rel}: {why}")
    if verbose and ok:
        print("all byte-identical")
    return 0 if (differ == 0 and failed == 0 and ok > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
