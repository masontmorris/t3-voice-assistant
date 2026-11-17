# Fork environment

One command brings the fork's separate T3 environment up, and one takes it down. The tool is `voice/t3/fork_env.py`, standard library only.

## Why a separate environment

The seam runs inside the fork's own server. That server runs on the Desktop as a separate environment with its own T3 home, linked from Mason's apps in Settings > Connections. Fork code never writes his daily database. This page describes the tool that makes that environment repeatable.

## Use

Run from the repo root.

```text
python voice/t3/fork_env.py up      # create the home once, then start the server
python voice/t3/fork_env.py status  # up or down, plus port, home, and sandbox project
python voice/t3/fork_env.py down    # stop only the recorded server, then clear the pid file
python voice/t3/fork_env.py token   # mint the voice session while the environment is down
python voice/t3/fork_env.py pair    # mint a pairing URL while the environment is up
```

Paths hang off `VOICE_HOME`, default `~/voice`. The T3 home is `$VOICE_HOME/t3-fork-home`. The sandbox repo is `$VOICE_HOME/t3-fork-sandbox`. Pass `--home` or `--sandbox` to move either one.

## What up does

First it runs the idempotent setup with the server stopped. It creates the sandbox repo with one commit, registers it as a project, and writes `voice-seam.json` once. A second run creates nothing new. Then it starts `vp run dev` with `--home-dir` and a fixed port, and records the pid.

The port is 3779. This differs from Mason's daily server on 3773 and from the dev runner's own range. Up fails clearly when the port is taken.

The run mode is the dev server. The free `Replay (dev)` provider only registers on a dev server, since it needs a dev web URL in its config. A plain `t3 serve` would skip it. The dev runner seeds nothing here, since up passes `--no-seed`. The fork home starts empty except for the sandbox project.

## Secrets

`token` mints one bearer session with subject `voice` and scope `orchestration:read`, following the design. It refuses while the environment is up, because CLI writes to a running server's database are unverified. It writes the token to `t3-seam-token` in the home with user-only permissions, and never prints it.

`pair` mints one pairing URL for the running server and writes it to `pairing-url.txt` with user-only permissions. It never prints the URL and never opens it. The PM hands the link to Mason.

## Files

The home holds `fork-env.json` (port and sandbox project id), `fork-env.pid`, `fork-env.log`, `voice-seam.json`, `t3-seam-token`, and `pairing-url.txt`. The T3 runtime state lives under `userdata`, as usual. Nothing is ever written to `~/.t3`, and nothing is ever symlinked there.

## Ceiling

`voice-seam.json` caps the voice at `auto-accept-edits` for the sandbox project only. Its schema still belongs to unit 4, so this file stays minimal until that unit merges.
