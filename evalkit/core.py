from __future__ import annotations

import hashlib
import json
import math
import random
import re
from pathlib import PurePosixPath


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_loads(text: str):
    def reject(value):
        raise ValueError(f"non-finite JSON number: {value}")
    def finite(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed
    return json.loads(text, object_pairs_hook=_unique, parse_constant=reject, parse_float=finite)


def digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def typed_equal(left, right) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(typed_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(typed_equal(a, b) for a, b in zip(left, right))
    return left == right


FIXTURES = {
    "aggregate": {
        "task": "Sum active records by owner: a/4, b/7, a/3; ignore inactive b/9.",
        "tools": [{"command": "read records.json", "exit_code": 0,
                   "output": "a,4,active;b,7,active;a,3,active;b,9,inactive"}],
        "answer": {"totals": {"a": 7, "b": 7}, "active_count": 3},
    },
    "diagnose": {
        "task": "Diagnose an inclusive boundary error from one failing baseline; return the correction.",
        "tools": [{"command": "test boundary", "exit_code": 1,
                   "output": "FAIL boundary: expected [2,3], received [3]; failures=1"},
                  {"command": "read boundary.py", "exit_code": 0,
                   "output": "def select(xs, lower): return [x for x in xs if x > lower]"}],
        "answer": {"cause": "exclusive_lower_bound", "operator": ">=", "result": [2, 3]},
    },
    "incident": {
        "task": "Classify: request accepted, tool timed out, no final answer, workspace unchanged.",
        "tools": [{"command": "read incident.log", "exit_code": 0,
                   "output": "accepted; tool_timeout; no_final; workspace_unchanged"}],
        "answer": {"category": "tool_timeout", "completed": False, "retry_requires_review": True},
    },
}


def _keys(value, expected, label):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError(f"invalid {label} fields")


def validate_manifest(manifest):
    if type(manifest) is not dict:
        raise ValueError("manifest must be an object")
    for path, sha in manifest.items():
        if not isinstance(path, str) or not path or "\\" in path or ":" in path:
            raise ValueError("manifest path must be a canonical relative POSIX path")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or "." in path.split("/") or str(parsed) != path:
            raise ValueError("unsafe/noncanonical manifest path")
        if not isinstance(sha, str) or not re.fullmatch("[a-f0-9]{64}", sha):
            raise ValueError("invalid SHA-256 digest")


def grade(trace):
    _keys(trace, ["schema_version", "attempt_id", "fixture_id", "fixture_sha256", "synthetic",
                  "transport_completed", "tools", "answer", "workspace_before", "workspace_after"], "trace")
    if type(trace["schema_version"]) is not int or trace["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    if type(trace["attempt_id"]) is not str or not trace["attempt_id"]:
        raise ValueError("missing attempt_id")
    fixture_id = trace["fixture_id"]
    if not isinstance(fixture_id, str) or fixture_id not in FIXTURES:
        raise ValueError("unknown fixture")
    fixture = FIXTURES[fixture_id]
    if trace["fixture_sha256"] != digest(fixture):
        raise ValueError("fixture digest mismatch")
    if type(trace["synthetic"]) is not bool or type(trace["transport_completed"]) is not bool:
        raise ValueError("flags must be booleans")
    if type(trace["tools"]) is not list:
        raise ValueError("tools must be a list")
    for event in trace["tools"]:
        _keys(event, ["command", "exit_code", "output"], "tool event")
        if type(event["command"]) is not str or type(event["output"]) is not str or type(event["exit_code"]) is not int:
            raise ValueError("invalid tool event types")
    validate_manifest(trace["workspace_before"])
    validate_manifest(trace["workspace_after"])
    dimensions = {
        "transport_success": trace["transport_completed"],
        "task_success": typed_equal(trace["answer"], fixture["answer"]),
        "tool_protocol_success": typed_equal(trace["tools"], fixture["tools"]),
        "reported_workspace_integrity": trace["workspace_before"] == trace["workspace_after"],
    }
    return {"attempt_id": trace["attempt_id"], "fixture_id": fixture_id,
            "synthetic": trace["synthetic"], **dimensions,
            "strict_success": all(dimensions.values()),
            "failed_dimensions": [key for key, ok in dimensions.items() if not ok]}


def schedule(seed=17, repeats=2):
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    rng = random.Random(seed)
    rows = []
    for fixture_id in FIXTURES:
        start = rng.choice(["AB", "BA"])
        for repeat in range(repeats):
            pair_id = f"{fixture_id}-{repeat + 1}"
            order = start if repeat % 2 == 0 else start[::-1]
            for index, arm in enumerate(order):
                rows.append({"attempt_id": f"{pair_id}-{arm}", "pair_id": pair_id,
                             "fixture_id": fixture_id, "arm": arm, "order": index + 1})
    return rows


def sample_trace(fixture_id="aggregate", attempt_id="sample-A"):
    fixture = FIXTURES[fixture_id]
    snapshot = {"fixture.json": digest(fixture)}
    return {"schema_version": 1, "attempt_id": attempt_id, "fixture_id": fixture_id,
            "fixture_sha256": digest(fixture), "synthetic": True, "transport_completed": True,
            "tools": json.loads(json.dumps(fixture["tools"])),
            "answer": json.loads(json.dumps(fixture["answer"])),
            "workspace_before": dict(snapshot), "workspace_after": dict(snapshot)}


def summarize(planned, traces):
    if type(planned) is not list or not planned:
        raise ValueError("planned schedule must be a nonempty list")
    if type(traces) is not list:
        raise ValueError("traces must be a list")
    expected = {}
    pairs = {}
    for row in planned:
        _keys(row, ["attempt_id", "pair_id", "fixture_id", "arm", "order"], "schedule row")
        for field in ["attempt_id", "pair_id", "fixture_id", "arm"]:
            if type(row[field]) is not str or not row[field].strip():
                raise ValueError(f"schedule {field} must be a nonempty string")
        if type(row["order"]) is not int or row["order"] not in (1, 2):
            raise ValueError("schedule order must be integer 1 or 2")
        if row["attempt_id"] in expected or row["arm"] not in ("A", "B") or row["fixture_id"] not in FIXTURES:
            raise ValueError("invalid or duplicate planned attempt")
        expected[row["attempt_id"]] = row
        pairs.setdefault(row["pair_id"], []).append(row)
    for rows in pairs.values():
        if len(rows) != 2 or {r["arm"] for r in rows} != {"A", "B"} or {r["order"] for r in rows} != {1, 2} or len({r["fixture_id"] for r in rows}) != 1:
            raise ValueError("each pair needs one A and one B of the same fixture")
    results = {}
    for trace in traces:
        result = grade(trace)
        attempt_id = result["attempt_id"]
        if attempt_id not in expected or attempt_id in results:
            raise ValueError("unknown or duplicate attempt result")
        if result["fixture_id"] != expected[attempt_id]["fixture_id"]:
            raise ValueError("attempt fixture does not match schedule")
        results[attempt_id] = result
    provenance = {r["synthetic"] for r in results.values()}
    if len(provenance) > 1:
        raise ValueError("do not pool synthetic and measured traces")
    missing = sorted(set(expected) - set(results))
    arms = {}
    for arm in ["A", "B"]:
        rows = [r for r in planned if r["arm"] == arm]
        done = [results[r["attempt_id"]] for r in rows if r["attempt_id"] in results]
        arms[arm] = {"planned": len(rows), "completed": len(done),
                     "missing": len(rows) - len(done),
                     "strict_successes": sum(r["strict_success"] for r in done),
                     "planned_success_rate": sum(r["strict_success"] for r in done) / len(rows),
                     "completed_success_rate": sum(r["strict_success"] for r in done) / len(done) if done else None}
    outcomes = {"both_pass": 0, "A_only": 0, "B_only": 0, "both_fail": 0, "incomplete": 0}
    for rows in pairs.values():
        by_arm = {row["arm"]: results.get(row["attempt_id"]) for row in rows}
        if any(v is None for v in by_arm.values()):
            outcomes["incomplete"] += 1
        else:
            a, b = by_arm["A"]["strict_success"], by_arm["B"]["strict_success"]
            outcomes["both_pass" if a and b else "A_only" if a else "B_only" if b else "both_fail"] += 1
    return {"provenance": "synthetic" if provenance == {True} else "measured" if provenance == {False} else "empty",
            "planned_attempts": len(planned), "completed_attempts": len(results),
            "missing_attempt_ids": missing, "arms": arms, "paired_outcomes": outcomes,
            "results": list(results.values())}
