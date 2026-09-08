# Live-run guide

Live execution is optional. Offline tests, schedule preparation and replay require neither Codex nor an account.

## Requirements

- Two Windows environments, each with Python 3.10+, a compatible Codex CLI and an independently established CLI login.
- A user-configured OpenSSH alias. The runner does not create keys, change SSH policy or bypass host-key checking.
- Four model IDs accessible to your account. Availability is not implied by this repository.
- A dedicated remote experiment directory and an absolute Python path, using the restricted no-space Windows path syntax checked by the orchestrator.

The runner uses CLI JSON event output, an output schema and a read-only sandbox. Consult the [official Codex command reference](https://learn.chatgpt.com/docs/developer-commands#codex-exec) and the installed CLI's help. Policy compatibility is checked before live calls; do not disable checks to make an incompatible version appear supported.

## Explicit live mode

First prepare a schedule with actual values, then inspect it:

```text
python scripts/agent_eval_orchestrator.py --prepare-only --run-dir private-run --models MODEL_A MODEL_B MODEL_C MODEL_D --ssh-host YOUR_ALIAS --remote-python C:/Python/python.exe --remote-root C:/agent-eval/experiments
```

After reviewing the schedule, repeat the same parameters with `--resume --allow-live --preflight-only` instead of `--prepare-only`. This stages the experiment and checks both environments without model calls. Then repeat with `--resume --allow-live` and without `--preflight-only` to execute.

Do not literally use placeholder model IDs or paths. Keep seed, reasoning effort, timeout, model list and SSH identity consistent on resume. After code changes, use the frozen `private-run/artifacts/agent_eval_orchestrator.py`; the runner rejects source drift.

## Credentials and privacy

The source-derived live runner supports file-backed CLI authentication. It copies each host's existing local `.codex/auth.json` into that host's separate experiment home. `--allow-live` is the explicit opt-in; the remote helper separately requires `--stage-local-auth`. It does not copy credentials over SSH or put authentication bytes/hashes in result evidence. Keychain-only authentication requires separate operator setup; do not export credentials into the repository.

An isolated home still contains a real secret. Use a private operating-system account and restrictive directory permissions. Private logs can contain task text, tool output, account-status information and machine paths. Never upload an experiment home or raw run directory wholesale. Manage cleanup/revocation deliberately after use; this package does not erase credentials automatically.

`--allow-live` may consume model quota. There is no automatic unlimited retry. Investigate ambiguous in-flight attempts before using `--retry-ambiguous-running`. This public release was validated with offline tests and synthetic replay, not a new paid two-host benchmark.
