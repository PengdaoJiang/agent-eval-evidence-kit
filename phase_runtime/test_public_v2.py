"""Public regressions for checkpoint types and append-only continuity evidence."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("phase_public_base_v2", Path(__file__).with_name("test_public.py"))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
guard = base.guard


class RepairChecks(base.PhaseChecks):
    def rich_continuity(self):
        return {"owner_thread_id": "worker-A", "external_owner": {"thread_id": "worker-A", "process_id": "original-wrapper", "active": True},
                "process_records": [{"pid": 301, "record": {"path": "original-process-record", "sha256": "record-hash"}}],
                "open_tokens": ["token-A", "token-B"], "completed_work": ["already-built"]}

    def with_resources(self):
        previous = self.rich_continuity()
        self.base["continuity"] = copy.deepcopy(previous)
        registered = self.register()
        self.publish(registered)
        return registered, previous

    def test_v2_present_formal_order_wrong_types_never_publish(self):
        reg = self.register()
        invalid = [False, True, None, [], [self.formal], 0, 1.5, "", "relative.json", {},
                   {"path": self.formal["path"]}, dict(self.formal, bytes=True), dict(self.formal, sha256=False)]
        for index, value in enumerate(invalid):
            with self.subTest(value=value), self.assertRaises(guard.Refused):
                self.publish(reg, checkpoint=self.checkpoint("bad-formal-" + str(index), formal_work_order=value))
            self.assertEqual(self.recover()["anchor"]["sequence"], 0)
        self.publish(reg, checkpoint=self.checkpoint("good-descriptor", formal_work_order=self.formal))
        self.publish(reg, sequence=1, checkpoint=self.checkpoint("good-path", formal_work_order=self.formal["path"]))

    def test_v2_known_checkpoint_fields_reject_wrong_types(self):
        reg = self.register()
        changes = [{"schema": False}, {"schema": []}, {"job": False}, {"owner": ["worker-A"]},
                   {"owner_thread_id": None}, {"formal_work_order_sha256": False}, {"formal_work_order_sha256": []}]
        for index, change in enumerate(changes):
            with self.subTest(change=change), self.assertRaises(guard.Refused):
                self.publish(reg, checkpoint=self.checkpoint("wrong-field-" + str(index), **change))
        self.assertEqual(self.recover()["anchor"]["sequence"], 0)

    def test_v2_initial_checkpoint_rejects_false_formal_order(self):
        with self.assertRaises(guard.Refused):
            self.register(initial_checkpoint=self.checkpoint(formal_work_order=False))
        self.assertEqual(self.recover()["status"], "CURRENT_FORMAL_ASSIGNMENT_REQUIRED")

    def test_v2_partial_empty_and_removed_resource_references_refused(self):
        reg, previous = self.with_resources()
        variants = [{}, None, {"owner_thread_id": "worker-A"}]
        for key in previous:
            changed = copy.deepcopy(previous)
            del changed[key]
            variants.append(changed)
        for key in ("open_tokens", "process_records", "completed_work"):
            changed = copy.deepcopy(previous)
            changed[key] = []
            variants.append(changed)
        nested = copy.deepcopy(previous)
        del nested["external_owner"]["process_id"]
        variants.append(nested)
        for index, proposed in enumerate(variants):
            with self.subTest(proposed=proposed), self.assertRaises(guard.Refused):
                self.publish(reg, sequence=1, checkpoint=self.checkpoint("remove-" + str(index)), continuity=proposed)
            self.assertEqual(self.recover()["anchor"]["continuity"], previous)
            self.assertEqual(self.recover()["anchor"]["sequence"], 1)

    def test_v2_resource_reordering_replacement_and_type_alias_refused(self):
        reg, previous = self.with_resources()
        variants = []
        for key, value in (("open_tokens", ["token-B", "token-A"]), ("open_tokens", ["token-A", "replacement"]),
                           ("external_owner", False), ("external_owner", {"thread_id": "worker-A", "process_id": "other", "active": True}),
                           ("external_owner", {"thread_id": "worker-A", "process_id": "original-wrapper", "active": 1})):
            variants.append(dict(copy.deepcopy(previous), **{key: value}))
        changed_record = copy.deepcopy(previous)
        changed_record["process_records"][0]["pid"] = 999
        variants.append(changed_record)
        for index, proposed in enumerate(variants):
            with self.subTest(proposed=proposed), self.assertRaises(guard.Refused):
                self.publish(reg, sequence=1, checkpoint=self.checkpoint("replace-" + str(index)), continuity=proposed)
            self.assertEqual(self.recover()["anchor"]["continuity"], previous)

    def test_v2_omitted_continuity_inherits_every_reference(self):
        reg, previous = self.with_resources()
        self.publish(reg, sequence=1, checkpoint=self.checkpoint("next-immutable"))
        self.assertEqual(self.recover()["anchor"]["continuity"], previous)

    def test_v2_additional_closure_evidence_keeps_original_refs(self):
        reg, previous = self.with_resources()
        extended = copy.deepcopy(previous)
        extended["external_owner"]["new_observation"] = "owner-still-same"
        extended["open_tokens"].append("token-C")
        extended["process_records"].append({"pid": 302, "record": {"path": "second-record", "sha256": "second-hash"}})
        extended["closure_evidence"] = [{"token": "token-A", "original_protocol_receipt": "exact-retained-receipt"}]
        self.publish(reg, sequence=1, checkpoint=self.checkpoint("extended"), continuity=extended)
        view = self.recover()
        self.assertEqual(view["anchor"]["continuity"], extended)
        self.assertEqual(view["anchor"]["continuity"]["open_tokens"][:2], previous["open_tokens"])
        self.assertFalse(view["tokens_released"])

    def test_v2_same_assignment_epoch_cannot_drop_continuity(self):
        _, previous = self.with_resources()
        observation = self.observe("turn-2", "inProgress", "observation-2")
        with self.assertRaises(guard.Refused):
            self.register(expected_epoch=1, formal_turn_id="turn-2", registration_reason="continuation",
                          native_observation=observation, continuity={})
        extended = dict(copy.deepcopy(previous), continuation_observation="second-turn-record")
        self.register(expected_epoch=1, formal_turn_id="turn-2", registration_reason="continuation",
                      native_observation=observation, continuity=extended)
        self.assertEqual(self.recover()["anchor"]["continuity"], extended)

    def test_v2_nonfinite_json_and_nonobject_continuity_refused(self):
        for raw in (b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(guard.Refused):
                guard.parse(raw)
        for proposed in (None, False, [], "", {"extra": float("nan")}):
            with self.subTest(proposed=proposed), self.assertRaises(guard.Refused):
                self.register(continuity=proposed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
