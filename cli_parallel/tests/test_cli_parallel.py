"""Offline tests. subprocess entrypoints are mocked: no login, model call or billing."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

SPEC = importlib.util.spec_from_file_location("launcher", Path(__file__).resolve().parents[1] / "cli_parallel.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reference-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.exe = self.root / "codex.exe"
        self.exe.touch()  # Not executable; subprocess is mocked.
        self.policy = self.root / "policy.txt"
        self.policy.write_text("Fixture policy; no external writes.", encoding="utf-8")
        self.prompts = []
        for i in range(2):
            p = self.root / f"task{i}.txt"
            p.write_text(f"Synthetic task {i}; 中文输入。", encoding="utf-8")
            self.prompts.append(p)
        self.out = self.root / "batch"
        self.run = self.patch(cli.subprocess, "run", side_effect=AssertionError("live subprocess forbidden"))
        self.popen = self.patch(cli.subprocess, "Popen", side_effect=AssertionError("live CLI forbidden"))
        cli.STOP.clear()
        cli.ACTIVE.clear()
        self.addCleanup(cli.STOP.clear)
        self.addCleanup(cli.ACTIVE.clear)

    def patch(self, obj, name, **kwargs):
        p = mock.patch.object(obj, name, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def args(self):
        return SimpleNamespace(exe=self.exe, owner="synthetic-test-controller",
            model="synthetic-test-model", effort="high", sandbox="workspace-write",
            web_search="disabled", timeout=30, policy_text=self.policy.read_text(encoding="utf-8"),
            policy_sha256=cli.sha(self.policy))

    def argv(self, count=1):
        return [*map(str, self.prompts[:count]), "--exe", str(self.exe),
                "--out", str(self.out), "--policy", str(self.policy),
                "--owner", "synthetic-test-controller", "--model", "synthetic-test-model",
                "--effort", "high", "--workers", str(count)]

    def invoke(self, argv, error=None):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            if error is None:
                code = cli.main(argv)
            else:
                with self.assertRaises(SystemExit) as cm:
                    cli.main(argv)
                self.assertEqual(cm.exception.code, error)
                code = error
        return code, stdout.getvalue()

    def permit_probe(self, auth="Logged in using ChatGPT"):
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, "--no-daemon --ask-for-approval", ""),
            subprocess.CompletedProcess([], 0, "--ignore-user-config --ephemeral --json "
                "--output-last-message --sandbox --skip-git-repo-check", ""),
            subprocess.CompletedProcess([], 0, auth, "")]

    def fake(self, command, **kwargs):
        job = Path(kwargs["cwd"])
        index = int(job.name.rsplit("-", 1)[-1])
        proc = mock.Mock(pid=50000 + index, returncode=0)
        proc.poll.return_value = 0
        def communicate(prompt, timeout):
            kwargs["stdout"].write(json.dumps(
                {"type": "thread.started", "thread_id": str(uuid.UUID(int=index))}) + "\n")
            kwargs["stdout"].write('{"type":"turn.completed","usage":{"input_tokens":12}}\n')
            (job / "result.md").write_text("Synthetic result, unaccepted.", encoding="utf-8")
            (job / "artifacts" / "handoff.md").write_text("Synthetic checkpoint.", encoding="utf-8")
        proc.communicate.side_effect = communicate
        return proc

    def one(self, number=1):
        p = self.prompts[0]
        return cli.run_one(self.args(), p, p.read_text(encoding="utf-8"), self.root / f"job-{number:02}")

    def test_dry_run_is_non_billable_no_files_no_subprocess(self):
        code, text = self.invoke(self.argv(2))
        data = json.loads(text)
        self.assertEqual(code, 0)
        self.assertTrue(data["dry_run"])
        self.assertEqual(len(data["jobs"]), 2)
        self.assertFalse(self.out.exists())
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_command_defaults_and_no_shell(self):
        cmd = cli.command(self.args(), self.root / "a path with spaces")
        self.assertEqual(cmd[-1], "-")
        self.assertIn("--ephemeral", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "workspace-write")
        self.assertIn('web_search="disabled"', cmd)
        self.assertIn("agents.enabled=false", cmd)
        self.assertNotIn("danger-full-access", cmd)

    def test_reject_duplicate_queue_and_zero_timeout(self):
        cases = [self.argv(2) + ["--workers", "1"], self.argv() + ["--workers", "0"],
                 self.argv() + ["--timeout", "0"],
                 [str(self.prompts[0]), *self.argv()]]
        for argv in cases:
            self.invoke(argv, error=2)
        self.run.assert_not_called()

    def test_reject_existing_destination_and_missing_input(self):
        self.out.mkdir()
        self.invoke(self.argv(), error=2)
        self.prompts[0].unlink()
        self.invoke(self.argv(), error=2)
        self.run.assert_not_called()

    def test_full_access_requires_explicit_acknowledgment(self):
        self.invoke(self.argv() + ["--sandbox", "danger-full-access"], error=2)
        code, _ = self.invoke(self.argv() + ["--sandbox", "danger-full-access", "--allow-full-access"])
        self.assertEqual(code, 0)
        self.popen.assert_not_called()

    def test_snapshot_and_true_identity_not_owner(self):
        self.popen.side_effect = self.fake
        args = self.args()
        text = self.prompts[0].read_text(encoding="utf-8")
        self.policy.write_text("Changed after freeze.", encoding="utf-8")
        record = cli.run_one(args, self.prompts[0], text, self.root / "job-01")
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["task_id"], str(uuid.UUID(int=1)))
        self.assertNotEqual(record["task_id"], record["coordinator"])
        self.assertEqual(record["acceptance"], "unaccepted")
        prompt = (self.root / "job-01/prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Fixture policy", prompt)
        self.assertNotIn("Changed after freeze", prompt)
        self.assertFalse((self.root / "job-01/request.json.tmp").exists())
        self.assertFalse(self.popen.call_args.kwargs.get("shell", False))

    def test_api_key_variables_removed(self):
        with mock.patch.dict(cli.os.environ, {"OPENAI_API_KEY": "fake", "CODEX_API_KEY": "fake"}):
            env = cli.environment()
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)

    def test_missing_identity_is_failure_never_fallback(self):
        def no_id(command, **kw):
            proc = self.fake(command, **kw)
            def communicate(prompt, timeout):
                kw["stdout"].write('{"type":"turn.completed"}\n')
                (Path(kw["cwd"]) / "result.md").write_text("Result.", encoding="utf-8")
            proc.communicate.side_effect = communicate
            return proc
        self.popen.side_effect = no_id
        record = self.one()
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["task_id"])

    def test_reported_error_not_accepted_despite_result(self):
        def error(command, **kw):
            proc = self.fake(command, **kw)
            original = proc.communicate.side_effect
            def communicate(prompt, timeout):
                original(prompt, timeout)
                kw["stdout"].write('{"type":"error","message":"synthetic"}\n')
            proc.communicate.side_effect = communicate
            return proc
        self.popen.side_effect = error
        self.assertEqual(self.one()["status"], "failed")

    def test_timeout_keeps_partial_and_does_not_retry(self):
        def timeout(command, **kw):
            proc = self.fake(command, **kw)
            def communicate(prompt, timeout):
                kw["stdout"].write(json.dumps(
                    {"type":"thread.started","thread_id":str(uuid.UUID(int=1))}) + "\n")
                (Path(kw["cwd"]) / "artifacts/partial.md").write_text("Partial.", encoding="utf-8")
                raise subprocess.TimeoutExpired(command, timeout)
            proc.communicate.side_effect = communicate
            return proc
        self.popen.side_effect = timeout
        stop = self.patch(cli, "stop_process")
        row = self.one()
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["task_id"])
        self.assertTrue((self.root / "job-01/artifacts/partial.md").exists())
        self.popen.assert_called_once()
        stop.assert_called_once()

    def test_stop_prevents_new_launch(self):
        cli.STOP.set()
        self.assertEqual(self.one()["status"], "not_started")
        self.popen.assert_not_called()
        self.assertFalse((self.root / "job-01").exists())

    def test_auth_rejection_before_jobs(self):
        self.permit_probe("Logged in using API key")
        self.invoke(self.argv() + ["--execute"], error=2)
        self.assertFalse(self.out.exists())
        self.popen.assert_not_called()

    def test_incompatible_cli_does_not_launch(self):
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", "")]
        self.invoke(self.argv() + ["--execute"], error=2)
        self.assertFalse(self.out.exists())
        self.popen.assert_not_called()

    def test_success_summary_unique_ids_and_frozen_outputs(self):
        self.permit_probe()
        self.popen.side_effect = self.fake
        code, output = self.invoke(self.argv(2) + ["--execute"])
        self.assertEqual(code, 0)
        self.assertEqual(len(output.strip().splitlines()), 2)
        summary = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["execution_success"])
        self.assertEqual(len({row["task_id"] for row in summary["jobs"]}), 2)
        self.invoke(self.argv(2) + ["--execute"], error=2)

    def test_duplicate_identity_fails_batch(self):
        self.permit_probe()
        row = {"status": "completed", "task_id": str(uuid.UUID(int=1))}
        self.patch(cli, "run_one", return_value=row)
        code, _ = self.invoke(self.argv(2) + ["--execute"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads((self.out / "summary.json").read_text())["execution_success"])

    def test_individual_handoff_and_failure_does_not_block_peer(self):
        self.permit_probe()
        notified = threading.Event()
        notices = []
        def worker(args, source, source_text, job):
            if job.name == "job-01":
                raise OSError("Synthetic setup failure.")
            if not notified.wait(3):
                raise AssertionError("Controller waited for entire batch.")
            return {"status": "completed", "task_id": str(uuid.UUID(int=2))}
        def emit(value, **kwargs):
            notices.append(json.loads(value))
            notified.set()
        self.patch(cli, "run_one", side_effect=worker)
        with mock.patch("builtins.print", side_effect=emit):
            code, _ = self.invoke(self.argv(2) + ["--execute"])
        self.assertEqual(code, 1)
        self.assertEqual([n["status"] for n in notices], ["failed", "completed"])
        self.popen.assert_not_called()

    def test_evidence_parser_handles_noise_and_ambiguous_identity(self):
        log = self.root / "events"
        log.write_text("noise\n[]\n" + "\n".join(json.dumps(
            {"type":"thread.started","thread_id":str(uuid.UUID(int=n))}) for n in (1, 2)),
            encoding="utf-8")
        ids, complete, _, _ = cli.evidence(log)
        self.assertEqual(len(ids), 2)
        self.assertFalse(complete)


if __name__ == "__main__":
    unittest.main()
