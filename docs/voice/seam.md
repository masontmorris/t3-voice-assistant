# Upstream files the fork changes

Every file this fork changes outside `voice/` and `docs/voice/` belongs in the table below with the reason it exists, because upstream also edits those files and each one adds merge work to the daily sync. Add a row before you push a change to an upstream file, and delete the row when the change goes away.

| Path | Reason |
| --- | --- |

`python voice/ci/check_seam.py` fails, naming the file, when a changed upstream file has no row here with a reason.
