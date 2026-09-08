from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_eval_probe as probe  # noqa: E402


class AgentEvalProbeTests(unittest.TestCase):
    def test_execpolicy_stderr_only_ignores_exact_current_home_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = pathlib.Path(temporary) / "codex-home"
            codex_home.mkdir()
            rendered_home = str(codex_home.resolve()).replace("\\", "\\\\")
            warning = (
                "WARNING: proceeding, even though we could not create PATH aliases: "
                "Refusing to create helper binaries under temporary dir "
                '"C:\\\\Temp\\\\" '
                f'(codex_home: AbsolutePathBuf("{rendered_home}"))'
            )
            ignored, unexpected = probe._classify_execpolicy_stderr(
                warning + "\n", codex_home=codex_home
            )
            wrong_home_ignored, wrong_home_unexpected = (
                probe._classify_execpolicy_stderr(
                    warning.replace("codex-home", "other-home") + "\n",
                    codex_home=codex_home,
                )
            )
            extra_ignored, extra_unexpected = probe._classify_execpolicy_stderr(
                warning + " appended\n", codex_home=codex_home
            )
        self.assertEqual(ignored, [warning])
        self.assertEqual(unexpected, [])
        self.assertEqual(wrong_home_ignored, [])
        self.assertEqual(len(wrong_home_unexpected), 1)
        self.assertEqual(extra_ignored, [])
        self.assertEqual(len(extra_unexpected), 1)

    def test_jsonl_preserves_unknown_object_and_rejects_non_object(self) -> None:
        parsed, rejected = probe.parse_json_lines(
            [
                {
                    "t_ms": 1.0,
                    "line": '{"type":"item.completed","item":{"id":"x","type":"future_tool","payload":{"x":1}}}',
                },
                {"t_ms": 2.0, "line": '["valid","but","not","an","event"]'},
                {"t_ms": 3.0, "line": "not-json"},
            ]
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["event"]["item"]["type"], "future_tool")
        self.assertEqual(parsed[0]["event"]["item"]["payload"], {"x": 1})
        self.assertEqual(len(rejected), 2)
        self.assertIn("non-object", rejected[0]["error"])

    def test_workspace_snapshot_and_diff_capture_content_and_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "one.txt").write_text("one", encoding="utf-8")
            before = probe.snapshot_workspace(root)
            (root / "one.txt").write_text("changed", encoding="utf-8")
            (root / "two.txt").write_text("two", encoding="utf-8")
            after = probe.snapshot_workspace(root)
        diff = probe.diff_snapshots(before, after)
        self.assertEqual(diff["added"], ["two.txt"])
        self.assertEqual(diff["modified"], ["one.txt"])
        self.assertEqual(diff["removed"], [])
        self.assertEqual(after["files"]["one.txt"]["content_utf8"], "changed")

    def test_command_uses_fixture_specific_sandbox_and_machine_output(self) -> None:
        root = ROOT / "fixtures" / "agent_eval" / "f2_python_bugfix"
        command = probe.build_codex_command(
            codex="codex",
            fixture_id="f2_python_bugfix",
            fixture_root=root,
            model="gpt-test",
            effort="high",
        )
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--json", command)
        self.assertIn("--output-schema", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertNotIn("--ignore-rules", command)
        self.assertIn('cli_auth_credentials_store="file"', command)
        self.assertNotIn("features.code_mode_host=true", command)
        self.assertEqual(command[-1], "-")

    def test_all_fixtures_are_read_only_and_have_no_write_allowance(self) -> None:
        self.assertEqual(
            {
                fixture_id: config["minimum_command_completions"]
                for fixture_id, config in probe.FIXTURES.items()
            },
            {
                "f1_manifest_join_readonly": 4,
                "f2_python_bugfix": 3,
                "f3_incident_report": 5,
            },
        )
        for config in probe.FIXTURES.values():
            self.assertEqual(config["sandbox"], "read-only")
            self.assertEqual(config["allowed_writes"], [])
            self.assertEqual(config["required_changed_paths"], [])

    def test_codex_environment_and_home_audit_do_not_expose_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = pathlib.Path(temporary) / "codex-home"
            (codex_home / "rules").mkdir(parents=True)
            secret = "do-not-record-this-token"
            (codex_home / "auth.json").write_text(secret, encoding="utf-8")
            (codex_home / "config.toml").write_text(
                'cli_auth_credentials_store = "file"\n', encoding="utf-8"
            )
            (codex_home / "rules" / "default.rules").write_text(
                "prefix_rule(pattern=['python'], decision='allow')\n",
                encoding="utf-8",
            )
            environment = probe.codex_environment(codex_home)
            audit = probe.audit_codex_home(codex_home)
        self.assertEqual(environment["CODEX_HOME"], str(codex_home))
        self.assertTrue(audit["auth_present"])
        self.assertNotIn(secret, repr(audit))
        self.assertNotIn("auth_sha256", audit)

    def test_execpolicy_preflight_covers_positive_negative_and_resolved_cases(self) -> None:
        expected = {
            tuple(case["argv"]): case["expected_decision"]
            for case in probe.execpolicy_cases()
        }

        def fake_capture(command: list[str], timeout_s: float, *, environment: dict[str, str]):
            self.assertEqual(environment["CODEX_HOME"], str(codex_home))
            self.assertEqual(command[1:3], ["execpolicy", "check"])
            argv = command[command.index("--") + 1 :]
            decision = expected[tuple(argv)]
            payload = {"matchedRules": []}
            if decision is not None:
                payload = {
                    "matchedRules": [
                        {"prefixRuleMatch": {"matchedPrefix": argv, "decision": decision}}
                    ],
                    "decision": decision,
                }
            rendered_home = str(codex_home.resolve()).replace("\\", "\\\\")
            known_warning = (
                probe._TEMP_PATH_ALIAS_WARNING_PREFIX
                + '"C:\\\\Temp\\\\" '
                + f'(codex_home: AbsolutePathBuf("{rendered_home}"))\n'
            )
            return {
                "command": command,
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 1.0,
                "stdout": json.dumps(payload),
                "stderr": known_warning,
            }

        def unexpected_capture(
            command: list[str], timeout_s: float, *, environment: dict[str, str]
        ):
            captured = fake_capture(
                command,
                timeout_s,
                environment=environment,
            )
            captured["stderr"] += "unexpected diagnostic\n"
            return captured

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            rules = root / "controlled.rules"
            rules.write_text("rules", encoding="utf-8")
            with mock.patch.object(probe, "capture_command", side_effect=fake_capture) as captured:
                summary = probe.run_execpolicy_preflight(
                    codex="codex",
                    codex_home=codex_home,
                    rules=rules,
                )
            with mock.patch.object(
                probe, "capture_command", side_effect=unexpected_capture
            ):
                rejected_summary = probe.run_execpolicy_preflight(
                    codex="codex",
                    codex_home=codex_home,
                    rules=rules,
                )
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["case_count"], len(probe.execpolicy_cases()) * 2)
        self.assertEqual(captured.call_count, len(probe.execpolicy_cases()) * 2)
        self.assertTrue(
            all(
                case["stderr_classification"]
                == "known_temp_home_path_alias_warning"
                and case["ignored_stderr_count"] == 1
                and case["ignored_stderr_sha256"]
                for case in summary["cases"]
            )
        )
        self.assertFalse(rejected_summary["passed"])
        self.assertTrue(
            all(
                case["stderr_classification"] == "unexpected"
                and case["unexpected_stderr_lines"] == ["unexpected diagnostic"]
                for case in rejected_summary["cases"]
            )
        )
        forms = {case["argv_form"] for case in summary["cases"]}
        self.assertEqual(forms, {"inner", "outer_powershell"})
        self.assertEqual(
            sum(case["resolve_host_executables"] for case in summary["cases"]),
            len(probe.execpolicy_cases()),
        )
        self.assertTrue(any(case["known_prefix_boundary"] for case in summary["cases"]))
        self.assertTrue(
            any(case["expected_decision"] is None for case in summary["cases"])
        )
        self.assertTrue(
            any(
                case["name"].startswith("deny_removed_validator")
                and case["expected_decision"] is None
                for case in summary["cases"]
            )
        )
        self.assertFalse(
            any(case["name"].startswith("allow_validator") for case in summary["cases"])
        )


if __name__ == "__main__":
    unittest.main()
