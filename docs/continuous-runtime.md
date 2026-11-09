# Exact continuation across long agent tasks

Long-running work needs more than a loop that resubmits a prompt. The runtime
must know which owner/session is active, which input version a tool actually
used, whether a request was accepted before a connection failed, and whether a
terminal receipt covers every phase and tool event.

The November additions contain two actual implementations:

* `cli_parallel/` is a finite Codex CLI launcher with isolated job directories,
  a frozen policy, real thread identifiers, cancellation and durable partial
  results. Its default is a plan; execution is explicit. It does not grade its
  own output or promise a continuously resident scheduler.
* `harness_runtime/` contains the DeepSeek Harness continuation state machine,
  exact selected-input executor, versioned base/delta store, execution ledger,
  and the registered constructor/reviewer repair-round protocol.

## State and evidence

```text
registered job + exact owner/session + input catalog
       -> persisted operation intent
       -> native phase events / immutable receipt
       -> checkpoint
       -> optional compaction with separate usage accounting
       -> next authorized phase
       -> exact-cut terminal confirmation
       -> frozen candidate -> independent review -> next registered repair round
```

`continuation_v2.mjs` and `run_headless_v2.mjs` preserve the current sequence,
phase chain, prompt identity and operation ownership. A lost acknowledgement is
an uncertain intent; it is reconciled instead of automatically resubmitted.
`run_headless_v2_1.mjs` finalizes only an idle exact native cut, after the observer
connection is closed. It retains every original phase receipt.

The `astra_loop_*` modules extend this contract to registered repair rounds.
Each round binds the immediate predecessor's frozen candidate and the public
feedback/order. Model aliases in the module names describe the source route;
they do not mean a particular model was called by the public tests. Private
reviewer inputs do not become constructor inputs.

`selected_execution_v2.py` records exact code, compiler, spec and input versions
before deterministic execution. `versioned_inputs_v2.py` keeps selected immutable
bases plus JSON deltas; reconstruction rejects missing or drifted dependencies.
`execution_ledger_v2.py` independently reconstructs the per-invocation evidence
from frozen records. Return code zero alone is not scientific acceptance.

`usage_metrics.mjs` distinguishes exact totals, observed partial values and
unknown fields. Duplicate event sequences do not multiply usage; failed attempts
and compaction remain visible. Token consumption is accounting, not a quality score.

## Run locally

Python 3.10+ and Node.js 22+, no package installation needed for offline tests:

```sh
python -B verify_harness.py
```

Tests create synthetic workspaces and mock the native transport, compiler and
model execution. They cover interrupted submissions, duplicate authorizations,
owner conflicts, changed history, stale checkpoints, changed inputs, storage
reconstruction, missing usage and premature finalization. The wrapper uses a
canonical temporary path so Windows short-name aliases are not mistaken for
redirected production roots.

For the finite CLI launcher, read `cli_parallel/README.md` and
`cli_parallel/OPERATING-GUIDE.md`; its offline tests never run Codex.

## Live integration boundary

The native HTTP/WebSocket adapter remains in the extraction, but a deployment
must configure `DSH_LAB`, `DSH_LOG_ROOT`, `DSH_BOOTSTRAP_LOG` (registered loop)
and `DSH_PACKAGE_JSON` for its existing local Harness installation. Exact
session/job/catalog/authorization records must be prepared by that deployment.
The sample workspace UUID is synthetic. No live sessions, bootstrap logs,
credentials or private SDK package are included. This repository does not
install a service, create a model account or start a research task on import.

The native adapter reflects the inspected source installation's loopback
protocol. Public tests qualify the state machine with fake transport; they do
not certify every Harness version or continuous uptime. File ownership checks
are contract enforcement among cooperating processes, not an OS sandbox.

## Provenance and adaptations

The original core implementations and regression cases were extracted from the
maintained research workspace. Changes are limited to deployment identities and
paths, temporary test roots, two fixture registrations that formerly read the
live registry, and public packaging. Exact source dependency pins, when present,
refer to the adapted public bytes. The CLI reference package came from the
generic October handoff and retains its own provenance and checksum list.

No model rankings, original task prompts, candidate engineering sources,
vendor code or historical session records are redistributed. The runtime is
MIT-licensed with this repository. External Codex, Harness and WebSocket
packages keep their own licenses and are not vendored here.
