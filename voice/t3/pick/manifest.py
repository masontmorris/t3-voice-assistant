"""Freeze the eval inputs once, then check every run against that freeze.

The manifest hashes the snapshots, the labels and the snapshot spec, and records the commit that
built them. `write_once` refuses to replace an existing manifest, and `verify` fails a run whose
inputs no longer match, so a label or snapshot cannot change after the first run.
"""

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SPEC = "snapshot-spec.json"
SNAPSHOTS = "snapshots/*.json"
LABELS = "labels/*.jsonl"
MANIFEST = "labels/frozen-manifest.json"


def _sha256(path):
	return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def collect(root):
	root = Path(root)
	files = {}
	for pattern in (SPEC, SNAPSHOTS, LABELS):
		for path in sorted(root.glob(pattern)):
			if path.name == "frozen-manifest.json":
				continue
			files[str(path.relative_to(root)).replace("\\", "/")] = _sha256(path)
	return files


def builder_commit(repo=None):
	repo = repo or Path(__file__).resolve().parents[3]
	out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
	                     capture_output=True, text=True, check=True)
	return out.stdout.strip()


def write_once(root, repo=None, commit=None):
	target = Path(root) / MANIFEST
	if target.exists():
		raise FileExistsError(f"{target} is already frozen; a run may not rewrite it")
	target.parent.mkdir(parents=True, exist_ok=True)
	body = {
		"writtenAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
		"builderCommit": commit or builder_commit(repo),
		"files": collect(root),
	}
	target.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
	return body


def verify(root):
	target = Path(root) / MANIFEST
	if not target.exists():
		raise FileNotFoundError(f"{target} is missing; freeze the inputs before a run")
	frozen = json.loads(target.read_text(encoding="utf-8"))
	current = collect(root)
	if current != frozen["files"]:
		changed = sorted(set(current) ^ set(frozen.get("files", {}))) or \
			sorted(k for k in current if current[k] != frozen["files"].get(k))
		raise ValueError(f"frozen inputs changed since {frozen['writtenAt']}: {changed}")
	return frozen


def main(argv):
	if len(argv) != 3 or argv[1] not in ("write", "verify"):
		raise SystemExit("usage: python -m voice.t3.pick.manifest <write|verify> <root>")
	action, root = argv[1], argv[2]
	print(json.dumps(write_once(root) if action == "write" else verify(root), indent=1))


if __name__ == "__main__":
	main(sys.argv)
