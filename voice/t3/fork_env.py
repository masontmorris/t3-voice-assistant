"""One command to bring the fork's separate T3 environment up and down.

Mason's daily T3 install is never touched. The fork's server runs from this
checkout against a dedicated T3 home, so fork code never writes his database.
Run with ``python voice/t3/fork_env.py <up|down|status|token|pair>`` from the
repo root. Every path below hangs off VOICE_HOME, which defaults to
``~/voice``. Only the standard library is used. Windows only: process checks
use Get-CimInstance and taskkill, and secret files use icacls.

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
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

FORK_PORT = 3779
SERVER_READY_TIMEOUT_S = 240
SERVER_POLL_INTERVAL_S = 2
DOWN_TIMEOUT_S = 30
DOWN_POLL_INTERVAL_S = 1
PAIR_ATTEMPTS = 3
PAIR_RETRY_DELAY_S = 2

STATE_FILE = "fork-env.json"
SETTINGS_FILE = "userdata/settings.json"
PID_FILE = "fork-env.pid"
SEAM_CONFIG_FILE = "voice-seam.json"
TOKEN_FILE = "t3-seam-token"
PAIR_FILE = "pairing-url.txt"
LOG_FILE = "fork-env.log"

RUNTIME_STATE_FILES = ("userdata/server-runtime.json", "dev/server-runtime.json")

DEV_REPLAY_PROVIDER = "devReplay"
DEV_REPLAY_MODEL = "showcase"

TOKEN_SUBJECT = "voice"
TOKEN_SCOPE = "orchestration:read"
TOKEN_TTL = "30d"
TOKEN_LABEL = "Voice server (ceiling in voice-seam.json)"

SEAM_OPERATIONS = ["list", "read", "watch", "launch", "send", "interrupt"]

PROJECT_ADD_RE = re.compile(r"Added project (\S+)")
ALREADY_EXISTS_RE = re.compile(r"already exists", re.IGNORECASE)
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


def _process_info(pid: int) -> tuple[str | None, str | None]:
    """Creation stamp and command line for a pid. (None, None) when unknown.

    Both halves must match what this tool recorded before the pid counts as
    ours. A recycled pid fails the stamp, and another program fails the
    command line. Never raises.
    """
    if pid is None or pid <= 0:
        return (None, None)
    try:
        if os.name == "nt":
            return _process_info_windows(pid)
        return _process_info_ps(pid)
    except Exception:
        return (None, None)


def _process_info_windows(pid: int) -> tuple[str | None, str | None]:
    """Creation stamp and command line from CIM. (None, None) when unknown."""
    probe = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' "
        "| Select-Object -First 1; "
        "if ($p) { "
        "@{creation=$p.CreationDate.ToString('o'); cmdline=$p.CommandLine} "
        "| ConvertTo-Json -Compress }"
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if done.returncode != 0:
        return (None, None)
    text = (done.stdout or "").strip()
    if not text:
        return (None, None)
    try:
        doc = json.loads(text)
    except ValueError:
        return (None, None)
    if not isinstance(doc, dict):
        return (None, None)
    creation = doc.get("creation")
    cmdline = doc.get("cmdline")
    if not creation or not isinstance(cmdline, str):
        return (None, None)
    return (str(creation), cmdline)


def _process_info_ps(pid: int) -> tuple[str | None, str | None]:
    """Creation stamp and command line from ps. Fallback for non-Windows."""
    done = subprocess.run(
        ["ps", "-o", "lstart=", "-o", "args=", "-p", str(int(pid))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if done.returncode != 0:
        return (None, None)
    text = (done.stdout or "").strip()
    if not text:
        return (None, None)
    parts = text.split(None, 5)
    if len(parts) < 5:
        return (None, None)
    cmdline = parts[5] if len(parts) > 5 else ""
    return (" ".join(parts[:5]), cmdline)


def _cmdline_is_ours(cmdline: str | None, home: Path) -> bool:
    """True when this command line started our server for this home."""
    if not cmdline:
        return False
    home_text = os.path.normcase(os.path.abspath(str(home)))
    shown = os.path.normcase(cmdline)
    return "--home-dir" in shown and home_text in shown


def servers_on_home(paths: EnvPaths) -> list[int] | None:
    """Pids of live processes started with --home-dir on this home. None when the scan cannot run."""
    try:
        if os.name == "nt":
            probe = (
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
                "$_.CommandLine.Contains('--home-dir') } | ForEach-Object { "
                "@{pid=$_.ProcessId; cmdline=$_.CommandLine} } | ConvertTo-Json -Compress"
            )
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", probe],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if done.returncode != 0:
                return None
            text = (done.stdout or "").strip()
            if not text:
                return []
            doc = json.loads(text)
            rows = doc if isinstance(doc, list) else [doc]
            found = [(int(row["pid"]), row.get("cmdline")) for row in rows if isinstance(row, dict)]
        else:
            done = subprocess.run(
                ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=30
            )
            if done.returncode != 0:
                return None
            found = []
            for line in (done.stdout or "").splitlines():
                head, _, rest = line.strip().partition(" ")
                if head.isdigit():
                    found.append((int(head), rest))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None
    return [pid for pid, cmdline in found if pid != os.getpid() and _cmdline_is_ours(cmdline, paths.home)]


def foreign_server_refusal(paths: EnvPaths) -> str | None:
    """Refusal when some server already uses this home, or when that cannot be checked."""
    pids = servers_on_home(paths)
    if pids is None:
        return "fork_env: cannot check for other servers on this home, refusing"
    if pids:
        return f"fork_env: another server uses this home (pid {pids[0]}), stop it first"
    return None


def server_identity_ok(paths: EnvPaths, pid: int) -> bool:
    """True when pid is the server this tool recorded for this home."""
    try:
        state = read_state(paths)
        if state.get("serverPid") != pid:
            return False
        stamp = state.get("serverStarted")
        if not stamp:
            return False
        creation, cmdline = _process_info(pid)
        if creation != stamp:
            return False
        return _cmdline_is_ours(cmdline, paths.home)
    except Exception:
        return False


def record_server_identity(paths: EnvPaths, pid: int, attempts: int = 5) -> bool:
    """Stamp this spawn's creation time into the state file. False when unknown."""
    for _ in range(attempts):
        creation, _cmdline = _process_info(pid)
        if creation is not None:
            update_state(paths, serverPid=pid, serverStarted=creation)
            return True
        time.sleep(1.0)
    return False


def clear_pid_file(paths: EnvPaths) -> None:
    """Drop the pid file. Only for pids this tool checked first."""
    try:
        paths.pid_file.unlink()
    except OSError:
        pass


def environment_up(paths: EnvPaths) -> int | None:
    """Pid of our server when it is up, else None. A stale file is cleared."""
    pid = read_pid(paths)
    if pid is None:
        return None
    if server_identity_ok(paths, pid) and pid_alive(pid):
        return pid
    clear_pid_file(paths)
    return None


def runtime_candidates(paths: EnvPaths) -> list[Path]:
    """Server runtime files, userdata first. The dev variant covers old layouts."""
    return [paths.home / name for name in RUNTIME_STATE_FILES]


def runtime_state(paths: EnvPaths) -> dict | None:
    """First parseable server runtime file, else None. Never raises."""
    for candidate in runtime_candidates(paths):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            doc = json.loads(text)
        except ValueError:
            continue
        if isinstance(doc, dict):
            return doc
    return None


def runtime_web_url(paths: EnvPaths) -> str | None:
    """Web URL the pairing link uses, from the runtime file. None when unknown."""
    state = runtime_state(paths)
    if not state:
        return None
    for key in ("devUrl", "origin"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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


def _same_root(left: str, right: str) -> bool:
    """True when two workspace roots name the same directory on this platform."""
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def find_project_id_by_root(home: Path, sandbox: Path) -> str | None:
    """Live project id for this root in this home, else None. Read-only."""
    want = str(sandbox)
    for variant in ("userdata", "dev"):
        db = home / variant / "statev2.sqlite"
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            continue
        try:
            try:
                rows = conn.execute(
                    "SELECT project_id, workspace_root FROM projection_projects "
                    "WHERE deleted_at IS NULL"
                )
            except sqlite3.Error:
                continue
            for project_id, workspace_root in rows:
                if isinstance(workspace_root, str) and _same_root(workspace_root, want):
                    return project_id
        except sqlite3.Error:
            continue
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return None


def ensure_project(repo: Path, home: Path, sandbox: Path, stored_id: str | None) -> tuple[str, bool]:
    """Register the sandbox project. True when it registered a new one.

    When the state file is lost and add answers that the project already
    exists, recovers the id from the database instead of failing.
    """
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
    if done.returncode == 0:
        match = PROJECT_ADD_RE.search(done.stdout)
        if match is None:
            raise RuntimeError(f"fork_env: project add printed no id: {done.stdout.strip()}")
        return match.group(1), True
    output = f"{done.stdout or ''}\n{done.stderr or ''}"
    if ALREADY_EXISTS_RE.search(output):
        found = find_project_id_by_root(home, sandbox)
        if found is not None:
            return found, False
        raise RuntimeError(
            "fork_env: project add says the sandbox already exists but no live "
            f"project names {sandbox} (exit {done.returncode})"
        )
    raise RuntimeError(
        f"fork_env: project add failed (exit {done.returncode}): "
        f"{(done.stderr or done.stdout).strip()}"
    )


def seam_config_document(project_id: str) -> dict:
    """Ceiling document, in the shape VoiceSeamConfig in voice/t3/seam.schema.json decodes.

    The sandbox launches in its root: a worktree needs a base ref, and the sandbox's branch name
    depends on the machine's git config.
    """
    return {
        "maxRuntimeMode": "auto-accept-edits",
        "operations": list(SEAM_OPERATIONS),
        "projects": [{"projectId": project_id, "spokenNames": ["sandbox", "voice sandbox"]}],
        "workspace": {project_id: {"type": "root"}},
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


def ensure_sandbox_model(paths: EnvPaths, project_id: str) -> bool:
    """Give the sandbox Replay (dev) as its default model. True when it wrote it.

    A voice launch takes the project's default model, so the sandbox's launches run the free
    replay agent and never a paid one. A default model already set for the sandbox is kept.
    Runs with the server stopped, like the rest of setup.
    """
    file = paths.home / SETTINGS_FILE
    try:
        settings = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    sandbox = settings.setdefault("projectSettingsOverrides", {}).setdefault(project_id, {})
    if sandbox.get("defaultModelSelection") is not None:
        return False
    sandbox["defaultModelSelection"] = {"instanceId": DEV_REPLAY_PROVIDER, "model": DEV_REPLAY_MODEL}
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
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


def update_state(paths: EnvPaths, **fields) -> dict:
    """Merge fields into the saved state and write it. Returns the state."""
    state = read_state(paths)
    state.update(fields)
    write_state(paths, state)
    return state


def setup_environment(repo: Path, paths: EnvPaths) -> dict:
    """Idempotent first-time setup, with the server stopped. Reports what it made."""
    paths.home.mkdir(parents=True, exist_ok=True)
    state = read_state(paths)
    sandbox_created = ensure_sandbox_repo(paths.sandbox)
    project_id, project_created = ensure_project(repo, paths.home, paths.sandbox, state.get("sandboxProjectId"))
    write_state(paths, {"port": FORK_PORT, "sandboxProjectId": project_id})
    seam_created = ensure_seam_config(paths, project_id)
    model_set = ensure_sandbox_model(paths, project_id)
    return {
        "sandbox_created": sandbox_created,
        "project_created": project_created,
        "project_id": project_id,
        "seam_created": seam_created,
        "model_set": model_set,
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


def wait_ready(
    port: int,
    paths: EnvPaths,
    proc: subprocess.Popen | None = None,
    timeout_s: int = SERVER_READY_TIMEOUT_S,
    poll_s: float = SERVER_POLL_INTERVAL_S,
) -> bool:
    """Wait until the server answers TCP and has written its runtime file.

    The runtime file is what pair reads, so waiting for it as well as the
    port means pair works right after up. Returns False early when the child
    process dies instead of waiting out the timeout. The file names the inner
    server, never the spawned wrapper, so no pid equality is required here;
    the port plus a live recorded pid is what readiness means.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None:
            try:
                if proc.poll() is not None:
                    return False
            except Exception:
                return False
        if port_open(port):
            state = runtime_state(paths)
            pid = state.get("pid") if isinstance(state, dict) else None
            if (
                isinstance(state, dict)
                and state.get("port") == port
                and isinstance(pid, int)
                and pid_alive(pid)
            ):
                return True
        time.sleep(poll_s)
    return False


def kill_tree(pid: int) -> subprocess.CompletedProcess[str]:
    """Stop the spawned server and its process tree. Returns the taskkill result."""
    try:
        return subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=["taskkill"], returncode=1, stdout="", stderr=str(exc))


def restrict_file(path: Path) -> bool:
    """Limit a secret file to the current user. True when the ACL held."""
    if os.name == "nt":
        try:
            user = getpass.getuser()
        except Exception:
            return False
        try:
            done = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
                timeout=60,
            )
            return done.returncode == 0
        except OSError:
            return False
    try:
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def write_secret_file(path: Path, text: str) -> bool:
    """Write a secret atomically with user-only permissions. True when the ACL held.

    Writes to a temp file in the same directory, locks it, then renames, so a
    crash never leaves a half-written or wide-open secret behind. Warns on
    stderr when icacls fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        held = restrict_file(tmp)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if not held:
        print(
            f"fork_env: warning: icacls failed for {path}, permissions are inherited",
            file=sys.stderr,
        )
    return held


def validate_home(home: Path) -> str | None:
    """Refusal reason when this home would touch the daily install, else None."""
    def canonical(path: str) -> str:
        # Windows paths ignore case and may pass through junctions, so compare resolved, case-folded forms.
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    text = canonical(str(home))
    protected = [canonical(str(Path.home() / ".t3"))]
    ambient = os.environ.get("T3CODE_HOME")
    if ambient:
        protected.append(canonical(ambient))
    for guard in protected:
        if text == guard or text.startswith(guard + os.sep):
            return f"fork_env: refusing --home under Mason's daily install ({guard})"
    return None


def cmd_up(repo: Path, paths: EnvPaths) -> int:
    """Start the environment. A second up starts nothing new."""
    live = environment_up(paths)
    if live is not None:
        print(f"fork_env: already up (pid {live})")
        return 0
    if port_open(FORK_PORT):
        print(f"fork_env: port {FORK_PORT} is taken, down that server first", file=sys.stderr)
        return 1
    refusal = foreign_server_refusal(paths)
    if refusal:
        print(refusal, file=sys.stderr)
        return 1
    made = setup_environment(repo, paths)
    print(f"fork_env: sandbox project {made['project_id']}")
    if made["model_set"]:
        print(f"fork_env: sandbox default model is {DEV_REPLAY_PROVIDER}/{DEV_REPLAY_MODEL}")
    if not any(made[key] for key in ("sandbox_created", "project_created", "seam_created", "model_set")):
        print("fork_env: setup already done, created nothing")
    proc = spawn_dev_server(repo, paths, FORK_PORT)
    assert proc.pid is not None
    if not record_server_identity(paths, proc.pid):
        print(
            f"fork_env: cannot verify spawned server (pid {proc.pid}), stopping it",
            file=sys.stderr,
        )
        kill_tree(proc.pid)
        return 1
    paths.pid_file.write_text(str(proc.pid), encoding="utf-8")
    if not wait_ready(FORK_PORT, paths, proc):
        print(f"fork_env: server did not answer on {FORK_PORT}, see {paths.log_file}", file=sys.stderr)
        return 1
    print(f"fork_env: up on port {FORK_PORT} (pid {proc.pid})")
    return 0


def cmd_down(paths: EnvPaths) -> int:
    """Stop the recorded server only. A stale pid file is cleared, never killed."""
    pid = read_pid(paths)
    if pid is None:
        print("fork_env: down (no pid file)")
        return 0
    if environment_up(paths) is None:
        print(f"fork_env: pid {pid} is not ours or already gone, cleared the stale file")
        return 0
    done = kill_tree(pid)
    if done.returncode != 0:
        print(
            f"fork_env: taskkill failed for pid {pid} (exit {done.returncode}), "
            "keeping the pid file",
            file=sys.stderr,
        )
        return 1
    deadline = time.monotonic() + DOWN_TIMEOUT_S
    while time.monotonic() < deadline:
        if not pid_alive(pid) and not port_open(FORK_PORT):
            clear_pid_file(paths)
            print(f"fork_env: stopped pid {pid}")
            return 0
        time.sleep(DOWN_POLL_INTERVAL_S)
    print(
        f"fork_env: pid {pid} did not stop or port {FORK_PORT} is still open, "
        "keeping the pid file",
        file=sys.stderr,
    )
    return 1


def cmd_status(paths: EnvPaths) -> int:
    """Report whether the environment is up, and where it lives."""
    state = read_state(paths)
    live = environment_up(paths)
    home = paths.home
    print(f"state: {'up' if live is not None else 'down'}")
    print(f"port: {FORK_PORT}")
    web = runtime_web_url(paths) if live is not None else None
    print(f"web: {web if web is not None else 'unknown'}")
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
    refusal = foreign_server_refusal(paths)
    if refusal:
        print(refusal, file=sys.stderr)
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
        print(f"fork_env: token mint failed (exit {done.returncode})", file=sys.stderr)
        return 1
    token = done.stdout.strip()
    if not token:
        print(f"fork_env: token mint failed (exit {done.returncode})", file=sys.stderr)
        return 1
    write_secret_file(paths.token_file, token + "\n")
    print(f"fork_env: wrote {paths.token_file} (subject {TOKEN_SUBJECT}, scope {TOKEN_SCOPE})")
    return 0


def cmd_pair(repo: Path, paths: EnvPaths) -> int:
    """Mint a pairing URL for linking this environment. Never prints it."""
    if environment_up(paths) is None:
        print("fork_env: pair needs the environment up, run up first", file=sys.stderr)
        return 1
    done = None
    for _ in range(PAIR_ATTEMPTS):
        done = run_node_cli(repo, "pair", "--base-dir", str(paths.home))
        if done.returncode == 0:
            break
        time.sleep(PAIR_RETRY_DELAY_S)
    assert done is not None
    if done.returncode != 0:
        print(f"fork_env: pair failed (exit {done.returncode})", file=sys.stderr)
        return 1
    match = PAIR_URL_RE.search(done.stdout)
    if match is None:
        print("fork_env: pair printed no pairing URL", file=sys.stderr)
        return 1
    write_secret_file(paths.pair_file, match.group(1) + "\n")
    print(f"fork_env: wrote {paths.pair_file} (hand the link to Mason, do not open it)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI with one subcommand per lifecycle step."""
    parser = argparse.ArgumentParser(description="Bring the fork's T3 environment up and down.")
    parser.add_argument("--home", default=None, help="Fork T3 home (default VOICE_HOME/t3-fork-home).")
    parser.add_argument("--sandbox", default=None, help="Sandbox repo dir (default VOICE_HOME/t3-fork-sandbox).")
    parser.add_argument("command", choices=["up", "status", "down", "token", "pair"])
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Secrets never reach stdout on any path."""
    args = build_parser().parse_args(argv)
    voice_home = default_voice_home()
    home = Path(args.home) if args.home else voice_home / "t3-fork-home"
    home = Path(os.path.abspath(os.path.expanduser(str(home))))
    sandbox = Path(args.sandbox) if args.sandbox else voice_home / "t3-fork-sandbox"
    sandbox = Path(os.path.abspath(os.path.expanduser(str(sandbox))))
    refusal = validate_home(home)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2
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
