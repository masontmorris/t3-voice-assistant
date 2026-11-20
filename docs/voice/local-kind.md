# Local kind question: fictional follow-up replay

The local adapter adds examples to the request and chitchat options when the same segment has a T3 tool question. The hosted request is unchanged. This uses the model's kind decision; a tool pick does not override kind. No model-generated response metadata is saved in the local holdout fixture.

## Measured tuning results

We replayed 45 decision calls from the hosted d1.2 follow-up fixture (`voice/t3/fixtures/followup_decisions.json`) through local d1-2B. The hosted flow selected the bodies; local saw the same state and comparable questions, with hosted-only typed questions removed because llama.cpp cannot serve them. Counts are answers, not independent conversations. Local versus hosted agreement was: kind 32/45, readiness 23/45, side effect 44/45, tool 34/45, day 37/45, break position 2/2, and finished 2/2. Some local tool answers were right while kind was chitchat; other tool answers were wrong, so tool choice alone is not a reliable replacement.

On those calls, changing the local kind question gave the following kind agreement, divided by hosted kind. The examples were chosen before scoring the new holdout.

| Local wording                                | Hosted request | Hosted other |
| -------------------------------------------- | -------------: | -----------: |
| Original                                     |          21/32 |        11/13 |
| Short criteria                               |          28/32 |         6/13 |
| Examples in criteria                         |          29/32 |        12/13 |
| Reversed option order                        |          19/32 |        11/13 |
| Instruction saying questions can be requests |          25/32 |        10/13 |

These figures do not measure actual dispatch or prove improvement on unseen callers. Both models may be wrong, and repeated calls from one case are correlated.

## Measured holdout, once

The 48 fictional utterances in `voice/t3/eval/local_kind_holdout.jsonl` were written before the wording experiments. With the selected wording, each ran once through the end-of-turn path with a fictional recent thread in context. The recorded local answers are in `local_kind_local_answers.json`. Reaching the labeled tool (and no other T3 tool) was: thread questions 6/12, starts 6/6, tells 3/6, stops 4/6, weather 3/6; 12/12 chat controls routed nowhere. This is not yet reliable enough to claim the goal is met. The misses included kind, readiness and tool-confidence failures. No model or decision logic was changed after seeing these results.

The holdout was not scored on hosted: no paid calls were made. A PM with authorized hosted access can record it with `python -m voice.t3.eval.record_local_kind liquid`; this makes paid calls and writes a separate hosted fixture. Local recording uses `python -m voice.t3.eval.record_local_kind` with a leased running server and `VOICE_D1_URL` set. It records answer fields only, not the server's model path.
