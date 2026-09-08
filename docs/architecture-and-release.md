# Architecture and public release scope

The first release provided a small provider-neutral evaluator. This release adds the existing project's complete Agent-evaluation subsystem, including its regression tests, rather than describing the introductory demo as a full platform.

## Source organization

The collector, orchestrator, remote bootstrap, primary/supplemental analyzers, three fixtures and corresponding tests are source-derived. Public changes parameterize environment/model configuration, generalize identifiers, require explicit live-execution consent and separate credential-free planning from live preflight. `offline_replay.py` and its tests are new public integration examples.

The broader parent project's network collectors, external endpoints, raw historical runs, private machine inventory and account information are outside this repository. No unrelated application or company source tree is represented as open sourced.

## Experiment lifecycle

1. Freeze the schedule, fixture tree, rules, collector, orchestrator, remote helper and scorer.
2. In live mode, prepare a separate per-experiment CLI home on each host and validate policy behavior and versions before model calls.
3. Execute one call at a time, preserving adjacent paired calls and reversing their order within each model/task cell.
4. Collect events and atomic result files. Bind each result to the scheduled identity and source hashes.
5. On interruption, first recover any existing remote receipt. An unresolved in-flight attempt is not silently retried; a new attempt needs explicit review and opt-in.
6. Run the frozen primary scorer, then optional supplemental statistics. Preserve missingness and audit attempt history.

The orchestrator resumes experiments, not model conversations. Administrative recovery and task retry have different meanings and should not be pooled into one success statistic.

## Reviewable choices

- F1 joins and orders manifest-selected records; F2 diagnoses two known defects without editing; F3 applies inclusion, deduplication and enrichment rules to generated incident records.
- Shell wrappers are normalized, but extra arguments and extra calls remain violations. Exact negative-test evidence is required for the permitted red baseline.
- Unknown JSONL items remain available for audit. Started-without-terminal and duplicate-terminal events are tracked separately from task correctness.
- Before/after snapshots measure fixture files. They do not certify that no other host path changed.
- Supplemental analysis validates complete schedule/manifest identities and file hashes before importing the trusted frozen scorer. It is not a safe viewer for arbitrary downloaded executable evidence.

## Reproducibility

Ordinary tests are offline and use temporary directories or synthetic data. Public replay uses actual fixtures and the primary scorer, labels receipts synthetic, and introduces controlled failures. It is separate from historical experiment results.

Generated receipts, schedules, manifests, CSV and reports may contain local paths and runtime details. Keep them out of public commits unless separately reviewed. CI runs synthetic workflows only and has no model credentials.
