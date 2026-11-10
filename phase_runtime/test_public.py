"""Public offline phase-continuity checks. All mutable fixtures stay in scratch."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parent
ENTRY = PROJECT / "phase_guard.py"
SCRATCH = PROJECT / ".scratch"
spec = importlib.util.spec_from_file_location("phase_guard_public", ENTRY)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PhaseChecks(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=SCRATCH))
        self.registry = self.root / "controller/registry"
        self.progress = self.root / "worker/progress"
        self.formal = self.write("inputs/formal.json", {"goal": "current formal task"})
        self.assignment = self.write("controller/assignment.json", {
            "job": "JOB/a002", "native_thread_id": "worker-A", "native_turn_id": "turn-1",
            "formal_work_order": self.formal})
        self.observation = self.observe("turn-1", "inProgress", "observation-1")
        self.base = {"operation": "register", "registry_root": str(self.registry),
                     "controller_owner": "supervisor", "thread_id": "worker-A", "job_id": "JOB", "attempt": "a002",
                     "expected_epoch": 0, "formal_turn_id": "turn-1", "assignment": self.assignment,
                     "formal_work_order": self.formal, "progress_root": str(self.progress),
                     "native_observation": self.observation,
                     "continuity": {"owner_thread_id": "worker-A", "open_tokens": ["existing-token"],
                                    "process_records": ["original-process-record"], "completed_work": ["already-built"]}}

    def tearDown(self):
        # Exact test-owned directory, proven inside this task's scratch root.
        target = self.root.resolve()
        self.assertTrue(target.is_relative_to(SCRATCH.resolve()))
        shutil.rmtree(target)

    def write(self, rel, value):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(guard.encoded(value))
        return guard.file_identity(path)

    def observe(self, turn, status, name="terminal"):
        return self.write("controller/" + name + ".json", {
            "schema": "rea.phase_guard.native_observation.v1", "source": "supervisor_native_tool",
            "controller_owner": "supervisor", "native_tool": "wait_threads", "thread_id": "worker-A",
            "turn_id": turn, "status": status})

    def register(self, **changes):
        return guard.run(dict(self.base, **changes))

    def checkpoint(self, name="cp1", **changes):
        return self.write("worker/" + name + ".json", dict({"schema": "test.checkpoint.v1", "job": "JOB/a002",
                         "owner": "worker-A", "formal_work_order_sha256": self.formal["sha256"]}, **changes))

    def publish(self, registered, sequence=0, checkpoint=None, **changes):
        return guard.run(dict({"operation": "publish-progress", "registry_root": str(self.registry),
                              "thread_id": "worker-A", "caller_owner": "worker-A", "epoch": registered["epoch"],
                              "binding_id": registered["binding_id"], "expected_sequence": sequence,
                              "checkpoint": checkpoint or self.checkpoint(), "stage": "CURRENT_BUILD_COMPLETE",
                              "next_action": "Run the remaining independent checks."}, **changes))

    def recover(self, **changes):
        return guard.run(dict({"operation": "recover", "registry_root": str(self.registry), "thread_id": "worker-A"}, **changes))

    def delivery(self, **changes):
        view = self.recover()
        payload = {"schema": "rea.phase_guard.formal_delivery.v1", "kind": "formal",
                   **{key: view[key] for key in ("thread_id", "job_id", "attempt", "epoch", "binding_id", "formal_turn_id")},
                   "formal_work_order_sha256": view["formal_work_order"]["sha256"],
                   "progress_sequence": view["anchor"]["sequence"], "checkpoint_sha256": view["anchor"]["checkpoint"]["sha256"],
                   "native_terminal": {"thread_id": "worker-A", "turn_id": view["formal_turn_id"], "status": "completed"}}
        payload.update(changes)
        return self.write("worker/delivery.json", payload)

    def validate(self, delivery=None, observation=None):
        return guard.run({"operation": "validate-delivery", "registry_root": str(self.registry), "thread_id": "worker-A",
                          "delivery": delivery or self.delivery(), "native_observation": observation or self.observe("turn-1", "completed")})

    def test_missing_never_replays_startup_or_releases(self):
        view = self.recover()
        self.assertEqual(view["status"], "CURRENT_FORMAL_ASSIGNMENT_REQUIRED")
        self.assertFalse(view["startup_fallback"] or view["owner_transfer"] or view["tokens_released"])

    def test_normal_registration_progress_delivery(self):
        reg = self.register()
        self.assertEqual(self.recover()["status"], "CURRENT_FORMAL_ASSIGNMENT_NEEDS_CHECKPOINT")
        self.publish(reg)
        view = self.recover()
        self.assertEqual(view["anchor"]["continuity"]["open_tokens"], ["existing-token"])
        result = self.validate()
        self.assertEqual(result["status"], "CURRENT_FORMAL_IDENTITY_VALID")
        self.assertFalse(result["research_accepted"])

    def test_stale_epoch_and_sequence_cas(self):
        reg = self.register()
        with self.assertRaises(guard.Refused):
            self.register()
        self.publish(reg)
        with self.assertRaises(guard.Refused):
            self.publish(reg)
        with self.assertRaises(guard.Refused):
            self.publish(reg, sequence=1, epoch=0)

    def test_current_checkpoint_uses_sequence_not_timestamp(self):
        reg = self.register()
        first = self.checkpoint("first")
        self.publish(reg, checkpoint=first)
        second = self.checkpoint("second")
        os.utime(second["path"], (1, 1))
        self.publish(reg, 1, second, stage="LATEST")
        self.assertEqual(self.recover()["anchor"]["checkpoint"], second)
        self.assertEqual(self.recover()["anchor"]["stage"], "LATEST")

    def test_same_assignment_continuation_preserves_original_and_resources(self):
        reg = self.register()
        self.publish(reg)
        assignment_before = Path(self.assignment["path"]).read_bytes()
        cp = self.recover()["anchor"]["checkpoint"]
        reg2 = self.register(expected_epoch=1, formal_turn_id="turn-2", registration_reason="continuation",
                             native_observation=self.observe("turn-2", "inProgress", "observation-2"))
        view = self.recover()
        self.assertEqual(reg2["epoch"], 2)
        self.assertEqual(view["anchor"]["checkpoint"], cp)
        self.assertEqual(view["anchor"]["continuity"]["open_tokens"], ["existing-token"])
        self.assertEqual(Path(self.assignment["path"]).read_bytes(), assignment_before)
        with self.assertRaises(guard.Refused):
            self.publish(reg, sequence=1)
        self.publish(reg2, checkpoint=self.checkpoint("cp2"))

    def test_migration_failed_current_turn_is_readable_not_deliverable(self):
        self.register(formal_turn_id="failed-turn", registration_reason="migration",
                      native_observation=self.observe("failed-turn", "failed"),
                      initial_checkpoint=self.checkpoint(), run_state="WAITING_NATIVE_PLATFORM_ACCESS")
        view = self.recover()
        self.assertEqual(view["formal_turn_id"], "failed-turn")
        self.assertEqual(view["run_state"], "WAITING_NATIVE_PLATFORM_ACCESS")
        self.assertEqual(view["anchor"]["continuity"]["open_tokens"], ["existing-token"])
        with self.assertRaises(guard.Refused):
            self.validate(observation=self.observe("failed-turn", "completed", "fake-later"))

    def test_native_completion_not_worker_assertion(self):
        reg = self.register()
        self.publish(reg)
        with self.assertRaises(guard.Refused):
            self.validate(observation=self.observe("turn-1", "failed"))
        with self.assertRaises(guard.Refused):
            self.validate(observation=self.observe("wrong-turn", "completed", "wrong-turn"))
        worker_observation = self.write("worker/progress/fake-native.json", guard.parse(Path(self.observe("turn-1", "completed")["path"]).read_bytes()))
        with self.assertRaises(guard.Refused):
            self.validate(observation=worker_observation)

    def test_stale_startup_and_formal_dimensions_rejected(self):
        reg = self.register()
        self.publish(reg)
        startup = self.write("worker/startup.json", {"schema": "rea.native_startup_receipt.v1", "job": "OLD/a005"})
        with self.assertRaises(guard.Refused):
            self.validate(startup)
        for field, value in {"kind": "startup", "thread_id": "other", "job_id": "OLD", "attempt": "a001",
                             "epoch": 0, "binding_id": "0" * 64, "formal_turn_id": "old", "formal_work_order_sha256": "0" * 64,
                             "progress_sequence": 10, "checkpoint_sha256": "0" * 64}.items():
            with self.subTest(field=field), self.assertRaises(guard.Refused):
                self.validate(self.delivery(**{field: value}))

    def test_forged_checkpoint_and_assignment_dimensions(self):
        for change in ({"owner": "wrong"}, {"job": "JOB/a001"}, {"formal_work_order_sha256": "0" * 64}):
            with self.subTest(change=change), self.assertRaises(guard.Refused):
                self.register(initial_checkpoint=self.checkpoint(**change))
        bad = self.write("controller/bad-assignment.json", {"job": "JOB/a002", "native_thread_id": "wrong", "native_turn_id": "turn-1", "formal_work_order": self.formal})
        with self.assertRaises(guard.Refused):
            self.register(assignment=bad)

    def test_owner_roots_and_path_aliases(self):
        reg = self.register()
        self.publish(reg)
        with self.assertRaises(guard.Refused):
            self.register(controller_owner="other", expected_epoch=1)
        with self.assertRaises(guard.Refused):
            self.publish(reg, sequence=1, caller_owner="other")
        with self.assertRaises(guard.Refused):
            self.register(expected_epoch=1, progress_root=str(self.registry / "nested"))
        hardlink = self.root / "hardlink.json"
        os.link(self.formal["path"], hardlink)
        with self.assertRaises(guard.Refused):
            guard.identity(self.formal)

    def test_identity_changes_and_gaps_fail_closed(self):
        reg = self.register()
        self.publish(reg)
        cp = Path(self.recover()["anchor"]["checkpoint"]["path"])
        cp.write_text("{}", encoding="utf-8")
        with self.assertRaises(guard.Refused):
            self.recover()
        cp.write_bytes(guard.encoded({"schema": "test.checkpoint.v1", "job": "JOB/a002", "owner": "worker-A", "formal_work_order_sha256": self.formal["sha256"]}))
        record = Path(reg["registration"]["path"])
        record.rename(record.with_name("00000002.json"))
        with self.assertRaises(guard.Refused):
            self.recover()

    def test_duplicate_json_and_existing_lock_rejected(self):
        with self.assertRaises(guard.Refused):
            guard.parse(b'{"a":1,"a":2}')
        self.registry.mkdir(parents=True)
        (self.registry / ".phase-writer.lock").write_text("retained original owner", encoding="utf-8")
        with self.assertRaises(guard.Refused):
            self.register()
        self.assertTrue((self.registry / ".phase-writer.lock").exists())

    def test_optional_hook_has_no_assignment_or_tokens(self):
        self.register()
        result = guard.run({"operation": "hook-context", "project_root": str(self.root), "event": {
            "hook_event_name": "SessionStart", "source": "compact", "session_id": "shared-parent", "cwd": str(self.root)}})
        body = result["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("JOB/a002", body)
        self.assertNotIn("existing-token", body)
        self.assertIn("own actual thread/job identity", body)

    def test_cli_recovery_uses_native_thread_environment(self):
        self.publish(self.register())
        result = subprocess.run([sys.executable, "-B", str(ENTRY), "recover", "--registry-root", str(self.registry)],
                                env=dict(os.environ, CODEX_THREAD_ID="worker-A"), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["thread_id"], "worker-A")

    def test_failed_native_turn_cannot_be_relabelled_completed(self):
        self.register(native_observation=self.observe("turn-1", "failed"), initial_checkpoint=self.checkpoint())
        with self.assertRaises(guard.Refused):
            self.validate(observation=self.observe("turn-1", "completed", "contradictory"))
        with self.assertRaises(guard.Refused):
            self.register(expected_epoch=1, native_observation=self.observe("turn-1", "completed", "contradictory-register"))

    def test_integer_identity_does_not_accept_boolean_alias(self):
        reg = self.register()
        self.publish(reg)
        with self.assertRaises(guard.Refused):
            self.validate(self.delivery(epoch=True))
        with self.assertRaises(guard.Refused):
            self.publish(reg, sequence=1, epoch=True)

    def test_wrong_existing_progress_owner_refused_before_register(self):
        self.write("worker/progress/OWNER.json", {"schema": "rea.phase_guard.progress_owner.v1", "thread_id": "other", "job_id": "JOB", "attempt": "a002"})
        with self.assertRaises(guard.Refused):
            self.register()

    def test_continuation_cannot_reset_progress_root_or_checkpoint(self):
        self.publish(self.register())
        observation = self.observe("turn-2", "inProgress", "observation-2")
        changes = {"expected_epoch": 1, "formal_turn_id": "turn-2", "registration_reason": "continuation", "native_observation": observation}
        with self.assertRaises(guard.Refused):
            self.register(**changes, progress_root=str(self.root / "reset-progress"))
        with self.assertRaises(guard.Refused):
            self.register(**changes, initial_checkpoint=self.checkpoint("older-checkpoint"))

    def test_overwritten_checkpoint_cannot_be_silently_repaired_by_publish(self):
        reg = self.register()
        self.publish(reg)
        replacement = self.checkpoint("cp1", stage="new recorded stage")
        with self.assertRaises(guard.Refused):
            self.recover()
        with self.assertRaises(guard.Refused):
            self.publish(reg, sequence=1, checkpoint=replacement)

    def test_existing_waiting_user_platform_state_refuses_delivery(self):
        self.register(initial_checkpoint=self.checkpoint(), run_state="WAITING_USER_PLATFORM_ACCESS")
        self.assertEqual(self.recover()["run_state"], "WAITING_USER_PLATFORM_ACCESS")
        with self.assertRaises(guard.Refused):
            self.validate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
