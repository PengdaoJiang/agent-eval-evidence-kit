"""Synthetic, offline accounting fixtures; never executes candidate code."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import execution_ledger as ledger


class ExecutionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="execution-ledger-test-")
        self.base = Path(self.temporary.name).resolve()
        self.lab = self.base / "lab"
        self.job = "DS1017-XPM-TEST-001"
        self.root = self.lab / "runs" / self.job
        for relative in ("work/execution_records", "delivery", "evidence", "scratch"):
            (self.root / relative).mkdir(parents=True)
        self.session = "session-11111111-2222-3333-4444-555555555555"
        self.candidates = []
        self.input = self.root / "work" / "main.cpp"
        self.old_bytes = b"earlier overwritten source"
        self.old_identity = self.desc_bytes(self.input, self.old_bytes)
        self.input.write_bytes(b"current frozen source")
        self.candidates.append(self.desc(self.input))
        self.compiler = self.base / "explicit-compiler.exe"
        self.compiler.write_bytes(b"compiler selected identity only")
        self.spec = self.root / "work" / "build.json"
        self.spec.write_bytes(b'{"synthetic":"argv metadata"}')
        self.candidates.append(self.desc(self.spec))
        self.saved_artifact = self.root / "delivery" / "current.exe"
        self.saved_artifact.write_bytes(b"retained synthetic binary")
        self.candidates.append(self.desc(self.saved_artifact))
        self.copy_source = self.base / "catalog-copy.txt"
        self.copy_source.write_bytes(b"explicitly selected copy input")
        self.catalog_path = self.root / "evidence" / "INPUT_CATALOG.json"
        self.write(self.catalog_path, {"job_id": self.job, "references": [self.desc(self.compiler), self.desc(self.copy_source)]})
        self.receipt_paths = []
        for index, code in enumerate([3] * 6 + [4] * 2):
            run_id = f"selected-build-run-{index:032x}"
            path = self.root / "work" / "execution_records" / run_id / "receipt.json"
            path.parent.mkdir()
            row = self.binding(run_id, "build-run")
            row.update(inputs={"main": self.old_identity}, compiler=self.desc(self.compiler),
                       spec={"path": str(self.spec), "sha256": self.desc(self.spec)["sha256"]},
                       compile={"exit": 0}, run={"exit": code},
                       artifact=self.desc_bytes(self.root / "scratch" / run_id / "current.exe", self.saved_artifact.read_bytes()))
            self.write(path, row)
            self.receipt_paths.append(path)
            self.candidates.append(self.desc(path))
        run_id = "selected-copy-" + "a" * 32
        path = self.root / "work" / "execution_records" / run_id / "receipt.json"
        path.parent.mkdir()
        row = self.binding(run_id, "copy")
        row["files"] = [{"source": self.desc(self.copy_source), "destination": str(self.root / "work" / "copy.txt"), "status": "COPIED"}]
        self.write(path, row)
        self.candidates.append(self.desc(path))
        self.freeze_path = self.lab / "receipts" / "freeze" / f"{self.job}.json"
        self.freeze_path.parent.mkdir(parents=True)
        self.freeze()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def desc_bytes(path, data):
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def desc(self, path):
        return self.desc_bytes(path, path.read_bytes())

    def binding(self, run_id, operation):
        return {"schema": "dsh.lab.selected_execution.receipt.v1", "job_id": self.job,
                "session_id": self.session, "workspace_id": ledger.WORKSPACE, "run_id": run_id,
                "retained_root": str(self.root / "work" / "execution_records" / run_id),
                "scratch_root": str(self.root / "scratch" / run_id), "operation": operation, "status": "RECORDED"}

    def freeze(self):
        self.write(self.freeze_path, {"schema": "rea.harness.constructor-freeze.v1", "job_id": self.job,
                                    "session_id": self.session, "workspace_id": ledger.WORKSPACE,
                                    "owner_turn_terminal": True, "all_recorded_tools_ended": True,
                                    "catalog": self.desc(self.catalog_path), "candidate_files": self.candidates})

    def derive(self):
        return ledger.build_ledger(self.job, self.freeze_path, lab=self.lab)

    def test_exact_histogram_six_threes_two_fours_and_no_directory_scans(self):
        with patch("os.scandir", side_effect=AssertionError("No directory scan allowed")):
            report = self.derive()
        self.assertEqual(report["counts"]["receipts"], 9)
        self.assertEqual(report["counts"]["copy"], 1)
        self.assertEqual(report["counts"]["build_run"], 8)
        self.assertEqual(report["counts"]["compile_exit_raw_histogram"], {"0": 8})
        self.assertEqual(report["counts"]["run_exit_raw_histogram"], {"3": 6, "4": 2})
        self.assertFalse(report["provenance"]["constructor_original_delivery"])
        self.assertIn("No automatic scientific PASS/FAIL", report["semantic_boundary"])
        self.assertTrue(all(row["source_receipt_freeze_verified"] for row in report["executions"]))

    def test_overwritten_old_version_unavailable_even_when_an_unselected_copy_exists(self):
        (self.base / "not-selected-old-copy.cpp").write_bytes(self.old_bytes)
        report = self.derive()
        old = next(row for row in report["input_versions"] if row["sha256"] == self.old_identity["sha256"])
        self.assertEqual(old["recovery"]["status"], "UNAVAILABLE_IN_SELECTED_EVIDENCE")
        self.assertEqual(old["recovery"]["recoverable_files"], [])
        self.assertIn("not a whole-machine", old["recovery"]["boundary"])

    def test_explicit_same_identity_copy_recovers_old_version_and_drift_removes_availability(self):
        saved = self.root / "delivery" / "selected-old-source.cpp"
        saved.write_bytes(self.old_bytes)
        self.candidates.append(self.desc(saved))
        self.freeze()
        report = self.derive()
        old = next(row for row in report["input_versions"] if row["sha256"] == self.old_identity["sha256"])
        self.assertEqual(old["recovery"]["status"], "AVAILABLE_IN_SELECTED_EVIDENCE")
        saved.write_bytes(b"changed selected copy")
        report = self.derive()
        old = next(row for row in report["input_versions"] if row["sha256"] == self.old_identity["sha256"])
        self.assertEqual(old["recovery"]["status"], "UNAVAILABLE_IN_SELECTED_EVIDENCE")
        self.assertEqual(len(old["recovery"]["matching_selected_files_not_verified"]), 1)

    def test_frozen_receipt_drift_refuses_before_publishing(self):
        with self.receipt_paths[0].open("ab") as stream:
            stream.write(b" ")
        with self.assertRaisesRegex(ledger.Refused, "FROZEN_FILE_IDENTITY_DRIFT"):
            ledger.publish(self.job, self.freeze_path, lab=self.lab)
        self.assertFalse((self.lab / "receipts" / "measurements" / f"{self.job}.execution-ledger.json").exists())

    def test_identical_report_is_idempotent_and_different_history_is_not_overwritten(self):
        first = ledger.publish(self.job, self.freeze_path, lab=self.lab)
        path = Path(first["report"]["path"])
        original = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        again = ledger.publish(self.job, self.freeze_path, lab=self.lab)
        self.assertEqual(again["status"], "IDENTICAL_EXISTING_REPORT")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        existing = json.loads(original)
        existing["counts"]["copy"] = 99
        self.write(path, existing)
        changed = path.read_bytes()
        with self.assertRaisesRegex(ledger.Refused, "EXISTING_LEDGER_DIFFERS_HISTORY_PRESERVED"):
            ledger.publish(self.job, self.freeze_path, lab=self.lab)
        self.assertEqual(path.read_bytes(), changed)

    def test_wrong_freeze_path_and_nonterminal_freeze_are_refused(self):
        with self.assertRaisesRegex(ledger.Refused, "EXACT_JOB_FREEZE_REQUIRED"):
            ledger.build_ledger(self.job, self.base / "other.json", lab=self.lab)
        frozen = json.loads(self.freeze_path.read_text())
        frozen["owner_turn_terminal"] = False
        self.write(self.freeze_path, frozen)
        with self.assertRaisesRegex(ledger.Refused, "TERMINAL_FREEZE_REQUIRED"):
            self.derive()


if __name__ == "__main__":
    unittest.main(verbosity=2)
