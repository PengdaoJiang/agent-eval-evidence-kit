# Durable agent execution ownership

These modules are extracted from an operating document reconstruction and independent-review application. They retain the actual file-backed registration, process identity, persistence and conflict behavior rather than replacing it with an in-memory demonstration.

The useful separation is between a stable business assignment and a replaceable execution attempt. An exited process does not establish that its result was delivered; an old worker must not be able to write through a successor's registration.

| Concern | Implementation | Observable behavior |
| --- | --- | --- |
| One effective writer | `lib/paper-writer-registry.mjs` | Atomic acquisition; generation and token checks; live-child checks; explicit stopped-owner recovery |
| Source and blind-review pools | `lib/cli-pool-settings.mjs` | Independent persisted caps; revision conflicts; maintenance pause overrides admission |
| Exclusive scientific tool | `lib/wolfram-registry.mjs` | Atomic occupancy, actor identity and token-bound release; no automatic ownership stealing on expiry |
| Process registration | `lib/runner-registration.mjs` | Bounded launcher/runner handshake; identity checked before commitment |
| Durable records | `lib/io.mjs` | Atomic replacement, immutable records, safe relative paths and bounded Windows sharing-conflict retries |

Run with Node.js 20 or newer:

```sh
node --test runtime-kernel/tests/*.test.mjs
```

The writer tests preserve the source application's original cases with portable module-root binding. The unrelated CLI-launch test is excluded because launchers and live configuration are not exported. Additional pool/registration tests are public release support. All examples use explicitly synthetic identities, not actual execution receipts.

The default native process probes target Windows. The tests inject probes and do not inspect machine processes, launch models, call Wolfram or use credentials. These are cooperative control-plane primitives, not an adversarial sandbox. The original service's queue scheduler, UI, compiler configuration, human decisions, manuscript data and private runtime records are not included. Passing these tests is not evidence of full-service acceptance or document correctness.

Code is covered by the repository MIT license. Node.js, Codex CLI and Wolfram are separately licensed tools; none is redistributed here.
