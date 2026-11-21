# Real read-only tools in the prototype

Measured on 2026-11-16 (UTC). Each claim says whether it is measured, inferred, or a guess.

The prototype now answers weather and time requests with real lookups. Weather comes from Open-Meteo. The time comes from the reference MCP server `mcp-server-time` over stdio. Every lookup in the test runs returned the right result. Every reply that carried a result stated it correctly: 12 of 12 over three passes of the three clips that make lookups (measured, read by hand and checked against the lookup's own summary). A lookup takes 0.47 s for the time and 0.59 s for the weather (medians of 10, measured). The voice spoke the result 1.63 s after the user stopped talking (median of 12, measured). Most of that time goes to waiting for the first reply to finish, not to the lookup (measured, see [Where the time goes](#where-the-time-goes)).

Side-effect tasks are still held and never run. Read-only tasks that no tool covers, such as the sales report or the calendar, still run on the mock worker.

## How a task reaches a tool

The registry lives in `voice/tools/`, apart from `voice/prototype/`:

- `registry.py` has the registry. `Registry.route` gives the first tool whose parser accepts the task's instruction, or none. `Registry.run` runs the call under a 5 s timeout and never raises. Its `ToolResult` has the tool, the arguments, an outcome (`ok`, `failed`, or `timeout`), a summary the voice may speak, the latency, and a detail record.
- `lookups.py` has the two tools. Each one is a word-rule parser plus a call. The place is the capitalized name after "in", "for", or "at". When the request names no place, the tool uses `VOICE_DEFAULT_PLACE`. When that is unset too, the lookup fails and says that it needs a place.
- `open_meteo.py` sends the two Open-Meteo requests. A geocoding request sends only the place name. A forecast request sends only the coordinates that geocoding returned.
- `mcp.py` is a minimal MCP client over stdio. It does the `initialize` handshake and then sends `tools/call` requests one at a time. If a response comes after its call timed out, the client drops it.

The weather tool geocodes the place, fetches a 7-day forecast in Fahrenheit, and picks today, tomorrow, a weekday, or the weekend. The time tool geocodes the place to get its IANA time zone, then calls `get_current_time` on the MCP server. The MCP call is the only part of the time lookup that runs locally, and it takes 1.7 ms (median of 10, measured). The rest is the geocoding request.

The prototype hook is in `voice/prototype/tasks.py`. When the service starts a read-only task, it asks the registry for a route. A routed task runs its lookup on a worker thread and finishes when the lookup returns. It finishes as `completed` with the result, or as the new status `failed` with the reason as its summary. A task with no route runs on the mock worker, as before. Segmenter tasks and voice `delegate` tasks both come through the dispatch log, so they share this one hook. The execution record keeps the tool, the arguments, the outcome, the summary, and the latency. `run.py` starts the MCP server once per run and waits on running lookups as well as on mock deadlines. It also tells the voice what `failed` means.

## Results per clip

Each clip ran in one pass, and the three clips that make lookups ran in two more passes. Times are seconds, one value per pass. "Heard" is when the first audio of the reply that carried the result was generated, counted from the end of the clip's audio.

| Clip | Task the segmenter made | Tool and arguments | Lookup | Heard | Reply states the result |
| --- | --- | --- | --- | --- | --- |
| `11_tools` | The weather request | weather, Denver, weekend | 0.62, 0.62, 0.61 | 0.56, 1.81, 1.64 | Yes, 3 of 3 |
| `11_tools` | The time request | time, Tokyo | 0.48, 0.50, 0.49 | 2.32, 1.81, 1.64 | Yes, 3 of 3 |
| `01_single` | The weather request | weather, Denver, tomorrow | 0.60, 0.60, 0.60 | 1.38, 1.89, 1.41 | Yes, 3 of 3 |
| `07_implicit` | None | None | None | None | No lookup ran |
| `10_noisy` | None | None | None | None | No lookup ran |
| `single` (synthetic) | The time request | time, Tokyo | 0.47, 0.48, 0.47 | 1.41, 1.63, 1.27 | Yes, 3 of 3 |
| `two_request`, `correction`, `long` (synthetic) | As before | None. These tasks have no tool, and they ran on the mock or were held. | None | None | Not applicable |

All values are measured. Notes on the table:

- On clip `11_tools` the segmenter split the utterance into its two requests in every pass (measured). In pass 1 it dispatched the weather task 1.36 s before the audio ended, so the first reply already stated the forecast. In passes 2 and 3 it dispatched both tasks on the final transcript. The decisions differed because pass 1 missed the decider cache and made live calls at other transcript revisions (inferred from the timeline).
- Clips `07_implicit` and `10_noisy` produced no task, so no lookup ran. The hybrid misses these implicit requests, as [stage2-hybrid.md](stage2-hybrid.md) reported. Their replies invented no forecast (measured).
- Matching the result was literal except in one reply. On `01_single` pass 2, the reply said "no chance of rain" for a 0 percent chance of precipitation. That is correct (measured, read by hand).
- No first reply stated a result before its lookup had finished (measured, 12 replies read). One first reply on `11_tools` said that the time in Tokyo "is running". The status was right, but the wording is poor.
- The transcript of `11_tools` has the words of the script line in all three passes. It drops the comma after "weekend" (measured). No difference to report.
- `VOICE_DEFAULT_PLACE` was Denver for these runs, but no lookup used it. Every routed clip named its place.

## Where the time goes

The lookup is not what holds the result back. In 11 of the 12 deliveries, the lookup finished 0.59 to 0.93 s after the audio ended (measured). The update was heard 1.27 to 2.32 s after the audio ended (measured). The runner sends a `task_update` only after the first reply's `response.done`, and that came 1.18 to 2.24 s after the audio ended (measured from the timeline). From that moment, the update's first audio took 0.08 to 0.14 s (measured).

The table understates what a listener waits for. The first replies hold 6.4 to 13.2 s of speech (measured), and the prototype does not play audio. A listener would hear the update only after that speech ends (inferred).

The 0.47 s time lookup is one geocoding request plus a 1.7 ms MCP call (measured). So a place-to-time-zone cache would remove most of a repeat time lookup (inferred). The MCP server starts once per run, in 1.00 to 1.33 s (measured, 4 starts). Startup does not count toward any lookup.

## Calls, failures, and cost

- 30 bench lookups and 12 in-run lookups all returned `ok` (measured). The offline tests cover the failure paths: an unknown place, no place and no default, a timeout, a network error, an MCP tool error, and a lookup that never returns.
- The runs made 5 live decider calls, all on `11_tools`, all with HTTP 200 and no 429 (measured, from the run's call ledger). This lane has its own decider cache and call ledger, so it does not use another lane's budget. The cache started as a copy of the prototype's cached answers.
- Open-Meteo's free API allows 10,000 calls per day for non-commercial use (from its terms page). These runs made under 100 calls (measured: 2 per weather lookup, 1 per time lookup, plus the bench and the fixture recording). The MCP server needs no network. No paid service was used.

## Limits

- The parsers are word rules written for these clips. A lowercase place name, a place after another preposition such as "on", or a request with both weather and time in one task gets no lookup, or only the first tool (inferred from the rules). This is enough for development tests, not for use.
- A task gets at most one tool call.
- The timings come from 3 passes on one machine, with the decider cache warm in passes 2 and 3. The spread between passes, 0.5 s on the update, is larger than any tool-side change would be (measured).
- Weather results change by the hour, so the offline tests check the replayed recording, not today's forecast.

## Open decisions for Mason

- Should the first reply wait for a lookup that is almost done? With a hold of up to 0.8 s after the snapshot, all 12 results would have been in the first reply (inferred from the lookup finish times). The first audio would then come about 1 s after the audio ends, against 0.30 s now (median of 9 first replies, measured; the 1 s is inferred). The alternative is a shorter first reply, so the update comes sooner after it.
- Should clips `07` and `10` get a lookup? That needs the segmenter to treat "I wonder if" as a request, which is Stage 2 work, not tool work.
- Is Denver the right default place? It is a test setting in the environment, not in the code.

## Reproduce

Install the time server in its own venv under `$VOICE_HOME` (46 MB, measured):

```
uv venv "$VOICE_HOME/venv-mcp-time" --python 3.14
uv pip install --python "$VOICE_HOME/venv-mcp-time/Scripts/python.exe" "mcp-server-time==2026.11.11"
```

To use a different server command, set `VOICE_MCP_TIME` to that command line.

Run the offline tests from the repo root. They replay `voice/tools/fixtures/recorded.json`, and the MCP client tests use a stand-in server. One test also runs the real time server when it is installed. That test needs no network.

```
python -m unittest discover -s voice/tools/tests -t .
python -m unittest discover -s voice/prototype/tests -t .
```

Run the prototype tests with the Stage 1 venv's Python, because `voice/prototype` imports `soundfile`.

To route and run one instruction for real, use the registry's command line:

```
python -m voice.tools.registry "What time is it in Tokyo right now?"
```

Run the clips as [prototype.md](prototype.md) describes, inside the GPU lease and `quiet_run.py`, with `VOICE_DEFAULT_PLACE` set:

```
python -m voice.prototype.run "$VOICE_HOME/audio/real/11_tools.wav" ... --out "$VOICE_HOME/evidence/real-tools/run1" --decider-cache "$VOICE_HOME/evidence/real-tools/decider-cache" --decider-budget 80 --port 8097
```

`--mock-tools` turns the lookups off. Then summarize the lookups and time the tools on their own:

```
python -m voice.research.tools.report "$VOICE_HOME/evidence/real-tools/run1" "$VOICE_HOME/evidence/real-tools/run2" "$VOICE_HOME/evidence/real-tools/run3"
python -m voice.research.tools.bench -n 10
```

To record the fixture again, run `python -m voice.tools.record`. It sends only the place names in its `PLACES` list.

## Files outside the repo

| Path | What | Size |
| --- | --- | --- |
| `$VOICE_HOME/venv-mcp-time` | The venv for `mcp-server-time` | 46 MB |
| `$VOICE_HOME/evidence/real-tools` | Run records, reply WAVs, logs, the bench output, and this lane's decider cache | 17 MB |
