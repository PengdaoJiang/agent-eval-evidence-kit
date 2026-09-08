# Agent Evaluation Evidence Kit

Source-derived tooling for paired **Agent execution evaluation**: freeze tasks and schedules, collect tool-event traces, recover interrupted experiments, score independent failure dimensions, and analyze paired outcomes.

Built from an ongoing experimental project, this repository includes the collector, two-host orchestrator, remote bootstrap, primary and supplemental analyzers, task fixtures, policy rules, and regression tests. The central engineering question is: **when an agent run finishes, what evidence establishes that it completed the right task under the intended constraints?**

For a code-focused introduction, start with [three failure cases](docs/engineering-cases.md). For the execution path, read the [orchestrator](scripts/agent_eval_orchestrator.py), [collector](scripts/agent_eval_probe.py), then [analyzer](scripts/analyze_agent_eval.py). The smaller provider-neutral `evalkit/` is a separate introductory API.

## Start here — offline, no account required

Python 3.10+, standard library only:

```sh
python -B -m unittest discover -s tests -v
python -B scripts/agent_eval_orchestrator.py --prepare-only --run-dir planned-run
python -B scripts/offline_replay.py --out replay-output
python -B scripts/offline_replay.py --omit-last --out missing-receipt-output
python -B scripts/analyze_agent_eval_supplemental.py --run-dir replay-output --bootstrap-resamples 1000
```

Use a new output directory for each run. Planning does not call Codex, SSH, or read/copy credentials. Replay generates **synthetic** event receipts and runs the real analyzer, including duplicate-tool, wrong-answer, transport-failure and missing-receipt cases. Generated outcomes are pipeline tests, not measured model performance. Supplemental statistics require the complete registered paired schedule and reject incomplete evidence.

## Engineering design

| Layer | Implementation | Responsibility |
| --- | --- | --- |
| Task contracts | `fixtures/agent_eval/`, `agent_eval_rules/` | Three staged read-only tasks, exact read order, strict final-answer schemas |
| Collection | `scripts/agent_eval_probe.py` | Timestamped JSONL events, stdout/stderr, tool lifecycle, timeout, workspace manifests |
| Orchestration | `scripts/agent_eval_orchestrator.py` | Seeded AB/BA schedule, serial execution, frozen source hashes, atomic receipts, resumable attempts |
| Remote staging | `scripts/agent_eval_remote_bootstrap.py` | Bounded Windows SSH bootstrap and host-local isolated CLI configuration |
| Acceptance | `scripts/analyze_agent_eval.py` | Task answer, tool protocol, transport, workspace changes, expected-failure exception |
| Inference | `scripts/analyze_agent_eval_supplemental.py` | Paired outcomes, exact inference, order effects, resampling, lifecycle and usage diagnostics |

The design uses four configurable model IDs × three fixtures × two order-balanced repetitions × two environments. An intentionally failing unit-test baseline is allowed exactly once and only with the expected failure signature. A repeated or unrelated failure is not excused. Missing receipts remain in the planned denominator.

## Live experiments — explicit opt-in

The source-derived live transport targets **two Windows environments** with Python, OpenSSH and an independently authenticated Codex CLI on each. Model IDs, SSH alias, Python path and remote experiment root are parameters, not embedded machine configuration. The `config-a` … `config-d` defaults are offline labels, not valid model IDs.

Read [the live-run guide](docs/live-run-guide.md) before using `--allow-live`. It authorizes host-local credential staging and network/model execution, which may consume account quota. No credential is transferred between hosts. Raw run directories must remain private.

## Evidence and interpretation

- Answer correctness, tool adherence, execution completion and workspace changes are separate measurements. Their conjunction is a strict completion criterion, not a universal model-quality score.
- Workspace manifests cover the isolated fixture, not the whole machine. Post-run transcript checks are not a sandbox or an attestation system.
- Freeze code, inputs and scoring before execution. A checksum detects drift; it does not authenticate an untrusted run supplied by someone else.
- Order balancing and paired analysis do not make an uncontrolled environment comparison causal. Small samples, dependence and host differences still matter.
- The supplemental analyzer imports the frozen scoring module: analyze only runs and frozen code you trust.

## 中文说明

从既有 Agent 实验工程整理的评测子系统，保留任务编排、执行采集、失败分类、结果恢复、配对分析和任务样例。重点是把执行过程变成可检查的工程证据：区分答对、遵守工具约束和成功完成传输，保留中断与缺失结果，避免只看成功率。开发过程使用 Codex 辅助实现和迭代。机器与模型通过参数配置，历史原始日志和凭据不公开。

## Further reading

- [Architecture and release scope](docs/architecture-and-release.md)
- [Live-run guide](docs/live-run-guide.md)
- [Historical paired experiment](docs/paired-evaluation-case-study.md) — aggregate historical observations, separate from generated replay
- [Introductory trace contract](docs/trace-contract.md) — applies to the smaller `evalkit/` API

## License

[MIT](LICENSE). Codex CLI is an external dependency and is not redistributed. See [third-party notices](THIRD_PARTY_NOTICES.md).
