"""The T3 a scenario's call runs against: the fork sandbox in a home of its own, with free Replay threads.

The gate never touches the sandbox home used for daily work. It builds a template home once (the sandbox project, a seam
token, and the settings below), and every run restores that template, so no thread from an earlier run is still
there to share a title with this run's. The template disables Codex and Claude and points title generation at an
unconfigured instance. T3 uses provider defaults if its settings file is missing or invalid. The sandbox project
selects `Replay (dev)`, which plays recorded turns for free.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from voice.t3 import fork_env as F
from voice.t3 import types as T
from voice.t3.seam import T3Seam

VOICE = Path(os.environ.get("VOICE_HOME", Path.home() / "voice"))
TIMEOUT_S = 120
CONFIG_SETTLE_S = 3.0
WORKING_MODES = ("approval-required", "auto-accept-edits", "full-access")


@dataclass(frozen=True)
class Seed:
    """A thread that exists before the call. `replay` is the recorded scenario it plays and `mode` the runtime mode it
    starts in: an approval is asked only in `approval-required`. `ends` is the activity the thread reaches by itself."""

    title: str
    replay: str = "showcase"
    mode: str = "full-access"
    ends: T.ThreadActivity = T.ThreadActivity.FINISHED
    message: str = "go ahead"


def pending(title: str, replay: str = "approval") -> Seed:
    """A thread that stops to ask for approval of a command, and stays there."""
    return Seed(title, replay, "approval-required", T.ThreadActivity.WAITING_ON_YOU)


def finished(title: str) -> Seed:
    """A thread whose run ended with a reply. It runs in a mode the seam's ceiling allows, or T3 refuses a message to it."""
    return Seed(title, "todo-list", "auto-accept-edits")


class World:
    """One sandbox T3: up, configured, seeded, read, and down."""

    def __init__(self, voice_home: Path = VOICE):
        self.home, self.sandbox = voice_home / "t3-gate-home", voice_home / "t3-gate-sandbox"
        self.template = voice_home / "t3-gate-home.template"
        self.paths = F.EnvPaths.from_home(self.home, self.sandbox)
        self.repo = F.repo_root()
        self.seam, self.ids, self.runs = None, {}, {}

    @property
    def token_file(self) -> Path:
        return self.paths.token_file

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{F.FORK_PORT}"

    @property
    def project(self) -> str:
        return F.read_state(self.paths)["sandboxProjectId"]

    def up(self) -> None:
        """The sandbox up on a fresh copy of the template."""
        if F.environment_up(self.paths) is not None:
            self.down()
        if not self.template.is_dir():
            self.build_template()
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.copytree(self.template, self.home)
        self.start()

    def start(self) -> None:
        if F.cmd_up(self.repo, self.paths) != 0:
            raise SystemExit("the gate's sandbox T3 did not come up, see its fork-env.log")
        self.seam, self.ids = None, {}

    def down(self) -> None:
        self.seam = None
        if F.cmd_down(self.paths) != 0:
            raise RuntimeError("the gate's sandbox T3 did not shut down")

    def build_template(self) -> None:
        """First time only: set the sandbox up, mint the seam token with the server down, write the settings, and keep
        the result."""
        shutil.rmtree(self.home, ignore_errors=True)
        refusal = F.validate_home(self.home)
        if refusal:
            raise SystemExit(refusal)
        if F.cmd_up(self.repo, self.paths) != 0:
            raise SystemExit("the gate's sandbox T3 did not come up while it was being built")
        self.down()
        if F.cmd_token(self.repo, self.paths) != 0:
            raise SystemExit("no seam token for the gate's sandbox")
        settings = F.read_settings(self.paths)
        for instance in ("codex", "claudeAgent"):
            settings.setdefault("providerInstances", {})[instance] = {"driver": instance, "enabled": False}
        settings["textGenerationModelSelection"] = {"instanceId": "gate-no-titles", "model": "none"}
        F.write_settings(self.paths, settings)
        for stale in (self.paths.pid_file, self.home / "userdata" / "logs"):
            shutil.rmtree(stale, ignore_errors=True) if stale.is_dir() else stale.unlink(missing_ok=True)
        shutil.rmtree(self.template, ignore_errors=True)
        shutil.copytree(self.home, self.template)

    def configure(self, model: str | None = None, mode: str | None = None) -> None:
        """What the next thread this T3 starts runs on and in: `model` is a Replay scenario, `mode` a runtime mode that
        threads started from the command line take (a voice launch always runs at the seam's ceiling). The server
        reads the file again when it changes, and a thread started in the first seconds still gets the old values."""
        settings = F.read_settings(self.paths)
        sandbox = settings["projectSettingsOverrides"][self.project]
        if model is not None:
            sandbox["defaultModelSelection"] = {"instanceId": F.DEV_REPLAY_PROVIDER, "model": model}
        if mode is not None:
            sandbox["defaultRuntimeMode"] = mode
        fd, name = tempfile.mkstemp(dir=self.home / "userdata", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
        os.replace(name, self.home / F.SETTINGS_FILE)
        time.sleep(CONFIG_SETTLE_S)

    def connection(self) -> T3Seam:
        if self.seam is None:
            self.seam = T3Seam.connect(self.origin, self.token_file)
        return self.seam

    def state(self, thread_id: str) -> T.ThreadState:
        return self.connection().read_thread(thread_id)

    def titles(self) -> dict[str, str]:
        """Every thread the sandbox holds, as id to title."""
        return {t.thread_id: t.title for t in self.connection().list_threads(limit=200).threads}

    def start_thread(self, seed: Seed) -> str:
        done = F.run_node_cli(self.repo, "thread", "start", "--base-dir", str(self.home), "--provider",
                              F.DEV_REPLAY_PROVIDER, "--model", seed.replay, "--title", seed.title, "--json",
                              self.project, seed.message)
        if done.returncode != 0:
            raise RuntimeError(f"thread start failed for {seed.title!r}: {done.stderr[-300:]}")
        return json.loads(done.stdout)["threadId"]

    def seed(self, seeds: tuple[Seed, ...]) -> dict[str, str]:
        """The seeds started, in the mode each wants, and waited for until each is where it ends. Returns title to id."""
        found = {}
        for mode in dict.fromkeys(seed.mode for seed in seeds):
            self.configure(mode=mode)
            group = [seed for seed in seeds if seed.mode == mode]
            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                found.update(zip((s.title for s in group), pool.map(self.start_thread, group)))
        deadline = time.monotonic() + TIMEOUT_S
        for seed in seeds:
            while self.state(found[seed.title]).activity is not seed.ends:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"seed {seed.title!r} never reached {seed.ends.value}")
                time.sleep(1.0)
        self.ids.update(found)
        self.runs = {title: self.run_of(thread) for title, thread in found.items()}
        return found

    def run_of(self, thread_id: str) -> str | None:
        run = self.state(thread_id).run
        return run.run_id if run else None

    def snapshot(self, seeded: dict[str, str], before: set[str]) -> dict:
        """What the sandbox holds when the call is over: each seeded thread's activity and whether it still waits on an
        answer, and the threads the call made (any that was not there before the call and was not seeded)."""
        out = {"seeded": {}, "made": {}}
        for title, thread_id in seeded.items():
            state = self.state(thread_id)
            need = state.pending
            out["seeded"][title] = {"thread": thread_id, "activity": state.activity.value,
                                    "waiting_on": need.kind.value if need else None,
                                    "need": need.prompt if need else None,
                                    "new_run": self.run_of(thread_id) != self.runs.get(title)}
        for thread_id, title in self.titles().items():
            if thread_id not in before and thread_id not in seeded.values():
                state = self.state(thread_id)
                out["made"][thread_id] = {"title": title, "activity": state.activity.value,
                                          "waiting_on": state.pending.kind.value if state.pending else None}
        return out
