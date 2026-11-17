#!/usr/bin/env bash
# Runs every voice test suite: the Python tests under voice/ and the bot's Node tests.
# Prefers an existing venv under $VOICE_HOME that has soundfile and sounddevice, so the live-audio tests run
# instead of skipping; falls back to python on PATH. Skips must match voice/ci/known_skips.txt and failures
# must match voice/ci/known_failures.txt.
set -uo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if ! repo=$(git -C "$here" rev-parse --show-toplevel 2>/dev/null); then
  echo "test.sh: $here is not inside a git worktree" >&2
  exit 2
fi

voice_home=${VOICE_HOME:-$HOME/voice}

audio_python() {
  for candidate in "$voice_home"/venv-*/Scripts/python.exe "$voice_home"/venv-*/bin/python; do
    [ -f "$candidate" ] || continue
    if "$candidate" -c 'import soundfile, sounddevice' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

python=${VOICE_PYTHON:-$(audio_python || command -v python || command -v python3 || true)}
if [ -z "$python" ]; then
  echo "test.sh: no python on PATH, set VOICE_PYTHON to an interpreter" >&2
  exit 2
fi

export PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}"
exec "$python" "$repo/voice/ci/voice_tests.py" --repo "$repo" "$@"
