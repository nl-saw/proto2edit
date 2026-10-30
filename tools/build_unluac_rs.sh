#!/usr/bin/env bash
# Build unluac-rs, the Rust decompiler used as the second fallback source. Pinned commit; MIT-licensed
# (see the upstream Cargo.toml), built here and never vendored.
#
# CARGO_HOME is redirected under build/ because the host's cargo registry cache may be read-only.
#
#   tools/build_unluac_rs.sh
#   UNLUAC_RS_REV=<sha> tools/build_unluac_rs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REV="${UNLUAC_RS_REV:-3186d2e392e44a2ddaadd1826a779c98c718be53}"
OUT="$ROOT/build/unluac-rs"
SRC="$OUT/src"

command -v cargo >/dev/null || { echo "unluac-rs needs a Rust toolchain (cargo not found)" >&2; exit 1; }

mkdir -p "$OUT" "$OUT/cargo-home"
if [ ! -d "$SRC/.git" ]; then
    git clone -q https://github.com/x3zvawq/unluac-rs.git "$SRC"
fi
git -C "$SRC" fetch -q origin "$REV" || true
git -C "$SRC" checkout -q "$REV"

CARGO_HOME="$OUT/cargo-home" CARGO_TARGET_DIR="$OUT/target" \
    cargo build --release --manifest-path "$SRC/Cargo.toml" -p unluac-cli
install -m 0755 "$OUT/target/release/unluac-cli" "$OUT/unluac-cli"
printf '%s\n' "$REV" > "$OUT/REVISION"
echo "built unluac-rs $REV -> $OUT/unluac-cli"
