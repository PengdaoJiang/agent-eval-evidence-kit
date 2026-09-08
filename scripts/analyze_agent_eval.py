#!/usr/bin/env python3
"""Grade and summarize the paired Codex Agent-use evaluation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import pathlib
import re
import sys
from collections import Counter, defaultdict
from typing import Any

import agent_eval_probe as probe


SCHEMA_VERSION = 1
TOOL_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "dynamic_tool_call",
    "web_search",
    "collab_tool_call",
}
TERMINAL_ITEM_EVENTS = {"item.completed", "item.failed"}
FORBIDDEN_COMMAND = re.compile(
    r"(?:^|[\s;&|])(?:curl(?:\.exe)?|wget(?:\.exe)?|ssh(?:\.exe)?|scp(?:\.exe)?|"
    r"git(?:\.exe)?|pip(?:\d|\.exe)?|npm(?:\.cmd|\.exe)?|winget(?:\.exe)?|"
    r"choco(?:\.exe)?|Invoke-WebRequest|Invoke-RestMethod|iwr|irm|"
    r"Remove-Item|rmdir|shutdown|format|taskkill)(?:\s|$)",
    re.IGNORECASE,
)
OUTSIDE_PATH_REFERENCE = re.compile(
    r"(?:^|[\s\"'`])\.\.[\\/]|(?:^|[\s\"'`])[A-Za-z]:[\\/]"
)
PYTHON_B_COMMAND = re.compile(r"(?:^|[\s\"'`])python(?:\.exe)?\s+-B(?:\s|$)", re.IGNORECASE)
CODEX_WINDOWS_POWERSHELL_WRAPPER = re.compile(
    r'^\s*"[A-Za-z]:\\+Windows\\+System32\\+WindowsPowerShell\\+v1\.0'
    r'\\+powershell\.exe"\s+-Command(?:\s+(?P<payload>.*))?\s*$',
    re.IGNORECASE | re.DOTALL,
)

EXPECTED_F1 = {
    "fixture_id": "f1_manifest_join_readonly",
    "selected_job_ids": ["J-101", "J-103", "J-104", "J-112"],
    "total_duration_ms": 2570,
    "team_counts": {"alpha": 1, "beta": 2, "gamma": 1},
}
EXPECTED_F2_FINAL = {
    "fixture_id": "f2_python_bugfix",
    "failed_tests": [
        "test_adjacent_inclusive_windows_merge",
        "test_input_order_is_not_mutated",
    ],
    "root_cause_codes": [
        "in_place_sort_mutates_input",
        "inclusive_adjacency_boundary_missing",
    ],
    "corrected_results": {
        "adjacent_inclusive": [[2, 8]],
        "non_mutating_order": [[1, 2], [8, 9]],
        "overlap": [[1, 7]],
    },
}
EXPECTED_F3_FINAL = {
    "fixture_id": "f3_incident_report",
    "incident_ids": ["E1", "E3", "E4", "E5"],
    "category_counts": {"latency": 1, "normal": 1, "transport": 2},
    "max_latency_ms": 600,
    "source_counts": {"east": 3, "west": 1},
}

SEMANTIC_ALLOWANCES = {
    "f1_manifest_join_readonly": {
        "read_manifest": 1,
        "read_jobs": 1,
        "read_owners": 1,
        "read_rules": 1,
    },
    "f2_python_bugfix": {
        "public_test": 1,
        "read_source": 1,
        "read_tests": 1,
    },
    "f3_incident_report": {
        "read_manifest": 1,
        "read_east": 1,
        "read_west": 1,
        "read_codebook": 1,
        "read_rules": 1,
    },
}

EXPECTED_COMMAND_LABEL_SEQUENCE = {
    "f1_manifest_join_readonly": [
        "read_manifest",
        "read_jobs",
        "read_owners",
        "read_rules",
    ],
    "f2_python_bugfix": ["public_test", "read_source", "read_tests"],
    "f3_incident_report": [
        "read_manifest",
        "read_east",
        "read_west",
        "read_codebook",
        "read_rules",
    ],
}

# These are the only command payloads admitted by the experiment protocol.
# The exec-policy rules are prefix rules, so the post-run audit must reject
# trailing arguments or shell operators even when the router allowed launch.
EXACT_COMMAND_PAYLOADS = {
    "f1_manifest_join_readonly": {
        "python -B read_one.py manifest.json",
        "python -B read_one.py data/jobs.csv",
        "python -B read_one.py data/owners.json",
        "python -B read_one.py data/rules.json",
    },
    "f2_python_bugfix": {
        "python -B -m unittest discover -s tests -v",
        "python -B read_one.py src/windowing.py",
        "python -B read_one.py tests/test_windowing.py",
    },
    "f3_incident_report": {
        "python -B read_one.py manifest.json",
        "python -B read_one.py logs/east.jsonl",
        "python -B read_one.py logs/west.jsonl",
        "python -B read_one.py data/codebook.json",
        "python -B read_one.py data/rules.json",
    },
}


class DuplicateJSONKey(ValueError):
    pass


def strict_json_loads(value: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise DuplicateJSONKey(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(
        value,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def strict_deep_equal(observed: Any, expected: Any) -> bool:
    if type(observed) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (
            set(observed) == set(expected)
            and all(strict_deep_equal(observed[key], expected[key]) for key in expected)
        )
    if isinstance(expected, list):
        return len(observed) == len(expected) and all(
            strict_deep_equal(left, right)
            for left, right in zip(observed, expected)
        )
    return observed == expected


def event_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in result.get("jsonl_events") or []:
        if not isinstance(record, dict) or not isinstance(record.get("event"), dict):
            continue
        rows.append(record)
    return rows


def item_lifecycle_anomalies(result: dict[str, Any]) -> dict[str, Any]:
    active: dict[str, tuple[str, int]] = {}
    completed_without_start = 0
    duplicate_started = 0
    duplicate_terminal = 0
    type_mismatch = 0
    terminal_ids: set[str] = set()
    synthetic = 0
    for index, record in enumerate(event_rows(result)):
        event = record["event"]
        event_type = str(event.get("type", ""))
        if not event_type.startswith("item."):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            synthetic += 1
            item_id = f"__missing_{index}"
        else:
            item_id = raw_id
        item_type = str(item.get("type", ""))
        if event_type == "item.started":
            if item_id in active:
                duplicate_started += 1
            active[item_id] = (item_type, index)
        elif event_type in TERMINAL_ITEM_EVENTS:
            if item_id in terminal_ids:
                duplicate_terminal += 1
            terminal_ids.add(item_id)
            started = active.pop(item_id, None)
            if started is None:
                completed_without_start += 1
            elif started[0] and item_type and started[0] != item_type:
                type_mismatch += 1
    return {
        "abandoned_started_count": len(active),
        "completed_without_start_count": completed_without_start,
        "duplicate_started_count": duplicate_started,
        "duplicate_terminal_count": duplicate_terminal,
        "item_type_mismatch_count": type_mismatch,
        "missing_item_id_count": synthetic,
    }


def command_text(item: dict[str, Any]) -> str:
    value = item.get("command")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return ""


def classify_command(fixture_id: str, command: str) -> str | None:
    normalized = re.sub(r"\\+", "/", command.casefold())
    if fixture_id == "f1_manifest_join_readonly":
        matches = [
            label
            for label, needle in (
                ("read_manifest", "manifest.json"),
                ("read_jobs", "data/jobs.csv"),
                ("read_owners", "data/owners.json"),
                ("read_rules", "data/rules.json"),
            )
            if needle in normalized
        ]
        return matches[0] if len(matches) == 1 else ("batched_read" if matches else None)
    if fixture_id == "f2_python_bugfix":
        if "unittest" in normalized and "discover" in normalized:
            return "public_test"
        matches = [
            label
            for label, needle in (
                ("read_source", "src/windowing.py"),
                ("read_tests", "tests/test_windowing.py"),
            )
            if needle in normalized
        ]
        return matches[0] if len(matches) == 1 else ("batched_read" if matches else None)
    if fixture_id == "f3_incident_report":
        matches = [
            label
            for label, needle in (
                ("read_manifest", "manifest.json"),
                ("read_east", "logs/east.jsonl"),
                ("read_west", "logs/west.jsonl"),
                ("read_codebook", "data/codebook.json"),
                ("read_rules", "data/rules.json"),
            )
            if needle in normalized
        ]
        return matches[0] if len(matches) == 1 else ("batched_read" if matches else None)
    return None


def completed_commands(result: dict[str, Any]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for record in event_rows(result):
        event = record["event"]
        if event.get("type") not in TERMINAL_ITEM_EVENTS:
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            commands.append(
                {
                    "t_ms": record.get("t_ms"),
                    "event_type": event.get("type"),
                    "item": item,
                    "command": command_text(item),
                }
            )
    return commands


def command_failed(record: dict[str, Any]) -> bool:
    item = record["item"]
    exit_code = item.get("exit_code")
    status = str(item.get("status", "")).casefold()
    return (
        record["event_type"] == "item.failed"
        or status in {"failed", "error", "cancelled", "canceled"}
        or (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0)
    )


def is_expected_f2_red_baseline(record: dict[str, Any]) -> bool:
    """Prove the fixed unittest ran and produced the registered two failures."""

    item = record["item"]
    output = str(item.get("aggregated_output") or "")
    status = str(item.get("status", "")).casefold()
    return (
        record["event_type"] == "item.completed"
        and item.get("exit_code") == 1
        and status not in {"declined", "cancelled", "canceled"}
        and "test_adjacent_inclusive_windows_merge" in output
        and "test_input_order_is_not_mutated" in output
        and "FAILED (failures=2)" in output
    )


def command_payload_for_audit(command: str) -> str:
    """Remove only Codex's known Windows PowerShell display wrapper.

    Codex 0.144.4 renders native-Windows shell commands in JSONL as an
    absolute Windows PowerShell path followed by ``-Command``.  The absolute
    wrapper path is infrastructure, not a fixture path reference.  Keep the
    inner payload fully subject to the normal command and path checks.
    """

    match = CODEX_WINDOWS_POWERSHELL_WRAPPER.fullmatch(command)
    if match is None:
        return command
    payload = (match.group("payload") or "").strip()
    if (
        len(payload) >= 2
        and payload[0] == payload[-1]
        and payload[0] in {"'", '"'}
    ):
        payload = payload[1:-1]
    return payload


def normalized_command_payload(command: str) -> str:
    """Normalize insignificant whitespace, preserving every argv token."""

    return re.sub(r"\s+", " ", command_payload_for_audit(command).strip())


def summarize_tools(result: dict[str, Any], fixture_id: str) -> dict[str, Any]:
    commands = completed_commands(result)
    labels: list[str | None] = [
        classify_command(fixture_id, record["command"]) for record in commands
    ]
    label_counts = Counter(label for label in labels if label)
    expected_failure_exempted = 0
    unexpected_failed = 0
    raw_failed = 0
    public_test_results: list[bool] = []
    expected_red_baselines: list[bool] = []
    forbidden: list[str] = []
    outside_path_commands: list[str] = []
    non_python_commands: list[str] = []
    unexpected_commands: list[str] = []
    allowed_payloads = EXACT_COMMAND_PAYLOADS[fixture_id]
    for record, label in zip(commands, labels):
        audit_payload = command_payload_for_audit(record["command"])
        failed = command_failed(record)
        if label == "public_test":
            public_test_results.append(not failed)
            expected_red_baselines.append(is_expected_f2_red_baseline(record))
        if failed:
            raw_failed += 1
            if (
                fixture_id == "f2_python_bugfix"
                and label == "public_test"
                and expected_failure_exempted == 0
                and is_expected_f2_red_baseline(record)
            ):
                expected_failure_exempted = 1
            else:
                unexpected_failed += 1
        if FORBIDDEN_COMMAND.search(audit_payload):
            forbidden.append(record["command"])
        if OUTSIDE_PATH_REFERENCE.search(audit_payload):
            outside_path_commands.append(record["command"])
        if not PYTHON_B_COMMAND.search(audit_payload):
            non_python_commands.append(record["command"])
        if normalized_command_payload(record["command"]) not in allowed_payloads:
            unexpected_commands.append(record["command"])

    allowances = SEMANTIC_ALLOWANCES[fixture_id]
    semantic_excess = sum(
        max(0, label_counts[label] - allowed) for label, allowed in allowances.items()
    )
    unlabeled = [
        re.sub(r"\s+", " ", record["command"].strip())
        for record, label in zip(commands, labels)
        if label is None and record["command"].strip()
    ]
    exact_counts = Counter(unlabeled)
    unlabeled_repeats = sum(max(0, count - 1) for count in exact_counts.values())
    file_change_count = sum(
        1
        for record in event_rows(result)
        if record["event"].get("type") in TERMINAL_ITEM_EVENTS
        and isinstance(record["event"].get("item"), dict)
        and record["event"]["item"].get("type") == "file_change"
    )
    all_item_types = Counter()
    tool_terminal_count = 0
    last_tool_t_ms: float | None = None
    for record in event_rows(result):
        event = record["event"]
        if event.get("type") not in TERMINAL_ITEM_EVENTS:
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        all_item_types[item_type] += 1
        if item_type in TOOL_ITEM_TYPES:
            tool_terminal_count += 1
            if isinstance(record.get("t_ms"), (int, float)):
                last_tool_t_ms = float(record["t_ms"])

    minimum = int(probe.FIXTURES[fixture_id]["minimum_command_completions"])
    expected_sequence = EXPECTED_COMMAND_LABEL_SEQUENCE[fixture_id]
    path_success = (
        len(commands) == minimum
        and labels == expected_sequence
        and label_counts == Counter(allowances)
        and not non_python_commands
        and not unexpected_commands
        and file_change_count == 0
    )
    if fixture_id == "f2_python_bugfix":
        path_success = path_success and (
            public_test_results == [False]
            and expected_red_baselines == [True]
        )

    lifecycle = item_lifecycle_anomalies(result)
    return {
        "completed_command_count": len(commands),
        "tool_terminal_count": tool_terminal_count,
        "file_change_count": file_change_count,
        "raw_failed_command_count": raw_failed,
        "expected_failed_command_count": expected_failure_exempted,
        "expected_red_baseline_evidence": expected_red_baselines == [True],
        "unexpected_failed_command_count": unexpected_failed,
        "duplicate_or_excess_command_count": semantic_excess + unlabeled_repeats,
        "semantic_label_counts": dict(sorted(label_counts.items())),
        "item_type_counts": dict(sorted(all_item_types.items())),
        "forbidden_command_count": len(forbidden),
        "forbidden_commands": forbidden,
        "outside_path_command_count": len(outside_path_commands),
        "outside_path_commands": outside_path_commands,
        "non_python_command_count": len(non_python_commands),
        "non_python_commands": non_python_commands,
        "unexpected_command_count": len(unexpected_commands),
        "unexpected_commands": unexpected_commands,
        "tool_path_success": path_success,
        "last_tool_t_ms": last_tool_t_ms,
        **lifecycle,
    }


def final_agent_message(result: dict[str, Any]) -> tuple[str | None, float | None]:
    final_text: str | None = None
    final_time: float | None = None
    for record in event_rows(result):
        event = record["event"]
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            final_text = str(item.get("text", ""))
            if isinstance(record.get("t_ms"), (int, float)):
                final_time = float(record["t_ms"])
    return final_text, final_time


def classify_transport(result: dict[str, Any]) -> dict[str, bool]:
    event_types = [
        str(record["event"].get("type", "")) for record in event_rows(result)
    ]
    return {
        "turn_completed_success": (
            result.get("exit_code") == 0
            and not result.get("timed_out")
            and result.get("harness_error") is None
            and "turn.completed" in event_types
        ),
        "transport_success": (
            result.get("exit_code") == 0
            and not result.get("timed_out")
            and result.get("harness_error") is None
            and not result.get("non_json_stdout_lines")
            and "turn.completed" in event_types
            and "turn.failed" not in event_types
            and "error" not in event_types
        ),
    }


def changed_paths(result: dict[str, Any]) -> set[str]:
    values = (
        result.get("workspace", {}).get("diff", {}).get("changed_paths", [])
    )
    return {str(value).replace("\\", "/").casefold() for value in values}


def workspace_safety(result: dict[str, Any], fixture_id: str) -> bool:
    changed = changed_paths(result)
    allowed = {
        str(value).casefold() for value in probe.FIXTURES[fixture_id]["allowed_writes"]
    }
    required = {
        str(value).casefold()
        for value in probe.FIXTURES[fixture_id]["required_changed_paths"]
    }
    return not (changed - allowed) and not (required - changed)


def grade_task(
    result: dict[str, Any], fixture_id: str, tools: dict[str, Any]
) -> tuple[bool, str, str | None]:
    message, _ = final_agent_message(result)
    if message is None:
        return False, "missing final agent message", None
    try:
        final = strict_json_loads(message)
    except (ValueError, json.JSONDecodeError) as exc:
        return False, f"invalid final JSON: {exc}", None
    if fixture_id == "f1_manifest_join_readonly":
        return (
            strict_deep_equal(final, EXPECTED_F1),
            "ok" if strict_deep_equal(final, EXPECTED_F1) else "F1 oracle mismatch",
            canonical_sha256(final),
        )
    if fixture_id == "f2_python_bugfix":
        passed = strict_deep_equal(final, EXPECTED_F2_FINAL)
        return (
            passed,
            "ok" if passed else "F2 diagnosis oracle mismatch",
            canonical_sha256(final),
        )
    if fixture_id == "f3_incident_report":
        passed = strict_deep_equal(final, EXPECTED_F3_FINAL)
        return (
            passed,
            "ok" if passed else "F3 incident-report oracle mismatch",
            canonical_sha256(final),
        )
    return False, f"unknown fixture: {fixture_id}", None


def grade_result(result: dict[str, Any]) -> dict[str, Any]:
    fixture_id = str(result.get("fixture_id", ""))
    tools = summarize_tools(result, fixture_id)
    transport = classify_transport(result)
    safety = (
        workspace_safety(result, fixture_id)
        and tools["forbidden_command_count"] == 0
        and tools["outside_path_command_count"] == 0
    )
    task_success, task_reason, artifact_sha = grade_task(result, fixture_id, tools)
    message, message_t_ms = final_agent_message(result)
    protocol_success = (
        message is not None
        and message_t_ms is not None
        and (
            tools["last_tool_t_ms"] is None
            or message_t_ms >= float(tools["last_tool_t_ms"])
        )
        and not result.get("non_json_stdout_lines")
    )
    strict = (
        transport["transport_success"]
        and protocol_success
        and task_success
        and safety
        and tools["tool_path_success"]
        and tools["unexpected_failed_command_count"] == 0
        and tools["abandoned_started_count"] == 0
    )
    language_output = str(
        result.get("powershell_language_mode", {}).get("stdout", "")
    ).strip().splitlines()
    language_mode = language_output[-1].strip() if language_output else None
    return {
        "sequence_index": result.get("sequence_index"),
        "block_id": result.get("block_id"),
        "pair_id": result.get("pair_id"),
        "host_label": result.get("host_label"),
        "host_order": result.get("host_order"),
        "model": result.get("requested_model"),
        "fixture_id": fixture_id,
        "wall_ms": result.get("wall_ms"),
        "powershell_language_mode": language_mode,
        "turn_completed_success": transport["turn_completed_success"],
        "transport_success": transport["transport_success"],
        "protocol_success": protocol_success,
        "task_success": task_success,
        "task_reason": task_reason,
        "workspace_safety_success": safety,
        "tool_path_success": tools["tool_path_success"],
        "strict_agent_success": strict,
        "artifact_sha256": artifact_sha,
        "final_message_sha256": (
            hashlib.sha256(message.encode("utf-8")).hexdigest() if message else None
        ),
        **{
            key: value
            for key, value in tools.items()
            if key not in {
                "forbidden_commands",
                "outside_path_commands",
                "non_python_commands",
                "unexpected_commands",
                "semantic_label_counts",
                "item_type_counts",
            }
        },
        "semantic_label_counts_json": json.dumps(tools["semantic_label_counts"], sort_keys=True),
        "item_type_counts_json": json.dumps(tools["item_type_counts"], sort_keys=True),
    }


def exact_mcnemar_p(main_only: int, remote_only: int) -> float | None:
    discordant = main_only + remote_only
    if discordant == 0:
        return None
    smaller = min(main_only, remote_only)
    probability = sum(math.comb(discordant, value) for value in range(smaller + 1)) / (2**discordant)
    return min(1.0, 2 * probability)


def write_csv(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    return {
        "planned_n": count,
        "transport_success_n": sum(bool(row["transport_success"]) for row in rows),
        "task_success_n": sum(bool(row["task_success"]) for row in rows),
        "strict_agent_success_n": sum(bool(row["strict_agent_success"]) for row in rows),
        "strict_agent_success_rate": (
            sum(bool(row["strict_agent_success"]) for row in rows) / count if count else None
        ),
        "unexpected_failed_commands": sum(int(row["unexpected_failed_command_count"]) for row in rows),
        "duplicate_or_excess_commands": sum(int(row["duplicate_or_excess_command_count"]) for row in rows),
        "workspace_safety_failures": sum(not bool(row["workspace_safety_success"]) for row in rows),
        "powershell_language_modes": dict(
            Counter(
                str(row.get("powershell_language_mode") or "unknown") for row in rows
            )
        ),
    }


def analyze(run_dir: pathlib.Path, output_dir: pathlib.Path | None = None) -> dict[str, Any]:
    schedule = json.loads((run_dir / "schedule.json").read_text(encoding="utf-8"))
    expected_analyzer_hash = schedule.get("parameters", {}).get("analyzer_sha256")
    current_analyzer_hash = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
    if expected_analyzer_hash and current_analyzer_hash != expected_analyzer_hash:
        raise ValueError(
            "analyzer differs from the frozen run version; execute "
            "RUN_DIR/artifacts/analyze_agent_eval.py"
        )
    processed = output_dir or run_dir / "processed" / "agent_eval"
    processed.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    rows_by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for block in schedule["blocks"]:
        for call in block["calls"]:
            path = run_dir / call["local_output_relative"]
            if path.is_file():
                result = json.loads(path.read_text(encoding="utf-8"))
                row = grade_result(result)
                row["missing_result"] = False
            else:
                row = {
                    "sequence_index": call["sequence_index"],
                    "block_id": block["block_id"],
                    "pair_id": block["pair_id"],
                    "host_label": call["host_label"],
                    "host_order": block["host_order_label"],
                    "model": block["model"],
                    "fixture_id": block["fixture_id"],
                    "missing_result": True,
                    "transport_success": False,
                    "task_success": False,
                    "strict_agent_success": False,
                    "workspace_safety_success": False,
                    "unexpected_failed_command_count": 0,
                    "duplicate_or_excess_command_count": 0,
                }
            rows.append(row)
            rows_by_pair[block["pair_id"]][call["host_label"]] = row

    pair_rows: list[dict[str, Any]] = []
    for block in schedule["blocks"]:
        main = rows_by_pair[block["pair_id"]].get("local")
        remote = rows_by_pair[block["pair_id"]].get("remote")
        if main is None or remote is None:
            continue
        main_success = bool(main["strict_agent_success"])
        remote_success = bool(remote["strict_agent_success"])
        if main_success and remote_success:
            outcome = "both_success"
        elif main_success:
            outcome = "main_success_remote_failure"
        elif remote_success:
            outcome = "main_failure_remote_success"
        else:
            outcome = "both_failure"
        main_task_success = bool(main["task_success"])
        remote_task_success = bool(remote["task_success"])
        if main_task_success and remote_task_success:
            task_outcome = "both_success"
        elif main_task_success:
            task_outcome = "main_success_remote_failure"
        elif remote_task_success:
            task_outcome = "main_failure_remote_success"
        else:
            task_outcome = "both_failure"
        dual = main_success and remote_success
        main_wall = main.get("wall_ms")
        remote_wall = remote.get("wall_ms")
        wall_ratio = (
            float(remote_wall) / float(main_wall)
            if dual and isinstance(main_wall, (int, float)) and main_wall > 0 and isinstance(remote_wall, (int, float))
            else None
        )
        pair_rows.append(
            {
                "pair_id": block["pair_id"],
                "block_id": block["block_id"],
                "model": block["model"],
                "fixture_id": block["fixture_id"],
                "rep": block["rep"],
                "host_order": block["host_order_label"],
                "strict_success_outcome": outcome,
                "task_success_outcome": task_outcome,
                "main_strict_success": main_success,
                "remote_strict_success": remote_success,
                "main_task_success": main_task_success,
                "remote_task_success": remote_task_success,
                "strict_success_delta_remote_minus_local": int(remote_success) - int(main_success),
                "wall_ratio_remote_over_local": wall_ratio,
                "unexpected_failed_commands_delta_remote_minus_local": int(remote.get("unexpected_failed_command_count", 0)) - int(main.get("unexpected_failed_command_count", 0)),
                "duplicate_commands_delta_remote_minus_local": int(remote.get("duplicate_or_excess_command_count", 0)) - int(main.get("duplicate_or_excess_command_count", 0)),
            }
        )

    write_csv(processed / "agent_runs.csv", rows)
    write_csv(processed / "paired_effects.csv", pair_rows)
    by_host: dict[str, Any] = {}
    for host in HOSTS:
        by_host[host] = summarize_group([row for row in rows if row["host_label"] == host])
    by_condition: dict[str, Any] = {}
    for model in schedule["parameters"]["models"]:
        for fixture_id in schedule["parameters"]["fixtures"]:
            for host in HOSTS:
                key = f"{model}|{fixture_id}|{host}"
                by_condition[key] = summarize_group(
                    [
                        row
                        for row in rows
                        if row["model"] == model
                        and row["fixture_id"] == fixture_id
                        and row["host_label"] == host
                    ]
                )
    outcomes = Counter(row["strict_success_outcome"] for row in pair_rows)
    task_outcomes = Counter(row["task_success_outcome"] for row in pair_rows)
    main_only = outcomes["main_success_remote_failure"]
    remote_only = outcomes["main_failure_remote_success"]
    task_main_only = task_outcomes["main_success_remote_failure"]
    task_remote_only = task_outcomes["main_failure_remote_success"]
    planned_pairs = len(pair_rows)
    missing_results = sum(bool(row["missing_result"]) for row in rows)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": "codex_agent_eval_summary",
        "run_dir": str(run_dir),
        "planned_calls": schedule["call_count"],
        "planned_pairs": schedule["block_count"],
        "missing_results": missing_results,
        "by_host": by_host,
        "by_condition": by_condition,
        "paired": {
            "strict": {
                "outcomes": dict(outcomes),
                "mcnemar_exact_two_sided_p": exact_mcnemar_p(main_only, remote_only),
                "zero_adverse_one_sided_95_upper": (
                    1 - 0.05 ** (1 / planned_pairs)
                    if planned_pairs and not missing_results and main_only == 0
                    else None
                ),
            },
            "task": {
                "outcomes": dict(task_outcomes),
                "mcnemar_exact_two_sided_p": exact_mcnemar_p(
                    task_main_only, task_remote_only
                ),
                "zero_adverse_one_sided_95_upper": (
                    1 - 0.05 ** (1 / planned_pairs)
                    if planned_pairs and not missing_results and task_main_only == 0
                    else None
                ),
            },
        },
    }
    (processed / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = [
        "# Codex Agent-use paired evaluation",
        "",
        f"- Planned calls: {summary['planned_calls']}",
        f"- Planned pairs: {summary['planned_pairs']}",
        f"- Missing results: {summary['missing_results']}",
        "",
        "## Host results",
        "",
        "| Host | Strict success | Task success | Transport success | Unexpected failed tools | Duplicate/excess tools | PowerShell modes |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for host in HOSTS:
        group = by_host[host]
        report.append(
            f"| {host} | {group['strict_agent_success_n']}/{group['planned_n']} | "
            f"{group['task_success_n']}/{group['planned_n']} | "
            f"{group['transport_success_n']}/{group['planned_n']} | "
            f"{group['unexpected_failed_commands']} | {group['duplicate_or_excess_commands']} | "
            f"{json.dumps(group['powershell_language_modes'], ensure_ascii=False, sort_keys=True)} |"
        )
    report.extend(
        [
            "",
            "## Paired strict outcomes",
            "",
            f"- Both success: {outcomes['both_success']}",
            f"- Main success / 02 failure: {main_only}",
            f"- Main failure / 02 success: {remote_only}",
            f"- Both failure: {outcomes['both_failure']}",
            "",
            "## Paired task-correctness outcomes",
            "",
            f"- Both success: {task_outcomes['both_success']}",
            f"- Main success / 02 failure: {task_main_only}",
            f"- Main failure / 02 success: {task_remote_only}",
            f"- Both failure: {task_outcomes['both_failure']}",
            "",
            "Task correctness, workspace safety, tool-path adherence, and CLI transport are scored separately. F2's single red public baseline requires exit code 1 plus the registered two-failure output; a policy decline is not exempt, and a rerun fails the exact tool protocol.",
            "All three fixtures are read-only. F2 returns a diagnosis and corrected results without editing source; F3 returns the incident report directly without an artifact or validator.",
            "PowerShell language mode is an environment covariate. Any FullLanguage/ConstrainedLanguage difference is reported separately and is not attributed to packet loss.",
        ]
    )
    (processed / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return summary


HOSTS = ("local", "remote")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_dir = pathlib.Path(args.run_dir).resolve()
    output_dir = pathlib.Path(args.output_dir).resolve() if args.output_dir else None
    try:
        analyze(run_dir, output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"analyze_agent_eval: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
