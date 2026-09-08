from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_agent_eval as analyze  # noqa: E402


def item_event(t_ms: float, event_type: str, item: dict) -> dict:
    return {"t_ms": t_ms, "event": {"type": event_type, "item": item}}


def command_event(
    t_ms: float,
    command: str,
    exit_code: int = 0,
    aggregated_output: str = "",
) -> dict:
    return item_event(
        t_ms,
        "item.completed",
        {
            "id": f"cmd-{t_ms}",
            "type": "command_execution",
            "command": command,
            "aggregated_output": aggregated_output,
            "exit_code": exit_code,
            "status": "completed" if exit_code == 0 else "failed",
        },
    )


def base_result(fixture_id: str, events: list[dict], before: dict, after: dict) -> dict:
    return {
        "fixture_id": fixture_id,
        "sequence_index": 1,
        "block_id": "block",
        "pair_id": "pair",
        "host_label": "local",
        "host_order": "local_then_remote",
        "requested_model": "gpt-test",
        "wall_ms": 1000.0,
        "exit_code": 0,
        "timed_out": False,
        "harness_error": None,
        "jsonl_events": events,
        "non_json_stdout_lines": [],
        "workspace": {
            "before": {"files": before},
            "after": {"files": after},
            "diff": analyze.probe.diff_snapshots(
                {"files": before}, {"files": after}
            ),
        },
    }


CODEX_WINDOWS_POWERSHELL_PREFIX = (
    r'"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
    "-Command "
)


def codex_windows_powershell_command(payload: str, quote: str = "'") -> str:
    return f"{CODEX_WINDOWS_POWERSHELL_PREFIX}{quote}{payload}{quote}"


F2_RED_OUTPUT = """\
test_adjacent_inclusive_windows_merge (...) ... FAIL
test_input_order_is_not_mutated (...) ... FAIL
test_overlap_still_merges (...) ... ok
Ran 3 tests in 0.001s
FAILED (failures=2)
"""


class AgentEvalGraderTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_and_nonfinite_values(self) -> None:
        with self.assertRaises(analyze.DuplicateJSONKey):
            analyze.strict_json_loads('{"x":1,"x":2}')
        with self.assertRaises(ValueError):
            analyze.strict_json_loads('{"x":NaN}')
        self.assertFalse(analyze.strict_deep_equal({"count": True}, {"count": 1}))

    def test_unknown_item_is_preserved_but_started_without_terminal_is_abandoned(self) -> None:
        result = {
            "jsonl_events": [
                item_event(1, "item.completed", {"id": "u", "type": "future_tool", "x": 1}),
                item_event(2, "item.started", {"id": "lost", "type": "command_execution", "command": "dir"}),
            ]
        }
        lifecycle = analyze.item_lifecycle_anomalies(result)
        self.assertEqual(lifecycle["completed_without_start_count"], 1)
        self.assertEqual(lifecycle["abandoned_started_count"], 1)
        self.assertEqual(result["jsonl_events"][0]["event"]["item"]["x"], 1)

    def test_f2_single_expected_red_test_and_reads_pass_tool_protocol(self) -> None:
        events = [
            command_event(
                1,
                "python -B -m unittest discover -s tests -v",
                1,
                F2_RED_OUTPUT,
            ),
            command_event(2, "python -B read_one.py src/windowing.py"),
            command_event(3, "python -B read_one.py tests/test_windowing.py"),
        ]
        tools = analyze.summarize_tools({"jsonl_events": events}, "f2_python_bugfix")
        self.assertEqual(tools["raw_failed_command_count"], 1)
        self.assertEqual(tools["expected_failed_command_count"], 1)
        self.assertEqual(tools["unexpected_failed_command_count"], 0)
        self.assertTrue(tools["expected_red_baseline_evidence"])
        self.assertTrue(tools["tool_path_success"])

    def test_f2_second_test_run_fails_exact_tool_protocol(self) -> None:
        events = [
            command_event(
                1,
                "python -B -m unittest discover -s tests -v",
                1,
                F2_RED_OUTPUT,
            ),
            command_event(2, "python -B read_one.py src/windowing.py"),
            command_event(3, "python -B read_one.py tests/test_windowing.py"),
            command_event(
                4,
                "python -B -m unittest discover -s tests -v",
                1,
                F2_RED_OUTPUT,
            ),
        ]
        tools = analyze.summarize_tools({"jsonl_events": events}, "f2_python_bugfix")
        self.assertEqual(tools["expected_failed_command_count"], 1)
        self.assertEqual(tools["unexpected_failed_command_count"], 1)
        self.assertEqual(tools["duplicate_or_excess_command_count"], 1)
        self.assertFalse(tools["tool_path_success"])

    def test_f2_blocked_test_command_is_not_mistaken_for_expected_red(self) -> None:
        blocked = command_event(
            1,
            "python -B -m unittest discover -s tests -v",
            -1,
            "rejected: blocked by policy",
        )
        blocked["event"]["item"]["status"] = "declined"
        events = [
            blocked,
            command_event(2, "python -B read_one.py src/windowing.py"),
            command_event(3, "python -B read_one.py tests/test_windowing.py"),
        ]
        tools = analyze.summarize_tools({"jsonl_events": events}, "f2_python_bugfix")
        self.assertEqual(tools["expected_failed_command_count"], 0)
        self.assertEqual(tools["unexpected_failed_command_count"], 1)
        self.assertFalse(tools["expected_red_baseline_evidence"])
        self.assertFalse(tools["tool_path_success"])

    def test_f2_read_only_end_to_end_strict_grade(self) -> None:
        final = json.dumps(analyze.EXPECTED_F2_FINAL, separators=(",", ":"))
        events = [
            command_event(
                1,
                "python -B -m unittest discover -s tests -v",
                1,
                F2_RED_OUTPUT,
            ),
            command_event(2, "python -B read_one.py src/windowing.py"),
            command_event(3, "python -B read_one.py tests/test_windowing.py"),
            item_event(4, "item.completed", {"id": "answer", "type": "agent_message", "text": final}),
            {"t_ms": 5, "event": {"type": "turn.completed", "usage": {}}},
        ]
        files = {"src/windowing.py": {"type": "file", "sha256": "x", "size_bytes": 1}}
        grade = analyze.grade_result(
            base_result("f2_python_bugfix", events, files, files)
        )
        self.assertTrue(grade["task_success"])
        self.assertTrue(grade["tool_path_success"])
        self.assertTrue(grade["workspace_safety_success"])
        self.assertTrue(grade["strict_agent_success"])

    def test_f1_end_to_end_strict_grade(self) -> None:
        final = json.dumps(analyze.EXPECTED_F1, separators=(",", ":"))
        events = [
            command_event(1, "python -B read_one.py manifest.json"),
            command_event(2, "python -B read_one.py data/jobs.csv"),
            command_event(3, "python -B read_one.py data/owners.json"),
            command_event(4, "python -B read_one.py data/rules.json"),
            item_event(5, "item.completed", {"id": "answer", "type": "agent_message", "text": final}),
            {"t_ms": 6, "event": {"type": "turn.completed", "usage": {}}},
        ]
        files = {"manifest.json": {"type": "file", "sha256": "x", "size_bytes": 1}}
        result = base_result("f1_manifest_join_readonly", events, files, files)
        grade = analyze.grade_result(result)
        self.assertTrue(grade["transport_success"])
        self.assertTrue(grade["task_success"])
        self.assertTrue(grade["tool_path_success"])
        self.assertTrue(grade["strict_agent_success"])

    def test_f3_read_only_end_to_end_strict_grade(self) -> None:
        final = json.dumps(analyze.EXPECTED_F3_FINAL, separators=(",", ":"))
        events = [
            command_event(1, "python -B read_one.py manifest.json"),
            command_event(2, "python -B read_one.py logs/east.jsonl"),
            command_event(3, "python -B read_one.py logs/west.jsonl"),
            command_event(4, "python -B read_one.py data/codebook.json"),
            command_event(5, "python -B read_one.py data/rules.json"),
            item_event(6, "item.completed", {"id": "answer", "type": "agent_message", "text": final}),
            {"t_ms": 7, "event": {"type": "turn.completed", "usage": {}}},
        ]
        files = {"manifest.json": {"type": "file", "sha256": "x", "size_bytes": 1}}
        grade = analyze.grade_result(
            base_result("f3_incident_report", events, files, files)
        )
        self.assertTrue(grade["task_success"])
        self.assertTrue(grade["tool_path_success"])
        self.assertTrue(grade["workspace_safety_success"])
        self.assertTrue(grade["strict_agent_success"])

    def test_non_python_and_outside_path_commands_fail_tool_protocol(self) -> None:
        events = [
            command_event(1, "Get-Content manifest.json"),
            command_event(2, "python -B read_one.py ../oracle.json"),
            command_event(3, "python -B read_one.py data/owners.json"),
            command_event(4, "python -B read_one.py data/rules.json"),
        ]
        tools = analyze.summarize_tools(
            {"jsonl_events": events}, "f1_manifest_join_readonly"
        )
        self.assertEqual(tools["non_python_command_count"], 1)
        self.assertEqual(tools["outside_path_command_count"], 1)
        self.assertFalse(tools["tool_path_success"])

    def test_shell_write_and_file_change_fail_read_only_tool_protocol(self) -> None:
        events = [
            command_event(1, "python -B read_one.py manifest.json"),
            command_event(2, "python -B read_one.py logs/east.jsonl"),
            command_event(3, "python -B read_one.py logs/west.jsonl"),
            command_event(4, "python -B read_one.py data/codebook.json"),
            command_event(5, "Set-Content -Path out/summary.json -Value '{}'"),
            item_event(
                6,
                "item.completed",
                {"id": "edit", "type": "file_change", "status": "completed"},
            ),
        ]
        tools = analyze.summarize_tools(
            {"jsonl_events": events}, "f3_incident_report"
        )
        self.assertEqual(tools["non_python_command_count"], 1)
        self.assertEqual(tools["unexpected_command_count"], 1)
        self.assertEqual(tools["file_change_count"], 1)
        self.assertFalse(tools["tool_path_success"])

    def test_codex_windows_powershell_wrapper_is_not_an_outside_path(self) -> None:
        events = [
            command_event(
                1,
                codex_windows_powershell_command(
                    "python -B read_one.py manifest.json"
                ),
            ),
            command_event(
                2,
                codex_windows_powershell_command(
                    "python -B read_one.py data/jobs.csv", quote='"'
                ),
            ),
            command_event(
                3,
                codex_windows_powershell_command(
                    "python -B read_one.py data/owners.json"
                ),
            ),
            command_event(
                4,
                codex_windows_powershell_command(
                    "python -B read_one.py data/rules.json"
                ),
            ),
        ]
        tools = analyze.summarize_tools(
            {"jsonl_events": events}, "f1_manifest_join_readonly"
        )
        self.assertEqual(tools["outside_path_command_count"], 0)
        self.assertEqual(tools["forbidden_command_count"], 0)
        self.assertEqual(tools["non_python_command_count"], 0)
        self.assertTrue(tools["tool_path_success"])

    def test_codex_windows_wrapper_keeps_inner_paths_and_commands_audited(self) -> None:
        events = [
            command_event(
                1,
                codex_windows_powershell_command(
                    r"python -B read_one.py ..\secret\oracle.json"
                ),
            ),
            command_event(
                2,
                codex_windows_powershell_command(
                    r"python -B read_one.py D:\secret\oracle.json"
                ),
            ),
            command_event(
                3,
                codex_windows_powershell_command(
                    "python -B read_one.py manifest.json; git status"
                ),
            ),
            command_event(
                4,
                codex_windows_powershell_command("Get-Content manifest.json"),
            ),
        ]
        tools = analyze.summarize_tools(
            {"jsonl_events": events}, "f1_manifest_join_readonly"
        )
        self.assertEqual(tools["outside_path_command_count"], 2)
        self.assertEqual(tools["forbidden_command_count"], 1)
        self.assertEqual(tools["non_python_command_count"], 1)
        self.assertFalse(tools["tool_path_success"])

    def test_unrecognized_absolute_shell_wrapper_is_still_an_outside_path(self) -> None:
        command = (
            r'"C:\\Tools\\powershell.exe" -Command '
            r"'python -B read_one.py manifest.json'"
        )
        tools = analyze.summarize_tools(
            {"jsonl_events": [command_event(1, command)]},
            "f1_manifest_join_readonly",
        )
        self.assertEqual(tools["outside_path_command_count"], 1)

    def test_allowed_prefix_with_trailing_argument_fails_exact_command_audit(self) -> None:
        events = [
            command_event(
                1,
                codex_windows_powershell_command(
                    "python -B read_one.py manifest.json extra-argument"
                ),
            ),
            command_event(2, "python -B read_one.py data/jobs.csv"),
            command_event(3, "python -B read_one.py data/owners.json"),
            command_event(4, "python -B read_one.py data/rules.json"),
        ]
        tools = analyze.summarize_tools(
            {"jsonl_events": events}, "f1_manifest_join_readonly"
        )
        self.assertEqual(tools["unexpected_command_count"], 1)
        self.assertFalse(tools["tool_path_success"])

    def test_exact_wrapped_commands_pass_exact_command_audit(self) -> None:
        events = [
            command_event(
                1,
                codex_windows_powershell_command(
                    "python -B read_one.py manifest.json"
                ),
            ),
            command_event(2, "python   -B read_one.py data/jobs.csv"),
            command_event(3, "python -B read_one.py data/owners.json"),
            command_event(4, "python -B read_one.py data/rules.json"),
        ]
        tools = analyze.summarize_tools(
            {"jsonl_events": events}, "f1_manifest_join_readonly"
        )
        self.assertEqual(tools["unexpected_command_count"], 0)
        self.assertTrue(tools["tool_path_success"])

    def test_analyze_writes_paired_task_and_strict_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = pathlib.Path(temporary)
            final = json.dumps(analyze.EXPECTED_F1, separators=(",", ":"))
            files = {
                "manifest.json": {"type": "file", "sha256": "x", "size_bytes": 1}
            }
            calls = []
            for sequence, host in enumerate(("local", "remote"), start=1):
                filename = f"{sequence:03d}_{host}.json"
                relative = f"raw/agent_runs/{host}/{filename}"
                calls.append(
                    {
                        "sequence_index": sequence,
                        "host_label": host,
                        "local_output_relative": relative,
                    }
                )
                events = [
                    command_event(1, "python -B read_one.py manifest.json"),
                    command_event(2, "python -B read_one.py data/jobs.csv"),
                    command_event(3, "python -B read_one.py data/owners.json"),
                    command_event(4, "python -B read_one.py data/rules.json"),
                    item_event(5, "item.completed", {"id": "a", "type": "agent_message", "text": final}),
                    {"t_ms": 6, "event": {"type": "turn.completed", "usage": {}}},
                ]
                result = base_result(
                    "f1_manifest_join_readonly", events, files, files
                )
                result.update(
                    {
                        "sequence_index": sequence,
                        "host_label": host,
                        "pair_id": "pair",
                        "block_id": "block",
                    }
                )
                path = run_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result), encoding="utf-8")
            analyzer_hash = analyze.hashlib.sha256(
                pathlib.Path(analyze.__file__).read_bytes()
            ).hexdigest()
            schedule = {
                "parameters": {
                    "models": ["gpt-test"],
                    "fixtures": ["f1_manifest_join_readonly"],
                    "analyzer_sha256": analyzer_hash,
                },
                "call_count": 2,
                "block_count": 1,
                "blocks": [
                    {
                        "block_id": "block",
                        "pair_id": "pair",
                        "model": "gpt-test",
                        "fixture_id": "f1_manifest_join_readonly",
                        "rep": 1,
                        "host_order_label": "local_then_remote",
                        "calls": calls,
                    }
                ],
            }
            (run_dir / "schedule.json").write_text(
                json.dumps(schedule), encoding="utf-8"
            )
            summary = analyze.analyze(run_dir)
        self.assertEqual(
            summary["paired"]["strict"]["outcomes"]["both_success"], 1
        )
        self.assertEqual(
            summary["paired"]["task"]["outcomes"]["both_success"], 1
        )


if __name__ == "__main__":
    unittest.main()
