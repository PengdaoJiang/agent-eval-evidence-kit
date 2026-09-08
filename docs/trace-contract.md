# Trace contract v1

This contract applies to the introductory `evalkit/` package. The source-derived CLI runner uses the event-receipt schema in `scripts/agent_eval_probe.py`; see the architecture guide for that workflow.

`schema_version` is integer 1. `attempt_id` binds the trace to a planned attempt. `fixture_id` selects a pinned built-in fixture; `fixture_sha256` hashes its canonical UTF-8 JSON. `synthetic` and `transport_completed` are booleans.

`tools` is an ordered list of `{command, exit_code, output}`. Commands are descriptive transcript values, never executed by the evaluator. The reference protocol requires exact command, exit status, output, count, and order. Production adapters should normalize paths, line endings, and volatile diagnostics **before** freezing their protocol; they should not weaken matching after seeing outcomes.

`answer` is a JSON value matched recursively with exact types (`true` is not `1`). Duplicate JSON keys, NaN/Infinity, unknown fields, unknown fixtures, and invalid digest/path formats are rejected.

`workspace_before` and `workspace_after` map canonical relative POSIX paths to lower-case SHA-256 hashes. Capture the complete declared scope with an independent trusted runner, including file additions and deletions. This contract does not attest permissions, symlink targets, files outside that scope, or unrecorded intermediate writes.

Invalid input is not a task failure: it is an ungradeable receipt and must be investigated. Aggregation refuses duplicate results, attempts outside the frozen schedule, fixture mismatches, or pooling synthetic and measured results. Missing planned receipts and incomplete pairs are reported explicitly.

For real experiments, record model/version, prompt and tool-schema hashes, runner revision, time window, environment covariates, timeout policy, and retry policy in a separate immutable experiment manifest. This example intentionally does not implement an online provider adapter.
