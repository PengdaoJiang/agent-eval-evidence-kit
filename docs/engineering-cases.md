# Engineering cases: evaluating the execution, not just the answer

These cases follow the source-derived runner and its public regression fixtures. The replay constructs synthetic evidence; historical observations are documented separately in the [paired experiment](paired-evaluation-case-study.md).

## A correct answer with the wrong execution

An agent can return the expected answer after an extra tool invocation. Treating that run as simply successful would lose the distinction between reasoning outcome and adherence to an execution contract.

[The collector](../scripts/agent_eval_probe.py) records tool lifecycle events and workspace manifests. [The analyzer](../scripts/analyze_agent_eval.py) grades task correctness, transport, tool protocol and workspace changes separately. The replay deliberately includes repeated commands and incorrect answers, and [the public workflow test](../tests/test_public_workflow.py) runs those receipts through the same analyzer used for live experiments.

Design choice: retain the component results rather than reduce every failure to one label. The strict acceptance result is their conjunction, not a universal intelligence score.

## A missing receipt must remain visible

Interrupted execution creates a reporting trap: analyzing only completed files can make the completion rate improve when a difficult run disappears.

[The orchestrator](../scripts/agent_eval_orchestrator.py) freezes the planned schedule, tracks attempts and writes receipts atomically. [The analyzer](../scripts/analyze_agent_eval.py) joins results against that schedule. The command below intentionally withholds the last receipt:

```sh
python -B scripts/offline_replay.py --omit-last --out missing-receipt-output
```

Use a fresh output directory and compare it with a complete replay. Missing results remain in the planned denominator and incomplete pairs are reported. The supplemental inference path requires complete registered evidence rather than silently dropping a pair.

## An expected failure is a narrow exception

The bug-diagnosis fixture starts with a deliberately failing unit test. A generic rule accepting any nonzero exit would also forgive a broken invocation, a policy refusal or an unrelated test failure.

[The grading tests](../tests/test_analyze_agent_eval.py) exercise the registered baseline signature and command sequence. The exception applies to the known failure exactly once. An extra execution or unrelated failure stays a violation.

This design makes deliberate negative baselines compatible with strict evaluation. Evidence checks still depend on the supplied trace: they do not replace a sandbox or authenticate an untrusted third party's run.
