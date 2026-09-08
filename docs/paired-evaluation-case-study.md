# Case study: separating execution from correctness

## The engineering question

When an agent command finishes successfully, what has actually succeeded: the request, the task, the tool protocol, or preservation of the workspace? A single success flag hides these distinctions and makes environment comparisons difficult to interpret.

A July 2026 experiment planned 48 calls in 24 paired trials across three read-only tasks. The design used paired A/B execution with order reversal, explicit tool-call limits, and separate acceptance dimensions. All 48 results were present. Environment labels below are generic.

## Aggregate observations

| Dimension | Environment A | Environment B |
| --- | ---: | ---: |
| Request/transport completed | 24 / 24 | 24 / 24 |
| Task correct | 19 / 24 | 19 / 24 |
| Strict acceptance | 18 / 24 | 19 / 24 |

Of the 24 strict-acceptance pairs, 16 passed in both environments, two passed only in A, three passed only in B, and three failed in both. Equal aggregate task-correctness counts also concealed different failures: 17 task pairs passed in both, two only in A, two only in B, and three in neither.

These counts are transcribed from the retained local experiment report. The raw experiment logs and original environment configuration are not distributed here; the public repository cannot independently reproduce that historical run. The bundled synthetic demo is a separate 12-attempt evaluation-path test, not a reconstruction of these measurements.

## Decisions that mattered

**Register expected failure precisely.** One diagnosis task deliberately began with a known failing baseline. Its exception required the specified exit status and registered failure output. A policy refusal or an extra rerun did not satisfy that exception. This prevents a broad “nonzero is expected” rule from accepting unrelated faults.

**Keep planned attempts in the denominator.** A missing receipt is a missing result, not an attempt that disappears from the experiment. The public evaluator reports missing attempt IDs, incomplete pairs, and both planned-denominator and completed-only rates.

**Compare paired outcomes.** Marginal counts can be identical while different trials fail. Pair-level reporting preserves this information and avoids overstating a small difference in aggregate rates.

**Record environment covariates.** The two environments had different PowerShell language modes. That difference was recorded separately rather than attributed to network quality. This small, dependent sample does not establish a causal advantage for either environment or a general ranking of models.

**Separate evidence from enforcement.** A transcript can be evaluated without being trusted as a sandbox. The public kit checks supplied manifests, while a production runner must capture evidence independently and enforce the allowed tool and filesystem scope.

## What became reusable

The repository turns these acceptance decisions into strict JSON validation, pinned fixture digests, exact tool transcripts, typed answer matching, schedule binding, provenance separation, and adversarial tests. The reusable result is an inspectable evaluation contract, not a provider-specific wrapper or an asserted model leaderboard.
