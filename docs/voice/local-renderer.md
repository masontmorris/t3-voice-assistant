# Local renderer: LFM3-1.7B in place of particle-1

Every T3 line and thread summary the voice speaks comes from a text model (see [spoken-acts.md](spoken-acts.md)). Today that model is `particle-ai/particle-1:free`. In the pre-call rehearsal it ran out of OpenRouter's free daily quota, and the voice went silent. This probe asked whether `LiquidAI/LFM3-1.7B` on the Desktop's RTX 3060 can take its place, with no quota, no network hop, and no slowdown to the voice.

It cannot, as served here. It fits in memory and is fast enough. But it passed 11 of 40 acts through both checks against particle-1's 36, and none of its 30 summary answers got past the slot check (measured). Keep `particle-1` as the default.

A second, fairer test on 2026-11-19 gave the 1.7B the summary job alone, with a prompt written for it, worked examples, the card's sampling, JSON output and up to 16k tokens of the thread's own messages. Its best input is the last reply the seam gives today, not the history, and its best prompt is true on 25 of 30 threads not read before (measured on three sets). A truth gate by the local d1-2B removes most of its false summaries at the cost of about a third of the true ones. That is now an option on the call page's summariser picker, not the default; see [The local summariser](#the-local-summariser). See [The fair summary test](#the-fair-summary-test).

The act numbers come from one GPU run on 2026-11-18, `python -m voice.research.local_renderer.bench`, under the GPU lease and `quiet_run`. Its outputs are in `$VOICE_HOME/evals/local-renderer/run1/`. No run made an OpenRouter call: the bench replaces the cloud client's transport with one that raises.

## Choose the renderer

`$VOICE_RENDERER` picks the lane's model for CLI tools (`voice/t3/speech.py`, `text_model`). On a live call, the summaries' model is chosen on the call page instead ([The local summariser](#the-local-summariser)), and the T3 lines stay with the local writer.

- `particle-1`, the default, is the free cloud model.
- `local` is `LocalText`, a llama-server's OpenAI-compatible `/v1/chat/completions` at `$VOICE_RENDERER_URL` (default `http://127.0.0.1:8098`). No `:free` rule applies to it, since no request leaves the machine.

LFM3 reasons by default. With the chat template's `enable_thinking` set to false, it still opened `<think>` on render prompts and spent its whole 200-token budget there, returning an empty line (measured on a CPU smoke run). `LocalText` therefore bans the `<think>` token through `logit_bias`, which fixed it. The template already prefills `<think></think>` when thinking is off, yet the model often writes a draft, a second `</think>` (one token), and then its answer: 28 of 30 summaries did once the prompt held worked examples (measured, 2026-11-19). Any line containing a think tag is refused as not plain speech, for every renderer. JSON-constrained output cannot emit the tag at all. `LocalText` also fails loudly if it cannot build the `<think>` ban.

## Results against the bar

The bar was written before the run.

| Bar                                                                              | Result                                                                                                | Met                    |
| -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ---------------------- |
| VRAM fits the 12 GB card with headroom                                           | 5,949 MiB in use with all three models loaded                                                         | yes                    |
| LFM3-Audio first audio no more than 50 ms worse at the median                    | 1.7B idle: 6 ms faster per clip, no measurable difference. 1.7B rendering back to back: 136 ms slower | idle yes, rendering no |
| Summaries at least as true as particle-1 (28 of 29) and at least 27 of 30 useful | 0 of 30 spoken. The gists alone: 21 of 30 true, 28 of 30 useful                                       | no                     |
| The act eval passes both checks in at least 36 of 40                             | 11 of 40                                                                                              | no                     |
| Render p95 under 700 ms                                                          | 659 ms                                                                                                | yes                    |

## Memory

Total GPU memory, measured with `nvidia-smi` after each load. Per-process figures read N/A under WDDM.

| Loaded                                                             | MiB   |
| ------------------------------------------------------------------ | ----- |
| Nothing (other lanes' baseline)                                    | 821   |
| LFM3-Audio-2B, `-c 32768 -kvu`, 3 slots (the live call's settings) | 3,181 |
| Plus d1-2B, `-ub 2048`, 1 slot                                     | 4,592 |
| Plus LFM3-1.7B Q4_K_M, `-c 8192 -fa on`, 1 slot                    | 5,949 |
| Peak during the evals                                              | 5,959 |

The 1.7B adds 1,357 MiB and starts in 1.4 s (measured). That leaves about 6,300 MiB free on the card's 12,288 (measured peak 5,959 MiB).

## Voice slowdown

The bench streams each of the 10 synthetic clips from the m1 dress rehearsal (LFM3's own TTS) into an LFM3-Audio realtime session and times `response.create` to the first audio delta. It runs each clip once with the 1.7B not loaded, then twice idle and twice rendering, alternating clip by clip, then once more with the 1.7B stopped. "Rendering" means the 1.7B answers recorded render and summary prompts back to back for the whole reply. That load is the lane's worst case, since the voice calls the renderer only when it has T3 news to say.

| Condition                         | Trials | Median first audio | Range         |
| --------------------------------- | ------ | ------------------ | ------------- |
| 1.7B not loaded, before and after | 19     | 180 ms             | 121 to 307 ms |
| 1.7B loaded, idle                 | 20     | 185 ms             | 65 to 328 ms  |
| 1.7B rendering back to back       | 20     | 327 ms             | 150 to 407 ms |

- The first trial of the run took 4.1 s while LFM3 warmed up, so it is left out of the first row (measured).
- **Idle costs nothing.** Per clip, idle ran a median 6 ms faster than not loaded, and the not-loaded runs drifted 5 ms between before and after (measured). That is no measurable difference.
- **Rendering costs 136 ms.** Paired by clip and round, rendering was slower in 19 of 20 pairs, by a median of 136 ms and at most 217 ms (measured). The whole reply took a median 1.73 s to finish against 0.80 s idle. Both models then share the GPU's compute (inferred; no profile was taken).
- Beside an LFM3 reply, the 1.7B's own calls slowed to a median 470 ms and a p95 of 1,423 ms (measured, 82 calls).

## Render latency

The 1.7B answered 100 requests in the evals with LFM3-Audio and d1 loaded but quiet: median 318 ms, p95 659 ms, max 871 ms (measured).

| Request             | Median | p95    |
| ------------------- | ------ | ------ |
| Act renders (40)    | 220 ms | 322 ms |
| Summaries (30)      | 411 ms | 711 ms |
| Status renders (30) | 448 ms | 666 ms |

A least-squares fit over the 100 calls gives 19 ms, plus 0.13 ms per prompt token, plus 5.2 ms per output token (measured). The median call read 346 prompt tokens and wrote 48 output tokens, so writing output takes about 250 of its 318 ms (inferred from the fit). Decoding bounds the latency, and the 1.7B writes long lines. A line of particle-1's length, 9 to 25 tokens in the rehearsal, would take about 110 to 190 ms by the same fit (inferred).

For comparison, particle-1's 375 cached calls in the unit 11 evals had a median of 545 ms and a p95 of 1,489 ms (measured, from the cache records' own `ms`). Its seven render calls in the dress rehearsal took 400 to 679 ms (measured).

## First audio in the live path

In the dress rehearsal, the T3 turns took 2.10 to 3.24 s from the end of speech to first audio, and particle-1 worded each line in 400 to 679 ms of that (measured). The voice waits on the render, then the decider's check, then LFM3's TTS. Swapping in the 1.7B's act render, at a median of 220 ms, would cut about 0.2 to 0.4 s per line. Turn 001, for example, would go from 2.42 s to about 2.16 s (inferred, the rehearsal's time minus particle-1's call plus the 1.7B's median). That saving holds only for a line that passes. 29 of 40 act lines did not, and a refused line goes to LFM3's note path or is said nothing at all, which costs far more than 0.3 s.

## Quality

Both evals reused unit 11's frozen inputs, its code slot check and its decider check over Liquid's hosted d1.2. The comparison is particle-1's round 5 (PR #62).

### Acts: 11 of 40

| Refused by        | Lines |
| ----------------- | ----- |
| Code's slot check | 19    |
| The decider alone | 10    |

Of the 19 that code refused, 16 were too long and 9 left out a slot. Several did both (measured). The 1.7B narrates the request back ("I see you want to stop the work..."), paraphrases names (it turned a title of the form "X: A vs B" into "X about A versus B"), and invents ("I can't reach you on the T3 clamp thread" for `unreachable`) (measured).

8 of the 11 lines that passed end with a question nobody asked, such as "What's next?" (measured). Particle-1 did that on none of its 36. Neither check looks for it, so those 8 would be spoken.

### Summaries: none spoken

Code refused all 30 status lines (measured). The gists ran a median of 42 words against the prompt's limit of 25. Many quoted the last reply in markdown or read out code, and some left out the thread's name. Hand-scored against the frozen records with round 5's rubric, 21 of 30 gists were true and 28 of 30 answered the question (measured). Round 5's particle-1 passed 29 of 30, and 28 of those 29 were true (PR #62).

These evals run once per state at temperature 0, so they give no spread, and one reader wrote the hand scores.

## The fair summary test

The act eval gave the 1.7B the cloud model's summary prompt, the last reply alone, temperature 0 and free text, and had it word the whole line. The 2026-11-19 test changes each of those on the same 30 frozen threads and asks the 1.7B only for the gist. The live writer still words the line. The bar and the scoring rule were written before any run, in `$VOICE_HOME/evals/local-summariser/PLAN.md`. Every number below comes from three GPU runs under the lease and `quiet_run`, `python -m voice.research.local_summariser.bench ladder|arms|voice`.

### Context window

LFM3-1.7B's context is 256K tokens, not 128K. The card says "256K tokens (input + output)", and the GGUF's `lfm3.context_length` is 262,144 (measured, read from the file).

### What the 1.7B read

The seam's read gives one message today, the last reply, cut at 1,200 characters (`apps/server/src/voiceSeam/VoiceSeam.ts`, `readThread`). It falls back to an earlier run's reply only when the latest run has none. It carries no request, no earlier reply and no tool output. `readThreadNow` already reads messages by run, so a `history` field would mean the user and assistant messages of the latest runs, cut, added to the read's schema in `protocol.ts`, `seam.schema.json`, `VoiceSeam.readThread`, the Python `ThreadState` and `summary_messages` (inferred from the code; not built).

For the test, the histories came from a read-only open of the T3 database, cut at each state's frozen moment. Each view is JSON: `thread`, `status`, `waiting_for`, `pending`, then `messages` oldest first as `{"from": "request" | "thread", "text"}`, and `left_out`. `from` names who wrote each message, because the act eval's 1.7B mixed up who acted. Tool calls and their output are left out. They are where code and paths come from, and the thread's own messages say what its tools found. The last-reply view is the seam's read. A budgeted view shows the last reply, the first request, then the newest messages, each whole, and cuts only the one that crosses the budget.

Most threads are small. 21 of 30 have their whole history under 4k tokens, so the 4k, 8k and 16k views differ on 9 threads only (measured).

### Output form, one lever at a time

Each step adds one change to the one before it, on the last-reply input, one run each. The form check is `acts.check` on the gist for a `thread_status` act without its thread slot (the writer says the name), plus at most 25 words.

| Step | Adds                                                                             | Form pass | Median words | Median ms |
| ---- | -------------------------------------------------------------------------------- | --------- | ------------ | --------- |
| S0   | The act eval's gists                                                             | 3 of 30   | 41.5         |           |
| S1   | The card's sampling: temperature 0.1, top-k 50, repeat penalty 1.1               | 2         | 47.5         | 431       |
| S2   | A system prompt with the conflicting lines removed                               | 14        | 23           | 214       |
| S3   | The thread view JSON, each field described                                       | 5         | 33.5         | 312       |
| S4   | Four worked examples on made-up threads, as earlier chat turns                   | 0         | 46           | 450       |
| S5   | A short instruction beside the input                                             | 7         | 32           | 313       |
| S6   | `max_tokens` 60                                                                  | 6         | 33.5         | 310       |
| S7   | JSON schema output: a `gist` string that cannot hold markdown or code characters | 27        | 19.5         | 257       |

- S2's gain came from removing conflicts. The old prompt said not to state the thread's status, then to say what a thread with no reply waits on, and it allowed code "unless asked".
- S3 to S6 failed mostly on the second `</think>` above. The draft and the answer both reached the line (measured, 28 of 30 at S4). Keeping only the text after the tag would have passed 20 and 21 of 30 at S5 and S6 (measured on the saved outputs).
- The schema's grammar cannot emit `<`, a backtick or `*`, so S7 has no tag and no markdown (no forbidden character in 930 JSON outputs over both sets). The arms below run at S7.

### Truth, form and speed per input

Three seeds per arm at S7. True and useful are blind hand scores with round 5's rubric, against the full history up to the frozen moment. Latency is with LFM3-Audio and d1-2B loaded and quiet.

| Input       | Prompt tokens, median (max) | True       | Useful     | Form pass  | All three  | Median, p95 ms |
| ----------- | --------------------------- | ---------- | ---------- | ---------- | ---------- | -------------- |
| Last reply  | 1,112 (1,400)               | 24, 25, 24 | 29, 29, 29 | 27, 28, 28 | 23, 23, 23 | 251, 414       |
| 1k history  | 2,098 (2,309)               | 18, 18, 19 | 26, 26, 26 | 23, 24, 24 | 15, 12, 15 | 347, 461       |
| 4k history  | 3,635 (6,131)               | 15, 14, 15 | 25, 24, 23 | 16, 19, 16 | 10, 10, 10 | 398, 858       |
| 8k history  | 3,635 (10,620)              | 15, 13, 15 | 24, 25, 24 | 14, 20, 16 | 8, 8, 8    | 361, 1,351     |
| 16k history | 3,635 (19,468)              | 17, 15, 17 | 24, 25, 24 | 14, 18, 17 | 9, 8, 9    | 368, 719       |

More history made the gists less true (measured). The failures it added, from the scorer's reasons:

- It read the request sent to the thread as the thread's own words, so the thread seemed to ask the person to do its task.
- It repeated an earlier round's result that the latest reply had replaced, as the current answer.
- Facts from a made-up example, such as "three of seven tables migrated", reached real gists in the history arms and never on the last reply.
- Longer gists ran into `max_tokens` 60 and left unclosed JSON, most often over commit hashes.

The last reply's false gists, five or six per seed, came from the same six threads. Two said the work was finished where the run finished with the work undone. The others gave a wrong reason or cause, overclaimed a mixed result, or named the wrong file.

### Speed

Over the 450 arm calls, latency fits 6.7 ms, plus 0.122 ms per prompt token the server evaluated, plus 6.4 ms per output token (measured). The act eval's fit was 0.13 and 5.2 ms. The prompt rate holds up to 18.8k tokens. A cold 18,766-token prompt took 2,357 ms to read and 2.59 s in all (measured).

llama-server keeps recent prompts and reuses their longest matching prefix, which makes a long, mostly unchanged history cheap on the second request. With one new message on an 18.7k-token view, it evaluated 1,036 tokens and answered in 461 ms against 2,591 ms cold. A new question on an unchanged view evaluated about 510 tokens in about 400 ms (measured, 10 long threads). About 500 tokens are read again even when nothing changed (measured; the hybrid model's restore points are the likely cause, inferred). The view puts the records before the question for this reason. The per-arm medians above include such reuse across seeds, so cold cost is the fit's.

### Memory

Total GPU memory with LFM3-Audio at the live call's settings, d1-2B, and the 1.7B at each context, measured with `nvidia-smi` after each load. Other lanes held 1,030 MiB before any load.

| 1.7B context                      | Total MiB | Free of 12,288 |
| --------------------------------- | --------- | -------------- |
| Not loaded (LFM3-Audio and d1-2B) | 4,802     | 7,486          |
| 8k                                | 6,134     | 6,154          |
| 16k                               | 6,238     | 6,050          |
| 32k                               | 6,446     | 5,842          |

### Voice slowdown at the last-reply size

The same clip harness as above, with the 1.7B at 8k answering last-reply summary requests back to back for the whole reply.

| Condition                     | Trials | Median first audio |
| ----------------------------- | ------ | ------------------ |
| 1.7B not loaded               | 10     | 186 ms             |
| 1.7B loaded, idle             | 20     | 185 ms             |
| 1.7B summarising back to back | 20     | 328 ms             |

Paired by clip and round, summarising was slower in 19 of 20 pairs, by a median of 140 ms and at most 295 ms (measured). That is the act eval's 136 ms again. The 1.7B's own calls slowed to a median of 491 ms and a p95 of 792 ms beside a reply.

### Through the live writer

Each gist went into its `thread_status` act, the LFM3-Audio writer worded the line, and the line had to pass `acts.check`, `check_names` and the decider's line check. The decider was the local d1-2B, not hosted d1.2, because these records may not leave the machine. Particle-1's round 5 gists got 23 of 30 lines spoken, and the 1.7B's got 24, 25 and 23 (measured). Most refusals on both were the writer leaving out the thread's name.

### Against the bar

| Bar                                                        | Result                                                 | Met |
| ---------------------------------------------------------- | ------------------------------------------------------ | --- |
| True, median of 3 at least 27 of 30, no run under 26       | 24 (24 to 25) on the last reply; 13 to 19 with history | no  |
| Useful, median at least 27                                 | 29                                                     | yes |
| Form, median at least 28                                   | 28                                                     | yes |
| Writer and decider pass at least particle-1's count less 2 | 24 against 23                                          | yes |
| Summary median at most 545 ms, p95 at most 1,000 ms, quiet | 251 ms, 414 ms                                         | yes |
| First audio no more than 50 ms worse while it summarises   | 140 ms worse                                           | no  |
| At least 2,000 MiB free                                    | 6,154 MiB at 8k                                        | yes |

### What this test cannot see

One reader wrote every hand score, blind to the arm, with one score per distinct gist. That reader read all 30 threads to score them, so they could not serve as both a tuning set and a test set, which is why the next round used new threads. The three seeds at temperature 0.1 barely differ on the last reply, so the spread is small by construction. The slowdown above is the worst case, a summary during a spoken reply.

### A second, unread set

A second set of 30 threads, none of them read before, was frozen the same way with `summary_eval freeze --seed 1119 --exclude`. It holds no waiting-on-you state, because the database had none left outside the first set's threads. The bar and the scoring rule were written before the run, in `$VOICE_HOME/evals/local-summariser/fresh/PLAN.md`. The rule adds two cases the first round scored the same way: a present-tense claim that a later message replaced is false, and so is a thread's "I" read as the assistant's. Particle-1 has no score on this set, because its gists would send these records off the machine. Round 5's 28 true of 29 spoken, on the first set, is the only cloud reference.

Three seeds per arm, last reply unless noted, blind hand scores.

| Arm                                                                                    | True       | Useful     | Form pass  | True and form | Median, p95 ms |
| -------------------------------------------------------------------------------------- | ---------- | ---------- | ---------- | ------------- | -------------- |
| S7, as before                                                                          | 25, 24, 23 | 30, 30, 30 | 29, 29, 26 | 24, 24, 21    | 282, 419       |
| S8, status left out                                                                    | 23, 23, 23 | 25, 25, 25 | 26, 25, 26 | 21, 21, 21    | 287, 433       |
| S9, an example of a run that finished with its work undone                             | 27, 27, 26 | 29, 30, 30 | 25, 26, 23 | 23, 23, 21    | 278, 382       |
| S10, both                                                                              | 25, 24, 24 | 25, 25, 25 | 26, 26, 26 | 23, 22, 22    | 276, 399       |
| R1, the thread's own replies only, about 1k tokens, latest marked current, no examples | 23, 20, 24 | 26, 27, 28 | 4, 3, 3    | 4, 3, 3       | 659, 882       |

- S7 scored 24 again, so the first set's number holds on new threads (measured).
- The undone example (S9) reached the truth bar, 27 at the median and no seed under 26. It gained on four threads, not only on unfinished work, and lost on two. It made gists longer, and every one of its extra form failures is a gist over 25 words, so it passed both checks no more often than S7 (measured).
- Leaving the status out made the gists less true and less useful. Without it, the 1.7B told 5 failed or interrupted runs as still going (measured).
- R1, built to avoid the history arms' failures, still scored under the last reply. Without examples it wrote long JSON, 83 to 99 output tokens at the median against its cap of 100, and so failed form on nearly every thread. More of the thread's history does not make this model's gists truer (measured, on both sets).
- R1 hit one HTTP 500 from llama-server's own output parser ("does not match the expected peg-native format"), which did not recur on the same request. The schema's character rule held on all 930 JSON outputs of both sets, but its `additionalProperties: false` did not: 24 outputs added keys after `gist`, all of them R1's, the one arm without examples (measured). The bench reads `gist` and ignores other keys, which is what any consumer should do; the live path has no JSON reader yet.

### Where the summary overlaps the voice

In 620 recorded T3 ask turns from rehearsals through the live server, the task that starts the summary appeared a median of 0.13 s after the speech ended, not mid-utterance (measured from each turn's timeline; the task's first revision is a lower bound on when the prefetch fired). The voice's first audio came a median of 1.26 s after the speech ended.

The overlap run timed LFM3-Audio's first audio on the clip harness with one S9 summary started 0.5 s before the speech ended, 0.13 s after it ended, or not at all, 20 trials each, paired by clip and round.

| Summary started               | Median first audio | Paired against none                              | Running when audio started |
| ----------------------------- | ------------------ | ------------------------------------------------ | -------------------------- |
| None                          | 172 ms             |                                                  |                            |
| 0.5 s before the speech ended | 77 ms              | faster in 19 of 20, median 88 ms                 | 0 of 20                    |
| 0.13 s after the speech ended | 274 ms             | slower in 17 of 20, median 83 ms, at most 197 ms | 16 of 20                   |

A summary that finished before the reply did not slow it. Why first audio was faster after one is not known; the GPU keeping its clocks up is a guess. A summary still running when the reply starts costs about 83 ms, and takes 627 ms itself against 278 ms quiet (measured).

With those summary times laid over the recorded turns, the summary would still be running at the voice's first audio on 6% of status-line turns and 7% of turns that confirm the thread first, at the median summary time, and on 20% and 17% at the p95 (inferred: recorded timelines with the measured local times substituted).

The 50 ms limit on first audio came from the act probe's bar, before any overlap was measured. Mason accepted the measured cost, about 83 ms on roughly 1 status turn in 15, on 2026-11-19, to be improved later, so it no longer decides whether the local summariser ships.

Starting the summary earlier would avoid the cost, since a summary that finished before the reply cost nothing. Two things stand in the way (measured on the recorded turns, inferred from the code). The decider marks the ask before the speech ends on only 105 of 620 turns, 83 of them at least 0.6 s early, so the prefetch usually fires after the speech ends. And a summary started from partial words was cached under the partial question, so the final question missed it and the summary ran again; that is now fixed ([A summary started early](#a-summary-started-early)). Getting the decider to mark the ask earlier is the larger change and is not made.

### A third, unread set

A third set of 30 unread threads, frozen the same way (`--seed 1121`, excluding both earlier sets), tested shorter S9s. The bar and the scoring rule were written first, in `$VOICE_HOME/evals/local-summariser/third/PLAN.md`, with the voice slowdown no longer in it. Three seeds each, last reply, blind hand scores.

| Arm                                                                             | True       | Useful     | Form pass  | True and form | Median ms |
| ------------------------------------------------------------------------------- | ---------- | ---------- | ---------- | ------------- | --------- |
| S9                                                                              | 25, 26, 27 | 28, 28, 28 | 27, 29, 29 | 21, 24, 25    | 283       |
| S11, at most 20 words asked for in the prompt, the instruction and the examples | 25, 25, 25 | 27, 27, 27 | 28, 29, 30 | 22, 22, 23    | 271       |
| S12, S11 with the grammar bounded at 25 words, the last ending a sentence       | 25, 25, 25 | 27, 27, 27 | 30, 30, 30 | 23, 23, 23    | 277       |

- Asking for 20 words fixed the length: S11 kept all but 3 of 90 gists within 25 words, and both S11 and S12 met the form bar (measured).
- Truth did not hold. S9 scored 26 at the median here against 27 on the second set, and the shorter arms 25. S9's median was 27 on the second unread set and 26 on the third, and S7's 24 on the second, so the bar of 27 sits at the top of the model's range, not inside it (measured).
- The false gists clustered on five threads in every arm: a reversed subject (who rejected what), a misread phrase, "committed" for uncommitted work, a failed run's next step told as still to come, and an invented limit.
- The grammar bound never cut a sentence or a word. Near the bound the model instead fused words to stay inside it, several run together with no spaces or joined by hyphens, in 3 of 90 gists, all at exactly 25 words; S9 and S11 never did (measured). A text-to-speech voice would read those as nonsense words, so a bound that counts spaces is not a safe length limit.

### A truth gate by the local d1-2B

The remaining false gists are misreadings of the reply, so a second local model checks each one before it is spoken. The local d1-2B gets the thread's title, status, pending prompt, the last reply the summariser read, and the gist, and answers one typed yes/no question. When the gate does not pass a gist, the voice says the thread's status with no summary, which says nothing false. The gate classifies and writes nothing. No request leaves the machine.

Three wordings were tried, each in its own request, on the existing gists and blind hand scores with no rescoring: whether the gist states something the last reply and status do not support, whether everything it says is supported, and whether it gets wrong who did what, whether the work is done, or what the reply says. The setting was chosen on sets 1 and 2 (S7 and S9 gists; set 1 has no S9 or S11) as the one that spoke the most true gists with at most 1 false gist per 30. That was the first wording at P(unsupported) 0.10. It was then fixed and read on sets 3 and 4, both held out. The plan is in `$VOICE_HOME/evals/local-summariser/gate/PLAN.md`.

| Held-out gists      | True spoken per 30 | False spoken per 30 | Status said bare per 30 | True gists kept |
| ------------------- | ------------------ | ------------------- | ----------------------- | --------------- |
| Set 3, S11, no gate | 25.0               | 5.0                 | 0.0                     | 100%            |
| Set 3, S11, gate    | 16.3               | 0.7                 | 13.0                    | 65%             |
| Set 4, S11, no gate | 25.0               | 5.0                 | 0.0                     | 100%            |
| Set 4, S11, gate    | 19.7               | 2.0                 | 8.3                     | 79%             |

- The gate caught 13 of 15 false gists on set 3 and 9 of 15 on set 4, at a median of 30 to 32 ms per gist and a p95 of 71 ms (measured).
- The plan's build bar was at most 1.2 false gists per 30 with two thirds of true gists kept. Set 3 missed it by one true gist (49 of 75 kept, 50 needed). Set 4 missed it on false gists, 2.0 per 30, twice the cloud model's 1 in 29 (measured).
- The d1-2B's answers sit low. The chosen threshold drops a gist at P(unsupported) 0.10, and at 0.35 it caught almost nothing on the tuning sets, so the gate is a weak signal used aggressively (measured on sets 1 and 2).

### A fourth unread set: reasoning

Liquid's prompting guide, thinking page and model page (all updated 2026-11-16 to 2026-11-18) recommend leaving LFM3-1.7B's reasoning on and leaving room for it in the output limit. Every arm above had it off, with `<think>` banned and 60 tokens out. The guide also says a JSON schema constrains only the answer when the server parses reasoning, which llama.cpp does by default. A CPU smoke run confirmed it on this build: the reasoning arrived in `reasoning_content` and the JSON gist in `content`.

S13 is S11 with reasoning on, no `<think>` ban, and a 4,096-token limit. It ran on a fourth set of 30 unread threads, frozen like the others (plan in `$VOICE_HOME/evals/local-summariser/fourth/PLAN.md`). Three seeds, blind hand scores.

| Arm               | True       | Useful     | Form pass  | Median, p95    | Output tokens, median |
| ----------------- | ---------- | ---------- | ---------- | -------------- | --------------------- |
| S11               | 25, 25, 25 | 29, 29, 30 | 27, 28, 28 | 266 ms, 380 ms | 30                    |
| S13, reasoning on | 25, 25, 23 | 29, 28, 29 | 27, 28, 27 | 5.7 s, 18.2 s  | 1,092                 |

- Reasoning did not make the gists truer. It fixed some misreadings and made others, on different threads, and 2 of 90 summaries reasoned to the 4,096-token limit and gave no answer (measured).
- It costs about 21 times the latency. A status line waiting on it would start 5 s later at the median (measured).
- The rest of the guide matched what was tested. The sampling was the model page's (temperature 0.1, top_k 50, repetition_penalty 1.1, also stored in the GGUF). llama-server applies the checkpoint's own chat template (seen through `/apply-template`). The worked examples were already real user and assistant turns through that template, not text inside the prompt. One difference remains: llama-server adds its own top_p 0.95 and min_p 0.05, which the model page does not list. At temperature 0.1 they are unlikely to matter (guess).

### A summary started early

A summary the prefetch starts from words still being spoken is cached under those words, and is reused only when the final question normalises to the same words (`Speech._summary`). When the final question differs, the summary runs again. Taking the early summary over for any final question about the same thread and read was tried and withdrawn: a summary written for one question would then answer a different one, and the truth gate checks support, not relevance. In 574 recorded T3 ask turns the words differed on 33 (inferred: the task's creation stands in for the prefetch), so a reuse that could tell whether an early summary answers the final question would save about 271 ms on those turns.

## The local summariser

The call page's summariser picker (PR #78) offers LFM3-1.7B as `lfm3-1.7b` whenever the live server runs it. The default stays the cloud model. Choosing the local one changes only who writes the thread summaries. The live writer still words the T3 lines.

- **What it writes.** `LocalText.summarise` sends S11 from `voice/t3/local_summary.py` (the prompt measured above, moved there unchanged and pinned to the research prompt by a test). It reads `gist` from the JSON and ignores any other key, since llama.cpp's grammar does not enforce `additionalProperties`. It then asks the gate. A gist the gate does not pass, a missing gist, and an unreachable gate all leave the status said with no summary.
- **What the caller hears**, per 30 status questions on unread threads (measured on sets 3 and 4): 16 to 20 with a true summary, 0.7 to 2 with a false one, and 8 to 13 with the status and no summary. Particle-1 gave 28 true and 1 false of 29 spoken on set 1.
- **Speed.** A summary takes a median of about 270 ms plus about 30 ms for the gate, with the three models loaded and quiet (measured). One still running when the voice starts to speak costs about 83 ms of first audio on roughly 1 status turn in 15. The 50 ms limit on first audio came from the act probe's bar. Mason accepted the 83 ms on 2026-11-19, to be improved later.
- **Starting and stopping.** `--local-text auto` (the default when the GGUF is present), or `on`, starts LFM3-1.7B on port 8099 with one slot at `-c 8192`, which holds S11's prompt of about 1.3k tokens with room. The gate needs a d1-2B: with `--decider d1` it opens a second connection to the decider's own server, and otherwise the live server starts one for it on port 8100 (`summary_gate_server`). Everything is loaded at start, so choosing the local summariser never waits on a load, and stops with the server. Under `auto`, if a local server fails to start, the call starts anyway on the cloud model and the picker does not offer the local one; `on` fails loudly. `--local-text off` starts neither and the picker does not offer it.
- **Memory.** LFM3-Audio at the call's settings plus the two servers totalled 6,134 MiB on a card where other lanes held 1,030 MiB, which is 2,743 MiB over LFM3-Audio alone (measured, run 3: the same three models). With `--decider d1` the gate shares the decider's d1-2B, so the fully local stack loads the same three models (inferred: the shared case was not measured).

## Recommendation

Keep the default on the cloud model. The local summariser is an option for when the cloud is unavailable or a fully local call matters more than detail.

- A local fallback while particle-1 cools down would get about 11 of 40 lines through instead of none. But 8 of those 11 add a question nobody asked, which neither check catches (measured above). It is not worth wiring up without a check for that.
- Do not give the summariser more history. Every history arm on both sets scored under the last reply (measured).
- Do not turn reasoning on for summaries. It did not lift truth and costs about 5 s (measured).
- Prompting has moved the 1.7B's truth from 21 (the act eval) to 25 of 30 on unread threads, and the gate trades about a third of the true summaries for removing most false ones. The next lever is fine-tuning, which Liquid's card invites. Particle-1's gists over many threads would be the training data. That needs its own brief.

## Code

- `voice/t3/speech.py` adds `LocalText`, `text_model` and `Lane.summarise`, which sends the local model its own form. `voice/t3/local_summary.py` holds that form and the gate. `voice/live/t3_tasks.py`'s `default_model` builds the lane's model through `text_model`.
- `python -m voice.t3.eval.summary_eval run|acts --dir DIR --renderer local` runs unit 11's evals through the local renderer and writes each call's latency to `ledger-run.json` or `ledger-acts.json`.
- `voice/research/local_renderer/bench.py` produced the act eval's numbers, and `voice/research/local_summariser/bench.py` the fair summary test's, `--set fresh` for the second set. `voice/research/local_summariser/view.py` holds the thread view, each ladder step's prompt and the form check.
- The hand scores come from `$VOICE_HOME/evals/local-renderer/write_scores.py` and `$VOICE_HOME/evals/local-summariser/tally.py` and `fresh/tally.py`. The frozen data names real threads, so it stays outside the repo.
