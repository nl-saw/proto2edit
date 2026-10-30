#!/usr/bin/env python3
"""Replace one entry's payload inside a Cement archive, writing a new archive.

Rebuilding is safer than in-place editing: the index is sorted by hash and every
payload is 2048-aligned, so any size change shifts everything after it. This
tool reads every entry, substitutes the named one, and re-emits the archive with
`rcfpack.build`, then optionally re-reads it to prove the substitution.

The source archive is never modified.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rcf import Rcf  # noqa: E402
from rcfpack import build  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replace one entry in a .rcf archive")
    ap.add_argument("archive", help="source archive (read only)")
    ap.add_argument("entry", help="entry name, backslash or forward slash separated")
    ap.add_argument("payload", help="file holding the replacement payload")
    ap.add_argument("output", help="archive to write")
    ap.add_argument("--check", action="store_true", help="re-read and verify every payload")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    with open(a.payload, "rb") as fh:
        replacement = fh.read()

    with Rcf(a.archive) as arc:
        names = [e.name for e in arc.entries]
        wanted = a.entry.replace("/", "\\")
        if wanted not in names:
            lowered = {n.lower(): n for n in names}
            if wanted.lower() not in lowered:
                print(f"error: {a.entry!r} not in archive", file=sys.stderr)
                return 1
            wanted = lowered[wanted.lower()]
        entries = []
        old_size = None
        for e in arc.entries:
            data = arc.read(e)
            if e.name == wanted:
                old_size = len(data)
                data = replacement
            entries.append((e.name, data))

    if not a.quiet:
        print(f"replacing {wanted}: {old_size} -> {len(replacement)} bytes")

    blob = build(entries)
    with open(a.output, "wb") as fh:
        fh.write(blob)
    if not a.quiet:
        print(f"wrote {a.output}: {len(entries)} files, {len(blob)} bytes")

    if a.check:
        with Rcf(a.output) as check:
            assert check.count == len(entries), (check.count, len(entries))
            assert not check.unnamed_entries, check.unnamed_entries[:3]
            for name, payload in entries:
                got = check.read(check.find(name))
                assert got == payload, f"payload mismatch for {name}"
            got = check.read(check.find(wanted))
            assert got == replacement, "replacement was not stored"
        if not a.quiet:
            print("verify: ok (all payloads round-trip, replacement stored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
