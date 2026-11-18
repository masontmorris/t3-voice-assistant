import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice.t3 import fork_env
from voice.t3.fork_env import EnvPaths

REPO = Path(__file__).resolve().parents[2]


def make_paths(root: Path) -> EnvPaths:
    """Isolated home and sandbox under a temp dir, never the real voice home."""
    return EnvPaths.from_home(root / "t3-fork-home", root / "t3-fork-sandbox")


class FakeProc:
    """Stand-in for the spawned dev server, with a pid nothing can hold."""

    pid = 999_999_999


def fake_identity(paths: EnvPaths, stamp: str = "stamp-1"):
    """OS answer for a server this tool started. Reports our home in its command."""
    return (stamp, f"node vp run dev --home-dir {paths.home} --no-seed --port 3779")


class SecondUpTest(unittest.TestCase):
    def test_a_second_up_creates_nothing_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            added = mock.Mock(
                return_value=mock.Mock(
                    returncode=0,
                    stdout="Added project proj_sandbox (Voice sandbox) at X.",
                    stderr="",
                )
            )
            with (
                mock.patch.object(fork_env, "port_open", return_value=False),
                mock.patch.object(fork_env, "run_node_cli", added),
                mock.patch.object(fork_env, "spawn_dev_server", return_value=FakeProc()),
                mock.patch.object(fork_env, "wait_ready", return_value=True),
                mock.patch.object(
                    fork_env, "_process_info", side_effect=lambda pid: fake_identity(paths)
                ),
            ):
                self.assertEqual(fork_env.cmd_up(Path(tmp), paths), 0)
                first_calls = added.call_count
                state = fork_env.read_state(paths)
                self.assertEqual(state["sandboxProjectId"], "proj_sandbox")
                self.assertEqual(state["serverPid"], FakeProc.pid)
                # The recorded pid is dead, so the server is down. A second up
                # runs setup again and must create nothing new.
                self.assertEqual(fork_env.cmd_up(Path(tmp), paths), 0)
                self.assertEqual(added.call_count, first_calls)
            state = fork_env.read_state(paths)
            self.assertEqual(state["sandboxProjectId"], "proj_sandbox")


class PidAliveTest(unittest.TestCase):
    def test_a_live_pid_reads_alive_and_a_dead_one_reads_down(self):
        self.assertTrue(fork_env.pid_alive(os.getpid()))
        self.assertFalse(fork_env.pid_alive(999_999_999))


class StalePidTest(unittest.TestCase):
    def test_down_with_a_stale_pid_file_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.home.mkdir(parents=True)
            paths.pid_file.write_text("999999999\n", encoding="utf-8")
            killed = mock.Mock()
            with mock.patch.object(fork_env, "kill_tree", killed):
                self.assertEqual(fork_env.cmd_down(paths), 0)
            killed.assert_not_called()
            self.assertFalse(paths.pid_file.exists())


def start_sleeper():
    """A live process this tool did not start. The caller stops it."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_sleeper(proc):
    """Stop a sleeper the test started, whether or not the tool killed it first."""
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


class OwnershipTest(unittest.TestCase):
    def test_down_leaves_a_foreign_live_process_alone(self):
        proc = start_sleeper()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                paths = make_paths(Path(tmp))
                paths.home.mkdir(parents=True)
                paths.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
                self.assertIsNone(fork_env.environment_up(paths))
                self.assertEqual(fork_env.cmd_down(paths), 0)
                self.assertFalse(paths.pid_file.exists())
                self.assertIsNone(proc.poll())
        finally:
            stop_sleeper(proc)

    def test_down_leaves_a_recycled_pid_alone(self):
        proc = start_sleeper()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                paths = make_paths(Path(tmp))
                paths.home.mkdir(parents=True)
                paths.pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
                fork_env.write_state(
                    paths,
                    {"port": fork_env.FORK_PORT, "serverPid": proc.pid, "serverStarted": "stale-stamp"},
                )
                self.assertIsNone(fork_env.environment_up(paths))
                self.assertEqual(fork_env.cmd_down(paths), 0)
                self.assertFalse(paths.pid_file.exists())
                self.assertIsNone(proc.poll())
        finally:
            stop_sleeper(proc)

    def test_matching_identity_reads_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.home.mkdir(parents=True)
            paths.pid_file.write_text("1234\n", encoding="utf-8")
            fork_env.write_state(
                paths,
                {"port": fork_env.FORK_PORT, "serverPid": 1234, "serverStarted": "stamp-1"},
            )
            with (
                mock.patch.object(
                    fork_env, "_process_info", side_effect=lambda pid: fake_identity(paths)
                ),
                mock.patch.object(fork_env, "pid_alive", return_value=True),
            ):
                self.assertEqual(fork_env.environment_up(paths), 1234)


class DownKillTest(unittest.TestCase):
    def _live_setup(self, tmp: str):
        paths = make_paths(Path(tmp))
        paths.home.mkdir(parents=True)
        paths.pid_file.write_text("1234\n", encoding="utf-8")
        fork_env.write_state(
            paths,
            {"port": fork_env.FORK_PORT, "serverPid": 1234, "serverStarted": "stamp-1"},
        )
        return paths

    def _as_ours(self, paths: EnvPaths):
        return (
            mock.patch.object(
                fork_env, "_process_info", side_effect=lambda pid: fake_identity(paths)
            ),
            mock.patch.object(fork_env, "pid_alive", return_value=True),
        )

    def test_down_keeps_the_file_when_taskkill_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._live_setup(tmp)
            failed = subprocess.CompletedProcess(args=["taskkill"], returncode=1)
            ownership, alive = self._as_ours(paths)
            with (
                ownership,
                alive,
                mock.patch.object(fork_env, "kill_tree", return_value=failed),
                mock.patch.object(fork_env, "port_open", return_value=False),
            ):
                self.assertEqual(fork_env.cmd_down(paths), 1)
            self.assertTrue(paths.pid_file.exists())

    def test_down_keeps_the_file_when_the_port_stays_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._live_setup(tmp)
            done = subprocess.CompletedProcess(args=["taskkill"], returncode=0)
            ownership, alive = self._as_ours(paths)
            with (
                ownership,
                alive,
                mock.patch.object(fork_env, "kill_tree", return_value=done),
                mock.patch.object(fork_env, "port_open", return_value=True),
                mock.patch.object(fork_env, "DOWN_TIMEOUT_S", 0.2),
                mock.patch.object(fork_env, "DOWN_POLL_INTERVAL_S", 0.05),
            ):
                self.assertEqual(fork_env.cmd_down(paths), 1)
            self.assertTrue(paths.pid_file.exists())

    def test_down_clears_only_when_the_pid_is_gone_and_the_port_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._live_setup(tmp)
            done = subprocess.CompletedProcess(args=["taskkill"], returncode=0)
            ownership, _alive = self._as_ours(paths)
            with (
                ownership,
                mock.patch.object(fork_env, "pid_alive", return_value=False),
                mock.patch.object(fork_env, "kill_tree", return_value=done),
                mock.patch.object(fork_env, "port_open", return_value=False),
            ):
                self.assertEqual(fork_env.cmd_down(paths), 0)
            self.assertFalse(paths.pid_file.exists())


class WaitReadyTest(unittest.TestCase):
    def _runtime_file(self, paths: EnvPaths, pid: int):
        runtime = paths.home / "userdata" / "server-runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            json.dumps({"version": 1, "pid": pid, "port": fork_env.FORK_PORT}) + "\n",
            encoding="utf-8",
        )

    def test_ready_needs_the_runtime_file_and_the_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            self._runtime_file(paths, os.getpid())
            with mock.patch.object(fork_env, "port_open", return_value=True):
                self.assertTrue(
                    fork_env.wait_ready(fork_env.FORK_PORT, paths, timeout_s=2, poll_s=0.05)
                )

    def test_missing_runtime_file_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.home.mkdir(parents=True)
            with mock.patch.object(fork_env, "port_open", return_value=True):
                self.assertFalse(
                    fork_env.wait_ready(fork_env.FORK_PORT, paths, timeout_s=0.3, poll_s=0.05)
                )

    def test_a_dead_child_stops_the_wait_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.home.mkdir(parents=True)
            dead = mock.Mock()
            dead.poll.return_value = 1
            with mock.patch.object(fork_env, "port_open", return_value=True):
                self.assertFalse(
                    fork_env.wait_ready(
                        fork_env.FORK_PORT, paths, proc=dead, timeout_s=30, poll_s=5
                    )
                )
            dead.poll.assert_called()


def make_project_db(home: Path, project_id: str, workspace_root: str) -> None:
    """A minimal statev2.sqlite with one live project, for recovery tests."""
    db = home / "userdata" / "statev2.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE projection_projects ("
            "project_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "workspace_root TEXT NOT NULL, scripts_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT)"
        )
        conn.execute(
            "INSERT INTO projection_projects VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (project_id, "Voice sandbox", workspace_root, "[]", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
    finally:
        conn.close()


class ProjectRecoveryTest(unittest.TestCase):
    def test_recovers_the_project_id_when_state_is_lost_but_the_project_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.home.mkdir(parents=True)
            make_project_db(paths.home, "proj_kept", str(paths.sandbox))
            failed = mock.Mock(
                return_value=mock.Mock(
                    returncode=1,
                    stdout="",
                    stderr=(
                        "An active project already exists for "
                        f"'{paths.sandbox}'. (ProjectAlreadyExistsError)"
                    ),
                )
            )
            with mock.patch.object(fork_env, "run_node_cli", failed):
                project_id, created = fork_env.ensure_project(
                    Path(tmp), paths.home, paths.sandbox, None
                )
            self.assertEqual(project_id, "proj_kept")
            self.assertFalse(created)
            self.assertEqual(failed.call_count, 1)

    def test_reports_when_recovery_finds_no_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.home.mkdir(parents=True)
            failed = mock.Mock(
                return_value=mock.Mock(
                    returncode=1,
                    stdout="",
                    stderr=f"An active project already exists for '{paths.sandbox}'.",
                )
            )
            with mock.patch.object(fork_env, "run_node_cli", failed):
                with self.assertRaises(RuntimeError):
                    fork_env.ensure_project(Path(tmp), paths.home, paths.sandbox, None)


class HomeGuardTest(unittest.TestCase):
    def test_refuses_a_home_inside_dot_t3(self):
        inside = str(Path.home() / ".t3" / "fork-home")
        self.assertEqual(fork_env.main(["--home", inside, "status"]), 2)

    def test_refuses_dot_t3_itself(self):
        self.assertEqual(fork_env.main(["--home", str(Path.home() / ".t3"), "status"]), 2)

    def test_refuses_a_home_under_t3code_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"T3CODE_HOME": tmp}):
                nested = str(Path(tmp) / "nested")
                self.assertEqual(fork_env.main(["--home", nested, "status"]), 2)

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_refuses_a_case_variant_of_dot_t3(self):
        shouted = str(Path.home() / ".t3").upper()
        self.assertIsNotNone(fork_env.validate_home(Path(shouted)))
        self.assertEqual(fork_env.main(["--home", shouted, "status"]), 2)

    def test_allows_a_home_outside_dot_t3(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                fork_env.main(["--home", str(Path(tmp) / "fork-home"), "status"]), 0
            )


class ForeignServerTest(unittest.TestCase):
    """A server on this home that the pid file does not name must stop setup and token."""

    def test_up_refuses_while_another_server_uses_this_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            setup = mock.Mock()
            with (
                mock.patch.object(fork_env, "environment_up", return_value=None),
                mock.patch.object(fork_env, "port_open", return_value=False),
                mock.patch.object(fork_env, "servers_on_home", return_value=[4242]),
                mock.patch.object(fork_env, "setup_environment", setup),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(fork_env.cmd_up(Path(tmp), paths), 1)
            setup.assert_not_called()

    def test_up_refuses_when_the_scan_cannot_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            setup = mock.Mock()
            with (
                mock.patch.object(fork_env, "environment_up", return_value=None),
                mock.patch.object(fork_env, "port_open", return_value=False),
                mock.patch.object(fork_env, "servers_on_home", return_value=None),
                mock.patch.object(fork_env, "setup_environment", setup),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(fork_env.cmd_up(Path(tmp), paths), 1)
            setup.assert_not_called()

    def test_token_refuses_while_another_server_uses_this_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            minted = mock.Mock()
            with (
                mock.patch.object(fork_env, "environment_up", return_value=None),
                mock.patch.object(fork_env, "servers_on_home", return_value=[4242]),
                mock.patch.object(fork_env, "run_node_cli", minted),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(fork_env.cmd_token(Path(tmp), paths), 1)
            minted.assert_not_called()

    def test_the_scan_finds_a_live_process_on_this_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "fork-home"
            sleeper = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)", "--home-dir", str(home)]
            )
            try:
                found = fork_env.servers_on_home(EnvPaths.from_home(home, Path(tmp) / "sandbox"))
                self.assertIsNotNone(found)
                self.assertIn(sleeper.pid, found)
            finally:
                sleeper.kill()
                sleeper.wait()


class SecretWriteTest(unittest.TestCase):
    def test_token_write_path_uses_a_fake_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            minted = mock.Mock(
                return_value=mock.Mock(returncode=0, stdout="tok-abc-123\n", stderr="")
            )
            out = io.StringIO()
            with (
                mock.patch.object(fork_env, "run_node_cli", minted),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(fork_env.cmd_token(Path(tmp), paths), 0)
            self.assertEqual(paths.token_file.read_text(encoding="utf-8"), "tok-abc-123\n")
            self.assertNotIn("tok-abc-123", out.getvalue())
            leftovers = list(paths.home.glob("t3-seam-token.*.tmp"))
            self.assertEqual(leftovers, [])

    def test_token_failure_prints_a_fixed_message_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            failed = mock.Mock(
                return_value=mock.Mock(
                    returncode=3, stdout="STDOUT-LEAK", stderr="STDERR-LEAK"
                )
            )
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(fork_env, "run_node_cli", failed),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                self.assertEqual(fork_env.cmd_token(Path(tmp), paths), 1)
            self.assertNotIn("STDOUT-LEAK", out.getvalue() + err.getvalue())
            self.assertNotIn("STDERR-LEAK", out.getvalue() + err.getvalue())
            self.assertIn("exit 3", err.getvalue())
            self.assertFalse(paths.token_file.exists())

    def test_pair_failure_prints_a_fixed_message_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            failed = mock.Mock(
                return_value=mock.Mock(
                    returncode=1,
                    stdout="Pairing URL: https://pair.example/SECRET",
                    stderr="",
                )
            )
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(fork_env, "environment_up", return_value=1234),
                mock.patch.object(fork_env, "run_node_cli", failed),
                mock.patch.object(fork_env, "PAIR_RETRY_DELAY_S", 0),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                self.assertEqual(fork_env.cmd_pair(Path(tmp), paths), 1)
            self.assertNotIn("pair.example", out.getvalue() + err.getvalue())
            self.assertNotIn("SECRET", out.getvalue() + err.getvalue())
            self.assertIn("exit 1", err.getvalue())
            self.assertFalse(paths.pair_file.exists())


class StatusWebTest(unittest.TestCase):
    def test_status_shows_the_web_port_the_pairing_link_uses(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.home.mkdir(parents=True)
            paths.pid_file.write_text("1234\n", encoding="utf-8")
            fork_env.write_state(
                paths,
                {"port": fork_env.FORK_PORT, "serverPid": 1234, "serverStarted": "stamp-1"},
            )
            runtime = paths.home / "userdata" / "server-runtime.json"
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pid": 1234,
                        "port": fork_env.FORK_PORT,
                        "origin": "http://127.0.0.1:3779",
                        "devUrl": "http://localhost:6146/",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            out = io.StringIO()
            with (
                mock.patch.object(
                    fork_env, "_process_info", side_effect=lambda pid: fake_identity(paths)
                ),
                mock.patch.object(fork_env, "pid_alive", return_value=True),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(fork_env.cmd_status(paths), 0)
            self.assertIn("6146", out.getvalue())


class TokenGuardTest(unittest.TestCase):
    def test_token_refuses_while_the_environment_is_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            issued = mock.Mock()
            with (
                mock.patch.object(fork_env, "environment_up", return_value=1234),
                mock.patch.object(fork_env, "run_node_cli", issued),
            ):
                self.assertEqual(fork_env.cmd_token(Path(tmp), paths), 1)
            issued.assert_not_called()
            self.assertFalse(paths.token_file.exists())


class PortTakenTest(unittest.TestCase):
    def test_up_fails_clearly_when_the_port_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            spawned = mock.Mock()
            with (
                mock.patch.object(fork_env, "port_open", return_value=True),
                mock.patch.object(fork_env, "spawn_dev_server", spawned),
            ):
                self.assertEqual(fork_env.cmd_up(Path(tmp), paths), 1)
            spawned.assert_not_called()
            self.assertFalse(paths.state_file.exists())


class SeamConfigTest(unittest.TestCase):
    def test_the_ceiling_names_only_the_sandbox_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.home.mkdir(parents=True)
            self.assertTrue(fork_env.ensure_seam_config(paths, "proj_sandbox"))
            self.assertFalse(fork_env.ensure_seam_config(paths, "proj_other"))
            doc = json.loads(paths.seam_config.read_text(encoding="utf-8"))
            self.assertEqual(doc["maxRuntimeMode"], "auto-accept-edits")
            self.assertEqual(
                set(doc["operations"]), {"list", "read", "watch", "launch", "send", "interrupt"}
            )
            self.assertEqual([p["projectId"] for p in doc["projects"]], ["proj_sandbox"])
            self.assertEqual(doc["workspace"], {"proj_sandbox": {"type": "root"}})

    def test_the_sandbox_launches_on_replay_and_a_model_already_set_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            settings_file = paths.home / "userdata" / "settings.json"
            settings_file.parent.mkdir(parents=True)
            settings_file.write_text(
                json.dumps({"providerInstances": {"devReplay": {"driver": "acpRegistry"}}}),
                encoding="utf-8",
            )
            self.assertTrue(fork_env.ensure_sandbox_model(paths, "proj_sandbox"))
            self.assertFalse(fork_env.ensure_sandbox_model(paths, "proj_sandbox"))
            doc = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertEqual(doc["providerInstances"], {"devReplay": {"driver": "acpRegistry"}})
            self.assertEqual(
                doc["projectSettingsOverrides"],
                {"proj_sandbox": {"defaultModelSelection": {"instanceId": "devReplay", "model": "showcase"}}},
            )

            chosen = {"instanceId": "codex", "model": "gpt-5.4"}
            doc["projectSettingsOverrides"]["proj_sandbox"]["defaultModelSelection"] = chosen
            settings_file.write_text(json.dumps(doc), encoding="utf-8")
            self.assertFalse(fork_env.ensure_sandbox_model(paths, "proj_sandbox"))
            kept = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertEqual(
                kept["projectSettingsOverrides"]["proj_sandbox"]["defaultModelSelection"], chosen
            )

    def test_the_ceiling_has_the_keys_the_seam_schema_decodes(self):
        schema = json.loads((REPO / "voice" / "t3" / "seam.schema.json").read_text(encoding="utf-8"))
        config = schema["$defs"]["VoiceSeamConfig"]
        doc = fork_env.seam_config_document("proj_sandbox")
        self.assertLessEqual(set(doc), set(config["properties"]))
        self.assertLessEqual(set(config["required"]), set(doc))
        project_ids = {project["projectId"] for project in doc["projects"]}
        self.assertLessEqual(set(doc["workspace"]), project_ids)


if __name__ == "__main__":
    unittest.main()
