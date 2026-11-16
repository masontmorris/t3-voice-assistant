"""Builds the laptop client zip under $VOICE_HOME/live/dist and prints its path and SHA-256.

    python -m voice.live.package [--port 8770]

The zip holds the client, its recording module, its requirements, SETUP-LAPTOP.md, config.json naming this machine's
Tailscale DNS name, and the shared secret. It stays under $VOICE_HOME, never in the repo. Send it with
`tailscale file cp <zip> <laptop>:`.
"""

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from voice.live.server import LIVE_HOME, shared_secret

HERE = Path(__file__).resolve().parent
FILES = ["client.py", "recording.py", "aec.py", "rawmic.py", "requirements-client.txt", "SETUP-LAPTOP.md"]


def tailscale_dns_name():
    result = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, check=True, timeout=10)
    return json.loads(result.stdout)["Self"]["DNSName"].rstrip(".")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    dist = LIVE_HOME / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / "voice-live-client.zip"
    config = {"server": f"ws://{tailscale_dns_name()}:{args.port}"}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in FILES:
            zf.write(HERE / name, name)
        zf.writestr("config.json", json.dumps(config, indent=1) + "\n")
        zf.writestr("secret", shared_secret() + "\n")
    print(path)
    print("sha256", hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
