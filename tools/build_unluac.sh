#!/usr/bin/env bash
# Build the second decompiler (unluac) used as a fallback source when LuaDec's reconstruction does not reproduce the
# bytecode. Pinned commit, so a rebuild is reproducible; needs a JDK and network the first time.
# unluac is MIT-licensed (Copyright (c) 2011-2015 tehtmi); it is built here, never vendored into the tree.
#
#   tools/build_unluac.sh            # build into build/unluac/classes
#   UNLUAC_REV=<sha> tools/build_unluac.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REV="${UNLUAC_REV:-1b2bfed6045eb719a27f026311744d7a674847f5}"
OUT="$ROOT/build/unluac"
SRC="$OUT/src"

command -v java >/dev/null || { echo "unluac needs a JDK (java not found)" >&2; exit 1; }
command -v javac >/dev/null || { echo "unluac needs a JDK (javac not found)" >&2; exit 1; }

mkdir -p "$OUT"
if [ ! -d "$SRC/.git" ]; then
    git clone -q https://github.com/HansWessels/unluac.git "$SRC"
fi
git -C "$SRC" fetch -q origin "$REV" || true
git -C "$SRC" checkout -q "$REV"
rm -rf "$OUT/classes"
mkdir -p "$OUT/classes"
javac -nowarn -d "$OUT/classes" $(find "$SRC/src" -name '*.java')
printf '%s\n' "$REV" > "$OUT/REVISION"
echo "built unluac $REV -> $OUT/classes"
echo "test: java -cp $OUT/classes unluac.Main --version"
