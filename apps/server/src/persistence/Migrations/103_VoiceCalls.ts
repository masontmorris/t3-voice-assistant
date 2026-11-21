import * as Effect from "effect/Effect";
import * as SqlClient from "effect/sql/SqlClient";

/**
 * Voice calls get their own table, so no read of a thread's items, counts or positions can see one. A call recorded
 * as a turn item before this moves here, following the item before it, and its record event is dropped.
 */
export default Effect.gen(function* () {
  const sql = yield* SqlClient.SqlClient;

  yield* sql`
    CREATE TABLE orchestration_v2_projection_voice_calls (
      voice_call_id TEXT PRIMARY KEY,
      thread_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      payload_json TEXT NOT NULL
    )
  `;
  yield* sql`
    CREATE INDEX orchestration_v2_projection_voice_calls_thread_idx
    ON orchestration_v2_projection_voice_calls(thread_id, ordinal, voice_call_id)
  `;
  yield* sql`
    INSERT INTO orchestration_v2_projection_voice_calls (voice_call_id, thread_id, ordinal, payload_json)
    SELECT turn_item_id, thread_id, MAX(ordinal - 1, 0),
      json_set(payload_json, '$.ordinal', MAX(ordinal - 1, 0))
    FROM orchestration_v2_projection_turn_items
    WHERE type = 'voice_call'
  `;
  yield* sql`DELETE FROM orchestration_v2_projection_turn_items WHERE type = 'voice_call'`;
  yield* sql`
    DELETE FROM orchestration_events
    WHERE event_type = 'turn-item.updated' AND json_extract(payload_json, '$.type') = 'voice_call'
  `;
});
