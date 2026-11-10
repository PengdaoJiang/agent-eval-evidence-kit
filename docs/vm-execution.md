# A recoverable VM task boundary

[`vm_execution/tools/vmctl.py`](../vm_execution/tools/vmctl.py) is the actual SSH
task controller and embedded Linux guest runner extracted from an EDA experiment
environment. It separates task execution, guest lifetime and artifact publication.
The implementation is useful for tools whose working files, child processes and
machine state outlive a single model tool call.

## Identity survives disconnects; old identity does not survive rollback

A job binds a VM UUID, host-controlled epoch, guest boot ID, unique job ID and the
exact staged script. The original source is retained separately from its explicit
UTF-8/LF normalization for Bash. A task key prevents duplicate admission for the
same project/task while unrelated projects can hold separate leases.

An interrupted SSH call after dispatch leaves an `UNKNOWN` lease. `recover`
collects the existing job's receipt and artifacts; it never resubmits the script.
A changed VM epoch or boot ID rejects attachment to a new machine incarnation.
The host lifecycle provider and this client share an admission lock. Rollback
must exclude both active and unknown leases, and consume the old epoch before
attempting restoration, including a restoration that later fails.

## Process completion and evidence publication are separate

The embedded guest runner uses Linux child-subreaper ownership and records PID
start ticks. Cleanup rechecks identity before signaling descendants, reaps
zombies, and retains failure evidence when process inspection or receipt writing
fails. This is cooperative process ownership; a task must not delegate work to
an independent service and expect this runner to control it.

```text
admit + freeze script -> dispatch -> guest terminal receipt
        |                              |
        +-- lost connection -> UNKNOWN +-- recover original job
                                       |
                       stage logs, receipt and output archive
                                       |
                      verify boot/epoch again + publish export
                                       |
                         terminal result + RELEASED lease
```

An SSH exit code alone cannot close a lease. A passing receipt must agree with
the job identity, source hash, execution flags, exit status and empty owned
process tree. Artifacts are staged before publication. A failed or changed
re-export preserves already published evidence; an identical re-export reuses
its original publication. Export temporarily reacquires an active lease, closing
the race with a rollback during transfer.

The output TAR is inspected and retained **without extraction**. Absolute or
parent-traversal member paths are refused. The archive may still contain links;
callers must not treat this metadata inspection as a general safe-extraction API.

## Offline verification and deployment scope

`python -B verify_control.py` runs the original controller regressions against a
fake transport and mock guest processes. Cases cover a lost acknowledgement,
nonterminal or contradictory receipts, epoch changes before recovery or during
export, guest reboot, changed re-export, path escape, source normalization and
cleanup after a receipt-write failure. Tests never invoke SSH, start a VM, load
a vendor program or consume a model account.

Live use requires a **Windows host with OpenSSH**, a prepared Linux guest with
Python 3/Bash, pinned SSH host keys, a writable `/srv/experiments`, and a separately
provisioned host lifecycle provider following the shared-lock/lease/epoch contract.
`VM_LAB_ROOT` or `--lab-root` selects deployment state. `state/config.json` must
contain `vm_name`, `vm_id`, `epoch`, `host_ip`, `user`, `key_path` and `known_hosts`;
the key and known-hosts paths are local absolute files. No deployable credentials
or private network configuration are distributed.

The original environment used an administrator-controlled Hyper-V broker. The
broker installer, disk images, machine configuration, vendor software/licenses
and Windows/VMware deployment scripts are not part of this extraction. The
public artifact is the complete task controller and its guest runner, not a
one-command VM provisioner. Source deployment smoke tests demonstrated execution,
export and rollback/re-run; these are historical bounded checks, not an uptime
guarantee or proof of an entire chip-design workflow.

Only the default private lab path was changed to a configurable path. Existing
protocol names and original regression cases are retained. The code is covered
by this repository's MIT license; external software keeps its own terms.
