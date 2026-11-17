#!/usr/bin/env bash
# Pulls upstream T3 into the fork: fast-forwards the mirror, merges it into voice/main, runs our checks, then pushes.
# Stops at the first problem and never force-pushes anything.
#
#   bash voice/ci/upstream_sync.sh --dry-run     do everything except the two pushes
#   bash voice/ci/upstream_sync.sh              the real run
#
# Environment: VOICE_SYNC_SOURCE_REPO (default ~/source/repos/t3-voice), VOICE_SYNC_WORKTREE (default
# ~/source/repos/t3-voice-sync, created if missing), VOICE_SYNC_VOICE_BRANCH (default voice/main),
# VOICE_HOME (default ~/voice), VOICE_SYNC_LOG, VOICE_PYTHON. The job runs only in its own worktree,
# never in the source repo or an integration worktree.
set -uo pipefail

source_repo=${VOICE_SYNC_SOURCE_REPO:-$HOME/source/repos/t3-voice}
worktree=${VOICE_SYNC_WORKTREE:-$HOME/source/repos/t3-voice-sync}
voice_home=${VOICE_HOME:-$HOME/voice}
voice_branch=${VOICE_SYNC_VOICE_BRANCH:-voice/main}
log_file=${VOICE_SYNC_LOG:-$voice_home/sync/sync.log}
log_dir=$(dirname "$log_file")
dry_run=0

exit_ok=0
exit_usage=2
exit_fetch=10
exit_not_mirror=11
exit_conflict=12
exit_tests=13
exit_seam=14
exit_push=15
exit_setup=16
exit_locked=17

fetch_state="not started"
main_state="not started"
merge_state="not started"
test_state="not started"
seam_state="not started"
push_state="not started"
upstream_short="-"
origin_main_short="-"
voice_short="-"
merged_count="-"
lock_dir=""

usage() {
  sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --repo) source_repo=${2:?--repo needs a path}; shift ;;
    --worktree) worktree=${2:?--worktree needs a path}; shift ;;
    --log) log_file=${2:?--log needs a path}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "upstream_sync.sh: unknown argument $1" >&2; exit $exit_usage ;;
  esac
  shift
done
log_dir=$(dirname "$log_file")

finish() {
  result=$1
  code=$2
  mkdir -p "$log_dir" 2>/dev/null &&
    printf '%s\tdry_run=%s\tresult=%s\tcode=%s\tupstream=%s\torigin_main=%s\tvoice_main=%s\tmerged=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$dry_run" "$result" "$code" \
      "$upstream_short" "$origin_main_short" "$voice_short" "$merged_count" >>"$log_file"

  printf '\n=== voice upstream sync ===\n'
  printf 'result:      %s (exit %s%s)\n' "$result" "$code" "$([ "$dry_run" = 1 ] && printf ', dry run')"
  printf 'worktree:    %s\n' "$worktree"
  printf 'fetch:       %s\n' "$fetch_state"
  printf 'mirror main: %s\n' "$main_state"
  printf 'merge:       %s\n' "$merge_state"
  printf 'tests:       %s\n' "$test_state"
  printf 'seam check:  %s\n' "$seam_state"
  printf 'push:        %s\n' "$push_state"
  printf 'log:         %s\n' "$log_file"
  [ -n "$lock_dir" ] && rmdir "$lock_dir" 2>/dev/null
  exit "$code"
}

mkdir -p "$log_dir" 2>/dev/null || {
  echo "upstream_sync.sh: cannot create $log_dir" >&2
  exit $exit_setup
}
lock_path=$log_dir/run.lock
if mkdir "$lock_path" 2>/dev/null; then
  lock_dir=$lock_path
  trap 'rmdir "$lock_dir" 2>/dev/null' EXIT
else
  finish "another sync is already running" $exit_locked
fi

python_cmd=${VOICE_PYTHON:-$(command -v python || command -v python3 || true)}

if ! git -C "$source_repo" rev-parse --git-dir >/dev/null 2>&1; then
  finish "no git repo at $source_repo, set VOICE_SYNC_SOURCE_REPO" $exit_setup
fi
for remote in origin upstream; do
  if ! git -C "$source_repo" config --get "remote.$remote.url" >/dev/null; then
    finish "the repo at $source_repo has no $remote remote" $exit_setup
  fi
done
if [ -z "$python_cmd" ]; then
  finish "no python on PATH, set VOICE_PYTHON" $exit_setup
fi

if ! git -C "$worktree" rev-parse --git-dir >/dev/null 2>&1; then
  mkdir -p "$(dirname "$worktree")" &&
    git -C "$source_repo" worktree prune &&
    git -C "$source_repo" worktree add --detach "$worktree" HEAD >/dev/null 2>&1 ||
    finish "cannot create the sync worktree at $worktree" $exit_setup
fi
if [ "$(cd "$worktree" && pwd)" = "$(cd "$source_repo" && pwd)" ]; then
  finish "refusing to run in the source repo itself, give the sync its own worktree" $exit_setup
fi
if [ -n "$(git -C "$worktree" status --porcelain --untracked-files=no)" ]; then
  finish "refusing to reset $worktree, it has changes to tracked files" $exit_setup
fi

if ! git -C "$worktree" fetch --no-tags upstream main >/dev/null 2>&1; then
  fetch_state="FAILED: git fetch upstream main"
  finish "fetch failed" $exit_fetch
fi
if ! git -C "$worktree" fetch --no-tags origin main "$voice_branch" >/dev/null 2>&1; then
  fetch_state="FAILED: git fetch origin main $voice_branch"
  finish "fetch failed" $exit_fetch
fi
fetch_state="fetched upstream/main, origin/main, origin/$voice_branch"

upstream_sha=$(git -C "$worktree" rev-parse upstream/main)
origin_main_sha=$(git -C "$worktree" rev-parse origin/main)
voice_sha=$(git -C "$worktree" rev-parse "origin/$voice_branch")
upstream_short=$(git -C "$worktree" rev-parse --short=10 upstream/main)
origin_main_short=$(git -C "$worktree" rev-parse --short=10 origin/main)
voice_short=$(git -C "$worktree" rev-parse --short=10 "origin/$voice_branch")

if [ "$upstream_sha" = "$origin_main_sha" ] && git -C "$worktree" merge-base --is-ancestor "$upstream_sha" "$voice_sha"; then
  main_state="already a mirror of upstream ($origin_main_short)"
  merge_state="nothing new"
  test_state="skipped, nothing new"
  seam_state="skipped, nothing new"
  push_state="skipped, nothing new"
  finish "up to date" $exit_ok
fi

if ! git -C "$worktree" merge-base --is-ancestor "$origin_main_sha" "$upstream_sha"; then
  main_state="ALARM: origin/main ($origin_main_short) is not an ancestor of upstream/main ($upstream_short)"
  finish "main is not a pure mirror of upstream" $exit_not_mirror
fi
if [ "$upstream_sha" = "$origin_main_sha" ]; then
  main_state="already a mirror of upstream ($origin_main_short)"
else
  main_state="fast-forward $origin_main_short -> $upstream_short"
fi

git -C "$worktree" merge --abort >/dev/null 2>&1
git -C "$worktree" checkout --detach --force "$voice_sha" >/dev/null 2>&1 &&
  git -C "$worktree" reset --hard --quiet "$voice_sha" &&
  git -C "$worktree" clean -fdq ||
  finish "cannot check out origin/$voice_branch in $worktree" $exit_setup

ahead=$(git -C "$worktree" rev-list --count "$origin_main_sha..$upstream_sha")
before=$(git -C "$worktree" rev-parse HEAD)
if [ ! -f "$worktree/voice/ci/test.sh" ] || [ ! -f "$worktree/voice/ci/check_seam.py" ]; then
  finish "the branch has no voice/ci/test.sh or no voice/ci/check_seam.py" $exit_setup
fi
if ! merge_output=$(git -C "$worktree" merge --no-ff --no-edit \
  -m "Merge upstream T3 main ($upstream_short) into $voice_branch" "$upstream_sha" 2>&1); then
  conflicts=$(git -C "$worktree" diff --name-only --diff-filter=U | tr '\n' ' ')
  git -C "$worktree" merge --abort >/dev/null 2>&1
  merge_state="CONFLICT, aborted: ${conflicts:-unknown paths}"
  finish "merge conflict between upstream and $voice_branch" $exit_conflict
fi
after=$(git -C "$worktree" rev-parse HEAD)
if [ "$before" = "$after" ]; then
  merged_count=0
  merge_state="nothing to merge, $voice_branch already contains $upstream_short"
else
  merged_count=$ahead
  merge_state="merged $ahead upstream commit(s) into $voice_branch, HEAD $(git -C "$worktree" rev-parse --short=10 HEAD)"
fi

test_log=$log_dir/last-test-output.txt
if ! bash "$worktree/voice/ci/test.sh" >"$test_log" 2>&1; then
  test_state="FAILED, output in $test_log"
  grep -E '^(voice tests|  )' "$test_log" | tail -n 12
  finish "voice tests failed after the merge" $exit_tests
fi
test_state=$(grep -E '^voice tests OK' "$test_log" | tail -n 1)
[ -n "$test_state" ] || test_state="passed"

if ! seam_output=$("$python_cmd" "$worktree/voice/ci/check_seam.py" --repo "$worktree" \
  --base "$upstream_sha" --head HEAD 2>&1); then
  seam_state="FAILED, see the listed files below"
  printf '%s\n' "$seam_output"
  finish "the merge changes upstream files that the seam allowlist does not cover" $exit_seam
fi
seam_state=$(printf '%s' "$seam_output" | head -n 1)

if [ "$dry_run" = 1 ]; then
  push_state="skipped, dry run: would push upstream/main to origin/main and HEAD to origin/$voice_branch"
  finish "dry run passed, nothing pushed" $exit_ok
fi

if ! git -C "$worktree" push --quiet origin "refs/remotes/upstream/main:refs/heads/main" 2>&1; then
  push_state="FAILED: main not pushed; $voice_branch not touched"
  finish "push of main failed" $exit_push
fi
if ! git -C "$worktree" push --quiet origin "HEAD:refs/heads/$voice_branch" 2>&1; then
  push_state="FAILED: main pushed, $voice_branch push failed"
  finish "push of $voice_branch failed" $exit_push
fi
push_state="pushed main at $upstream_short and $voice_branch at $(git -C "$worktree" rev-parse --short=10 HEAD)"
finish "upstream merged and pushed" $exit_ok
