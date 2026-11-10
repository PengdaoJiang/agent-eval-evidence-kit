# Recover the current assignment, then read its exact inputs

A persistent conversation can contain an old startup instruction, several formal
assignments and many checkpoints. Resuming the conversation alone does not tell
a worker which assignment is authoritative. `phase_runtime/` and
`context_runtime/` extract the corresponding control and reading mechanisms from
the maintained research workflow.

## Two writers and two monotonically ordered streams

The controller owns the registration root; the worker owns a disjoint progress
root. Each registration binds the original assignment, exact formal order,
observed native thread/turn and an epoch. Each progress record binds that epoch,
an expected sequence, an immutable checkpoint and the next action.

```text
controller's native observation + original assignment + formal order
      -> immutable epoch registration (compare expected epoch)
worker -> immutable checkpoint and progress (compare expected sequence)
resume -> current epoch + latest valid checkpoint + next action
delivery + controller's independent terminal observation
      -> current formal identity valid
      -> separate scientific review and resource-release decisions
```

Selection follows a contiguous, hash-linked receipt sequence, not modification
times or a mutable "latest" pointer. A missing registration is a pending control
record. Recovery never substitutes the old startup instruction. A continuation
preserves the original assignment and carries its current checkpoint into the
new epoch; it does not manufacture a new owner.

Continuity evidence is recursively append-only. Existing scalar values keep
their JSON types, old lists remain exact prefixes, and old keys cannot disappear.
Closure adds evidence beside the original process/resource references. Replacing
the entire resource list with an empty list cannot falsely imply release.

`phase_control.py` checks the caller's native identity and loads an exact selected
release. A worker cannot register another worker, select a sibling's recovery
context or validate controller deliveries. `phase_guard.py` validates the local
protocol; the controller remains responsible for obtaining authentic native
observations. These checks are not OS authentication, a sandbox, or a change to
the model's native context compaction.

## Context navigation preserves the contract

`context_view.py` provides different views over the same selected records:

| View | What it returns | What it deliberately does not infer |
| --- | --- | --- |
| Worker | Goal, owned roots, exact document identities and every top-level field pointer | That a navigation view replaces the scientific contract |
| Input index | Existing records filtered by exact role or path substring, with omitted field names | New access rights or the contents of referenced files |
| Section/core | The complete selected JSON field or document, including unknown fields | A shortened scientific premise or recursive reference traversal |
| Supervisor | Active rows, recorded ready rows, unconsumed terminal events, pending transactions and cursors | Fresh dependency readiness or scientific acceptance |
| Architect | Route groups plus the current control view, with stale route snapshots marked | That a historical "running" label proves a live worker |

A large field is returned completely. This reader reduces selection scope; it is
not a hard byte-budget pager. Every listed scientific field, including unfamiliar
ones, remains part of the reading obligation. Index results explicitly identify
the fields omitted from the navigation response so qualifiers are not silently
lost. Unknown queue states remain visible instead of being folded into history.

The source rollout measured a smaller serialized control response and recorded
adoption by the supervisor. That observation is not a token-cost or throughput
claim. The public tests instead check exact identities, cross-owner rejection,
unknown-field preservation, complete sections and stale-state presentation.

## Reproduce

From the repository root, run `python -B verify_control.py`. Tests use synthetic
orders, native observations and caller identifiers. They create no live chats.
The additional wrapper tests exercise caller separation and changed-release
rejection through the extracted entry point.

For a real deployment, `PHASE_POLICY` selects a local policy with an exact release
descriptor, controller owner and registry root; the schema is described in
[`phase_runtime/API.json`](../phase_runtime/API.json). Do not copy a historical
release hash into a changed checkout. The context reader uses `PHASE_PROJECT_ROOT`,
`PHASE_ARCHITECT_THREAD` and `PHASE_SUPERVISOR_THREAD` for deployment routing.
`CODEX_THREAD_ID` must come from the actual caller environment. The offline tests'
fixture identities are not live identities.

The source schemas are retained to preserve interoperability. Adaptations replace
machine paths and fixed owner IDs with deployment parameters and relocate test
scratch. Original scientific inputs, task histories and private verifier data
are not included. A valid phase identity alone never accepts a scientific result.
