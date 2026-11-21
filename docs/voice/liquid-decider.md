# Liquid's hosted d1.2 as the decider

The non-local decider is Liquid AI's hosted `d1.2`, called over the System One decisions API. pplx-decider-flash on
OpenRouter remains selectable as `cloud`, and the local llama.cpp d1 as `d1`. The free pplx quota is exhausted, and
`d1.2` costs $0.02 per million input tokens with output free.

This page reports what was measured on 2026-11-16, on branch `voice/liquid-decider` from `4aa3cd7ddf`.

## The route

|          |                                                                                                     |
| -------- | --------------------------------------------------------------------------------------------------- |
| Endpoint | `POST https://api.liquid.ai/decisions/v1/systemone` with `Authorization: Bearer $LIQUID_API_KEY`    |
| Body     | `{model, state, questions}`, at most 128 questions per request                                      |
| Answer   | `answers` in TypeSafe's choice and noul shape, plus `usage {input_tokens, output_tokens, cost}`     |
| Key      | `LIQUID_API_KEY` in the env file named by `VOICE_ENV_FILE`, placed only in the Authorization header |

The segmenter keeps building its existing bodies. The Liquid client replaces the body's model field with `d1.2` on the
wire and keeps `state` and `questions` as they are, which is the same adapter the local decider uses
(`voice/local_decider/client.py`). A body with more than 128 questions is split and its answers merged, and the state is
billed once per request, so an ordinary hybrid call is one request (measured: the 94 recorded hybrid requests held 3 to
20 questions, median 10).

A body with no questions makes no request. Liquid rejects one with a 422 while pplx answers it with an empty answer set
(measured: one live turn sent an empty question set and drew a 422). The client answers that case itself with an empty
200 and charges nothing.

The choice `confidence` Liquid returns is already the margin the gate reads. On a probe, a three-way answer with
probabilities 0.349/0.333/0.318 came back as confidence 0.0229, which is `(p_max - 1/3) / (1 - 1/3)` to seven places
(measured). The replay re-checked that identity on 589 answers and every one matched, worst gap 2.2e-16 (measured). No
rescaling is applied.

## Spend guard

Every attempt appends the `usage.cost` the API reports to `$VOICE_HOME/evidence/liquid/calls.jsonl`, and the UTC day's
total refuses a further call once it reaches $1.00, which is about 50 million input tokens. The refusal writes
`SPEND_STOP` beside the ledger and raises, so a second process stops too.

A 429 is a rate limit. It backs off on `Retry-After` through the same pacer the pplx client uses and never writes a
billing stop. Only a 402 or a payment error writes `BILLING_STOP`.

Total for every call made this session, measured from `usage.cost`: **$0.00573** over 345 ledgered calls and 286,240
input tokens, plus one probe outside the ledger at $0.0000039. The $1.00 guard did not trip.

## Latency and agreement with pplx

`python -m voice.research.liquid_decider.replay --tag hybrid` sent the hybrid's 94 recorded decision requests to `d1.2` once
each, in call order, with no cache. Those are the 11 original scenarios, the 18 held-out scenarios and the 10 clips.
All 94 returned 200 (measured).

| Measurement                              | d1.2   | pplx     |
| ---------------------------------------- | ------ | -------- |
| Call latency, median                     | 138 ms | 98.9 ms  |
| Call latency, p95                        | 231 ms | 223.6 ms |
| Call latency, max                        | 444 ms | 642.2 ms |
| Median at 1 to 4 questions (11 calls)    | 168 ms |          |
| Median at 5 to 8 questions (30 calls)    | 142 ms |          |
| Median at 9 or more questions (53 calls) | 136 ms |          |

The pplx column is `timing.ms` from the same 94 cached records, so both columns are the same requests, the same bodies
and the same machine. d1.2's median is 39 ms slower and its tail is shorter (measured). The brief's 93 to 101 ms figure
and [local-decider.md](local-decider.md)'s 101 ms are on the same order.

Answers to the same questions (measured; a noul answer agrees when both sit on the same side of 0.5):

| Question      | Answers | Agreement |
| ------------- | ------- | --------- |
| `kind`        | 176     | 95.5%     |
| `ready`       | 176     | 68.2%     |
| `side_effect` | 176     | 93.2%     |
| `depends`     | 82      | 89.0%     |
| `corrects`    | 82      | 98.8%     |
| `finished`    | 70      | 77.1%     |
| `at`          | 70      | 55.7%     |
| `same_as`     | 3       | 100%      |
| all           | 835     | 84.1%     |

26 of the 94 requests matched pplx on every answer. `ready` is the weak one, and the gate scores it.

## The gate eval before and after

`python -m voice.research.segmentation.live_gate_eval --decider liquid --fresh` ran the 70 items three times each, 210 traces,
$0.00185 (measured). The pplx column is the committed trace set scored by the shipped rule, with no new calls.

| Split             | pplx accuracy | d1.2 accuracy | pplx false dispatch | d1.2 false dispatch | pplx false hold | d1.2 false hold |
| ----------------- | ------------- | ------------- | ------------------- | ------------------- | --------------- | --------------- |
| Train 46 items    | 96%           | 89%           | 0                   | 0                   | 2               | 5               |
| Held-out 24 items | 88%           | 83%           | 1                   | 0                   | 2               | 4               |

The gate is not what holds these items. Its score spread on the 23 items that reached it was median 0.094, mean 0.097,
max 0.265, and 3 items straddled the 0.40 bar (measured). The nine false holds break down as four whose `ready` answer
was `clarify`, so no task ever reached the gate, three held by the 0.80 side-effect bar at `ready` margins of 0.35 to
0.42, one at a `ready` margin of 0.29, and one whose `side_effect` answer sat at 0.49 and scored 0.02 (measured).

Two sweeps were run on the training items only, and held-out is reported beside them:

- Lowering the general threshold from 0.40 gains nothing. Train accuracy is 89% at every bar from 0.25 to 0.45, and
  held-out moves between 83% and 88% with no train evidence to choose (measured).
- Lowering the side-effect bar from 0.80 to 0.35 raises train accuracy to 96% with no false dispatch, and held-out does
  not move at all, 83% with 4 false holds (measured). No held-out item changes, so the gain is unproven on unseen data
  and it weakens the bar that stops a wrong send.

The shipped gate stays at 0.40 and 0.80 (decision, from that evidence).

## Mason's 37 turns

One live server from this branch with `--decider liquid --mock-tools`, in a `voice-liquid` GPU lease, and the real
client over loopback sending the 37 `turn_NNN_input.wav` clips in order. Driver and scorer are
`$VOICE_HOME/evidence/liquid/real-audio/`; records are in its `liquid/` directory. The whole replay took 16 minutes
(measured).

The 37 clips produced 40 server turns, so turn numbers do not line up with the recording's. 36 replay turns carried
words, and 34 of the recording's 35 spoken turns matched one by transcript (measured). Four replay turns carried no
words at all (`turn_009`, `turn_011`, `turn_012`, `turn_038`), the long utterance at the recording's `turn_010` came
back three times as two fragments and the full text, and one recording turn (`turn_035`) was never heard. The recording
itself has two wordless turns (`turn_008` and `turn_009`), so these belong to the audio path, not to the decider
(inferred).

| Measurement                                | d1.2     | pplx |
| ------------------------------------------ | -------- | ---- |
| Spoken turns with a transcript             | 36       | 35   |
| Recording turns matched                    | 34 of 35 |      |
| Tasks created in the whole call            | 12       | 13   |
| Matched turns whose task behaviour changed | 1        |      |

The one changed turn is the recording's `turn_003`, a question about the assistant's own state. pplx created one lookup
task for it, and d1.2 created none (measured). The utterance asks about the assistant rather than for work, and the
70-item eval's `about_assistant` items expect no dispatch, so the change reads as a correction rather than a regression
(judgment).

Real audio therefore shows one changed task decision across the 34 matched turns. Synthetic shows the wider agreement
numbers above. Real audio decides, and it does not reproduce the synthetic eval's false-hold pattern in this call.

## What still fails

- `ready` agreement is 68.2%, and the gate's false holds rise from 2 to 5 on train and from 2 to 4 on held-out
  (measured). d1.2 states readiness with less confidence than pplx, so the same bar holds more of its tasks.
- `at` agreement is 55.7% on break placement, worse than the local d1's 75.7% on the same requests (measured). The gate
  scores `ready` and `side_effect`, not `at`, but the hybrid's break handling reads it.
- The live audio path loses and duplicates turns on a clip replay (measured). Two late turns also waited 30 s for a
  final transcript, which is the ASR path, not the decider.
- The `decider_live_calls` metric counted pplx's ledger for every route, so the live replay's records say 0 while
  `decider_calls` reads 2, 6 and 4. Fixed after that run, not re-measured.
- There is no fallback to `cloud` when Liquid fails at run time. A route is chosen per run with `--decider`, and a
  failed call is recorded as a problem on the decision point, the way pplx's failures are.
