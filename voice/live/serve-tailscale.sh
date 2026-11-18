#!/usr/bin/env bash
# Usage: bash voice/live/serve-tailscale.sh [live-port [https-port]]
set -euo pipefail

live_port="${1:-8770}"
https_port="${2:-8446}"

for port in "$live_port" "$https_port"; do
  if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
    echo "Expected a port between 1 and 65535." >&2
    exit 2
  fi
done

python_cmd=python
if command -v python3 >/dev/null 2>&1; then
  python_cmd=python3
fi

"$python_cmd" -c '
import socket
import sys

port = int(sys.argv[1])
try:
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass
except OSError:
    sys.exit(f"No live server is listening on 127.0.0.1:{port}. Start it with --host 127.0.0.1 --port {port}.")
' "$live_port"

tailscale serve status --json | "$python_cmd" -c '
import json
import sys

config = json.load(sys.stdin) or {}
port = sys.argv[1]
if port in (config.get("TCP") or {}) or any(
    host.endswith(":" + port)
    for section in ("Web", "AllowFunnel")
    for host in (config.get(section) or {})
):
    sys.exit("Tailscale already uses this port. Choose an unused port.")
' "$https_port"

echo "Running: tailscale serve --bg --https=$https_port http://127.0.0.1:$live_port"
echo "Undo: tailscale serve --https=$https_port off"
tailscale serve --bg "--https=$https_port" "http://127.0.0.1:$live_port"
