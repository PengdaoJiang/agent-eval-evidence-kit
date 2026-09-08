#!/usr/bin/env python3
"""Supplemental statistics for the fixed 48-call paired Agent evaluation.

This analyzer is deliberately outside the run's frozen artifacts.  It verifies
and dynamically loads ``RUN_DIR/artifacts/analyze_agent_eval.py`` and delegates
every call-level score to that frozen analyzer's ``grade_result`` function.
Only a complete 48-call / 24-pair run is accepted.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import math
import pathlib
import random
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
HOSTS = ("local", "remote")
ORDER_LABELS = ("local_then_remote", "remote_then_local")
OUTCOMES = {
    "strict": "strict_agent_success",
    "task": "task_success",
    "transport": "transport_success",
    "tool_path": "tool_path_success",
}
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
LIFECYCLE_FIELDS = (
    "abandoned_started_count",
    "completed_without_start_count",
    "duplicate_started_count",
    "duplicate_terminal_count",
    "item_type_mismatch_count",
    "missing_item_id_count",
)
EXPECTED_CALLS = 48
EXPECTED_PAIRS = 24
EXPECTED_MODELS = 4
EXPECTED_FIXTURES = 3
EXPECTED_REPETITIONS = 2
DEFAULT_BOOTSTRAP_RESAMPLES = 100_000
DEFAULT_SEED = 20260716
ALPHA = 0.05


class SupplementalAnalysisError(ValueError):
    """The run is incomplete, inconsistent, or not the fixed formal design."""


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SupplementalAnalysisError(f"JSON root is not an object: {path}")
    return value


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def safe_count(value: Any) -> int:
    parsed = safe_int(value)
    return parsed if parsed is not None else 0


def positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SupplementalAnalysisError(f"{label} is not numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise SupplementalAnalysisError(f"{label} must be finite and positive")
    return parsed


def load_frozen_analyzer(
    run_dir: pathlib.Path, schedule: dict[str, Any]
) -> tuple[Any, pathlib.Path, str]:
    """Load the exact analyzer frozen into the run without modifying it."""

    artifact_dir = run_dir / "artifacts"
    probe_path = artifact_dir / "agent_eval_probe.py"
    if not probe_path.is_file():
        raise SupplementalAnalysisError(f"frozen probe is missing: {probe_path}")
    observed_probe_hash = sha256_file(probe_path)
    expected_probe_hash = schedule.get("parameters", {}).get("probe_sha256")
    if not isinstance(expected_probe_hash, str) or not expected_probe_hash:
        raise SupplementalAnalysisError("schedule has no frozen probe hash")
    if observed_probe_hash != expected_probe_hash:
        raise SupplementalAnalysisError(
            "frozen probe hash differs from schedule.parameters.probe_sha256"
        )

    analyzer_path = artifact_dir / "analyze_agent_eval.py"
    if not analyzer_path.is_file():
        raise SupplementalAnalysisError(f"frozen analyzer is missing: {analyzer_path}")
    observed_hash = sha256_file(analyzer_path)
    expected_hash = schedule.get("parameters", {}).get("analyzer_sha256")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise SupplementalAnalysisError("schedule has no frozen analyzer hash")
    if observed_hash != expected_hash:
        raise SupplementalAnalysisError(
            "frozen analyzer hash differs from schedule.parameters.analyzer_sha256"
        )

    module_name = f"_agent_eval_frozen_{observed_hash[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, analyzer_path)
    if spec is None or spec.loader is None:
        raise SupplementalAnalysisError("cannot create frozen analyzer import spec")
    module = importlib.util.module_from_spec(spec)

    # The frozen analyzer imports its sibling frozen agent_eval_probe by the
    # plain module name.  Temporarily prioritize the artifact directory, then
    # restore any caller-owned module binding after the analyzer has captured
    # the frozen probe in its globals.
    marker = object()
    previous_probe = sys.modules.get("agent_eval_probe", marker)
    previous_analyzer = sys.modules.get(module_name, marker)
    sys.modules.pop("agent_eval_probe", None)
    sys.path.insert(0, str(artifact_dir))
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if sys.path and sys.path[0] == str(artifact_dir):
            del sys.path[0]
        else:
            try:
                sys.path.remove(str(artifact_dir))
            except ValueError:
                pass
        sys.modules.pop("agent_eval_probe", None)
        if previous_probe is not marker:
            sys.modules["agent_eval_probe"] = previous_probe
        if previous_analyzer is marker:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_analyzer

    if not callable(getattr(module, "grade_result", None)):
        raise SupplementalAnalysisError("frozen analyzer has no callable grade_result")
    imported_probe = getattr(module, "probe", None)
    imported_probe_file = getattr(imported_probe, "__file__", None)
    if not isinstance(imported_probe_file, str):
        raise SupplementalAnalysisError(
            "frozen analyzer did not import its sibling agent_eval_probe"
        )
    if pathlib.Path(imported_probe_file).resolve() != probe_path.resolve():
        raise SupplementalAnalysisError(
            "frozen analyzer resolved agent_eval_probe outside the run artifacts"
        )
    if sha256_file(pathlib.Path(imported_probe_file)) != expected_probe_hash:
        raise SupplementalAnalysisError("frozen analyzer imported a mismatched probe")
    return module, analyzer_path, observed_hash


def resolve_result_path(run_dir: pathlib.Path, relative: Any) -> pathlib.Path:
    if not isinstance(relative, str) or not relative:
        raise SupplementalAnalysisError("call has no local_output_relative")
    relative_path = pathlib.PurePosixPath(relative.replace("\\", "/"))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise SupplementalAnalysisError(f"unsafe result path: {relative!r}")
    path = (run_dir / pathlib.Path(*relative_path.parts)).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise SupplementalAnalysisError(f"result escapes run directory: {relative}") from exc
    return path


def validate_schedule_manifest_binding(
    schedule_path: pathlib.Path, manifest: dict[str, Any]
) -> str:
    expected = manifest.get("schedule_sha256")
    if not isinstance(expected, str) or not expected:
        raise SupplementalAnalysisError("manifest has no nonempty schedule_sha256")
    observed = sha256_file(schedule_path)
    if observed != expected:
        raise SupplementalAnalysisError(
            "schedule.json hash differs from manifest.schedule_sha256"
        )
    return observed


def validate_fixed_design(
    schedule: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    parameters = schedule.get("parameters")
    blocks = schedule.get("blocks")
    if not isinstance(parameters, dict) or not isinstance(blocks, list):
        raise SupplementalAnalysisError("schedule is missing parameters or blocks")
    models = parameters.get("models")
    fixtures = parameters.get("fixtures")
    if (
        not isinstance(models, list)
        or len(models) != EXPECTED_MODELS
        or len(set(models)) != EXPECTED_MODELS
        or not all(isinstance(value, str) and value for value in models)
    ):
        raise SupplementalAnalysisError("formal design must contain exactly four models")
    if (
        not isinstance(fixtures, list)
        or len(fixtures) != EXPECTED_FIXTURES
        or len(set(fixtures)) != EXPECTED_FIXTURES
        or not all(isinstance(value, str) and value for value in fixtures)
    ):
        raise SupplementalAnalysisError("formal design must contain exactly three fixtures")
    if parameters.get("paired") is not True:
        raise SupplementalAnalysisError("formal design is not marked paired")
    if parameters.get("repetitions") != EXPECTED_REPETITIONS:
        raise SupplementalAnalysisError("formal design must use two repetitions")
    if parameters.get("concurrent_model_calls") != 1:
        raise SupplementalAnalysisError("formal design must use serial model calls")
    if schedule.get("call_count") != EXPECTED_CALLS:
        raise SupplementalAnalysisError("formal schedule must declare 48 calls")
    if schedule.get("block_count") != EXPECTED_PAIRS or len(blocks) != EXPECTED_PAIRS:
        raise SupplementalAnalysisError("formal schedule must contain 24 paired blocks")

    sequence_indices: list[int] = []
    pair_ids: list[str] = []
    result_paths: list[str] = []
    model_counts: Counter[str] = Counter()
    fixture_counts: Counter[str] = Counter()
    cell_counts: Counter[tuple[str, str]] = Counter()
    cell_orders: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    cell_reps: defaultdict[tuple[str, str], set[int]] = defaultdict(set)
    for block in blocks:
        if not isinstance(block, dict):
            raise SupplementalAnalysisError("schedule block is not an object")
        model = block.get("model")
        fixture = block.get("fixture_id")
        pair_id = block.get("pair_id")
        order = block.get("host_order_label")
        calls = block.get("calls")
        if model not in models or fixture not in fixtures:
            raise SupplementalAnalysisError("block has an unknown model or fixture")
        if not isinstance(pair_id, str) or not pair_id or pair_id in pair_ids:
            raise SupplementalAnalysisError("pair_id is missing or duplicated")
        if order not in ORDER_LABELS:
            raise SupplementalAnalysisError(f"unexpected host order: {order!r}")
        expected_order = order.split("_then_")
        if block.get("host_order") != expected_order:
            raise SupplementalAnalysisError("block host_order does not match its label")
        if not isinstance(calls, list) or len(calls) != 2:
            raise SupplementalAnalysisError("every paired block must contain two calls")
        call_hosts = [call.get("host_label") for call in calls if isinstance(call, dict)]
        if call_hosts != expected_order or set(call_hosts) != set(HOSTS):
            raise SupplementalAnalysisError("block calls do not follow the declared host order")
        rep = block.get("rep")
        if isinstance(rep, bool) or rep not in {1, 2}:
            raise SupplementalAnalysisError("block repetition must be 1 or 2")

        pair_ids.append(pair_id)
        model_counts[str(model)] += 1
        fixture_counts[str(fixture)] += 1
        cell = (str(model), str(fixture))
        cell_counts[cell] += 1
        cell_orders[cell].add(str(order))
        cell_reps[cell].add(int(rep))
        for call in calls:
            sequence = call.get("sequence_index")
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise SupplementalAnalysisError("call sequence_index is not an integer")
            sequence_indices.append(sequence)
            relative = call.get("local_output_relative")
            if not isinstance(relative, str) or relative in result_paths:
                raise SupplementalAnalysisError("result path is missing or duplicated")
            result_paths.append(relative)

    if sorted(sequence_indices) != list(range(1, EXPECTED_CALLS + 1)):
        raise SupplementalAnalysisError("call sequence indices are not exactly 1..48")
    if any(model_counts[model] != 6 for model in models):
        raise SupplementalAnalysisError("each model must contribute exactly six pairs")
    if any(fixture_counts[fixture] != 8 for fixture in fixtures):
        raise SupplementalAnalysisError("each fixture must contribute exactly eight pairs")
    for model in models:
        for fixture in fixtures:
            cell = (model, fixture)
            if cell_counts[cell] != 2:
                raise SupplementalAnalysisError("each model x fixture cell must contain two pairs")
            if cell_orders[cell] != set(ORDER_LABELS):
                raise SupplementalAnalysisError(
                    "each model x fixture cell must contain both host orders"
                )
            if cell_reps[cell] != {1, 2}:
                raise SupplementalAnalysisError(
                    "each model x fixture cell must contain repetitions 1 and 2"
                )

    if manifest.get("state") != "completed":
        raise SupplementalAnalysisError("manifest state is not completed")
    if manifest.get("completed_call_count") != EXPECTED_CALLS:
        raise SupplementalAnalysisError("manifest does not record 48 completed calls")
    if manifest.get("failed_call_count") not in {0, None}:
        raise SupplementalAnalysisError("manifest retains failed final calls")
    manifest_calls = manifest.get("calls")
    if not isinstance(manifest_calls, dict) or set(manifest_calls) != {
        str(index) for index in range(1, EXPECTED_CALLS + 1)
    }:
        raise SupplementalAnalysisError("manifest call registry is not exactly 1..48")
    for sequence, state in manifest_calls.items():
        if not isinstance(state, dict) or state.get("status") != "completed":
            raise SupplementalAnalysisError(f"manifest call {sequence} is not completed")
        if not isinstance(state.get("result_sha256"), str):
            raise SupplementalAnalysisError(f"manifest call {sequence} has no result hash")

    return {
        "models": models,
        "fixtures": fixtures,
        "model_pair_counts": dict(model_counts),
        "fixture_pair_counts": dict(fixture_counts),
        "cell_pair_count": 2,
        "orders_per_cell": list(ORDER_LABELS),
    }


def validate_result_identity(
    result: dict[str, Any],
    schedule: dict[str, Any],
    block: dict[str, Any],
    call: dict[str, Any],
    path: pathlib.Path,
    manifest_state: dict[str, Any],
) -> None:
    expected = {
        "kind": "codex_agent_eval_call",
        "host_label": call["host_label"],
        "sequence_index": call["sequence_index"],
        "block_id": block["block_id"],
        "pair_id": block["pair_id"],
        "host_order": block["host_order_label"],
        "fixture_id": block["fixture_id"],
        "requested_model": block["model"],
        "reasoning_effort": schedule["parameters"]["effort"],
    }
    for key, value in expected.items():
        if result.get(key) != value:
            raise SupplementalAnalysisError(
                f"result {path.name} identity mismatch for {key}: "
                f"expected {value!r}, got {result.get(key)!r}"
            )
    if sha256_file(path) != manifest_state.get("result_sha256"):
        raise SupplementalAnalysisError(f"result hash differs from manifest: {path}")


def attempt_audit(manifest_state: dict[str, Any]) -> tuple[int, int]:
    attempts = manifest_state.get("attempts")
    if not isinstance(attempts, list):
        return 0, 0
    invalid = 0
    for attempt in attempts:
        if not isinstance(attempt, dict):
            invalid += 1
            continue
        outcome = attempt.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("result_valid") is not True:
            invalid += 1
    return len(attempts), invalid


def build_call_row(
    raw: dict[str, Any],
    grade: dict[str, Any],
    expected_command_count: int,
    manifest_state: dict[str, Any],
) -> dict[str, Any]:
    required_bools = tuple(OUTCOMES.values()) + (
        "protocol_success",
        "workspace_safety_success",
    )
    for field in required_bools:
        if not isinstance(grade.get(field), bool):
            raise SupplementalAnalysisError(f"frozen grade_result omitted boolean {field}")
    wall_ms = positive_number(raw.get("wall_ms"), "result.wall_ms")
    completed_commands = safe_count(grade.get("completed_command_count"))
    unexpected_commands = safe_count(grade.get("unexpected_command_count"))
    terminal_tools = safe_count(grade.get("tool_terminal_count"))
    attempts, invalid_attempts = attempt_audit(manifest_state)
    lifecycle = {field: safe_count(grade.get(field)) for field in LIFECYCLE_FIELDS}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    transport_success = bool(grade["transport_success"])
    reasons: list[str] = []
    if raw.get("timed_out") is True:
        reasons.append("timeout")
    if raw.get("harness_error") not in {None, ""}:
        reasons.append("harness_error")
    if not transport_success:
        reasons.append("transport_failure")
    row: dict[str, Any] = {
        "sequence_index": int(raw["sequence_index"]),
        "block_id": str(raw["block_id"]),
        "pair_id": str(raw["pair_id"]),
        "host_label": str(raw["host_label"]),
        "host_order": str(raw["host_order"]),
        "model": str(raw["requested_model"]),
        "fixture_id": str(raw["fixture_id"]),
        "wall_ms": wall_ms,
        "exit_code": raw.get("exit_code"),
        "timed_out": raw.get("timed_out") is True,
        "harness_error_present": raw.get("harness_error") not in {None, ""},
        "operational_failure": not transport_success,
        "operational_failure_reasons": ";".join(reasons),
        "protocol_expected_command_count": expected_command_count,
        "completed_command_count": completed_commands,
        "expected_payload_command_count": max(
            0, min(expected_command_count, completed_commands - unexpected_commands)
        ),
        "unexpected_command_count": unexpected_commands,
        "other_tool_terminal_count": max(0, terminal_tools - completed_commands),
        "tool_terminal_count": terminal_tools,
        "file_change_count": safe_count(grade.get("file_change_count")),
        "expected_failed_command_count": safe_count(
            grade.get("expected_failed_command_count")
        ),
        "unexpected_failed_command_count": safe_count(
            grade.get("unexpected_failed_command_count")
        ),
        "duplicate_or_excess_command_count": safe_count(
            grade.get("duplicate_or_excess_command_count")
        ),
        "forbidden_command_count": safe_count(grade.get("forbidden_command_count")),
        "outside_path_command_count": safe_count(
            grade.get("outside_path_command_count")
        ),
        "non_python_command_count": safe_count(grade.get("non_python_command_count")),
        "manifest_attempt_count": attempts,
        "manifest_invalid_attempt_count": invalid_attempts,
        "item_type_counts_json": str(grade.get("item_type_counts_json") or "{}"),
        "semantic_label_counts_json": str(
            grade.get("semantic_label_counts_json") or "{}"
        ),
        "usage_present": bool(usage),
        **{field: bool(grade[field]) for field in required_bools},
        **lifecycle,
    }
    row["lifecycle_anomaly_count"] = sum(lifecycle.values())
    for field in TOKEN_FIELDS:
        row[field] = safe_int(usage.get(field))
    return row


def pair_outcome(main: bool, remote: bool) -> str:
    if main and remote:
        return "both_success"
    if main:
        return "main_only_adverse_for_02"
    if remote:
        return "remote_only"
    return "both_failure"


def build_pairs(
    schedule: dict[str, Any], calls_by_pair: dict[str, dict[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for block in schedule["blocks"]:
        hosts = calls_by_pair.get(block["pair_id"], {})
        if set(hosts) != set(HOSTS):
            raise SupplementalAnalysisError(f"pair {block['pair_id']} lacks both hosts")
        pairs.append(
            {
                "pair_id": block["pair_id"],
                "block_id": block["block_id"],
                "model": block["model"],
                "fixture_id": block["fixture_id"],
                "rep": block["rep"],
                "host_order": block["host_order_label"],
                "hosts": hosts,
            }
        )
    return pairs


def flatten_pair(pair: dict[str, Any]) -> dict[str, Any]:
    main = pair["hosts"]["local"]
    remote = pair["hosts"]["remote"]
    transport_ratio_eligible = bool(
        main["transport_success"]
        and remote["transport_success"]
        and not main["timed_out"]
        and not remote["timed_out"]
    )
    strict_ratio_eligible = bool(
        main["strict_agent_success"]
        and remote["strict_agent_success"]
        and not main["timed_out"]
        and not remote["timed_out"]
    )
    row: dict[str, Any] = {
        key: pair[key]
        for key in ("pair_id", "block_id", "model", "fixture_id", "rep", "host_order")
    }
    for name, field in OUTCOMES.items():
        main_value = bool(main[field])
        remote_value = bool(remote[field])
        row[f"{name}_main"] = main_value
        row[f"{name}_remote"] = remote_value
        row[f"{name}_delta_remote_minus_local"] = int(remote_value) - int(main_value)
        row[f"{name}_outcome"] = pair_outcome(main_value, remote_value)
    row.update(
        {
            "wall_ms_main": main["wall_ms"],
            "wall_ms_remote": remote["wall_ms"],
            "transport_complete_ratio_eligible": transport_ratio_eligible,
            "both_strict_ratio_eligible": strict_ratio_eligible,
            "wall_ratio_remote_over_local": (
                remote["wall_ms"] / main["wall_ms"]
                if transport_ratio_eligible
                else None
            ),
            "timeout_main": main["timed_out"],
            "timeout_remote": remote["timed_out"],
            "operational_failure_main": main["operational_failure"],
            "operational_failure_remote": remote["operational_failure"],
            "expected_commands_main": main["protocol_expected_command_count"],
            "expected_commands_remote": remote["protocol_expected_command_count"],
            "other_tools_main": main["other_tool_terminal_count"],
            "other_tools_remote": remote["other_tool_terminal_count"],
            "lifecycle_anomalies_main": main["lifecycle_anomaly_count"],
            "lifecycle_anomalies_remote": remote["lifecycle_anomaly_count"],
        }
    )
    for field in TOKEN_FIELDS:
        row[f"{field}_main"] = main[field]
        row[f"{field}_remote"] = remote[field]
        row[f"{field}_delta_remote_minus_local"] = (
            remote[field] - main[field]
            if remote[field] is not None and main[field] is not None
            else None
        )
    return row


def exact_two_sided_binomial(left: int, right: int) -> float:
    discordant = left + right
    if discordant == 0:
        return 1.0
    smaller = min(left, right)
    lower = sum(math.comb(discordant, value) for value in range(smaller + 1))
    return min(1.0, 2.0 * lower / (2**discordant))


def exact_one_sided_harm(adverse: int, beneficial: int) -> float:
    """P[X >= adverse] under X~Binomial(discordant, .5).

    Here an adverse discordance means main succeeds while remote fails.
    """

    discordant = adverse + beneficial
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, value) for value in range(adverse, discordant + 1))
    return tail / (2**discordant)


def zero_event_upper_95(n: int, observed: int) -> float | None:
    if n <= 0 or observed != 0:
        return None
    return 1.0 - ALPHA ** (1.0 / n)


def quantile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def deterministic_rng(seed: int, label: str) -> random.Random:
    payload = f"{seed}|{label}".encode("utf-8")
    derived = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return random.Random(derived)


def stratified_bootstrap(
    pairs: list[dict[str, Any]],
    strata_fields: tuple[str, ...],
    statistic: Callable[[list[dict[str, Any]]], dict[str, float | None]],
    resamples: int,
    seed: int,
    label: str,
) -> dict[str, dict[str, Any]]:
    if not pairs:
        return {}
    grouped: defaultdict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        grouped[tuple(pair[field] for field in strata_fields)].append(pair)
    strata = [grouped[key] for key in sorted(grouped, key=lambda item: tuple(map(str, item)))]
    rng = deterministic_rng(seed, label)
    distributions: defaultdict[str, list[float]] = defaultdict(list)
    for _ in range(resamples):
        sample: list[dict[str, Any]] = []
        for stratum in strata:
            sample.extend(rng.choice(stratum) for _ in range(len(stratum)))
        for metric, value in statistic(sample).items():
            if value is not None and math.isfinite(float(value)):
                distributions[metric].append(float(value))
    return {
        metric: {
            "bootstrap_95_ci_low": quantile(values, ALPHA / 2),
            "bootstrap_95_ci_high": quantile(values, 1 - ALPHA / 2),
            "valid_resamples": len(values),
            "requested_resamples": resamples,
        }
        for metric, values in distributions.items()
    }


def binary_summary(
    pairs: list[dict[str, Any]], outcome: str, infer: bool
) -> dict[str, Any]:
    field = OUTCOMES[outcome]
    counts: Counter[str] = Counter()
    deltas: list[int] = []
    main_success = 0
    remote_success = 0
    for pair in pairs:
        main = bool(pair["hosts"]["local"][field])
        remote = bool(pair["hosts"]["remote"][field])
        counts[pair_outcome(main, remote)] += 1
        main_success += int(main)
        remote_success += int(remote)
        deltas.append(int(remote) - int(main))
    n = len(pairs)
    adverse = counts["main_only_adverse_for_02"]
    beneficial = counts["remote_only"]
    return {
        "scheduled_pair_n": n,
        "main_success_n": main_success,
        "remote_success_n": remote_success,
        "main_success_rate": main_success / n if n else None,
        "remote_success_rate": remote_success / n if n else None,
        "risk_difference_remote_minus_local": statistics.fmean(deltas) if deltas else None,
        "outcomes": {
            "both_success": counts["both_success"],
            "main_only_adverse_for_02": adverse,
            "remote_only": beneficial,
            "both_failure": counts["both_failure"],
        },
        "discordant_n": adverse + beneficial,
        "mcnemar_exact_two_sided_p": (
            exact_two_sided_binomial(adverse, beneficial) if infer else None
        ),
        "binomial_exact_one_sided_harm_p": (
            exact_one_sided_harm(adverse, beneficial) if infer else None
        ),
        "zero_adverse_pair_rate_exact_95_upper": zero_event_upper_95(n, adverse),
        "inference_status": (
            "prespecified_primary" if infer and outcome == "strict" and n == 24
            else "exploratory_small_sample" if infer
            else "descriptive_only"
        ),
    }


def binary_bootstrap_statistic(sample: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for outcome, field in OUTCOMES.items():
        deltas = [
            int(pair["hosts"]["remote"][field])
            - int(pair["hosts"]["local"][field])
            for pair in sample
        ]
        result[f"{outcome}_rd"] = statistics.fmean(deltas)
    return result


def geomean(values: list[float]) -> float | None:
    if not values or any(value <= 0 for value in values):
        return None
    return math.exp(statistics.fmean(math.log(value) for value in values))


def wall_eligible(pair: dict[str, Any], analysis_set: str) -> bool:
    main = pair["hosts"]["local"]
    remote = pair["hosts"]["remote"]
    no_timeout = not main["timed_out"] and not remote["timed_out"]
    if analysis_set == "transport_complete":
        return bool(
            main["transport_success"] and remote["transport_success"] and no_timeout
        )
    if analysis_set == "both_strict":
        return bool(
            main["strict_agent_success"]
            and remote["strict_agent_success"]
            and no_timeout
        )
    raise KeyError(analysis_set)


def wall_ratio(pair: dict[str, Any]) -> float:
    return pair["hosts"]["remote"]["wall_ms"] / pair["hosts"]["local"]["wall_ms"]


def wall_summary(
    pairs: list[dict[str, Any]], analysis_set: str, infer: bool
) -> dict[str, Any]:
    eligible = [pair for pair in pairs if wall_eligible(pair, analysis_set)]
    ratios = [wall_ratio(pair) for pair in eligible]
    slower = sum(ratio > 1 and not math.isclose(ratio, 1.0, rel_tol=1e-12) for ratio in ratios)
    faster = sum(ratio < 1 and not math.isclose(ratio, 1.0, rel_tol=1e-12) for ratio in ratios)
    ties = len(ratios) - slower - faster
    return {
        "scheduled_pair_n": len(pairs),
        "eligible_pair_n": len(eligible),
        "excluded_pair_n": len(pairs) - len(eligible),
        "median_wall_ratio_remote_over_local": statistics.median(ratios) if ratios else None,
        "geometric_mean_wall_ratio_remote_over_local": geomean(ratios),
        "remote_slower_n": slower,
        "remote_faster_n": faster,
        "tie_n": ties,
        "sign_test_n": slower + faster,
        "sign_exact_two_sided_p": (
            exact_two_sided_binomial(slower, faster) if infer else None
        ),
        "sign_exact_one_sided_02_slower_p": (
            exact_one_sided_harm(slower, faster) if infer else None
        ),
        "inference_status": "exploratory_small_sample" if infer else "descriptive_only",
        "timeout_or_transport_failures_are_not_ratios": True,
    }


def wall_bootstrap_statistic(sample: list[dict[str, Any]]) -> dict[str, float | None]:
    ratios = [wall_ratio(pair) for pair in sample]
    return {
        "median": statistics.median(ratios) if ratios else None,
        "geomean": geomean(ratios),
    }


def correct_tasks_per_minute(
    pairs: list[dict[str, Any]], field: str, host: str
) -> float:
    calls = [pair["hosts"][host] for pair in pairs]
    successes = sum(bool(call[field]) for call in calls)
    wall_ms = sum(float(call["wall_ms"]) for call in calls)
    return 60_000.0 * successes / wall_ms


def efficiency_point(pairs: list[dict[str, Any]], outcome: str) -> dict[str, Any]:
    field = OUTCOMES[outcome]
    main = correct_tasks_per_minute(pairs, field, "local")
    remote = correct_tasks_per_minute(pairs, field, "remote")
    return {
        "scheduled_calls_per_host": len(pairs),
        "main_correct_tasks_per_min": main,
        "remote_correct_tasks_per_min": remote,
        "difference_remote_minus_local": remote - main,
        "ratio_remote_over_local": remote / main if main > 0 else None,
        "formula": "60000 * sum(success) / sum(wall_ms); failures retain wall time",
    }


def efficiency_bootstrap_statistic(
    sample: list[dict[str, Any]],
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for outcome in ("strict", "task"):
        point = efficiency_point(sample, outcome)
        for metric in (
            "main_correct_tasks_per_min",
            "remote_correct_tasks_per_min",
            "difference_remote_minus_local",
            "ratio_remote_over_local",
        ):
            result[f"{outcome}_{metric}"] = point[metric]
    return result


def summarize_group(
    pairs: list[dict[str, Any]],
    *,
    level: str,
    value: str,
    infer: bool,
    strata_fields: tuple[str, ...],
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "level": level,
        "value": value,
        "scheduled_pair_n": len(pairs),
        "outcomes": {
            outcome: binary_summary(pairs, outcome, infer)
            for outcome in OUTCOMES
        },
        "wall": {},
        "efficiency": {
            outcome: efficiency_point(pairs, outcome)
            for outcome in ("strict", "task")
        },
    }
    if infer:
        intervals = stratified_bootstrap(
            pairs,
            strata_fields,
            binary_bootstrap_statistic,
            resamples,
            seed,
            f"{level}|{value}|binary",
        )
        for outcome in OUTCOMES:
            summary["outcomes"][outcome].update(intervals.get(f"{outcome}_rd", {}))

    for analysis_set in ("transport_complete", "both_strict"):
        wall = wall_summary(pairs, analysis_set, infer)
        eligible = [pair for pair in pairs if wall_eligible(pair, analysis_set)]
        if infer and eligible:
            wall_intervals = stratified_bootstrap(
                eligible,
                strata_fields,
                wall_bootstrap_statistic,
                resamples,
                seed,
                f"{level}|{value}|wall|{analysis_set}",
            )
            median_ci = wall_intervals.get("median", {})
            geomean_ci = wall_intervals.get("geomean", {})
            wall.update(
                {
                    "median_bootstrap_95_ci_low": median_ci.get("bootstrap_95_ci_low"),
                    "median_bootstrap_95_ci_high": median_ci.get("bootstrap_95_ci_high"),
                    "geomean_bootstrap_95_ci_low": geomean_ci.get("bootstrap_95_ci_low"),
                    "geomean_bootstrap_95_ci_high": geomean_ci.get("bootstrap_95_ci_high"),
                    "bootstrap_valid_resamples": min(
                        median_ci.get("valid_resamples", 0),
                        geomean_ci.get("valid_resamples", 0),
                    ),
                    "bootstrap_requested_resamples": resamples,
                }
            )
        summary["wall"][analysis_set] = wall

    if infer:
        efficiency_intervals = stratified_bootstrap(
            pairs,
            strata_fields,
            efficiency_bootstrap_statistic,
            resamples,
            seed,
            f"{level}|{value}|efficiency",
        )
        for outcome in ("strict", "task"):
            for metric in (
                "main_correct_tasks_per_min",
                "remote_correct_tasks_per_min",
                "difference_remote_minus_local",
                "ratio_remote_over_local",
            ):
                interval = efficiency_intervals.get(f"{outcome}_{metric}", {})
                summary["efficiency"][outcome][f"{metric}_bootstrap_95_ci_low"] = (
                    interval.get("bootstrap_95_ci_low")
                )
                summary["efficiency"][outcome][f"{metric}_bootstrap_95_ci_high"] = (
                    interval.get("bootstrap_95_ci_high")
                )
                summary["efficiency"][outcome][f"{metric}_valid_resamples"] = (
                    interval.get("valid_resamples", 0)
                )
    return summary


def holm_adjust(values: dict[str, float | None]) -> dict[str, float | None]:
    present = sorted(
        ((key, float(value)) for key, value in values.items() if value is not None),
        key=lambda item: (item[1], item[0]),
    )
    adjusted: dict[str, float | None] = {key: None for key in values}
    running = 0.0
    count = len(present)
    for index, (key, value) in enumerate(present):
        candidate = min(1.0, (count - index) * value)
        running = max(running, candidate)
        adjusted[key] = running
    return adjusted


def apply_holm(groups: dict[str, dict[str, Any]]) -> None:
    for outcome in OUTCOMES:
        for raw_field, adjusted_field in (
            ("mcnemar_exact_two_sided_p", "mcnemar_exact_two_sided_p_holm"),
            (
                "binomial_exact_one_sided_harm_p",
                "binomial_exact_one_sided_harm_p_holm",
            ),
        ):
            adjusted = holm_adjust(
                {key: group["outcomes"][outcome][raw_field] for key, group in groups.items()}
            )
            for key, value in adjusted.items():
                groups[key]["outcomes"][outcome][adjusted_field] = value
    for analysis_set in ("transport_complete", "both_strict"):
        for raw_field, adjusted_field in (
            ("sign_exact_two_sided_p", "sign_exact_two_sided_p_holm"),
            (
                "sign_exact_one_sided_02_slower_p",
                "sign_exact_one_sided_02_slower_p_holm",
            ),
        ):
            adjusted = holm_adjust(
                {key: group["wall"][analysis_set][raw_field] for key, group in groups.items()}
            )
            for key, value in adjusted.items():
                groups[key]["wall"][analysis_set][adjusted_field] = value


def token_field_summary(calls: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [call[field] for call in calls if call[field] is not None]
    return {
        "present_n": len(values),
        "missing_n": len(calls) - len(values),
        "sum": sum(values) if values else None,
        "median": statistics.median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def usage_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        host: {
            "call_n": len(host_calls := [call for call in calls if call["host_label"] == host]),
            "usage_object_present_n": sum(call["usage_present"] for call in host_calls),
            "fields": {
                field: token_field_summary(host_calls, field) for field in TOKEN_FIELDS
            },
        }
        for host in HOSTS
    }


def audit_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    tool_fields = (
        "protocol_expected_command_count",
        "expected_payload_command_count",
        "completed_command_count",
        "unexpected_command_count",
        "other_tool_terminal_count",
        "tool_terminal_count",
        "file_change_count",
        "expected_failed_command_count",
        "unexpected_failed_command_count",
        "duplicate_or_excess_command_count",
        "forbidden_command_count",
        "outside_path_command_count",
        "non_python_command_count",
    )
    for host in HOSTS:
        host_calls = [call for call in calls if call["host_label"] == host]
        lifecycle_totals = {
            field: sum(call[field] for call in host_calls) for field in LIFECYCLE_FIELDS
        }
        result[host] = {
            "call_n": len(host_calls),
            "strict_success_n": sum(call["strict_agent_success"] for call in host_calls),
            "task_success_n": sum(call["task_success"] for call in host_calls),
            "transport_success_n": sum(call["transport_success"] for call in host_calls),
            "tool_path_success_n": sum(call["tool_path_success"] for call in host_calls),
            "operational_failure_n": sum(call["operational_failure"] for call in host_calls),
            "timeout_n": sum(call["timed_out"] for call in host_calls),
            "harness_error_n": sum(call["harness_error_present"] for call in host_calls),
            "tool_counts": {
                field: sum(call[field] for call in host_calls) for field in tool_fields
            },
            "lifecycle": {
                **lifecycle_totals,
                "calls_with_any_anomaly": sum(
                    call["lifecycle_anomaly_count"] > 0 for call in host_calls
                ),
            },
            "execution_attempts": {
                "attempt_n": sum(call["manifest_attempt_count"] for call in host_calls),
                "invalid_attempt_n": sum(
                    call["manifest_invalid_attempt_count"] for call in host_calls
                ),
                "calls_with_retries": sum(
                    call["manifest_attempt_count"] > 1 for call in host_calls
                ),
            },
        }
    return result


def host_order_sensitivity(
    pairs: list[dict[str, Any]], resamples: int, seed: int
) -> dict[str, Any]:
    by_order = {
        order: summarize_group(
            [pair for pair in pairs if pair["host_order"] == order],
            level="host_order",
            value=order,
            infer=False,
            strata_fields=("model", "fixture_id"),
            resamples=resamples,
            seed=seed,
        )
        for order in ORDER_LABELS
    }
    cells: defaultdict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for pair in pairs:
        cells[(pair["model"], pair["fixture_id"])][pair["host_order"]] = pair
    complete_cells = [value for value in cells.values() if set(value) == set(ORDER_LABELS)]

    def outcome_contrast(sample: list[dict[str, dict[str, Any]]]) -> dict[str, float]:
        metrics: dict[str, float] = {}
        for outcome, field in OUTCOMES.items():
            differences: list[int] = []
            for cell in sample:
                remote_first = cell["remote_then_local"]
                main_first = cell["local_then_remote"]
                remote_first_delta = (
                    int(remote_first["hosts"]["remote"][field])
                    - int(remote_first["hosts"]["local"][field])
                )
                main_first_delta = (
                    int(main_first["hosts"]["remote"][field])
                    - int(main_first["hosts"]["local"][field])
                )
                differences.append(remote_first_delta - main_first_delta)
            metrics[outcome] = statistics.fmean(differences)
        return metrics

    point = outcome_contrast(complete_cells)
    rng = deterministic_rng(seed, "host-order-outcome-contrast")
    distributions: defaultdict[str, list[float]] = defaultdict(list)
    for _ in range(resamples):
        sample = [rng.choice(complete_cells) for _ in range(len(complete_cells))]
        for metric, value in outcome_contrast(sample).items():
            distributions[metric].append(value)
    outcome_contrasts = {
        outcome: {
            "rd_difference_remote_first_minus_main_first": point[outcome],
            "bootstrap_95_ci_low": quantile(distributions[outcome], ALPHA / 2),
            "bootstrap_95_ci_high": quantile(distributions[outcome], 1 - ALPHA / 2),
            "cell_n": len(complete_cells),
            "inference_status": "secondary_exploratory_order_sensitivity",
        }
        for outcome in OUTCOMES
    }

    wall_contrasts: dict[str, Any] = {}
    for analysis_set in ("transport_complete", "both_strict"):
        eligible_cells = [
            cell
            for cell in complete_cells
            if all(wall_eligible(cell[order], analysis_set) for order in ORDER_LABELS)
        ]
        log_differences = [
            math.log(wall_ratio(cell["remote_then_local"]))
            - math.log(wall_ratio(cell["local_then_remote"]))
            for cell in eligible_cells
        ]
        interval_values: list[float] = []
        if eligible_cells:
            wall_rng = deterministic_rng(seed, f"host-order-wall|{analysis_set}")
            for _ in range(resamples):
                sample = [wall_rng.choice(log_differences) for _ in range(len(log_differences))]
                interval_values.append(math.exp(statistics.fmean(sample)))
        wall_contrasts[analysis_set] = {
            "complete_model_fixture_cell_n": len(eligible_cells),
            "geometric_ratio_of_ratios_remote_first_over_main_first": (
                math.exp(statistics.fmean(log_differences)) if log_differences else None
            ),
            "bootstrap_95_ci_low": quantile(interval_values, ALPHA / 2),
            "bootstrap_95_ci_high": quantile(interval_values, 1 - ALPHA / 2),
            "inference_status": "secondary_exploratory_order_sensitivity",
        }
    return {
        "by_order": by_order,
        "outcome_rd_contrasts": outcome_contrasts,
        "wall_ratio_contrasts": wall_contrasts,
        "interpretation": (
            "Each model x fixture cell contributes one pair in each order. "
            "The contrast is a sensitivity diagnostic, not a packet-loss effect."
        ),
    }


def group_csv_rows(groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups:
        common = {
            "level": group["level"],
            "value": group["value"],
            "scheduled_pair_n": group["scheduled_pair_n"],
        }
        for outcome, metric in group["outcomes"].items():
            rows.append({**common, "metric_type": "paired_binary", "metric": outcome, **metric})
        for analysis_set, metric in group["wall"].items():
            rows.append({**common, "metric_type": "wall_ratio", "metric": analysis_set, **metric})
        for outcome, metric in group["efficiency"].items():
            rows.append({**common, "metric_type": "correct_tasks_per_min", "metric": outcome, **metric})
    for row in rows:
        if isinstance(row.get("outcomes"), dict):
            row["outcomes"] = json.dumps(row["outcomes"], sort_keys=True)
    return rows


def write_csv(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "NA"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def pct(value: Any) -> str:
    return "NA" if value is None else f"{100 * float(value):.1f}%"


def fmt_p(value: Any) -> str:
    if value is None:
        return "NA"
    parsed = float(value)
    if 0 < parsed < 0.001:
        return f"{parsed:.2e}"
    return f"{parsed:.3f}"


def interval(metric: dict[str, Any], prefix: str = "bootstrap_95_ci") -> str:
    return (
        f"[{fmt(metric.get(prefix + '_low'))}, "
        f"{fmt(metric.get(prefix + '_high'))}]"
    )


def build_report(summary: dict[str, Any]) -> str:
    overall = summary["groups"]["overall"]
    strict = overall["outcomes"]["strict"]
    lines = [
        "# Supplemental paired Agent evaluation",
        "",
        "## Completion and analysis status",
        "",
        f"- Complete formal run: {summary['design']['completed_calls']}/48 calls, {summary['design']['completed_pairs']}/24 pairs.",
        f"- Frozen scorer: `{summary['frozen_grader']['path']}` (`{summary['frozen_grader']['sha256']}`). Every call was scored by its `grade_result`.",
        f"- Bootstrap: {summary['analysis_plan']['bootstrap_resamples']:,} deterministic paired resamples; seed {summary['analysis_plan']['seed']}.",
        "",
        "## Primary endpoint: strict Agent success (ITT)",
        "",
        "All 24 scheduled pairs are retained. Timeout, harness, and transport failures are operational failures and therefore strict failures.",
        "",
        "| Main | 02 | RD (02-main) | Stratified bootstrap 95% CI | Main-only adverse | 02-only | Exact McNemar p | One-sided harm p |",
        "| ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |",
        f"| {strict['main_success_n']}/24 | {strict['remote_success_n']}/24 | {fmt(strict['risk_difference_remote_minus_local'])} | {interval(strict)} | {strict['outcomes']['main_only_adverse_for_02']} | {strict['outcomes']['remote_only']} | {fmt_p(strict['mcnemar_exact_two_sided_p'])} | {fmt_p(strict['binomial_exact_one_sided_harm_p'])} |",
        "",
    ]
    if strict["zero_adverse_pair_rate_exact_95_upper"] is not None:
        lines.append(
            "With zero main-only adverse strict pairs, the exact one-sided 95% upper bound on that pair-event rate is "
            f"{pct(strict['zero_adverse_pair_rate_exact_95_upper'])}. This is not an equivalence or non-inferiority result."
        )
        lines.append("")

    lines.extend(
        [
            "## Secondary paired outcomes",
            "",
            "| Outcome | Main | 02 | RD (02-main) | Bootstrap 95% CI | McNemar p | One-sided harm p |",
            "| --- | ---: | ---: | ---: | --- | ---: | ---: |",
        ]
    )
    for outcome in ("task", "transport", "tool_path"):
        metric = overall["outcomes"][outcome]
        lines.append(
            f"| {outcome} | {metric['main_success_n']}/24 | {metric['remote_success_n']}/24 | "
            f"{fmt(metric['risk_difference_remote_minus_local'])} | {interval(metric)} | "
            f"{fmt_p(metric['mcnemar_exact_two_sided_p'])} | {fmt_p(metric['binomial_exact_one_sided_harm_p'])} |"
        )

    lines.extend(
        [
            "",
            "## Wall-time ratios",
            "",
            "Ratios are 02/main. Transport failures and timeouts are excluded from ordinary ratios; they remain in the strict ITT and correct-tasks/minute denominators.",
            "",
            "| Analysis set | Eligible | Median | Bootstrap 95% CI | Geomean | Bootstrap 95% CI | 02 slower/faster/tie | Sign p |",
            "| --- | ---: | ---: | --- | ---: | --- | ---: | ---: |",
        ]
    )
    for name in ("transport_complete", "both_strict"):
        metric = overall["wall"][name]
        lines.append(
            f"| {name} | {metric['eligible_pair_n']}/24 | {fmt(metric['median_wall_ratio_remote_over_local'])} | "
            f"[{fmt(metric.get('median_bootstrap_95_ci_low'))}, {fmt(metric.get('median_bootstrap_95_ci_high'))}] | "
            f"{fmt(metric['geometric_mean_wall_ratio_remote_over_local'])} | "
            f"[{fmt(metric.get('geomean_bootstrap_95_ci_low'))}, {fmt(metric.get('geomean_bootstrap_95_ci_high'))}] | "
            f"{metric['remote_slower_n']}/{metric['remote_faster_n']}/{metric['tie_n']} | "
            f"{fmt_p(metric['sign_exact_two_sided_p'])} |"
        )

    lines.extend(
        [
            "",
            "## Correct tasks per minute",
            "",
            "This is an end-to-end operational utility measure, not model token throughput: `60000 × successes / sum(wall_ms)`, with failed calls retaining their wall time.",
            "",
            "| Success definition | Main | 02 | Difference | Ratio 02/main |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for outcome in ("strict", "task"):
        metric = overall["efficiency"][outcome]
        lines.append(
            f"| {outcome} | {fmt(metric['main_correct_tasks_per_min'])} | "
            f"{fmt(metric['remote_correct_tasks_per_min'])} | "
            f"{fmt(metric['difference_remote_minus_local'])} | {fmt(metric['ratio_remote_over_local'])} |"
        )

    lines.extend(
        [
            "",
            "## Model and fixture sensitivity",
            "",
            "Model groups have six scheduled pairs and fixture groups have eight. These are secondary, small-sample analyses; exact-test p-values are Holm-adjusted separately within each model or fixture family. Each model × fixture cell has n=2 and is descriptive only.",
            "",
            "| Level | Group | Strict RD | Strict one-sided p (Holm) | Task RD | Task one-sided p (Holm) |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for level in ("by_model", "by_fixture"):
        for name, group in summary["groups"][level].items():
            strict_group = group["outcomes"]["strict"]
            task_group = group["outcomes"]["task"]
            lines.append(
                f"| {level.removeprefix('by_')} | {name} | {fmt(strict_group['risk_difference_remote_minus_local'])} | "
                f"{fmt_p(strict_group.get('binomial_exact_one_sided_harm_p_holm'))} | "
                f"{fmt(task_group['risk_difference_remote_minus_local'])} | "
                f"{fmt_p(task_group.get('binomial_exact_one_sided_harm_p_holm'))} |"
            )

    order = summary["host_order_sensitivity"]
    lines.extend(
        [
            "",
            "## Host-order sensitivity",
            "",
            "The contrast below is RD when 02 ran first minus RD when main ran first, paired within the 12 model × fixture cells.",
            "",
            "| Outcome | Order contrast | Bootstrap 95% CI |",
            "| --- | ---: | --- |",
        ]
    )
    for outcome in OUTCOMES:
        metric = order["outcome_rd_contrasts"][outcome]
        lines.append(
            f"| {outcome} | {fmt(metric['rd_difference_remote_first_minus_main_first'])} | "
            f"[{fmt(metric['bootstrap_95_ci_low'])}, {fmt(metric['bootstrap_95_ci_high'])}] |"
        )

    audit = summary["audit"]
    lines.extend(
        [
            "",
            "## Tool and lifecycle audit",
            "",
            "| Host | Expected command slots | Expected-payload commands | Other terminal tools | Unexpected commands | Lifecycle anomalies | Operational failures | Attempts (invalid) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for host in HOSTS:
        item = audit[host]
        tool = item["tool_counts"]
        lifecycle_total = sum(item["lifecycle"][field] for field in LIFECYCLE_FIELDS)
        attempts = item["execution_attempts"]
        lines.append(
            f"| {host} | {tool['protocol_expected_command_count']} | "
            f"{tool['expected_payload_command_count']} | {tool['other_tool_terminal_count']} | "
            f"{tool['unexpected_command_count']} | {lifecycle_total} | "
            f"{item['operational_failure_n']} | {attempts['attempt_n']} ({attempts['invalid_attempt_n']}) |"
        )

    usage = summary["usage"]["overall"]
    lines.extend(
        [
            "",
            "## Usage-token telemetry",
            "",
            "Token fields are descriptive client-reported usage only; they are not server compute, FLOPS, or model-performance counters.",
            "",
            "| Host | Input | Cached input | Output | Reasoning output |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for host in HOSTS:
        fields = usage[host]["fields"]
        lines.append(
            f"| {host} | {fmt(fields['input_tokens']['sum'])} | "
            f"{fmt(fields['cached_input_tokens']['sum'])} | {fmt(fields['output_tokens']['sum'])} | "
            f"{fmt(fields['reasoning_output_tokens']['sum'])} |"
        )

    lines.extend(["", "## Interpretation guardrails", ""])
    lines.extend(f"- {item}" for item in summary["caveats"])
    return "\n".join(lines) + "\n"


def analyze(
    run_dir: pathlib.Path,
    output_dir: pathlib.Path | None = None,
    *,
    bootstrap_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if bootstrap_resamples < 100:
        raise SupplementalAnalysisError("bootstrap_resamples must be at least 100")
    schedule_path = run_dir / "schedule.json"
    schedule = read_json(schedule_path)
    manifest = read_json(run_dir / "manifest.json")
    validate_schedule_manifest_binding(schedule_path, manifest)
    design = validate_fixed_design(schedule, manifest)
    frozen, frozen_path, frozen_hash = load_frozen_analyzer(run_dir, schedule)
    expected_sequences = getattr(frozen, "EXPECTED_COMMAND_LABEL_SEQUENCE", None)
    if not isinstance(expected_sequences, dict):
        raise SupplementalAnalysisError(
            "frozen analyzer does not expose EXPECTED_COMMAND_LABEL_SEQUENCE"
        )
    for fixture in design["fixtures"]:
        if not isinstance(expected_sequences.get(fixture), list):
            raise SupplementalAnalysisError(
                f"frozen analyzer lacks expected command sequence for {fixture}"
            )

    calls: list[dict[str, Any]] = []
    calls_by_pair: defaultdict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    manifest_calls = manifest["calls"]
    for block in schedule["blocks"]:
        for call in block["calls"]:
            path = resolve_result_path(run_dir, call["local_output_relative"])
            if not path.is_file():
                raise SupplementalAnalysisError(
                    f"formal run is incomplete; result is missing: {path}"
                )
            raw = read_json(path)
            state = manifest_calls[str(call["sequence_index"])]
            validate_result_identity(raw, schedule, block, call, path, state)
            try:
                grade = frozen.grade_result(raw)
            except Exception as exc:
                raise SupplementalAnalysisError(
                    f"frozen grade_result failed for {path.name}: {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(grade, dict):
                raise SupplementalAnalysisError("frozen grade_result did not return an object")
            row = build_call_row(
                raw,
                grade,
                len(expected_sequences[block["fixture_id"]]),
                state,
            )
            calls.append(row)
            calls_by_pair[block["pair_id"]][call["host_label"]] = row
    if len(calls) != EXPECTED_CALLS:
        raise SupplementalAnalysisError("formal run did not yield exactly 48 graded calls")
    pairs = build_pairs(schedule, calls_by_pair)

    overall = summarize_group(
        pairs,
        level="overall",
        value="all",
        infer=True,
        strata_fields=("model", "fixture_id"),
        resamples=bootstrap_resamples,
        seed=seed,
    )
    by_model = {
        model: summarize_group(
            [pair for pair in pairs if pair["model"] == model],
            level="model",
            value=model,
            infer=True,
            strata_fields=("fixture_id",),
            resamples=bootstrap_resamples,
            seed=seed,
        )
        for model in design["models"]
    }
    by_fixture = {
        fixture: summarize_group(
            [pair for pair in pairs if pair["fixture_id"] == fixture],
            level="fixture",
            value=fixture,
            infer=True,
            strata_fields=("model",),
            resamples=bootstrap_resamples,
            seed=seed,
        )
        for fixture in design["fixtures"]
    }
    apply_holm(by_model)
    apply_holm(by_fixture)
    by_cell = {
        f"{model}|{fixture}": summarize_group(
            [
                pair
                for pair in pairs
                if pair["model"] == model and pair["fixture_id"] == fixture
            ],
            level="model_x_fixture",
            value=f"{model}|{fixture}",
            infer=False,
            strata_fields=("host_order",),
            resamples=bootstrap_resamples,
            seed=seed,
        )
        for model in design["models"]
        for fixture in design["fixtures"]
    }

    usage = {
        "overall": usage_summary(calls),
        "by_model": {
            model: usage_summary([call for call in calls if call["model"] == model])
            for model in design["models"]
        },
        "by_fixture": {
            fixture: usage_summary(
                [call for call in calls if call["fixture_id"] == fixture]
            )
            for fixture in design["fixtures"]
        },
        "interpretation": "Descriptive CLI usage telemetry; not a measure of server compute.",
    }
    caveats = [
        "The primary endpoint is strict Agent success over all 24 scheduled pairs (ITT). Missing results are not imputed: this analyzer refuses to run until all 48 call files and their manifest hashes are present.",
        "Timeout, harness, and transport failures count as strict operational failures. They are not converted into ordinary wall-time ratios.",
        "Task correctness, transport completion, and exact tool-path adherence are separate secondary outcomes; task correctness alone is not an end-to-end success claim.",
        "Bootstrap intervals are paired and stratified by model × fixture overall, by fixture within model, and by model within fixture. They quantify sampling variability under this fixed schedule, not causal identification.",
        "Model (n=6) and fixture (n=8) analyses are secondary and small-sample. Holm adjustments are within each reported model or fixture family. Model × fixture cells (n=2) are descriptive only.",
        "No interval or zero-event bound establishes equivalence or non-inferiority; no equivalence margin was prespecified or tested.",
        "Host order, PowerShell language mode, Python/runtime differences, and other host-specific conditions can confound main-versus-02 contrasts.",
        "These read-only fixtures do not estimate write/edit Agent behavior, general software-engineering quality, server FLOPS, or pure inference time.",
        "Usage-token fields are client-reported telemetry and must not be interpreted as compute released or lost.",
    ]
    order_sensitivity = host_order_sensitivity(pairs, bootstrap_resamples, seed)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": "codex_agent_eval_supplemental_summary",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "frozen_grader": {
            "path": str(frozen_path),
            "sha256": frozen_hash,
            "entrypoint": "grade_result",
            "calls_scored": len(calls),
        },
        "design": {
            **design,
            "completed_calls": len(calls),
            "completed_pairs": len(pairs),
            "formal_complete": True,
        },
        "analysis_plan": {
            "primary_endpoint": "strict Agent success, paired ITT over all 24 pairs",
            "primary_effect": "mean paired risk difference (remote minus main)",
            "exact_tests": (
                "two-sided exact McNemar/binomial and one-sided exact binomial "
                "harm alternative (main-only adverse discordance)"
            ),
            "bootstrap_method": "deterministic percentile paired bootstrap with fixed stratum sizes",
            "bootstrap_resamples": bootstrap_resamples,
            "seed": seed,
            "overall_strata": ["model", "fixture_id"],
            "model_strata": ["fixture_id"],
            "fixture_strata": ["model"],
            "multiplicity": "Holm within model and fixture families for exact outcome and sign tests",
            "alpha": ALPHA,
        },
        "primary": {"strict_itt": overall["outcomes"]["strict"]},
        "groups": {
            "overall": overall,
            "by_model": by_model,
            "by_fixture": by_fixture,
            "by_model_fixture": by_cell,
        },
        "host_order_sensitivity": order_sensitivity,
        "usage": usage,
        "audit": audit_summary(calls),
        "caveats": caveats,
    }

    processed = (output_dir or run_dir / "processed" / "agent_eval_supplemental").resolve()
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "supplemental_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    write_csv(processed / "supplemental_calls.csv", sorted(calls, key=lambda row: row["sequence_index"]))
    write_csv(processed / "supplemental_pairs.csv", [flatten_pair(pair) for pair in pairs])
    all_groups = [
        overall,
        *by_model.values(),
        *by_fixture.values(),
        *by_cell.values(),
        *order_sensitivity["by_order"].values(),
    ]
    write_csv(processed / "supplemental_group_statistics.csv", group_csv_rows(all_groups))
    order_rows = [
        {
            "metric_type": "paired_binary_order_contrast",
            "metric": outcome,
            **metric,
        }
        for outcome, metric in order_sensitivity["outcome_rd_contrasts"].items()
    ] + [
        {
            "metric_type": "wall_ratio_order_contrast",
            "metric": analysis_set,
            **metric,
        }
        for analysis_set, metric in order_sensitivity["wall_ratio_contrasts"].items()
    ]
    write_csv(processed / "supplemental_host_order_sensitivity.csv", order_rows)
    (processed / "supplemental_report.md").write_text(
        build_report(summary), encoding="utf-8"
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--bootstrap-resamples", type=int, default=DEFAULT_BOOTSTRAP_RESAMPLES
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        analyze(
            pathlib.Path(args.run_dir),
            pathlib.Path(args.output_dir) if args.output_dir else None,
            bootstrap_resamples=args.bootstrap_resamples,
            seed=args.seed,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SupplementalAnalysisError) as exc:
        print(
            f"analyze_agent_eval_supplemental: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
