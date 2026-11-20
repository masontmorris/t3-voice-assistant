"""Starting the local summariser's servers for a call: under `auto` a failure leaves the call on the cloud model."""

import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from voice.live import t3_tasks


class Servers:
    def __init__(self, fail_gate=False):
        self.fail_gate, self.stopped = fail_gate, []

    def text(self, log_path):
        return self._server("text", False)

    def gate(self, log_path):
        return self._server("gate", self.fail_gate)

    @contextmanager
    def _server(self, name, fail):
        if fail:
            raise RuntimeError(f"{name} exited")
        try:
            yield SimpleNamespace(proc=SimpleNamespace(pid=1), startup_s=0.1)
        finally:
            self.stopped.append(name)


class LocalSummariserTest(unittest.TestCase):
    def start(self, mode, servers, shared_gate=None):
        with patch.object(t3_tasks, "local_text_server", servers.text), \
                patch.object(t3_tasks, "summary_gate_server", servers.gate), \
                patch.object(Path, "is_file", return_value=True):
            with t3_tasks.local_summariser(mode, Path("."), shared_gate) as urls:
                return urls

    def test_auto_starts_the_call_on_the_cloud_model_when_a_server_fails(self):
        servers = Servers(fail_gate=True)
        self.assertEqual(self.start("auto", servers), (None, None))
        self.assertEqual(servers.stopped, ["text"], "the server that did start is stopped")

    def test_on_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            self.start("on", Servers(fail_gate=True))

    def test_a_shared_decider_is_the_gate(self):
        servers = Servers()
        urls = self.start("auto", servers, shared_gate="http://127.0.0.1:8096")
        self.assertEqual(urls, (f"http://127.0.0.1:{t3_tasks.LOCAL_TEXT_PORT}", "http://127.0.0.1:8096"))
        self.assertEqual(servers.stopped, ["text"], "no second d1-2B was started")


if __name__ == "__main__":
    unittest.main()
