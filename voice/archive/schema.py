"""The loop archive format, `voice.loop.archive/1`.

One archive is one demo run, replayable without the live system:

    manifest.json     schema, duration, stems, clips, per-turn clock alignment, and the SHA-256 of every other file
    timeline.jsonl    one event per line, sorted by `t`, seconds on the replay clock (0 = the first recorded frame)
    waveform.json     RMS at 100 per second for the microphone and the assistant playback, for drawing
    stems/            microphone.wav, assistant-playback-NNN.wav and conversation.wav, as the client recorded them
    clips/            the server's per-turn input, reply and update WAVs, which the validator uses to check alignment

Every event has `t`, `seq` (its line order), `type`, `turn` and `src`: "server" for an event the server logged,
"client" for one the client logged, and "derived" for one the exporter inferred from logged state. EVENTS lists the
payload fields each type must carry. An event may also carry `audio`, a pointer the validator checks against a stem:
{"clip": path, "clip_offset_s": seconds into the clip, "stem": path, "at": replay seconds where that point sounds}.
"""

SCHEMA = "voice.loop.archive/1"

EVENTS = {
    "speech_start": ("turn",),
    "speech_end": ("turn",),
    "turn_end": ("turn", "trailing_ms", "forced"),
    "transcript_partial": ("turn", "revision", "text"),
    "transcript_final": ("turn", "text"),
    "decision_point": ("turn", "revision", "is_final", "segments", "decisions"),
    "task_created": ("turn", "task_id", "text", "sources"),
    "task_split": ("turn", "before", "after"),
    "task_merged": ("turn", "before", "after"),
    "gate_verdict": ("turn", "task_id", "score", "threshold", "passed", "weakest"),
    "task_status": ("turn", "task_id", "status"),
    "tool_call": ("turn", "name", "arguments", "result"),
    "handoff": ("turn", "final_transcript", "unreviewed_speech", "snapshot"),
    "lfm_action": ("turn", "calls", "text"),
    "reply_start": ("turn", "reply", "reply_id", "text"),
    "reply_end": ("turn", "reply", "reply_id", "played_s", "interrupted"),
    "barge_in": ("turn",),
    "backlog_reprompt": ("turn", "task_ids"),
}

SOURCES = ("server", "client", "derived")

ORDER = (
    ("speech_start", "speech_end", "turn"),
    ("speech_end", "turn_end", "turn"),
    ("transcript_final", "handoff", "turn"),
    ("turn_end", "handoff", "turn"),
    ("handoff", "lfm_action", "turn"),
    ("task_created", "task_status", "task_id"),
    ("reply_start", "reply_end", "reply_id"),
)
"""(earlier, later, key): for every key value, the first `earlier` event comes no later than the first `later` one."""

ALIGN_TOLERANCE_S = 0.020
MATCH_SCORE = 0.5
