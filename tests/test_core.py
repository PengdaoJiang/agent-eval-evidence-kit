import copy
import unittest

from evalkit.core import digest, grade, sample_trace, schedule, strict_loads, summarize, typed_equal


class EvaluationTests(unittest.TestCase):
    def test_all_fixtures_pass(self):
        for fixture in ["aggregate", "diagnose", "incident"]:
            with self.subTest(fixture=fixture):
                self.assertTrue(grade(sample_trace(fixture))["strict_success"])

    def test_bool_is_not_integer(self):
        self.assertFalse(typed_equal({"a": True}, {"a": 1}))

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            strict_loads('{"a": 1, "a": 2}')

    def test_nonfinite_rejected(self):
        for value in ["NaN", "Infinity", "-Infinity", "1e999"]:
            with self.assertRaises(ValueError):
                strict_loads(value)

    def test_digest_canonical(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))

    def test_digest_drift_rejected(self):
        trace = sample_trace()
        trace["fixture_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            grade(trace)

    def test_extra_fields_rejected(self):
        trace = sample_trace()
        trace["unexpected"] = True
        with self.assertRaises(ValueError):
            grade(trace)

    def test_transport_orthogonal(self):
        trace = sample_trace()
        trace["transport_completed"] = False
        result = grade(trace)
        self.assertFalse(result["strict_success"])
        self.assertTrue(result["task_success"])
        self.assertEqual(result["failed_dimensions"], ["transport_success"])

    def test_task_orthogonal(self):
        trace = sample_trace()
        trace["answer"]["active_count"] = 4
        result = grade(trace)
        self.assertEqual(result["failed_dimensions"], ["task_success"])

    def test_expected_failure_not_general_exception(self):
        trace = sample_trace("diagnose")
        trace["tools"][0]["output"] = "execution denied"
        self.assertFalse(grade(trace)["tool_protocol_success"])

    def test_duplicate_tool_fails(self):
        trace = sample_trace("diagnose")
        trace["tools"].append(copy.deepcopy(trace["tools"][0]))
        self.assertFalse(grade(trace)["tool_protocol_success"])

    def test_missing_tool_fails(self):
        trace = sample_trace()
        trace["tools"] = []
        self.assertFalse(grade(trace)["tool_protocol_success"])

    def test_tool_order_matters(self):
        trace = sample_trace("diagnose")
        trace["tools"].reverse()
        self.assertFalse(grade(trace)["tool_protocol_success"])

    def test_boolean_exit_code_rejected(self):
        trace = sample_trace()
        trace["tools"][0]["exit_code"] = False
        with self.assertRaises(ValueError):
            grade(trace)

    def test_workspace_change_fails(self):
        trace = sample_trace()
        trace["workspace_after"]["new.txt"] = "0" * 64
        result = grade(trace)
        self.assertEqual(result["failed_dimensions"], ["reported_workspace_integrity"])

    def test_unsafe_paths_rejected(self):
        for path in ["../secret", "/root", "C:/data", "a\\b", "a/./b", "a//b", "."]:
            trace = sample_trace()
            trace["workspace_before"] = {path: "0" * 64}
            with self.subTest(path=path), self.assertRaises(ValueError):
                grade(trace)

    def test_bad_hash_rejected(self):
        trace = sample_trace()
        trace["workspace_before"] = {"file": "not-a-hash"}
        with self.assertRaises(ValueError):
            grade(trace)

    def test_order_reversal_and_seed(self):
        rows = schedule(17, 2)
        self.assertEqual(rows, schedule(17, 2))
        self.assertEqual(rows[0]["arm"], rows[3]["arm"])
        self.assertEqual(len(rows), 12)

    def test_missing_attempt_visible(self):
        rows = schedule()
        result = summarize(rows, [sample_trace(rows[0]["fixture_id"], rows[0]["attempt_id"])])
        self.assertEqual(len(result["missing_attempt_ids"]), 11)
        self.assertEqual(result["paired_outcomes"]["incomplete"], 6)

    def test_duplicates_rejected(self):
        rows = schedule()
        trace = sample_trace(rows[0]["fixture_id"], rows[0]["attempt_id"])
        with self.assertRaises(ValueError):
            summarize(rows, [trace, trace])

    def test_unknown_attempt_rejected(self):
        with self.assertRaises(ValueError):
            summarize(schedule(), [sample_trace()])

    def test_wrong_scheduled_fixture_rejected(self):
        rows = schedule()
        with self.assertRaises(ValueError):
            summarize(rows, [sample_trace("incident", rows[0]["attempt_id"])])

    def test_mixed_provenance_rejected(self):
        rows = schedule()
        traces = [sample_trace(r["fixture_id"], r["attempt_id"]) for r in rows[:2]]
        traces[1]["synthetic"] = False
        with self.assertRaises(ValueError):
            summarize(rows, traces)

    def test_complete_pair_outcomes(self):
        rows = schedule()
        traces = [sample_trace(r["fixture_id"], r["attempt_id"]) for r in rows]
        result = summarize(rows, traces)
        self.assertEqual(result["paired_outcomes"]["both_pass"], 6)
        self.assertEqual(result["arms"]["A"]["completed_success_rate"], 1)

    def test_invalid_pair_rejected(self):
        with self.assertRaises(ValueError):
            summarize(schedule()[:1], [])

    def test_empty_schedule_rejected(self):
        with self.assertRaises(ValueError):
            summarize([], [])

    def test_schedule_container_types_rejected(self):
        with self.assertRaises(ValueError):
            summarize(tuple(schedule()), [])
        with self.assertRaises(ValueError):
            summarize(schedule(), {})

    def test_schedule_identifier_types_rejected(self):
        for field in ["attempt_id", "pair_id", "fixture_id", "arm"]:
            for value in [None, True, 1, [], "", " "]:
                rows = schedule()
                rows[0][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    summarize(rows, [])

    def test_schedule_order_types_rejected(self):
        for value in [True, 1.0, "1", 0, 3, None]:
            rows = schedule()
            rows[0]["order"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                summarize(rows, [])

    def test_schedule_seed_and_repeats_types_rejected(self):
        for value in [True, "17", 1.0, None]:
            with self.subTest(seed=value), self.assertRaises(ValueError):
                schedule(seed=value)
        for value in [True, 0, -1, "2", 2.0]:
            with self.subTest(repeats=value), self.assertRaises(ValueError):
                schedule(repeats=value)

    def test_planned_denominator_preserves_missing_attempts(self):
        rows = schedule()
        first = rows[0]
        report = summarize(rows, [sample_trace(first["fixture_id"], first["attempt_id"])])
        arm = report["arms"][first["arm"]]
        self.assertEqual(arm["missing"], 5)
        self.assertEqual(arm["completed_success_rate"], 1)
        self.assertEqual(arm["planned_success_rate"], 1 / 6)


if __name__ == "__main__":
    unittest.main()
