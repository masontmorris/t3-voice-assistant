"""One command to bring the fork's separate T3 environment up and down.

Mason's daily T3 install is never touched. The fork's server runs from this
checkout against a dedicated T3 home, so fork code never writes his database.
Run with ``python voice/t3/fork_env.py <up|down|status|token|pair>`` from the
repo root. Every path below hangs off VOICE_HOME, which defaults to
``~/voice``. Only the standard library is used.

The server runs as ``vp run dev`` with ``--home-dir``, because the free
``Replay (dev)`` provider only registers on a dev server. It needs a dev web
URL in its config, and only the dev runner sets one. A plain ``t3 serve``
would skip it.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

FORK_PORT = 3779
SERVER_READY_TIMEOUT_S = 240
SERVER_POLL_INTERVAL_S = 2

STATE_FILE = "fork-env.json"
PID_FILE = "fork-env.pid"
SEAM_CONFIG_FILE = "voice-seam.json"
TOKEN_FILE = "t3-seam-token"
PAIR_FILE = "pairing-url.txt"
LOG_FILE = "fork-env.log"

DEV_REPLAY_PROVIDER = "devReplay"
DEV_REPLAY_MODEL = "showcase"

TOKEN_SUBJECT = "voice"
TOKEN_SCOPE = "orchestration:read"
TOKEN_TTL = "30d"
TOKEN_LABEL = "Voice server (ceiling in voice-seam.json)"

SEAM_OPERATIONS = ["list", "read", "watch", "launch", "send", "interrupt"]

PROJECT_ADD_RE = re.compile(r"Added project (\S+)")
PAIR_URL_RE = re.compile(r"Pairing URL:\s*(\S+)")


def default_voice_home() -> Path:
    """Voice home from the environment, else Mason's voice directory."""
    override = os.environ.get("VOICE_HOME")
    if override:
        return Path(override)
    return Path.home() / "voice"


def repo_root() -> Path:
    """Checkout root that owns this file, whatever the caller cwd is."""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class EnvPaths:
    """Every file the environment owns, all inside the fork home."""

    home: Path
    sandbox: Path
    state_file: Path
    pid_file: Path
    seam_config: Path
    token_file: Path
    pair_file: Path
    log_file: Path

    @classmethod
    def from_home(cls, home: Path, sandbox: Path) -> EnvPaths:
        return cls(
            home=home,
            sandbox=sandbox,
            state_file=home / STATE_FILE,
            pid_file=home / PID_FILE,
            seam_config=home / SEAM_CONFIG_FILE,
            token_file=home / TOKEN_FILE,
            pair_file=home / PAIR_FILE,
            log_file=home / LOG_FILE,
        )


def read_pid(paths: EnvPaths) -> int | None:
    """Pid from the pid file, or None when it is missing or unparseable."""
    try:
        return int(paths.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """True when a process with this pid exists. Never raises.

    Signal 0 is invalid on Windows, so os.kill cannot probe there. The task
    list owns the answer on Windows, and signal 0 owns it elsewhere.
    """
    if os.name == "nt":
        return _tasklist_has_pid(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _tasklist_has_pid(pid: int) -> bool:
    """True when the Windows task list names this pid. Never raises."""
    try:
        done = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    try:
        rows = list(csv.reader(io.StringIO(done.stdout)))
    except csv.Error:
        return False
    return any(len(row) > 1 and row[1].strip() == str(pid) for row in rows)


def environment_up(paths: EnvPaths) -> int | None:
    """Pid of our server when it is up, else None. A stale file reads as down."""
    pid = read_pid(paths)
    if pid is None:
        return None
    return pid if pid_alive(pid) else None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    """True when something accepts TCP on this port. Never raises."""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def run_node_cli(repo: Path, *args: str, timeout_s: int = 120) -> subprocess.CompletedProcess[str]:
    """Run this checkout's T3 CLI. Always scoped to the fork, never the daily home."""
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("fork_env: no node on PATH, cannot run the T3 CLI")
    cmd = [node, str(repo / "apps" / "server" / "src" / "bin.ts"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=str(repo))


def ensure_sandbox_repo(sandbox: Path) -> bool:
    """Create the sandbox git repo with one commit. True when it created it."""
    if (sandbox / ".git").is_dir():
        done = subprocess.run(
            ["git", "-C", str(sandbox), "rev-parse", "HEAD"],
            capture_output=True,
        )
        if done.returncode == 0:
            return False
    sandbox.mkdir(parents=True, exist_ok=True)
    if not (sandbox / ".git").is_dir():
        subprocess.run(["git", "init"], cwd=str(sandbox), check=True, capture_output=True)
    readme = sandbox / "README.md"
    if not readme.exists():
        readme.write_text(
            "Voice sandbox. Voice-started threads run here overnight.\n",
            encoding="utf-8",
        )
    subprocess.run(["git", "add", "README.md"], cwd=str(sandbox), check=True, capture_output=True)
    made = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t3-voice",
            "-c",
            "user.email=t3-voice@local",
            "commit",
            "-m",
            "voice sandbox seed",
        ],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
    )
    if made.returncode != 0 and "nothing to commit" not in (made.stderr + made.stdout):
        raise RuntimeError(f"fork_env: seed commit failed: {made.stderr.strip()}")
    return True


def ensure_project(repo: Path, home: Path, sandbox: Path, stored_id: str | None) -> tuple[str, bool]:
    """Register the sandbox project. True when it registered a new one."""
    if stored_id:
        return stored_id, False
    done = run_node_cli(
        repo,
        "project",
        "add",
        str(sandbox),
        "--title",
        "Voice sandbox",
        "--base-dir",
        str(home),
    )
    if done.returncode != 0:
        raise RuntimeError(f"fork_env: project add failed: {(done.stderr or done.stdout).strip()}")
    match = PROJECT_ADD_RE.search(done.stdout)
    if match is None:
        raise RuntimeError(f"fork_env: project add printed no id: {done.stdout.strip()}")
    return match.group(1), True


def seam_config_document(project_id: str) -> dict:
    """Ceiling document. Unit 4 owns this schema, so this stays minimal."""
    return {
        "maxRuntimeMode": "auto-accept-edits",
        "operations": list(SEAM_OPERATIONS),
        "projects": [{"projectId": project_id, "spokenNames": ["sandbox", "voice sandbox"]}],
        "maxWatches": 16,
    }


def ensure_seam_config(paths: EnvPaths, project_id: str) -> bool:
    """Write voice-seam.json once. True when it wrote it. Never overwrites."""
    if paths.seam_config.exists():
        return False
    paths.seam_config.write_text(
        json.dumps(seam_config_document(project_id), indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def read_state(paths: EnvPaths) -> dict:
    """Saved setup state, or an empty dict when setup never ran."""
    try:
        return json.loads(paths.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(paths: EnvPaths, state: dict) -> None:
    """Save setup state. The token and pairing files are never stored here."""
    paths.state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def setup_environment(repo: Path, paths: EnvPaths) -> dict:
    """Idempotent first-time setup, with the server stopped. Reports what it made."""
    paths.home.mkdir(parents=True, exist_ok=True)
    state = read_state(paths)
    sandbox_created = ensure_sandbox_repo(paths.sandbox)
    project_id, project_created = ensure_project(repo, paths.home, paths.sandbox, state.get("sandboxProjectId"))
    seam_created = ensure_seam_config(paths, project_id)
    write_state(paths, {"port": FORK_PORT, "sandboxProjectId": project_id})
    return {
        "sandbox_created": sandbox_created,
        "project_created": project_created,
        "project_id": project_id,
        "seam_config_created": seam_created,
    }


def vp_node_path(repo: Path) -> str:
    """NODE_PATH the vp wrapper sets, rebuilt from this checkout. Never hardcoded."""
    existing = os.environ.get("NODE_PATH", "")
    entries = []
    for cand in sorted((repo / "node_modules" / ".pnpm").glob("vite-plus@*")):
        entries.append(str(cand / "node_modules" / "vite-plus" / "node_modules"))
        entries.append(str(cand / "node_modules"))
    entries.append(str(repo / "node_modules" / ".pnpm" / "node_modules"))
    entries = [e for e in entries if Path(e).is_dir()]
    if existing:
        entries.append(existing)
    return os.pathsep.join(entries)


def spawn_dev_server(repo: Path, paths: EnvPaths, port: int):
    """Start the dev server detached, logging to the fork home. Returns the process.

    Node runs the vp entry point directly, so the recorded pid owns the whole
    dev session. A cmd wrapper would exit and orphan it.
    """
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("fork_env: no node on PATH, cannot start the dev server")
    vp_main = repo / "node_modules" / "vite-plus" / "bin" / "vp"
    if not vp_main.is_file():
        raise RuntimeError("fork_env: vite-plus is missing, run pnpm install --frozen-lockfile first")
    env = dict(os.environ)
    env["NODE_PATH"] = vp_node_path(repo)
    log = paths.log_file.open("a", encoding="utf-8")
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen(
        [
            node,
            str(vp_main),
            "run",
            "dev",
            "--home-dir",
            str(paths.home),
            "--no-seed",
            "--port",
            str(port),
        ],
        cwd=str(repo),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=detached,
        env=env,
    )


def wait_ready(port: int, timeout_s: int = SERVER_READY_TIMEOUT_S) -> bool:
    """Wait until the server accepts TCP. Polls, so no fixed sleep can pass."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if port_open(port):
            return True
        time.sleep(SERVER_POLL_INTERVAL_S)
    return False


def kill_tree(pid: int) -> None:
    """Stop the spawned server and its process tree. Only the recorded pid."""
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        timeout=60,
    )


def restrict_file(path: Path) -> None:
    """Limit a secret file to the current user. Best effort on Windows."""
    try:
        user = getpass.getuser()
        done = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=60,
        )
        if done.returncode == 0:
            return
    except OSError:
        pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def cmd_up(repo: Path, paths: EnvPaths) -> int:
    """Start the environment. A second up starts nothing new."""
    live = environment_up(paths)
    if live is not None:
        print(f"fork_env: already up (pid {live})")
        return 0
    if port_open(FORK_PORT):
        print(f"fork_env: port {FORK_PORT} is taken, down that server first", file=sys.stderr)
        return 1
    made = setup_environment(repo, paths)
    print(f"fork_env: sandbox project {made['project_id']}")
    if not made["sandbox_created"] and not made["project_created"] and not made["seam_config_created"]:
        print("fork_env: setup already done, created nothing")
    proc = spawn_dev_server(repo, paths, FORK_PORT)
    assert proc.pid is not None
    paths.pid_file.write_text(str(proc.pid), encoding="utf-8")
    if not wait_ready(FORK_PORT):
        print(f"fork_env: server did not answer on {FORK_PORT}, see {paths.log_file}", file=sys.stderr)
        return 1
    print(f"fork_env: up on port {FORK_PORT} (pid {proc.pid})")
    return 0


def cmd_down(paths: EnvPaths) -> int:
    """Stop the recorded server only. A stale pid file is safe."""
    pid = read_pid(paths)
    if pid is None:
        print("fork_env: down (no pid file)")
        return 0
    if pid_alive(pid):
        kill_tree(pid)
        print(f"fork_env: stopped pid {pid}")
    else:
        print(f"fork_env: pid {pid} already gone, clearing the stale file")
    try:
        paths.pid_file.unlink()
    except OSError:
        pass
    return 0


def cmd_status(paths: EnvPaths) -> int:
    """Report whether the environment is up, and where it lives."""
    state = read_state(paths)
    live = environment_up(paths)
    home = paths.home
    print(f"state: {'up' if live is not None else 'down'}")
    print(f"port: {FORK_PORT}")
    print(f"home: {home}")
    print(f"sandbox: {paths.sandbox}")
    print(f"sandbox_project: {state.get('sandboxProjectId', 'unknown')}")
    if live is not None:
        print(f"pid: {live}")
    return 0


def cmd_token(repo: Path, paths: EnvPaths) -> int:
    """Mint the voice session. Refuses while the environment is up."""
    if environment_up(paths) is not None:
        print("fork_env: token refuses while the environment is up, run down first", file=sys.stderr)
        return 1
    done = run_node_cli(
        repo,
        "auth",
        "session",
        "issue",
        "--base-dir",
        str(paths.home),
        "--subject",
        TOKEN_SUBJECT,
        "--scope",
        TOKEN_SCOPE,
        "--ttl",
        TOKEN_TTL,
        "--label",
        TOKEN_LABEL,
        "--token-only",
    )
    if done.returncode != 0:
        print(f"fork_env: token mint failed: {(done.stderr or done.stdout).strip()}", file=sys.stderr)
        return 1
    token = done.stdout.strip()
    if not token:
        print("fork_env: token mint printed nothing", file=sys.stderr)
        return 1
    paths.token_file.write_text(token + "\n", encoding="utf-8")
    restrict_file(paths.token_file)
    print(f"fork_env: wrote {paths.token_file} (subject {TOKEN_SUBJECT}, scope {TOKEN_SCOPE})")
    return 0


def cmd_pair(repo: Path, paths: EnvPaths) -> int:
    """Mint a pairing URL for linking this environment. Never prints it."""
    if environment_up(paths) is None:
        print("fork_env: pair needs the environment up, run up first", file=sys.stderr)
        return 1
    done = run_node_cli(repo, "pair", "--base-dir", str(paths.home))
    if done.returncode != 0:
        print(f"fork_env: pair failed: {(done.stderr or done.stdout).strip()}", file=sys.stderr)
        return 1
    match = PAIR_URL_RE.search(done.stdout)
    if match is None:
        print("fork_env: pair printed no pairing URL", file=sys.stderr)
        return 1
    paths.pair_file.write_text(match.group(1) + "\n", encoding="utf-8")
    restrict_file(paths.pair_file)
    print(f"fork_env: wrote {paths.pair_file} (hand the link to Mason, do not open it)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI with one subcommand per lifecycle step."""
    parser = argparse.ArgumentParser(description="Bring the fork's T3 environment up and down.")
    parser.add_argument("--home", default=None, help="Fork T3 home (default VOICE_HOME/t3-fork-home).")
    parser.add_argument("--sandbox", default=None, help="Sandbox repo dir (default VOICE_HOME/t3-fork-sandbox).")
    parser.add_argument("command", choices=["up", "down", "status", "token", "pair"])
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Secrets never reach stdout on any path."""
    args = build_parser().parse_args(argv)
    voice_home = default_voice_home()
    home = Path(args.home) if args.home else voice_home / "t3-fork-home"
    sandbox = Path(args.sandbox) if args.sandbox else voice_home / "t3-fork-sandbox"
    paths = EnvPaths.from_home(home, sandbox)
    repo = repo_root()
    if args.command == "up":
        return cmd_up(repo, paths)
    if args.command == "down":
        return cmd_down(paths)
    if args.command == "status":
        return cmd_status(paths)
    if args.command == "token":
        return cmd_token(repo, paths)
    if args.command == "pair":
        return cmd_pair(repo, paths)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
