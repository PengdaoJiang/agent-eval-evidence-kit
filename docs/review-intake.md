# Independent review intake without repeated noise

`review_intake/results.py` comes from a working hardware-engineering review workflow. It archives the original reviewer response and coverage declaration without overwrite, then builds a concise index of new, changed and explicitly closed findings. The result is local data; it neither sends a review request nor runs an engineering tool.

The intake binds request, reviewer role, batch, snapshot and conversation identity. Repeated identical input is idempotent. Changed annotations cannot silently replace the archive. An omitted finding stays open with its original observed snapshot; disappearance from the next response is not closure. Explicit closure requires evidence. Module-boundary concerns, conditional reachability, integrated defects, telemetry and missing evidence remain distinct.

`tests/test_review_intake.py` uses newly authored synthetic reviews to demonstrate no-clobber intake, explicit closure, carry-forward state and delta-only reporting. It does not distribute original RTL, engineering reviews or protocol material.

For a complete local multi-role application with source/blind worker pools, human decisions and a browser interface, see [document-review-workbench](https://github.com/PengdaoJiang/document-review-workbench). The evaluator, small runtime kernel and full workbench remain separate deployable components.
