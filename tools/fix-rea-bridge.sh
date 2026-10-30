#!/usr/bin/env bash
# Repair the REA MCP Ghidra provider when the installed Java bridge does not
# match the running Node bundle.
#
# Symptom
#   Every native REA tool fails with:
#     Ghidra bridge handshake is invalid
#   and binary_session reports all Ghidra groups unavailable.
#
# Cause
#   REA validates the bridge's session handshake against a schema and a
#   capability list compiled into the Node bundle. A half-updated checkout can
#   pair a 6.4.0 bundle (dist/) with a 6.5.0 bridge (bridge/ghidra/*.java), and
#   then two independent checks fail:
#     1. the 6.5.0 bridge emits a "pdb" key, which the 6.4.0 schema rejects
#        because it is .strict();
#     2. the 6.5.0 bridge advertises 32 capabilities, the 6.4.0 bundle expects
#        exactly 30 (it adds search_bytes and read_binary_inventory_summary).
#
# Fix
#   Install the bridge that matches the bundle. This script backs up the
#   existing bridge first and refuses to run if it cannot find a matching one.
#
# Usage
#   tools/fix-rea-bridge.sh <rea-root> <known-good-package-root> [backup-dir]
#
# Example (this host):
#   tools/fix-rea-bridge.sh \
#     <rea-root> \
#     <rea-agents-package> \
#     <backup-dir>

set -euo pipefail

REA_ROOT="${1:?usage: fix-rea-bridge.sh <rea-root> <known-good-package-root> [backup-dir]}"
GOOD_ROOT="${2:?usage: fix-rea-bridge.sh <rea-root> <known-good-package-root> [backup-dir]}"
BACKUP_DIR="${3:-bridge-backup}"

BRIDGE="bridge/ghidra/ReaGhidraBridge.java"

capabilities() {
  # Count the entries of the bridge's CAPABILITIES = { ... } array.
  python3 - "$1" <<'PY'
import re, sys
src = open(sys.argv[1], encoding="utf-8", errors="replace").read()
m = re.search(r"CAPABILITIES\s*=\s*\{(.*?)\};", src, re.S)
print(len(re.findall(r'"([a-z_0-9]+)"', m.group(1))) if m else 0)
PY
}

emits_pdb() {
  grep -q 'result.add("pdb"' "$1" && echo yes || echo no
}

installed_version() {
  python3 - "$1" <<'PY'
import json, os, sys
root = sys.argv[1]
try:
    print(json.load(open(os.path.join(root, "package.json")))["version"])
except Exception:
    print("unknown")
PY
}

echo "REA root        : $REA_ROOT"
echo "package version : $(installed_version "$REA_ROOT")"
echo "known-good root : $GOOD_ROOT ($(installed_version "$GOOD_ROOT"))"

[ -f "$REA_ROOT/$BRIDGE" ] || { echo "error: $REA_ROOT/$BRIDGE not found" >&2; exit 1; }
[ -f "$GOOD_ROOT/$BRIDGE" ] || { echo "error: $GOOD_ROOT/$BRIDGE not found" >&2; exit 1; }

echo
echo "current bridge  : $(capabilities "$REA_ROOT/$BRIDGE") capabilities, emits pdb: $(emits_pdb "$REA_ROOT/$BRIDGE")"
echo "replacement     : $(capabilities "$GOOD_ROOT/$BRIDGE") capabilities, emits pdb: $(emits_pdb "$GOOD_ROOT/$BRIDGE")"

# The bundle's expectation is authoritative; read it out of dist when present.
DIST_BUNDLE=$(ls "$REA_ROOT"/dist/mcp/main-*.js 2>/dev/null | head -1 || true)
if [ -n "$DIST_BUNDLE" ] && grep -q '"REA Ghidra bridge"' "$DIST_BUNDLE"; then
  if grep -q 'pdb: z' "$DIST_BUNDLE"; then
    echo "bundle expects  : pdb key present"
  else
    echo "bundle expects  : no pdb key (strict schema)"
  fi
fi

if [ "$(sha256sum "$REA_ROOT/$BRIDGE" | cut -d' ' -f1)" = "$(sha256sum "$GOOD_ROOT/$BRIDGE" | cut -d' ' -f1)" ]; then
  echo
  echo "already matches; nothing to do."
  exit 0
fi

# Fail before touching anything if the install is not writable.
if ! ( : > "$REA_ROOT/.rea-write-probe" ) 2>/dev/null; then
  cat >&2 <<'EOF'

error: $REA_ROOT is not writable by this process.

The repair must replace a file inside the REA installation. Re-run with write
access to that directory (for a coding agent, request escalated file access for
this exact command; for a human, use sudo or fix ownership).
EOF
  exit 2
fi
rm -f "$REA_ROOT/.rea-write-probe"

mkdir -p "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$BACKUP_DIR/ReaGhidraBridge.java.$STAMP"
cp "$REA_ROOT/$BRIDGE" "$BACKUP"
echo
echo "backed up       : $BACKUP ($(sha256sum "$BACKUP" | cut -d' ' -f1))"

cp "$GOOD_ROOT/$BRIDGE" "$REA_ROOT/$BRIDGE"
echo "installed       : $(sha256sum "$REA_ROOT/$BRIDGE" | cut -d' ' -f1)"

echo
echo "done. REA spawns Ghidra per operation, so the next tool call uses the new"
echo "bridge; no server restart is required."
echo "verify with: rea binary_overview on any native target"
