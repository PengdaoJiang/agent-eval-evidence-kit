from __future__ import annotations

import csv
import json
import os
import pathlib
import subprocess
import sys
import unittest
from collections import Counter


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_agent_eval as analyze  # noqa: E402


FIXTURES = ROOT / "fixtures" / "agent_eval"


class AgentEvalFixtureTests(unittest.TestCase):
    def test_output_schemas_use_strict_response_format_shape(self) -> None:
        def check(node: object, location: str) -> None:
            if not isinstance(node, dict):
                return
            if "const" in node:
                self.assertIn("type", node, f"const lacks type at {location}")
            if node.get("type") == "object":
                properties = node.get("properties")
                self.assertIsInstance(properties, dict, f"object lacks properties at {location}")
                self.assertFalse(
                    node.get("additionalProperties", True),
                    f"object must reject additional properties at {location}",
                )
                self.assertEqual(
                    set(node.get("required", [])),
                    set(properties or {}),
                    f"all object properties must be required at {location}",
                )
            for key, value in node.items():
                if isinstance(value, dict):
                    check(value, f"{location}.{key}")
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        check(item, f"{location}.{key}[{index}]")

        for fixture in sorted(FIXTURES.iterdir()):
            schema = json.loads(
                (fixture / "final_schema.json").read_text(encoding="utf-8")
            )
            check(schema, fixture.name)

    def test_f1_external_oracle_is_derived_from_template(self) -> None:
        root = FIXTURES / "f1_manifest_join_readonly"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        owners = json.loads((root / manifest["sources"]["owners"]).read_text(encoding="utf-8"))
        rules = json.loads((root / manifest["sources"]["rules"]).read_text(encoding="utf-8"))
        with (root / manifest["sources"]["jobs"]).open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            jobs = list(csv.DictReader(handle))
        selected = [
            row
            for row in jobs
            if row["status"] == rules["include_status"]
            and int(row["duration_ms"]) >= rules["minimum_duration_ms"]
            and int(row["queued_ms"]) <= rules["maximum_queued_ms"]
        ]
        selected.sort(key=lambda row: (-int(row["duration_ms"]), row["job_id"]))
        observed = {
            "fixture_id": "f1_manifest_join_readonly",
            "selected_job_ids": [row["job_id"] for row in selected],
            "total_duration_ms": sum(int(row["duration_ms"]) for row in selected),
            "team_counts": dict(
                sorted(Counter(owners[row["owner_alias"]] for row in selected).items())
            ),
        }
        self.assertTrue(analyze.strict_deep_equal(observed, analyze.EXPECTED_F1))

    def test_f2_public_baseline_is_red_without_creating_pycache(self) -> None:
        root = FIXTURES / "f2_python_bugfix"
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertNotEqual(completed.returncode, 0)
        baseline = completed.stdout + completed.stderr
        self.assertIn("test_adjacent_inclusive_windows_merge", baseline)
        self.assertIn("test_input_order_is_not_mutated", baseline)
        self.assertFalse(any(path.name == "__pycache__" for path in root.rglob("*")))
        source = (root / "src" / "windowing.py").read_text(encoding="utf-8")
        self.assertIn("windows.sort()", source)
        self.assertIn("start > merged[-1][1]", source)

        def corrected(windows: list[tuple[int, int]]) -> list[list[int]]:
            merged: list[list[int]] = []
            for start, end in sorted(windows):
                if not merged or start > merged[-1][1] + 1:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)
            return merged

        observed = {
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
                "adjacent_inclusive": corrected([(2, 4), (5, 8)]),
                "non_mutating_order": corrected([(8, 9), (1, 2)]),
                "overlap": corrected([(1, 4), (3, 7)]),
            },
        }
        self.assertTrue(
            analyze.strict_deep_equal(observed, analyze.EXPECTED_F2_FINAL)
        )

    def test_f3_external_oracle_is_returned_directly_without_artifact(self) -> None:
        root = FIXTURES / "f3_incident_report"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("output", manifest)
        self.assertFalse((root / "validate_report.py").exists())
        self.assertFalse((root / "out" / "README.txt").exists())
        records = []
        for name in ("east", "west"):
            records.extend(
                json.loads(line)
                for line in (root / "logs" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
            )
        codebook = json.loads((root / "data" / "codebook.json").read_text(encoding="utf-8"))
        deduplicated: dict[str, dict] = {}
        for record in records:
            current = deduplicated.get(record["event_id"])
            if current is None or (record["latency_ms"], -ord(record["source"][0])) > (
                current["latency_ms"],
                -ord(current["source"][0]),
            ):
                deduplicated[record["event_id"]] = record
        selected = [
            record
            for record in deduplicated.values()
            if codebook[record["code"]]["category"] in {"transport", "latency"}
            or record["latency_ms"] >= 250
        ]
        rank = {"critical": 0, "warning": 1, "info": 2}
        selected.sort(
            key=lambda record: (
                rank[codebook[record["code"]]["severity"]],
                record["event_id"],
            )
        )
        observed = {
            "fixture_id": "f3_incident_report",
            "incident_ids": [record["event_id"] for record in selected],
            "category_counts": dict(
                sorted(Counter(codebook[record["code"]]["category"] for record in selected).items())
            ),
            "max_latency_ms": max(record["latency_ms"] for record in selected),
            "source_counts": dict(sorted(Counter(record["source"] for record in selected).items())),
        }
        self.assertTrue(
            analyze.strict_deep_equal(observed, analyze.EXPECTED_F3_FINAL)
        )


if __name__ == "__main__":
    unittest.main()
