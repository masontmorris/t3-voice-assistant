# Stage 2 scenario fixtures

Each file is one utterance. `events` is the ordered input stream. A `transcript` event replaces the whole utterance text. ASR partials have no punctuation, and the final revision has punctuation. A `delegate` event is the voice model's delegate tool submitting a task to the shared task service.

`expected` was written by hand before any model run. Each expected task has these keys:

- `match`: words that the task's own segment text must all contain, ignoring case. Matching is one-to-one, in order.
- `status`: the accepted final statuses.
- `depends_on`: the accepted dependency lists, as expected keys. If the key is absent, the runner does not check dependencies.
- `revises`: the expected key of the task that this task replaces.
- `sources`: the exact set of submitters. If the key is absent, the only accepted source is `orchestrator`.
- `never_dispatched`: the task must never dispatch.
- `dispatch_only_on_final`: the task may dispatch only on the final transcript.
- `instruction_contains`: words that the dispatched instruction must contain.

A scenario passes when every expected task matches and passes its checks, no task dispatches twice, and no unexpected task dispatches or stays live.
