"""Exercise the real frozen-run analyzer with explicitly synthetic receipts.

No CLI model, SSH, credentials or external network is used. This is a pipeline
integration test, not a benchmark result or a reproduction of historical calls.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import agent_eval_orchestrator as orchestrator
import agent_eval_probe as probe
import analyze_agent_eval as analyzer


ANSWERS = {
    "f1_manifest_join_readonly": analyzer.EXPECTED_F1,
    "f2_python_bugfix": analyzer.EXPECTED_F2_FINAL,
    "f3_incident_report": analyzer.EXPECTED_F3_FINAL,
}
RED_BASELINE = (
    "test_adjacent_inclusive_windows_merge (...) ... FAIL\n"
    "test_input_order_is_not_mutated (...) ... FAIL\n"
    "test_overlap_still_merges (...) ... ok\n"
    "Ran 3 tests in 0.001s\nFAILED (failures=2)\n"
)


def receipt(block: dict, call: dict, schedule: dict, *, faults: bool) -> dict:
    fixture = block["fixture_id"]
    commands = sorted(
        analyzer.EXACT_COMMAND_PAYLOADS[fixture],
        key=lambda command: analyzer.EXPECTED_COMMAND_LABEL_SEQUENCE[fixture].index(
            analyzer.classify_command(fixture, command)
        ),
    )
    if faults and call["sequence_index"] == 2:
        commands.append(commands[-1])
    events = []
    for index, command in enumerate(commands):
        red = "unittest discover" in command
        item = {"id": f"cmd-{index}", "type": "command_execution", "command": command}
        events.append({"t_ms": index * 10.0, "event": {"type": "item.started", "item": item}})
        terminal = {**item, "exit_code": int(red), "status": "failed" if red else "completed",
                    "aggregated_output": RED_BASELINE if red else "synthetic fixture read"}
        events.append({"t_ms": index * 10.0 + 5, "event": {"type": "item.completed", "item": terminal}})
    answer = copy.deepcopy(ANSWERS[fixture])
    if faults and call["sequence_index"] == 3:
        answer = {"deliberately_wrong": True}
    events.append({"t_ms": 900.0, "event": {"type": "item.completed", "item": {
        "id": "answer", "type": "agent_message", "text": json.dumps(answer)}}})
    events.append({"t_ms": 1000.0, "event": {"type": "turn.completed", "usage": {}}})
    workspace = probe.snapshot_workspace(orchestrator.SOURCE_FIXTURES / fixture)
    return {
        "kind": "codex_agent_eval_call", "evidence_class": "synthetic_pipeline_test",
        "host_label": call["host_label"], "sequence_index": call["sequence_index"],
        "block_id": block["block_id"], "pair_id": block["pair_id"],
        "host_order": block["host_order_label"], "fixture_id": fixture,
        "requested_model": block["model"], "reasoning_effort": schedule["parameters"]["effort"],
        "wall_ms": 1000.0 + 10 * call["sequence_index"],
        "exit_code": int(faults and call["sequence_index"] == 4),
        "timed_out": False, "harness_error": None, "jsonl_events": events,
        "non_json_stdout_lines": [], "usage": {},
        "workspace": {"before": workspace, "after": workspace,
                      "diff": probe.diff_snapshots(workspace, workspace)},
    }


def replay(destination: Path, *, omit_last: bool = False, faults: bool = True) -> dict:
    if destination.exists():
        raise ValueError("Choose a new output directory; existing runs are never replaced")
    args = orchestrator.build_parser().parse_args([
        "--run-dir", str(destination), "--prepare-only",
    ])
    run_dir, schedule, _, _, _, _, manifest = orchestrator.initialize(args)
    schedule["evidence_class"] = "synthetic_pipeline_test"
    orchestrator.atomic_json(run_dir / "schedule.json", schedule)
    manifest["schedule_sha256"] = orchestrator.sha256_file(run_dir / "schedule.json")
    present = 0
    for block, call in orchestrator.all_calls(schedule):
        if omit_last and call["sequence_index"] == schedule["call_count"]:
            continue
        path = run_dir / call["local_output_relative"]
        orchestrator.atomic_json(path, receipt(block, call, schedule, faults=faults))
        manifest["calls"][str(call["sequence_index"])] = {
            "status": "completed", "result_sha256": orchestrator.sha256_file(path),
            "attempts": [{"outcome": {"result_valid": True}}],
        }
        present += 1
    manifest.update({"state": "prepared" if omit_last else "completed",
                     "evidence_class": "synthetic_pipeline_test",
                     "completed_call_count": present, "failed_call_count": 0})
    orchestrator.atomic_json(run_dir / "manifest.json", manifest)
    (run_dir / "SYNTHETIC-ONLY.txt").write_text(
        "Generated receipts. No model calls. Not model-performance evidence.\n", encoding="utf-8"
    )
    summary = analyzer.analyze(run_dir)
    summary["evidence_class"] = "synthetic_pipeline_test"
    orchestrator.atomic_json(run_dir / "processed/agent_eval/summary.json", summary)
    report = run_dir / "processed/agent_eval/report.md"
    report.write_text(
        "> SYNTHETIC PIPELINE TEST — not model-performance evidence.\n\n"
        + report.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--omit-last", action="store_true")
    parser.add_argument("--all-correct", action="store_true")
    args = parser.parse_args()
    summary = replay(args.out, omit_last=args.omit_last, faults=not args.all_correct)
    print(json.dumps({"evidence_class": "synthetic_pipeline_test",
                      "planned": summary["planned_calls"], "missing": summary["missing_results"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
