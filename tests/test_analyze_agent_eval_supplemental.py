from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_agent_eval_supplemental as supplemental  # noqa: E402


MODELS = ["model-a", "model-b", "model-c", "model-d"]
FIXTURES = ["fixture-one", "fixture-two", "fixture-three"]
EXPECTED_COUNTS = {
    "fixture-one": 1,
    "fixture-two": 2,
    "fixture-three": 3,
}


FROZEN_PROBE = '''\
PROBE_MARKER = "frozen-probe"
'''


FROZEN_ANALYZER = '''\
import agent_eval_probe as probe

EXPECTED_COMMAND_LABEL_SEQUENCE = {
    "fixture-one": ["one"],
    "fixture-two": ["one", "two"],
    "fixture-three": ["one", "two", "three"],
}

def grade_result(result):
    grade = dict(result["synthetic_grade"])
    grade["task_success"] = bool(
        grade["task_success"] and probe.PROBE_MARKER == "frozen-probe"
    )
    grade.update({
        "sequence_index": result["sequence_index"],
        "block_id": result["block_id"],
        "pair_id": result["pair_id"],
        "host_label": result["host_label"],
        "host_order": result["host_order"],
        "model": result["requested_model"],
        "fixture_id": result["fixture_id"],
        "wall_ms": result["wall_ms"],
    })
    return grade
'''


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_grade(
    expected_commands: int,
    *,
    strict: bool,
    task: bool = True,
    transport: bool = True,
    tool_path: bool = True,
    lifecycle_anomaly: int = 0,
) -> dict:
    return {
        "strict_agent_success": strict,
        "task_success": task,
        "transport_success": transport,
        "tool_path_success": tool_path,
        "protocol_success": True,
        "workspace_safety_success": True,
        "completed_command_count": expected_commands,
        "unexpected_command_count": 0,
        "tool_terminal_count": expected_commands + 1,
        "file_change_count": 0,
        "expected_failed_command_count": 0,
        "unexpected_failed_command_count": 0,
        "duplicate_or_excess_command_count": 0,
        "forbidden_command_count": 0,
        "outside_path_command_count": 0,
        "non_python_command_count": 0,
        "abandoned_started_count": lifecycle_anomaly,
        "completed_without_start_count": 0,
        "duplicate_started_count": 0,
        "duplicate_terminal_count": 0,
        "item_type_mismatch_count": 0,
        "missing_item_id_count": 0,
        "item_type_counts_json": json.dumps(
            {"command_execution": expected_commands, "future_tool": 1}
        ),
        "semantic_label_counts_json": "{}",
    }


def build_complete_run(root: pathlib.Path) -> tuple[pathlib.Path, list[pathlib.Path]]:
    run_dir = root / "formal"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    probe = artifacts / "agent_eval_probe.py"
    probe.write_text(FROZEN_PROBE, encoding="utf-8")
    frozen = artifacts / "analyze_agent_eval.py"
    frozen.write_text(FROZEN_ANALYZER, encoding="utf-8")

    blocks = []
    manifest_calls = {}
    result_paths: list[pathlib.Path] = []
    sequence = 1
    pair_number = 0
    for model in MODELS:
        for fixture in FIXTURES:
            for rep, order in enumerate(
                (
                    ["local", "remote"],
                    ["remote", "local"],
                ),
                start=1,
            ):
                pair_id = f"pair-{pair_number:02d}"
                block_id = f"block-{pair_number:02d}"
                order_label = "_then_".join(order)
                calls = []
                for host in order:
                    relative = f"raw/agent_runs/{host}/{sequence:03d}.json"
                    call = {
                        "sequence_index": sequence,
                        "host_label": host,
                        "local_output_relative": relative,
                    }
                    calls.append(call)
                    remote = host == "remote"
                    strict = not remote or pair_number >= 3
                    transport = not (remote and pair_number == 0)
                    tool_path = not (remote and pair_number == 1)
                    result = {
                        "kind": "codex_agent_eval_call",
                        "host_label": host,
                        "sequence_index": sequence,
                        "block_id": block_id,
                        "pair_id": pair_id,
                        "host_order": order_label,
                        "fixture_id": fixture,
                        "requested_model": model,
                        "reasoning_effort": "high",
                        "wall_ms": 2000.0 if remote else 1000.0,
                        "exit_code": 0 if transport else 1,
                        "timed_out": False,
                        "harness_error": None,
                        "usage": {
                            "input_tokens": 20 if remote else 10,
                            "cached_input_tokens": 4 if remote else 2,
                            "output_tokens": 6 if remote else 3,
                            "reasoning_output_tokens": 2 if remote else 1,
                        },
                        "synthetic_grade": synthetic_grade(
                            EXPECTED_COUNTS[fixture],
                            strict=strict,
                            task=True,
                            transport=transport,
                            tool_path=tool_path,
                            lifecycle_anomaly=int(remote and pair_number == 2),
                        ),
                    }
                    path = run_dir / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(result), encoding="utf-8")
                    result_paths.append(path)
                    attempts = [{"outcome": {"result_valid": True}}]
                    if remote and pair_number == 0:
                        attempts = [
                            {"outcome": {"result_valid": False}},
                            {"outcome": {"result_valid": True}},
                        ]
                    manifest_calls[str(sequence)] = {
                        "status": "completed",
                        "result_sha256": sha256(path),
                        "attempts": attempts,
                    }
                    sequence += 1
                blocks.append(
                    {
                        "block_index": pair_number + 1,
                        "block_id": block_id,
                        "pair_id": pair_id,
                        "model": model,
                        "fixture_id": fixture,
                        "rep": rep,
                        "host_order": order,
                        "host_order_label": order_label,
                        "calls": calls,
                    }
                )
                pair_number += 1

    schedule = {
        "kind": "codex_agent_eval_schedule",
        "parameters": {
            "models": MODELS,
            "fixtures": FIXTURES,
            "repetitions": 2,
            "paired": True,
            "concurrent_model_calls": 1,
            "effort": "high",
            "probe_sha256": sha256(probe),
            "analyzer_sha256": sha256(frozen),
        },
        "call_count": 48,
        "block_count": 24,
        "blocks": blocks,
    }
    schedule_path = run_dir / "schedule.json"
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    manifest = {
        "state": "completed",
        "completed_call_count": 48,
        "failed_call_count": 0,
        "schedule_sha256": sha256(schedule_path),
        "calls": manifest_calls,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir, result_paths


class ExactInferenceTests(unittest.TestCase):
    def test_exact_harm_direction_and_zero_event_bound(self) -> None:
        self.assertEqual(supplemental.exact_two_sided_binomial(3, 0), 0.25)
        self.assertEqual(supplemental.exact_one_sided_harm(3, 0), 0.125)
        self.assertEqual(supplemental.exact_one_sided_harm(0, 3), 1.0)
        self.assertAlmostEqual(
            supplemental.zero_event_upper_95(24, 0),
            1 - 0.05 ** (1 / 24),
        )
        self.assertIsNone(supplemental.zero_event_upper_95(24, 1))

    def test_holm_is_monotone_in_sorted_p_values(self) -> None:
        adjusted = supplemental.holm_adjust({"a": 0.01, "b": 0.04, "c": 0.5})
        self.assertAlmostEqual(adjusted["a"], 0.03)
        self.assertAlmostEqual(adjusted["b"], 0.08)
        self.assertAlmostEqual(adjusted["c"], 0.5)


class SupplementalEndToEndTests(unittest.TestCase):
    def test_complete_run_uses_frozen_grader_and_writes_all_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = build_complete_run(pathlib.Path(temporary))
            output = pathlib.Path(temporary) / "output"
            summary = supplemental.analyze(
                run_dir,
                output,
                bootstrap_resamples=200,
                seed=17,
            )

            strict = summary["primary"]["strict_itt"]
            self.assertEqual(summary["frozen_grader"]["calls_scored"], 48)
            self.assertEqual(strict["main_success_n"], 24)
            self.assertEqual(strict["remote_success_n"], 21)
            self.assertEqual(strict["outcomes"]["main_only_adverse_for_02"], 3)
            self.assertAlmostEqual(strict["risk_difference_remote_minus_local"], -0.125)
            self.assertEqual(strict["mcnemar_exact_two_sided_p"], 0.25)
            self.assertEqual(strict["binomial_exact_one_sided_harm_p"], 0.125)

            wall = summary["groups"]["overall"]["wall"]
            self.assertEqual(wall["transport_complete"]["eligible_pair_n"], 23)
            self.assertEqual(wall["both_strict"]["eligible_pair_n"], 21)
            self.assertEqual(
                wall["transport_complete"]["median_wall_ratio_remote_over_local"], 2.0
            )
            efficiency = summary["groups"]["overall"]["efficiency"]
            self.assertAlmostEqual(
                efficiency["strict"]["main_correct_tasks_per_min"], 60.0
            )
            self.assertAlmostEqual(
                efficiency["strict"]["remote_correct_tasks_per_min"], 26.25
            )
            self.assertAlmostEqual(efficiency["task"]["ratio_remote_over_local"], 0.5)

            self.assertEqual(
                summary["usage"]["overall"]["remote"]["fields"]["input_tokens"]["sum"],
                480,
            )
            self.assertEqual(
                summary["audit"]["remote"]["lifecycle"]["abandoned_started_count"],
                1,
            )
            self.assertEqual(
                summary["audit"]["remote"]["execution_attempts"]["invalid_attempt_n"],
                1,
            )
            self.assertIsNone(
                summary["groups"]["by_model_fixture"]["model-a|fixture-one"]
                ["outcomes"]["strict"]["mcnemar_exact_two_sided_p"]
            )
            self.assertIn(
                "binomial_exact_one_sided_harm_p_holm",
                summary["groups"]["by_model"]["model-a"]["outcomes"]["strict"],
            )

            for name in (
                "supplemental_summary.json",
                "supplemental_calls.csv",
                "supplemental_pairs.csv",
                "supplemental_group_statistics.csv",
                "supplemental_host_order_sensitivity.csv",
                "supplemental_report.md",
            ):
                self.assertTrue((output / name).is_file(), name)
            report = (output / "supplemental_report.md").read_text(encoding="utf-8")
            self.assertIn("Primary endpoint: strict Agent success (ITT)", report)
            self.assertIn("No interval or zero-event bound establishes equivalence", report)
            self.assertIn("not server compute", report)

            with (output / "supplemental_pairs.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                pair_rows = {row["pair_id"]: row for row in csv.DictReader(handle)}
            self.assertEqual(
                pair_rows["pair-00"]["transport_complete_ratio_eligible"], "False"
            )
            self.assertEqual(pair_rows["pair-00"]["wall_ratio_remote_over_local"], "")
            self.assertEqual(
                pair_rows["pair-03"]["transport_complete_ratio_eligible"], "True"
            )
            self.assertEqual(
                float(pair_rows["pair-03"]["wall_ratio_remote_over_local"]), 2.0
            )

    def test_frozen_sibling_probe_overrides_and_then_restores_stale_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = build_complete_run(pathlib.Path(temporary))
            stale = types.ModuleType("agent_eval_probe")
            stale.PROBE_MARKER = "stale-global-probe"
            marker = object()
            previous = sys.modules.get("agent_eval_probe", marker)
            sys.modules["agent_eval_probe"] = stale
            try:
                summary = supplemental.analyze(
                    run_dir,
                    pathlib.Path(temporary) / "output",
                    bootstrap_resamples=100,
                    seed=17,
                )
                self.assertIs(sys.modules.get("agent_eval_probe"), stale)
            finally:
                if previous is marker:
                    sys.modules.pop("agent_eval_probe", None)
                else:
                    sys.modules["agent_eval_probe"] = previous
            self.assertEqual(
                summary["groups"]["overall"]["outcomes"]["task"]
                ["remote_success_n"],
                24,
            )

    def test_csv_ratio_is_blank_for_timeout_even_with_transport_grade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, _ = build_complete_run(pathlib.Path(temporary))
            schedule = json.loads(
                (run_dir / "schedule.json").read_text(encoding="utf-8")
            )
            block = next(item for item in schedule["blocks"] if item["pair_id"] == "pair-03")
            call = next(
                item for item in block["calls"] if item["host_label"] == "remote"
            )
            result_path = run_dir / call["local_output_relative"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertTrue(result["synthetic_grade"]["transport_success"])
            result["timed_out"] = True
            result_path.write_text(json.dumps(result), encoding="utf-8")
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["calls"][str(call["sequence_index"])]["result_sha256"] = sha256(
                result_path
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            output = pathlib.Path(temporary) / "output"
            supplemental.analyze(
                run_dir,
                output,
                bootstrap_resamples=100,
                seed=17,
            )
            with (output / "supplemental_pairs.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                pair_rows = {row["pair_id"]: row for row in csv.DictReader(handle)}
            self.assertEqual(
                pair_rows["pair-03"]["transport_complete_ratio_eligible"], "False"
            )
            self.assertEqual(pair_rows["pair-03"]["wall_ratio_remote_over_local"], "")
            self.assertEqual(
                float(pair_rows["pair-04"]["wall_ratio_remote_over_local"]), 2.0
            )

    def test_schedule_and_probe_hash_mutations_fail_closed(self) -> None:
        cases = (
            (
                "schedule",
                lambda run_dir: (run_dir / "schedule.json").write_text(
                    (run_dir / "schedule.json").read_text(encoding="utf-8") + " ",
                    encoding="utf-8",
                ),
                "schedule.json hash differs from manifest.schedule_sha256",
            ),
            (
                "probe",
                lambda run_dir: (run_dir / "artifacts" / "agent_eval_probe.py")
                .write_text(FROZEN_PROBE + "MUTATED = True\n", encoding="utf-8"),
                "frozen probe hash differs",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                run_dir, _ = build_complete_run(pathlib.Path(temporary))
                mutate(run_dir)
                output = pathlib.Path(temporary) / "output"
                with self.assertRaisesRegex(
                    supplemental.SupplementalAnalysisError, message
                ):
                    supplemental.analyze(
                        run_dir,
                        output,
                        bootstrap_resamples=100,
                        seed=17,
                    )
                self.assertFalse(output.exists())

    def test_incomplete_run_is_rejected_before_outputs_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, paths = build_complete_run(pathlib.Path(temporary))
            paths[-1].unlink()
            output = pathlib.Path(temporary) / "output"
            with self.assertRaisesRegex(
                supplemental.SupplementalAnalysisError,
                "incomplete; result is missing",
            ):
                supplemental.analyze(
                    run_dir,
                    output,
                    bootstrap_resamples=100,
                    seed=17,
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
