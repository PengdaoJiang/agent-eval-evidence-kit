"""Offline synthetic fixtures only; no compiler, model, or network is called."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import selected_execution as se


class SelectedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="selected-execution-test-")
        self.fixture = Path(self.temp.name).resolve()
        self.lab = self.fixture / "lab"
        self.job = "DS1017-XPM-TEST-001"
        self.root = self.lab / "runs" / self.job
        self.work = self.root / "work"
        self.scratch = self.root / "scratch"
        for relative in (".control", "evidence", "work", "scratch"):
            (self.root / relative).mkdir(parents=True)
        self.session = "session-11111111-2222-3333-4444-555555555555"
        binding = {"job_id": self.job, "session_id": self.session, "workspace_id": se.WORKSPACE}
        self.put_json(self.root / ".control" / "prepare.json", {
            **binding, "job": self.job, "schema": "rea.harness.task-storage.prepare.v1",
            "root": str(self.root), "target": str(self.lab)})
        self.put_json(self.root / "evidence" / "TASK_CONTRACT.json", binding)
        self.catalog = {"job_id": self.job, "references": []}
        self.source = self.fixture / "selected-source.txt"
        self.source.write_bytes(b"selected bytes\n")
        self.compiler = self.fixture / "synthetic-compiler.exe"
        self.compiler.write_bytes(b"synthetic executable identity; execution is mocked")
        self.select(self.source)
        self.select(self.compiler)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def put_json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def select(self, path, *, directory=False):
        data = b"" if directory else path.read_bytes()
        self.catalog["references"].append({"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.put_json(self.root / "evidence" / "INPUT_CATALOG.json", self.catalog)

    def context(self):
        return se.Context(self.job, lab=self.lab)

    def spec(self, **changes):
        (self.work / "main.cpp").write_bytes(b"synthetic source")
        value = {"schema": "dsh.lab.selected_execution.build-run.v1", "compiler": str(self.compiler),
                 "inputs": {"main": "main.cpp"}, "compile_args": ["{input:main}", "-o", "{artifact}"],
                 "artifact": "bin/current.exe", "run_args": ["--mode=consumer"]}
        value.update(changes)
        self.put_json(self.work / "build.json", value)
        return "build.json"

    def test_explicit_file_list_copies_without_directory_enumeration(self):
        second = self.fixture / "selected-second.txt"
        second.write_bytes(b"second")
        self.select(second)
        with patch("os.scandir", side_effect=AssertionError("Directory enumeration forbidden")):
            result, code = se.copy_selected(self.context(), [(str(self.source), "chosen/a.txt"), (str(second), "chosen/b.txt")])
        self.assertEqual(code, 0)
        self.assertEqual(result["copied_or_identical"], 2)
        self.assertEqual((self.work / "chosen/a.txt").read_bytes(), self.source.read_bytes())
        receipt = json.loads(Path(result["receipt"]).read_text())
        marker = json.loads((Path(receipt["scratch_root"]) / "execution_record.json").read_text())
        self.assertEqual(marker["receipt"], result["receipt"])
        self.assertTrue(Path(result["receipt"]).is_relative_to(self.work / "execution_records"))

    def test_directory_and_unselected_file_are_refused(self):
        directory = self.fixture / "selected-directory"
        directory.mkdir()
        self.select(directory, directory=True)
        with self.assertRaisesRegex(se.Refused, "SINGLE_FILE_REQUIRED"):
            se.copy_selected(self.context(), [(str(directory), "bad")])
        unselected = self.fixture / "unselected.txt"
        unselected.write_bytes(b"not selected")
        with self.assertRaisesRegex(se.Refused, "SOURCE_NOT_SELECTED"):
            se.copy_selected(self.context(), [(str(unselected), "bad")])
        self.assertFalse((self.work / "bad").exists())

    def test_changed_source_hash_is_refused(self):
        self.source.write_bytes(b"changed after catalog freeze")
        with self.assertRaisesRegex(se.Refused, "SELECTED_SOURCE_IDENTITY_CHANGED"):
            se.copy_selected(self.context(), [(str(self.source), "copy.txt")])
        self.assertFalse((self.work / "copy.txt").exists())

    def test_different_content_is_not_overwritten_and_identical_is_reused(self):
        destination = self.work / "copy.txt"
        destination.write_bytes(b"owned different content")
        with self.assertRaisesRegex(se.Refused, "DIFFERENT_DESTINATION_EXISTS"):
            se.copy_selected(self.context(), [(str(self.source), "copy.txt")])
        self.assertEqual(destination.read_bytes(), b"owned different content")
        destination.write_bytes(self.source.read_bytes())
        result, code = se.copy_selected(self.context(), [(str(self.source), "copy.txt")])
        self.assertEqual(code, 0)
        receipt = json.loads(Path(result["receipt"]).read_text())
        self.assertEqual(receipt["files"][0]["status"], "IDENTICAL_ALREADY_PRESENT")

    def test_path_escape_device_names_and_wrong_binding_are_refused(self):
        for destination in ("../escape", "a/../../escape", str(self.fixture / "outside"), "CON.txt"):
            with self.subTest(destination=destination), self.assertRaises(se.Refused):
                se.copy_selected(self.context(), [(str(self.source), destination)])
        with self.assertRaisesRegex(se.Refused, "INVALID_JOB_NAME"):
            se.Context("../other-project", lab=self.lab)
        contract = self.root / "evidence" / "TASK_CONTRACT.json"
        value = json.loads(contract.read_text())
        value["workspace_id"] = "other-workspace"
        self.put_json(contract, value)
        with self.assertRaisesRegex(se.Refused, "CONTRACT_BINDING_MISMATCH"):
            self.context()

    def test_reparse_attribute_is_rejected_before_file_access(self):
        original = Path.lstat
        source = self.source

        def lstat(path, *args, **kwargs):
            if path == source:
                return SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(se.Refused, "REPARSE_PATH_FORBIDDEN"):
            se.copy_selected(self.context(), [(str(self.source), "copy.txt")])

    def test_compile_failure_never_calls_old_or_partial_binary_and_logs_survive_scratch_cleanup(self):
        old = self.work / "old.exe"
        old.write_bytes(b"old binary must not run")
        spec = self.spec()

        def compile_failure(argv, **kwargs):
            self.assertFalse(kwargs["shell"])
            self.assertEqual(argv[0], str(self.compiler))
            artifact = Path(argv[-1])
            self.assertFalse(artifact.exists())
            artifact.write_bytes(b"incomplete output")
            kwargs["stdout"].write(b"complete compiler stdout\x00\n")
            kwargs["stderr"].write(b"compiler error\n")
            return subprocess.CompletedProcess(argv, 2)

        with patch.object(se.subprocess, "run", side_effect=compile_failure) as run:
            result, code = se.build_run(self.context(), spec)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(code, 2)
        self.assertEqual(result["compile_exit"], 2)
        self.assertIsNone(result["run_exit"])
        receipt_path = Path(result["receipt"])
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["status"], "COMPILE_FAILED_RUN_SKIPPED")
        self.assertIsNone(receipt["run"])
        self.assertEqual(receipt["artifact"]["sha256"], hashlib.sha256(b"incomplete output").hexdigest())
        self.assertNotEqual(receipt["planned_run_argv"][0], str(old))
        temporary = Path(receipt["scratch_root"])
        self.assertTrue(temporary.is_relative_to(self.fixture))
        shutil.rmtree(temporary)
        self.assertTrue(receipt_path.exists())
        self.assertEqual(Path(receipt["compile"]["stdout"]).read_bytes(), b"complete compiler stdout\x00\n")
        self.assertEqual(Path(receipt["compile"]["stderr"]).read_bytes(), b"compiler error\n")

    def test_only_this_fresh_artifact_runs_and_raw_nonzero_exits_are_retained(self):
        spec = self.spec()
        roots = []
        for run_exit in (0, 3, 4, -9):
            with self.subTest(run_exit=run_exit):
                artifact_seen = []

                def execute(argv, **kwargs):
                    self.assertIs(kwargs["shell"], False)
                    if not artifact_seen:
                        self.assertEqual(argv[0], str(self.compiler))
                        artifact = Path(argv[-1])
                        self.assertFalse(artifact.exists())
                        artifact.write_bytes(b"new unique artifact")
                        artifact_seen.append(artifact)
                        return subprocess.CompletedProcess(argv, 0)
                    self.assertEqual(argv[0], str(artifact_seen[0]))
                    self.assertEqual(argv[1:], ["--mode=consumer"])
                    self.assertEqual(Path(argv[0]).read_bytes(), b"new unique artifact")
                    kwargs["stdout"].write(b"full public program result\n")
                    kwargs["stderr"].write(b"full diagnostic\n")
                    return subprocess.CompletedProcess(argv, run_exit)

                with patch.object(se.subprocess, "run", side_effect=execute) as run:
                    result, code = se.build_run(self.context(), spec)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(code, run_exit)
                self.assertEqual(result["run_exit"], run_exit)
                receipt = json.loads(Path(result["receipt"]).read_text())
                self.assertEqual(receipt["run"]["exit"], run_exit)
                self.assertEqual(receipt["artifact"]["sha256"], hashlib.sha256(b"new unique artifact").hexdigest())
                self.assertEqual(receipt["status"], "RUN_FINISHED")
                self.assertIn("No scientific acceptance", receipt["claim_boundary"])
                self.assertEqual(Path(receipt["run"]["stdout"]).read_bytes(), b"full public program result\n")
                roots.append(receipt["scratch_root"])
        self.assertEqual(len(roots), len(set(roots)))

    def test_missing_fresh_artifact_prevents_run_even_if_compiler_exits_zero(self):
        spec = self.spec()
        (self.work / "current.exe").write_bytes(b"stale binary")
        with patch.object(se.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result, code = se.build_run(self.context(), spec)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(code, 1)
        self.assertIsNone(result["run_exit"])
        self.assertEqual(result["status"], "EXECUTION_REFUSED_OR_FAILED")

    def test_undeclared_argv_path_and_response_file_refuse_before_subprocess(self):
        for arg in ("@unselected.rsp", str(self.fixture / "unselected.cpp"), "{scratch}/../escape", "{input:unknown}"):
            with self.subTest(arg=arg):
                spec = self.spec(compile_args=[arg, "-o", "{artifact}"])
                with patch.object(se.subprocess, "run", side_effect=AssertionError("Must not execute")) as run:
                    result, code = se.build_run(self.context(), spec)
                self.assertEqual(run.call_count, 0)
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "EXECUTION_REFUSED_OR_FAILED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
