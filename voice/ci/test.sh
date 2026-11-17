#!/usr/bin/env bash
# Runs every voice test suite: the Python tests under voice/ and the bot's Node tests.
# Missing optional audio dependencies are reported as skips with their reason. Fails only on a failure that
# voice/ci/known_failures.txt does not list.
set -uo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if ! repo=$(git -C "$here" rev-parse --show-toplevel 2>/dev/null); then
  echo "test.sh: $here is not inside a git worktree" >&2
  exit 2
fi

python=${VOICE_PYTHON:-$(command -v python || command -v python3 || true)}
if [ -z "$python" ]; then
  echo "test.sh: no python on PATH, set VOICE_PYTHON to an interpreter" >&2
  exit 2
fi

export PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}"
exec "$python" "$repo/voice/ci/voice_tests.py" --repo "$repo" "$@"
