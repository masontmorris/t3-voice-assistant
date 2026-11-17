import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice.t3 import fork_env
from voice.t3.fork_env import EnvPaths


def make_paths(root: Path) -> EnvPaths:
    """Isolated home and sandbox under a temp dir, never the real voice home."""
    return EnvPaths.from_home(root / "t3-fork-home", root / "t3-fork-sandbox")


class FakeProc:
    """Stand-in for the spawned dev server, with a pid nothing can hold."""

    pid = 999_999_999


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
            ):
                self.assertEqual(fork_env.cmd_up(Path(tmp), paths), 0)
                first_calls = added.call_count
                with mock.patch.object(fork_env, "environment_up", return_value=1234):
                    self.assertEqual(fork_env.cmd_up(Path(tmp), paths), 0)
                self.assertEqual(added.call_count, first_calls)
            state = fork_env.read_state(paths)
            self.assertEqual(state["sandboxProjectId"], "proj_sandbox")


class PidAliveTest(unittest.TestCase):
    def test_a_live_pid_reads_alive_and_a_dead_one_reads_down(self):
        import os

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
            import json

            doc = json.loads(paths.seam_config.read_text(encoding="utf-8"))
            self.assertEqual(doc["maxRuntimeMode"], "auto-accept-edits")
            self.assertEqual(
                set(doc["operations"]), {"list", "read", "watch", "launch", "send", "interrupt"}
            )
            self.assertEqual([p["projectId"] for p in doc["projects"]], ["proj_sandbox"])


if __name__ == "__main__":
    unittest.main()
