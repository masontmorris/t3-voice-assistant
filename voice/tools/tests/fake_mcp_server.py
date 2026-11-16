"""A stand-in MCP server for the stdio client tests. echo returns its arguments, fail is a tool error, slow answers
after 0.5 s, and every call is preceded by a notification the client must skip."""

import json
import sys
import time


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"}}})
        continue
    name, arguments = message["params"]["name"], message["params"]["arguments"]
    send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": name}})
    if name == "slow":
        time.sleep(0.5)
    if name == "fail":
        result = {"content": [{"type": "text", "text": "bad arguments"}], "isError": True}
    else:
        result = {"content": [{"type": "text", "text": json.dumps(arguments)}], "structuredContent": arguments}
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})
