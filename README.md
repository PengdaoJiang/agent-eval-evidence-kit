# Agent Execution and Evaluation

Tools for keeping long-running agent work bound to the correct assignment, inputs and execution instance. The repository combines maintained control components with an evaluation pipeline for diagnosing task, tool and environment failures.

## Read the execution path

| Problem | Implementation | Guide |
| --- | --- | --- |
| Recover the current formal assignment | `phase_runtime/` | [Epochs, checkpoints and caller identity](docs/phase-and-context.md) |
| Read the selected inputs without losing conditions | `context_runtime/` | [Role-specific context views](docs/phase-and-context.md#context-navigation-preserves-the-contract) |
| Continue a session after uncertain submission | `harness_runtime/` | [Continuous-task state machines](docs/continuous-runtime.md) |
| Execute in a VM and recover the original job | `vm_execution/` | [Leases, rollback identity and artifact publication](docs/vm-execution.md) |
| Explain why an agent run failed | `scripts/agent_eval_*.py` | [Three engineering cases](docs/engineering-cases.md) |

Each component has its own entry point. The guides describe their integration boundaries and retained source schemas. The complete review application lives in [document-review-workbench](https://github.com/PengdaoJiang/document-review-workbench).

## Run offline

Python 3.10+ and Node.js; the control suites use the standard library and synthetic transports.

```sh
python -B verify_control.py
python -B verify_harness.py
python -B -m unittest discover -s tests -v
python -B scripts/offline_replay.py --out replay-output
python -B scripts/analyze_agent_eval_supplemental.py --run-dir replay-output --bootstrap-resamples 1000
```

The replay creates synthetic execution receipts and runs the actual analyzer. It exercises missing results, duplicate calls, transport failure and task errors. Generated replay outcomes describe pipeline behavior; historical model observations have a [separate case study](docs/paired-evaluation-case-study.md).

## Mechanisms worth inspecting

- Formal registration and worker progress have separate writers and immutable hash-linked records. Recovery selects the current epoch and checkpoint.
- Context navigation retains source identities and unknown fields. Complete sections remain available for checking scientific or engineering conditions.
- An uncertain dispatch is reconciled before further execution. A VM recovery collects the existing job, and a changed epoch or boot identity invalidates stale recovery.
- A terminal process, a valid result and released resources are separate facts. Artifact export is staged and verified before publication.
- Evaluation freezes tasks, scoring and execution order, then separates answer correctness, tool adherence, transport and workspace changes.

Additional components include the finite [CLI launcher](cli_parallel/README.md), [writer-ownership kernel](runtime-kernel/README.md), and [independent-review intake](docs/review-intake.md). The small `evalkit/` introductory API has its own [trace contract](docs/trace-contract.md).

## Deployment and evidence

Offline commands do not start a model or VM. Live transports require an explicitly configured local CLI or prepared VM environment; see the [live-run guide](docs/live-run-guide.md) and [VM deployment scope](docs/vm-execution.md#offline-verification-and-deployment-scope). Protocol identity checks are distinct from OS authentication and sandboxing. Scientific acceptance requires its own review.

Source boundaries and dependency provenance are documented in [architecture and release scope](docs/architecture-and-release.md). Original private task histories and deployment credentials remain outside the distribution.

## License

[MIT](LICENSE). External tools retain their own terms; see [third-party notices](THIRD_PARTY_NOTICES.md).
