import sys
import unittest
from pathlib import Path

from voice.tools.mcp import McpError, StdioClient
from voice.tools.registry import MCP_TIME

FAKE = [sys.executable, str(Path(__file__).parent / "fake_mcp_server.py")]


class StdioClientTest(unittest.TestCase):
    def test_a_call_returns_the_structured_result_past_a_notification(self):
        with StdioClient(FAKE) as mcp:
            self.assertEqual(mcp.server_info, {"name": "fake", "version": "1"})
            self.assertEqual(mcp.call_tool("echo", {"timezone": "Asia/Tokyo"}, 5), {"timezone": "Asia/Tokyo"})

    def test_a_tool_error_raises_with_the_servers_text(self):
        with StdioClient(FAKE) as mcp, self.assertRaisesRegex(McpError, "bad arguments"):
            mcp.call_tool("fail", {}, 5)

    def test_a_late_answer_times_out_and_does_not_leak_into_the_next_call(self):
        with StdioClient(FAKE) as mcp:
            with self.assertRaises(TimeoutError):
                mcp.call_tool("slow", {"n": 1}, 0.1)
            self.assertEqual(mcp.call_tool("echo", {"n": 2}, 5), {"n": 2})

    def test_a_server_that_exits_raises(self):
        with self.assertRaisesRegex(McpError, "exited"):
            StdioClient([sys.executable, "-c", "pass"])


@unittest.skipUnless(Path(MCP_TIME[0]).is_file(), "mcp-server-time is not installed in $VOICE_HOME/venv-mcp-time")
class RealTimeServerTest(unittest.TestCase):
    def test_the_reference_time_server_answers_over_stdio(self):
        with StdioClient(MCP_TIME) as mcp:
            self.assertEqual(mcp.server_info["name"], "mcp-time")
            result = mcp.call_tool("get_current_time", {"timezone": "Asia/Tokyo"}, 5)
            self.assertEqual(result["timezone"], "Asia/Tokyo")
            self.assertTrue(result["datetime"].endswith("+09:00"))
            with self.assertRaisesRegex(McpError, "Invalid timezone"):
                mcp.call_tool("get_current_time", {"timezone": "Not/AZone"}, 5)


if __name__ == "__main__":
    unittest.main()
