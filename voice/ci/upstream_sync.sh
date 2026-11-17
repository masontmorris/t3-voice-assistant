#!/usr/bin/env bash
# Pulls upstream T3 into the fork: fast-forwards the mirror, merges it into voice/main, runs our checks, then pushes.
# Stops at the first problem and never force-pushes anything. Runs non-interactively, so a credential prompt is an error.
#
#   bash voice/ci/upstream_sync.sh --dry-run     do everything except the push
#   bash voice/ci/upstream_sync.sh              the real run
#
# Environment: VOICE_SYNC_SOURCE_REPO (default ~/source/repos/t3-voice), VOICE_SYNC_WORKTREE (default
# ~/source/repos/t3-voice-sync, created if missing), VOICE_SYNC_VOICE_BRANCH (default voice/main),
# VOICE_HOME (default ~/voice), VOICE_SYNC_LOG, VOICE_PYTHON. It runs only in the linked worktree it
# created itself; any other path is refused before a single file changes. The voice-sync-worktree file it
# writes into that worktree's git dir is the proof of ownership: a checkout without it is refused too.
set -uo pipefail

# A scheduled run has no terminal to answer a credential prompt, so git must fail instead of waiting.
export GIT_TERMINAL_PROMPT=0
export GCM_INTERACTIVE=never

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
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
  release_lock
  exit "$code"
}

mkdir -p "$log_dir" 2>/dev/null || {
  echo "upstream_sync.sh: cannot create $log_dir" >&2
  exit $exit_setup
}
lock_path=$log_dir/run.lock
lock_pid_file=$lock_path/pid
lock_started_file=$lock_path/started
lock_stale_seconds=10800

release_lock() {
  [ -n "$lock_dir" ] || return 0
  rm -f "$lock_pid_file" "$lock_started_file" 2>/dev/null
  rmdir "$lock_path" 2>/dev/null
}

lock_age_seconds() {
  started=$(cat "$lock_started_file" 2>/dev/null)
  case "$started" in
    ''|*[!0-9]*) started=$(stat -c %Y "$lock_path" 2>/dev/null) ;;
  esac
  case "$started" in
    ''|*[!0-9]*) printf '%s\n' "-"; return ;;
  esac
  printf '%s\n' $(( $(date +%s) - started ))
}

pid_alive() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
  esac
  # The lock holds the pid of a bash process, not a Windows process: on Git Bash that pid is an MSYS pid,
  # which tasklist cannot see, so kill -0 answers first and answers for the ids this script writes.
  if kill -0 "$1" 2>/dev/null; then
    return 0
  fi
  if command -v tasklist >/dev/null 2>&1; then
    MSYS_NO_PATHCONV=1 tasklist /FI "PID eq $1" /NH 2>/dev/null | grep -q '[0-9]'
    return
  fi
  ps -p "$1" >/dev/null 2>&1
}

take_lock() {
  mkdir "$lock_path" 2>/dev/null || return 1
  lock_dir=$lock_path
  printf '%s\n' "$$" >"$lock_pid_file"
  printf '%s\n' "$(date +%s)" >"$lock_started_file"
  return 0
}

# Only the run whose rename succeeds holds the stale lock, so a takeover cannot delete a lock another run just took.
take_over_stale_lock() {
  local moved
  moved=$lock_path.stale.$$.$(date +%s%N)
  mv "$lock_path" "$moved" 2>/dev/null || return 1
  rm -rf "$moved"
  take_lock
}

trap release_lock EXIT
if ! take_lock; then
  lock_pid=$(cat "$lock_pid_file" 2>/dev/null)
  lock_age=$(lock_age_seconds)
  if [ "$lock_age" != "-" ] && [ "$lock_age" -ge "$lock_stale_seconds" ]; then
    printf 'upstream_sync.sh: taking over a stale lock: pid %s, started %s s ago\n' \
      "${lock_pid:-unknown}" "$lock_age" >&2
    take_over_stale_lock || true
  elif [ -n "$lock_pid" ] && ! pid_alive "$lock_pid"; then
    printf 'upstream_sync.sh: taking over a lock whose pid %s is gone, started %s s ago\n' \
      "$lock_pid" "$lock_age" >&2
    take_over_stale_lock || true
  fi
  if [ -z "$lock_dir" ]; then
    lock_pid=$(cat "$lock_pid_file" 2>/dev/null)
    lock_age=$(lock_age_seconds)
    printf 'upstream_sync.sh: another sync holds the lock: pid %s, started %s s ago\n' \
      "${lock_pid:-unknown}" "$lock_age" >&2
    finish "another sync is already running" $exit_locked
  fi
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

real_path() {
  "$python_cmd" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null
}

# Windows names compare case-insensitively and accept either separator, so two spellings can name one checkout.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) fold_case=1 ;;
  *) fold_case=0 ;;
esac

path_key() {
  if [ "$fold_case" = 1 ]; then
    printf '%s' "${1//\\//}" | tr 'A-Z' 'a-z'
  else
    printf '%s' "${1//\\//}"
  fi
}

# The checkouts here are reset and cleaned, so refuse a target that is or sits inside the source repo's work
# tree before creating anything on disk.
source_real=$(real_path "$source_repo")
if [ -z "$source_real" ]; then
  finish "cannot resolve the real path of $source_repo" $exit_setup
fi
source_root=$(git -C "$source_repo" rev-parse --show-toplevel 2>/dev/null)
source_root_real=$(real_path "${source_root:-$source_repo}")
if [ -z "$source_root_real" ]; then
  finish "cannot resolve the work tree of $source_repo" $exit_setup
fi
source_key=$(path_key "$source_real")
source_root_key=$(path_key "$source_root_real")

worktree_real=$(real_path "$worktree")
if [ -z "$worktree_real" ]; then
  finish "cannot resolve the real path of $worktree" $exit_setup
fi
worktree_key=$(path_key "$worktree_real")
if [ "$worktree_key" = "$source_key" ] || [ "$worktree_key" = "$source_root_key" ]; then
  finish "refusing to run in the source repo itself, give the sync its own worktree" $exit_setup
fi
case "$worktree_key/" in
  "$source_root_key"/*)
    finish "refusing $worktree: it is inside the source repo's work tree at $source_root_real, give the sync its own worktree" $exit_setup
    ;;
esac

if ! git -C "$worktree" rev-parse --git-dir >/dev/null 2>&1; then
  mkdir -p "$(dirname "$worktree")" &&
    git -C "$source_repo" worktree prune &&
    git -C "$source_repo" worktree add --detach "$worktree" HEAD >/dev/null 2>&1 ||
    finish "cannot create the sync worktree at $worktree" $exit_setup
  created_git_dir=$(git -C "$worktree" rev-parse --path-format=absolute --git-dir 2>/dev/null)
  printf 'created by voice/ci/upstream_sync.sh\n' >"$created_git_dir/voice-sync-worktree" ||
    finish "cannot mark the new worktree at $worktree" $exit_setup
  worktree_real=$(real_path "$worktree")
  worktree_key=$(path_key "$worktree_real")
fi

# The checkouts here are reset and cleaned, so prove the target is the worktree this job made: same real path as
# the argument, a linked worktree rather than the main checkout, and carrying the voice-sync-worktree marker
# written above, which is the ownership proof for a checkout this job did not create in this run.
toplevel=$(git -C "$worktree" rev-parse --show-toplevel 2>/dev/null)
if [ -z "$toplevel" ]; then
  finish "refusing $worktree: git cannot name its top level, so it is not a checkout this job may reset" $exit_setup
fi
toplevel_real=$(real_path "$toplevel")
if [ -z "$toplevel_real" ] || [ "$(path_key "$toplevel_real")" != "$worktree_key" ]; then
  finish "refusing $worktree: git reports its top level as ${toplevel_real:-nothing}, so it is a subfolder or another checkout" $exit_setup
fi
git_dir=$(git -C "$worktree" rev-parse --path-format=absolute --git-dir 2>/dev/null)
common_dir=$(git -C "$worktree" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
git_dir_real=$(real_path "$git_dir")
common_dir_real=$(real_path "$common_dir")
if [ -z "$git_dir_real" ] || [ -z "$common_dir_real" ]; then
  finish "cannot read the git dir of $worktree" $exit_setup
fi
if [ "$git_dir_real" = "$common_dir_real" ]; then
  finish "refusing $worktree: it is the primary checkout, not a linked worktree" $exit_setup
fi
if [ ! -f "$git_dir_real/voice-sync-worktree" ]; then
  finish "refusing $worktree: it has no $git_dir_real/voice-sync-worktree marker, so this job did not create it" $exit_setup
fi
if [ -n "$(git -C "$worktree" status --porcelain --untracked-files=no)" ]; then
  finish "refusing to reset $worktree, it has changes to tracked files; reset it by hand first" $exit_setup
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

resolve_sha() {
  git -C "$worktree" rev-parse --verify --quiet "$1" 2>/dev/null
}

upstream_sha=$(resolve_sha upstream/main)
origin_main_sha=$(resolve_sha origin/main)
voice_sha=$(resolve_sha "origin/$voice_branch")
if [ -z "$upstream_sha" ] || [ -z "$origin_main_sha" ] || [ -z "$voice_sha" ]; then
  finish "cannot resolve upstream/main, origin/main, or origin/$voice_branch" $exit_setup
fi
upstream_short=$(git -C "$worktree" rev-parse --short=10 "$upstream_sha")
origin_main_short=$(git -C "$worktree" rev-parse --short=10 "$origin_main_sha")
voice_short=$(git -C "$worktree" rev-parse --short=10 "$voice_sha")

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
  printf '%s\n' "$merge_output"
  if [ -n "$conflicts" ]; then
    merge_state="CONFLICT, aborted: $conflicts"
    finish "merge conflict between upstream and $voice_branch" $exit_conflict
  fi
  merge_state="MERGE FAILED, aborted; the git output is above"
  finish "merge failed between upstream and $voice_branch" $exit_conflict
fi
after=$(git -C "$worktree" rev-parse HEAD)
after_short=$(git -C "$worktree" rev-parse --short=10 "$after")
if [ "$before" = "$after" ]; then
  merged_count=0
  merge_state="nothing to merge, $voice_branch already contains $upstream_short"
else
  merged_count=$ahead
  merge_state="merged $ahead upstream commit(s) into $voice_branch, HEAD $after_short"
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
  push_state="skipped, dry run: would push $upstream_short to origin/main and $after_short to origin/$voice_branch atomically"
  finish "dry run passed, nothing pushed" $exit_ok
fi

# Push the commits that were tested, not the refs they were found at, and update both branches in one atomic push
# so a rejected voice branch never leaves origin/main ahead of it.
if ! push_output=$(git -C "$worktree" push --atomic origin \
  "$upstream_sha:refs/heads/main" "$after:refs/heads/$voice_branch" 2>&1); then
  push_state="FAILED: nothing pushed; $(printf '%s' "$push_output" | tr '\n' ' ')"
  finish "push failed" $exit_push
fi
push_state="pushed main at $upstream_short and $voice_branch at $after_short"
finish "upstream merged and pushed" $exit_ok
