#!/usr/bin/env bash
# Publish local commits to the public remote on top of a single root commit.
#
# The public branch has ONE parentless commit as its root (the tree as published first). Every later publish replays
# only the master commits that are new since the last publish, so online history = root + commits made from then on.
# Local `master` is never modified: replaying uses plumbing (commit-tree) and no worktree checkout.
#
#   ./proto2 publish              # replay new master commits onto public, push
#   ./proto2 publish --reset      # replace the public branch with a NEW single snapshot commit (erases history)
#   ./proto2 publish --status     # show what would be published
#
# Local refs used:
#   refs/heads/public               the published line (pushed to origin/master)
#   refs/proto2/published-master    last master commit already replayed (not pushed)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REMOTE="origin"; BRANCH="master"; MODE="publish"
for arg in "$@"; do
  case "$arg" in
    --reset) MODE="reset" ;;
    --status) MODE="status" ;;
    --*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) if [ "$REMOTE" = "origin" ]; then REMOTE="$arg"; else BRANCH="$arg"; fi ;;
  esac
done

git diff --quiet && git diff --cached --quiet || {
  echo "error: working tree has uncommitted changes; commit or stash first" >&2; exit 1; }

MASTER="$(git rev-parse master)"
MARKER="refs/proto2/published-master"
PY="${PYTHON:-python3}"

# Leak gate. Publishing is the moment game data, capture files or host paths would become public, and the manual
# audit that caught them once was luck with a grep. `--reset` publishes a tree, the incremental path publishes a
# range; both are checked before anything is written or pushed.
check_leaks() {
  if [ -f "$ROOT/tools/leakcheck.py" ]; then
    if ! "$PY" "$ROOT/tools/leakcheck.py" "$@"; then
      echo "publish refused: the leak check failed (see above)" >&2
      exit 1
    fi
  else
    echo "warning: tools/leakcheck.py missing; publishing without the leak gate" >&2
  fi
}

# parentless root commit from a tree, with a readable message
#
# PUBLISH_MESSAGE_FILE overrides the default text, so a squashed snapshot can say what it contains instead of
# repeating the first-release blurb. The file is read as-is (one trailing newline is added).
make_root() {
  local tree="$1"
  if [ -n "${PUBLISH_MESSAGE_FILE:-}" ]; then
    if [ ! -f "$PUBLISH_MESSAGE_FILE" ]; then
      echo "error: PUBLISH_MESSAGE_FILE=$PUBLISH_MESSAGE_FILE does not exist" >&2
      return 1
    fi
    git commit-tree "$tree" -F "$PUBLISH_MESSAGE_FILE"
    return
  fi
  printf '%s\n' "Initial public release

Prototype 2 mission editor and modding toolkit. This is the published root: later commits are work done after the
repository went public. See README.md for installation and docs/10 for the editing workflows." \
    | git commit-tree "$tree"
}

if [ "$MODE" = "status" ]; then
  echo "master:  $(git rev-parse --short master)"
  if git rev-parse --verify -q refs/heads/public >/dev/null; then
    echo "public:  $(git rev-parse --short public)  ($(git rev-list --count public) commit(s))"
    base="$(git rev-parse -q --verify "$MARKER" || true)"
    if [ -n "$base" ]; then
      echo "new since last publish: $(git rev-list --count "$base".."$MASTER")"
      git log --oneline "$base".."$MASTER"
    else
      echo "no publish marker; next publish will initialise the public root"
    fi
  else
    echo "public:  not created yet; next publish creates the single root commit"
  fi
  exit 0
fi

# initialise, or deliberately reset, the public line to one snapshot commit
if [ "$MODE" = "reset" ] || ! git rev-parse --verify -q refs/heads/public >/dev/null; then
  check_leaks --tree "$MASTER"
  ROOT_COMMIT="$(make_root "$(git rev-parse "$MASTER^{tree}")")"
  git update-ref refs/heads/public "$ROOT_COMMIT"
  git update-ref "$MARKER" "$MASTER"
  echo "public root: $(git rev-parse --short "$ROOT_COMMIT")  (tree of master $(git rev-parse --short "$MASTER"))"
  git push --force "$REMOTE" "$ROOT_COMMIT:refs/heads/$BRANCH"
  echo "done: $REMOTE/$BRANCH now has exactly one commit"
  exit 0
fi

# incremental: replay the new master commits onto the public line, without touching the worktree
BASE="$(git rev-parse -q --verify "$MARKER" || true)"
if [ -z "$BASE" ]; then
  echo "error: public exists but no publish marker; run ./proto2 publish --reset to re-root it" >&2
  exit 1
fi
git merge-base --is-ancestor "$BASE" "$MASTER" || {
  echo "error: the publish marker is not an ancestor of master (history changed underneath); use --reset" >&2
  exit 1; }

check_leaks --range "$BASE..$MASTER"

NEW="$(git rev-list --reverse "$BASE".."$MASTER")"
if [ -z "$NEW" ]; then
  echo "nothing new since $(git rev-parse --short "$BASE"); pushing public as-is"
  git push "$REMOTE" "public:refs/heads/$BRANCH"
  exit 0
fi

HEAD_NEW="$(git rev-parse public)"
for c in $NEW; do
  tree="$(git rev-parse "$c^{tree}")"
  an="$(git log -1 --format=%an "$c")"; ae="$(git log -1 --format=%ae "$c")"
  ad="$(git log -1 --format=%aD "$c")"; msg="$(git log -1 --format=%B "$c")"
  HEAD_NEW="$(printf '%s\n' "$msg" | GIT_AUTHOR_NAME="$an" GIT_AUTHOR_EMAIL="$ae" GIT_AUTHOR_DATE="$ad" \
      git commit-tree "$tree" -p "$HEAD_NEW")"
  echo "  + $(git log -1 --format='%h %s' "$c")"
done
git update-ref refs/heads/public "$HEAD_NEW"
git update-ref "$MARKER" "$MASTER"
git push "$REMOTE" "public:refs/heads/$BRANCH"
echo "done: $REMOTE/$BRANCH = $(git rev-list --count public) commit(s) (root + work published after it)"
