import * as Effect from "effect/Effect";
import * as SqlClient from "effect/sql/SqlClient";

/**
 * Voice calls get their own table, so no read of a thread's items, counts or positions can see one. A card's lines
 * sit apart from it, read only when the card is opened. A call recorded as a turn item before this moves here,
 * following the item before it, and its record event is dropped. The table may already exist: a fork database
 * ran this as migration 106 before upstream took that id, and is repaired by deleting that history row.
 */
export default Effect.gen(function* () {
  const sql = yield* SqlClient.SqlClient;

  yield* sql`
    CREATE TABLE IF NOT EXISTS orchestration_v2_projection_voice_calls (
      voice_call_id TEXT PRIMARY KEY,
      thread_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      started_at TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      lines_json TEXT NOT NULL
    )
  `;
  yield* sql`
    CREATE INDEX IF NOT EXISTS orchestration_v2_projection_voice_calls_thread_idx
    ON orchestration_v2_projection_voice_calls(thread_id, ordinal, started_at, voice_call_id)
  `;
  yield* sql`
    INSERT INTO orchestration_v2_projection_voice_calls
      (voice_call_id, thread_id, ordinal, started_at, payload_json, lines_json)
    SELECT turn_item_id, thread_id, MAX(ordinal - 1, 0),
      COALESCE(json_extract(payload_json, '$.startedAt'), ''),
      json_set(
        json_remove(payload_json, '$.lines'),
        '$.ordinal', MAX(ordinal - 1, 0),
        '$.lineCount', COALESCE(json_array_length(payload_json, '$.lines'), 0)
      ),
      COALESCE(json_extract(payload_json, '$.lines'), '[]')
    FROM orchestration_v2_projection_turn_items
    WHERE type = 'voice_call'
  `;
  yield* sql`DELETE FROM orchestration_v2_projection_turn_items WHERE type = 'voice_call'`;
  yield* sql`
    DELETE FROM orchestration_events
    WHERE event_type = 'turn-item.updated' AND json_extract(payload_json, '$.type') = 'voice_call'
  `;
});
