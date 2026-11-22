# Fork environment

One command brings the fork's separate T3 environment up, and one takes it down. The tool is `voice/t3/fork_env.py`, standard library only. It is Windows-only: process checks use Get-CimInstance and taskkill, and secret files use icacls.

## Why a separate environment

The seam runs inside the fork's own server. That server runs on the Desktop as a separate environment with its own T3 home, linked from Mason's apps in Settings > Connections. Fork code never writes his daily database. This page describes the tool that makes that environment repeatable.

## Use

Run from the repo root, as a module. Run as a script, `voice/t3/types.py` shadows Python's own `types` module and the tool fails on import.

```text
python -m voice.t3.fork_env up                 # create the home once, then start the server
python -m voice.t3.fork_env status             # up or down, plus ports, home, sandbox project, and its model
python -m voice.t3.fork_env down               # stop only the recorded server, then clear the pid file
python -m voice.t3.fork_env token              # mint the voice session while the environment is down
python -m voice.t3.fork_env pair               # mint a pairing URL while the environment is up
python -m voice.t3.fork_env model codex/gpt-x  # set the sandbox's default model while the environment is down
```

Paths hang off `VOICE_HOME`, default `~/voice`. The T3 home is `$VOICE_HOME/t3-fork-home`. The sandbox repo is `$VOICE_HOME/t3-fork-sandbox`. Pass `--home` or `--sandbox` to move either one. A `--home` equal to or under `~/.t3` or `$T3CODE_HOME` is refused, so the tool cannot be pointed at the daily install.

## What up does

First it runs the idempotent setup with the server stopped. It creates the sandbox repo with one commit, registers it as a project, and writes `voice-seam.json` once. A second run creates nothing new. When the state file is lost but the project already exists, setup recovers the project id from the database by workspace root and writes the state at once. Then it starts `vp run dev` with `--home-dir` and a fixed port, records the pid with the process creation time, and waits for both the TCP port and the server runtime file, so `pair` works right after `up`.

The API port is 3779. This differs from Mason's daily server on 3773 and from the dev runner's own range. Up fails clearly when the port is taken. `VOICE_FORK_PORT` moves it, so the whole-call gate's own sandbox can run while the daily sandbox holds 3779. The web port is derived from the dev runner's offset and shifts when the base ports are taken. Status shows it as `web`, and the pairing link uses it. The link expires after 5 minutes, so run `pair` just before linking the environment in Mason's apps.

The run mode is the dev server. The free `Replay (dev)` provider only registers on a dev server, since it needs a dev web URL in its config. A plain `t3 serve` would skip it. The dev runner seeds nothing here, since up passes `--no-seed`. The fork home starts empty except for the sandbox project.

## Model

A voice launch runs on the sandbox project's default model. Setup gives a new home `devReplay/showcase`, the free Replay (dev) agent, and tests keep it. `model <provider instance>/<model>` sets another one in the fork home's `userdata/settings.json`, under the sandbox's project override. The provider instance is the id T3 shows for the provider, such as `codex` or `claudeAgent`. The command refuses while the environment is up or another server uses the home, because the server reads its settings at start. A later `up` keeps the chosen model. `model devReplay/showcase` puts Replay back.

## Safety

Before `down` kills anything, before `up` reports already up, and before `token` refuses, the tool confirms the recorded pid is the server it started. It compares the live process creation time against the stamp in `fork-env.json` and checks the command line for `--home-dir` and this home. A pid that fails either check is stale: the file is cleared and the environment reads as down, so a recycled pid can never be killed. Down then checks the taskkill exit code and clears the pid file only after the pid is gone and the port is free. Any other outcome keeps the file and reports the failure.

## Secrets

`token` mints one bearer session with subject `voice` and scope `orchestration:read`, following the design. It refuses while the environment is up, because CLI writes to a running server's database are unverified. It writes the token to `t3-seam-token` in the home with user-only permissions, and never prints it. A mint failure prints only a fixed message and the exit code, never the CLI output.

`pair` retries the mint briefly, then writes one pairing URL for the running server to `pairing-url.txt` with user-only permissions. It never prints the URL and never opens it. The PM hands the link to Mason.

Secret files are written to a temp file in the same directory first. That file gets the user-only ACL, then it is renamed over the target, so a crash never leaves a half-written secret behind. An icacls failure is reported as a warning.

## Files

The home holds `fork-env.json` (port, sandbox project id, server pid and creation stamp), `fork-env.pid`, `fork-env.log`, `voice-seam.json`, `t3-seam-token`, and `pairing-url.txt`. The T3 runtime state lives under `userdata`, as usual. Nothing is ever written to `~/.t3`, and nothing is ever symlinked there.

## Ceiling

`voice-seam.json` caps the voice at `auto-accept-edits` for the sandbox project only, and launches there run in the sandbox's root. `VoiceSeamConfig` in `apps/server/src/voiceSeam/ceiling.ts` defines the file, and `voice/t3/seam.schema.json` carries the same schema for Python. Setup writes the file once and never overwrites it, so a home set up earlier keeps its old file. The `workspace` and `maxWatches` keys are optional, so that file still loads. Each new socket reads the file again. A missing file, or one that does not decode, answers 404 and turns the seam off.
