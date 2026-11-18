# Local renderer: LFM3-1.7B in place of particle-1

Every T3 line and thread summary the voice speaks comes from a text model (see [spoken-acts.md](spoken-acts.md)). Today that model is `particle-ai/particle-1:free`. In the pre-call rehearsal it ran out of OpenRouter's free daily quota, and the voice went silent. This probe asked whether `LiquidAI/LFM3-1.7B` on the Desktop's RTX 3060 can take its place, with no quota, no network hop, and no slowdown to the voice.

It cannot, as served here. It fits in memory and is fast enough. But it passed 11 of 40 acts through both checks against particle-1's 36, and none of its 30 summary answers got past the slot check (measured). Keep `particle-1` as the default.

All numbers come from one GPU run on 2026-11-18, `python -m voice.research.local_renderer.bench`, under the GPU lease and `quiet_run`. Its outputs are in `$VOICE_HOME/evals/local-renderer/run1/`. No run made an OpenRouter call: the bench replaces the cloud client's transport with one that raises.

## Choose the renderer

`$VOICE_RENDERER` picks the lane's model (`voice/t3/speech.py`, `text_model`). The lane carries the picker as well as the renderer and the summaries, so all three move together.

- `particle-1`, the default, is the free cloud model.
- `local` is `LocalText`, a llama-server's OpenAI-compatible `/v1/chat/completions` at `$VOICE_RENDERER_URL` (default `http://127.0.0.1:8098`). No `:free` rule applies to it, since no request leaves the machine.

Start the server with `llama-server -m $VOICE_HOME/gguf/LFM3-1.7B-Q4_K_M.gguf -ngl 99 -np 1 -c 8192 -fa on`. Nothing starts it for you yet.

LFM3 reasons by default. With the chat template's `enable_thinking` set to false, it still opened `<think>` on render prompts and spent its whole 200-token budget there, returning an empty line (measured on a CPU smoke run). `LocalText` therefore bans the `<think>` token through `logit_bias`, which fixed it. The model can still write a literal `</think>`. One summary in the run did. It was refused, but for its length and a missing thread slot, not for the tag (measured), so a tag check has since been added: any line containing a think tag is refused as not plain speech, for every renderer. `LocalText` also fails loudly if it cannot build the `<think>` ban.

## Results against the bar

The bar was written before the run.

| Bar | Result | Met |
| --- | --- | --- |
| VRAM fits the 12 GB card with headroom | 5,949 MiB in use with all three models loaded | yes |
| LFM3-Audio first audio no more than 50 ms worse at the median | 1.7B idle: 6 ms faster per clip, no measurable difference. 1.7B rendering back to back: 136 ms slower | idle yes, rendering no |
| Summaries at least as true as particle-1 (28 of 29) and at least 27 of 30 useful | 0 of 30 spoken. The gists alone: 21 of 30 true, 28 of 30 useful | no |
| The act eval passes both checks in at least 36 of 40 | 11 of 40 | no |
| Render p95 under 700 ms | 659 ms | yes |

## Memory

Total GPU memory, measured with `nvidia-smi` after each load. Per-process figures read N/A under WDDM.

| Loaded | MiB |
| --- | --- |
| Nothing (other lanes' baseline) | 821 |
| LFM3-Audio-2B, `-c 32768 -kvu`, 3 slots (the live call's settings) | 3,181 |
| Plus d1-2B, `-ub 2048`, 1 slot | 4,592 |
| Plus LFM3-1.7B Q4_K_M, `-c 8192 -fa on`, 1 slot | 5,949 |
| Peak during the evals | 5,959 |

The 1.7B adds 1,357 MiB and starts in 1.4 s (measured). That leaves about 6,300 MiB free on the card's 12,288 (measured peak 5,959 MiB).

## Voice slowdown

The bench streams each of the 10 synthetic clips from the m1 dress rehearsal (LFM3's own TTS) into an LFM3-Audio realtime session and times `response.create` to the first audio delta. It runs each clip once with the 1.7B not loaded, then twice idle and twice rendering, alternating clip by clip, then once more with the 1.7B stopped. "Rendering" means the 1.7B answers recorded render and summary prompts back to back for the whole reply. That load is the lane's worst case, since the voice calls the renderer only when it has T3 news to say.

| Condition | Trials | Median first audio | Range |
| --- | --- | --- | --- |
| 1.7B not loaded, before and after | 19 | 180 ms | 121 to 307 ms |
| 1.7B loaded, idle | 20 | 185 ms | 65 to 328 ms |
| 1.7B rendering back to back | 20 | 327 ms | 150 to 407 ms |

- The first trial of the run took 4.1 s while LFM3 warmed up, so it is left out of the first row (measured).
- **Idle costs nothing.** Per clip, idle ran a median 6 ms faster than not loaded, and the not-loaded runs drifted 5 ms between before and after (measured). That is no measurable difference.
- **Rendering costs 136 ms.** Paired by clip and round, rendering was slower in 19 of 20 pairs, by a median of 136 ms and at most 217 ms (measured). The whole reply took a median 1.73 s to finish against 0.80 s idle. Both models then share the GPU's compute (inferred; no profile was taken).
- Beside an LFM3 reply, the 1.7B's own calls slowed to a median 470 ms and a p95 of 1,423 ms (measured, 82 calls).

## Render latency

The 1.7B answered 100 requests in the evals with LFM3-Audio and d1 loaded but quiet: median 318 ms, p95 659 ms, max 871 ms (measured).

| Request | Median | p95 |
| --- | --- | --- |
| Act renders (40) | 220 ms | 322 ms |
| Summaries (30) | 411 ms | 711 ms |
| Status renders (30) | 448 ms | 666 ms |

A least-squares fit over the 100 calls gives 19 ms, plus 0.13 ms per prompt token, plus 5.2 ms per output token (measured). The median call read 346 prompt tokens and wrote 48 output tokens, so writing output takes about 250 of its 318 ms (inferred from the fit). Decoding bounds the latency, and the 1.7B writes long lines. A line of particle-1's length, 9 to 25 tokens in the rehearsal, would take about 110 to 190 ms by the same fit (inferred).

For comparison, particle-1's 375 cached calls in the unit 11 evals had a median of 545 ms and a p95 of 1,489 ms (measured, from the cache records' own `ms`). Its seven render calls in the dress rehearsal took 400 to 679 ms (measured).

## First audio in the live path

In the dress rehearsal, the T3 turns took 2.10 to 3.24 s from the end of speech to first audio, and particle-1 worded each line in 400 to 679 ms of that (measured). The voice waits on the render, then the decider's check, then LFM3's TTS. Swapping in the 1.7B's act render, at a median of 220 ms, would cut about 0.2 to 0.4 s per line. Turn 001, for example, would go from 2.42 s to about 2.16 s (inferred, the rehearsal's time minus particle-1's call plus the 1.7B's median). That saving holds only for a line that passes. 29 of 40 act lines did not, and a refused line goes to LFM3's note path or is said nothing at all, which costs far more than 0.3 s.

## Quality

Both evals reused unit 11's frozen inputs, its code slot check and its decider check over Liquid's hosted d1.2. The comparison is particle-1's round 5 (PR #62).

### Acts: 11 of 40

| Refused by | Lines |
| --- | --- |
| Code's slot check | 19 |
| The decider alone | 10 |

Of the 19 that code refused, 16 were too long and 9 left out a slot. Several did both (measured). The 1.7B narrates the request back ("I see you want to stop the work..."), paraphrases names (it turned a title of the form "X: A vs B" into "X about A versus B"), and invents ("I can't reach you on the T3 clamp thread" for `unreachable`) (measured).

8 of the 11 lines that passed end with a question nobody asked, such as "What's next?" (measured). Particle-1 did that on none of its 36. Neither check looks for it, so those 8 would be spoken.

### Summaries: none spoken

Code refused all 30 status lines (measured). The gists ran a median of 42 words against the prompt's limit of 25. Many quoted the last reply in markdown or read out code, and some left out the thread's name. I hand-scored each gist against its frozen record with round 5's rubric, to see what the 1.7B would have said had its line passed:

- 21 of 30 were true. Three of the nine untrue ones mixed up who acted. They said the person had not opened a PR, that "you" would open one, and that "your" lease request was queued, where each record says the thread did it (measured). Another read a JSON format example as a real result.
- 28 of 30 answered the question.
- 3 of 30 were 25 words or fewer.

Round 5's particle-1 passed 29 of 30, and 28 of those 29 were true (PR #62).

### What the evals cannot see

These are the same blind spots as unit 11. Neither check reads the records, so only the hand scores judge truth, and one reader wrote them. Both evals run once per state at temperature 0, so they give no spread. The model card recommends temperature 0.1, top-k 50 and repeat penalty 1.1. Changing them would not fix the narration or the length (guess).

## Recommendation

Do not switch. Keep `$VOICE_RENDERER` at `particle-1`. The quota problem stays open.

- A local fallback while particle-1 cools down would get about 11 of 40 lines through instead of none. But 8 of those 11 add a question nobody asked, which neither check catches (measured above). It is not worth wiring up without a check for that.
- Making a local model work would need prompts written for it, with examples and a hard word limit, or a larger model. Liquid's card says LFM3-1.7B is made to be fine-tuned. `LFM2.5-2.6B` would fit in the 6,300 MiB left (guess). Each of these options needs its own eval round.

## Code

- `voice/t3/speech.py` adds `LocalText` and `text_model`. `voice/live/t3_tasks.py`'s `default_model` builds the lane's model through `text_model`.
- `python -m voice.t3.eval.summary_eval run|acts --dir DIR --renderer local` runs unit 11's evals through the local renderer and writes each call's latency to `ledger-run.json` or `ledger-acts.json`.
- `voice/research/local_renderer/bench.py` produced every number here.
- The hand scores and the PM's sample come from `$VOICE_HOME/evals/local-renderer/write_scores.py`. The frozen data names real threads, so it stays outside the repo.
