#!/usr/bin/env bash
# Expose the live voice server's page and socket over the tailnet with HTTPS, so phones grant the
# microphone. Never funnel, never a public interface: tailscale serve stays inside the tailnet.
# Verified on this Desktop's tailscale 1.104: adding and removing a port touches only that port.
#
# Usage: serve-tailscale.sh [live-port [https-port]]
#   live-port   the live server's loopback port (default 8770)
#   https-port  the tailnet HTTPS port to serve it on (default 8446; 8443-8445 are taken here)
#
# Undo: tailscale serve --https=<https-port> off
set -uo pipefail

live_port="${1:-8770}"
https_port="${2:-8446}"

echo "Running: tailscale serve --bg --https=$https_port http://127.0.0.1:$live_port"
tailscale serve --bg "--https=$https_port" "http://127.0.0.1:$live_port"

echo "Status:"
tailscale serve status
echo "Undo: tailscale serve --https=$https_port off"
