from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_eval_orchestrator as orchestrator  # noqa: E402


def schedule() -> dict:
    fixtures = {fixture: f"tree-{fixture}" for fixture in orchestrator.FIXTURE_IDS}
    schemas = {fixture: f"schema-{fixture}" for fixture in orchestrator.FIXTURE_IDS}
    return orchestrator.build_schedule(
        seed=20260716,
        effort="high",
        timeout_s=420.0,
        experiment_id="agent-eval-offline",
        probe_sha256="probe-hash",
        orchestrator_sha256="orchestrator-hash",
        analyzer_sha256="analyzer-hash",
        fixture_tree_hashes=fixtures,
        fixture_schema_hashes=schemas,
        fixture_archive_sha256="archive-hash",
        controlled_rules_sha256="rules-hash",
        isolated_config_sha256="config-hash",
        remote_bootstrap_sha256="bootstrap-hash",
        local_python="C:/Python/python.exe",
        codex="codex",
        ssh_host="remote",
        remote_python="C:/Python/python.exe",
        remote_codex="codex",
        remote_root="C:/Temp/agent-eval",
    )


class AgentEvalScheduleTests(unittest.TestCase):
    def test_first_round_is_fixed_and_balanced_with_adjacent_pairs(self) -> None:
        value = schedule()
        self.assertEqual(value["block_count"], 24)
        self.assertEqual(value["call_count"], 48)
        self.assertEqual(value["parameters"]["models"], list(orchestrator.MODELS))
        self.assertEqual(value["parameters"]["fixtures"], list(orchestrator.FIXTURE_IDS))
        local_work_root = pathlib.Path(value["execution"]["local_work_root"])
        self.assertTrue(str(local_work_root).startswith(str(pathlib.Path(tempfile.gettempdir()))))
        self.assertFalse(str(local_work_root).startswith(str(ROOT)))
        local_codex_home = pathlib.Path(value["execution"]["local_codex_home"])
        self.assertTrue(
            str(local_codex_home).startswith(str(pathlib.Path(tempfile.gettempdir())))
        )
        self.assertFalse(str(local_codex_home).startswith(str(ROOT)))
        self.assertEqual(value["parameters"]["controlled_rules_sha256"], "rules-hash")
        self.assertEqual(
            value["parameters"]["remote_bootstrap_sha256"], "bootstrap-hash"
        )
        self.assertEqual(
            value["parameters"]["execpolicy_case_count"],
            len(orchestrator.probe.execpolicy_cases()) * 2,
        )
        self.assertEqual(
            value["parameters"]["execpolicy_argv_forms"],
            ["inner", "outer_powershell"],
        )
        self.assertTrue(
            value["execution"]["remote_codex_rules"].endswith(
                "/codex-home/rules/default.rules"
            )
        )
        self.assertTrue(
            value["execution"]["remote_bootstrap"].endswith(
                "/agent_eval_remote_bootstrap.py"
            )
        )

        by_condition: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        sequences: list[int] = []
        pair_ids: set[str] = set()
        for block in value["blocks"]:
            by_condition[(block["model"], block["fixture_id"])][
                block["host_order_label"]
            ] += 1
            pair_ids.add(block["pair_id"])
            self.assertEqual(len(block["calls"]), 2)
            self.assertEqual(
                block["prompt_sha256"],
                orchestrator.probe.sha256_text(block["prompt"]),
            )
            self.assertEqual(
                [call["host_label"] for call in block["calls"]],
                block["host_order"],
            )
            self.assertEqual(
                block["calls"][1]["sequence_index"],
                block["calls"][0]["sequence_index"] + 1,
            )
            sequences.extend(call["sequence_index"] for call in block["calls"])
        self.assertEqual(len(pair_ids), 24)
        self.assertEqual(sorted(sequences), list(range(1, 49)))
        for counts in by_condition.values():
            self.assertEqual(counts["local_then_remote"], 1)
            self.assertEqual(counts["remote_then_local"], 1)

    def test_schedule_is_seed_deterministic(self) -> None:
        first = schedule()
        second = schedule()
        identities = lambda value: [
            (
                block["model"],
                block["fixture_id"],
                block["rep"],
                block["host_order_label"],
                block["pair_id"],
            )
            for block in value["blocks"]
        ]
        self.assertEqual(identities(first), identities(second))

    def test_encoded_powershell_round_trip_supports_unicode_and_quotes(self) -> None:
        source = "$x='广西'; Write-Output $x"
        command = orchestrator.encoded_powershell(source)
        encoded = command[-1]
        self.assertEqual(base64.b64decode(encoded).decode("utf-16le"), source)

    def test_remote_command_rejects_oversized_encoded_argv_before_ssh(self) -> None:
        args = argparse.Namespace(
            ssh="ssh",
            ssh_host="remote",
            connect_timeout_s=15,
        )
        oversized = "Write-Output '" + ("x" * 6000) + "'"
        self.assertGreater(
            orchestrator.remote_powershell_command_chars(oversized),
            orchestrator.WINDOWS_REMOTE_COMMAND_LIMIT,
        )
        with mock.patch.object(orchestrator, "command_result") as executed:
            with self.assertRaisesRegex(
                orchestrator.OrchestratorError, "stage a helper file"
            ):
                orchestrator.remote_command(args, oversized, timeout_s=1.0)
        executed.assert_not_called()

    def test_prepare_isolated_home_copies_auth_without_recording_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            auth = root / "source-auth.json"
            secret = b'{"token":"do-not-record"}'
            auth.write_bytes(secret)
            rules = root / "controlled.rules"
            rules.write_text("rules", encoding="utf-8")
            config = root / "isolated-config.toml"
            config.write_text(orchestrator.ISOLATED_CONFIG_TEXT, encoding="utf-8")
            codex_home = root / "codex-home"
            audit = orchestrator.prepare_isolated_codex_home(
                codex_home,
                rules,
                config,
                auth_source=auth,
            )
            copied_auth = (codex_home / "auth.json").read_bytes()
        self.assertEqual(copied_auth, secret)
        self.assertEqual(audit["auth"], "present")
        self.assertNotIn("do-not-record", repr(audit))
        self.assertNotIn("auth_sha256", audit)

    def test_probe_arguments_bind_host_private_codex_home(self) -> None:
        value = schedule()
        block = value["blocks"][0]
        call = block["calls"][0]
        arguments = orchestrator.probe_arguments(
            value,
            block,
            call,
            fixture_root="C:/fixture",
            output="C:/result.json",
            codex="codex",
            codex_home="C:/private/codex-home",
        )
        self.assertEqual(
            arguments[arguments.index("--codex-home") + 1],
            "C:/private/codex-home",
        )

    def test_result_validation_requires_frozen_home_and_enabled_rules(self) -> None:
        value = schedule()
        block = value["blocks"][0]
        call = block["calls"][0]
        payload = {
            **orchestrator.expected_result_fields(value, block, call),
            "prompt_sha256": block["prompt_sha256"],
            "command": ["codex", "exec", "--ignore-user-config"],
            "codex_home_audit": {
                "auth_present": True,
                "rules_sha256": "rules-hash",
                "config_sha256": "config-hash",
            },
            "workspace": {
                "before": {
                    "tree_sha256": value["parameters"]["fixture_tree_hashes"][
                        block["fixture_id"]
                    ]
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "result.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            valid, reason = orchestrator.validate_result(path, value, block, call)
            payload["command"].append("--ignore-rules")
            path.write_text(json.dumps(payload), encoding="utf-8")
            invalid, invalid_reason = orchestrator.validate_result(
                path, value, block, call
            )
        self.assertTrue(valid, reason)
        self.assertFalse(invalid)
        self.assertIn("disabled controlled rules", invalid_reason)

    def test_initialize_freezes_rules_config_and_calls_local_home_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = pathlib.Path(temporary) / "run"
            args = argparse.Namespace(
                run_dir=str(run_dir),
                resume=False,
                seed=20260716,
                effort="high",
                timeout_s=420.0,
                local_python=sys.executable,
                codex="codex",
                ssh_host="remote",
                remote_python="C:/Python/python.exe",
                remote_codex="codex",
                remote_root="C:/Temp/agent-eval",
            )
            with mock.patch.object(
                orchestrator, "prepare_isolated_codex_home", return_value={"auth": "present"}
            ) as prepared:
                initialized = orchestrator.initialize(args)
            _, value, _, _, rules_snapshot, config_snapshot, _ = initialized
            frozen_rules_hash = orchestrator.sha256_file(rules_snapshot)
            frozen_config_hash = orchestrator.sha256_file(config_snapshot)
            frozen_config_text = config_snapshot.read_text(encoding="utf-8")
            frozen_bootstrap = run_dir / "artifacts" / "agent_eval_remote_bootstrap.py"
            frozen_bootstrap_hash = orchestrator.sha256_file(frozen_bootstrap)
        self.assertEqual(
            frozen_rules_hash, value["parameters"]["controlled_rules_sha256"]
        )
        self.assertEqual(
            frozen_config_hash, value["parameters"]["isolated_config_sha256"]
        )
        self.assertEqual(
            frozen_config_text,
            orchestrator.ISOLATED_CONFIG_TEXT,
        )
        self.assertEqual(
            frozen_bootstrap_hash, value["parameters"]["remote_bootstrap_sha256"]
        )
        prepared.assert_called_once()

    def test_local_preflight_uses_isolated_home_and_policy(self) -> None:
        value = schedule()
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = pathlib.Path(temporary) / "codex-home"
            (codex_home / "rules").mkdir(parents=True)
            (codex_home / "auth.json").write_text("secret", encoding="utf-8")
            config = codex_home / "config.toml"
            config.write_text(orchestrator.ISOLATED_CONFIG_TEXT, encoding="utf-8")
            rules = codex_home / "rules" / "default.rules"
            rules.write_text("rules", encoding="utf-8")
            value["execution"]["local_codex_home"] = str(codex_home)
            value["parameters"]["controlled_rules_sha256"] = orchestrator.sha256_file(rules)
            value["parameters"]["isolated_config_sha256"] = orchestrator.sha256_file(config)

            def fake_command(command: list[str], **_: object) -> dict:
                if command[-2:] == ["login", "status"]:
                    output = "Logged in using ChatGPT"
                elif command[-1] == "--version" and "python" in command[0].casefold():
                    output = "Python 3.14"
                else:
                    output = "codex-cli 0.144.4"
                return {
                    "exit_code": 0,
                    "timed_out": False,
                    "duration_ms": 1.0,
                    "stdout": output,
                    "stderr": "",
                }

            policy = {
                "passed": True,
                "rules_sha256": orchestrator.sha256_file(rules),
                "case_count": len(orchestrator.probe.execpolicy_cases()) * 2,
                "cases": [],
            }
            with mock.patch.object(orchestrator, "command_result", side_effect=fake_command), mock.patch.object(
                orchestrator.probe, "run_execpolicy_preflight", return_value=policy
            ) as policy_call:
                result = orchestrator.preflight_local(value)
        self.assertEqual(result["home"]["auth"], "present")
        policy_call.assert_called_once()
        self.assertEqual(
            policy_call.call_args.kwargs["codex_home"], codex_home
        )

    def test_remote_stage_is_fully_mocked_and_binds_remote_home(self) -> None:
        value = schedule()
        command_ok = {
            "exit_code": 0,
            "timed_out": False,
            "duration_ms": 1.0,
            "stdout": "",
            "stderr": "",
        }
        preflight_lines = {
            "bootstrap": "bootstrap-hash",
            "probe": "probe-hash",
            "archive": "archive-hash",
            "staged_rule": "rules-hash",
            "home_rule": "rules-hash",
            "staged_config": "config-hash",
            "home_config": "config-hash",
            "auth": "present",
            "auth_action": "copied",
            "python": "Python 3.14",
            "python_on_path": "Python 3.14",
            "python_path": "C:/Python/python.exe",
            "language_mode": "FullLanguage",
            "codex": "codex-cli 0.144.4",
            "login": "Logged in using ChatGPT",
        }
        expanded = {
            **command_ok,
            "stdout": "\n".join(f"{key}={item}" for key, item in preflight_lines.items()),
        }
        policy = {
            "passed": True,
            "rules_sha256": "rules-hash",
            "case_count": len(orchestrator.probe.execpolicy_cases()) * 2,
            "cases": [],
        }
        policy_result = {**command_ok, "stdout": json.dumps(policy)}
        args = argparse.Namespace(
            ssh="ssh",
            scp="scp",
            ssh_host="remote",
            connect_timeout_s=15,
        )
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = pathlib.Path(temporary)
            probe = artifacts / "agent_eval_probe.py"
            probe.write_text("probe", encoding="utf-8")
            bootstrap = artifacts / "agent_eval_remote_bootstrap.py"
            bootstrap.write_text("frozen bootstrap", encoding="utf-8")
            bootstrap_hash = orchestrator.sha256_file(bootstrap)
            value["parameters"]["remote_bootstrap_sha256"] = bootstrap_hash
            preflight_lines["bootstrap"] = bootstrap_hash
            expanded["stdout"] = "\n".join(
                f"{key}={item}" for key, item in preflight_lines.items()
            )
            with mock.patch.object(
                orchestrator,
                "remote_command",
                side_effect=[command_ok, expanded, policy_result],
            ) as remote, mock.patch.object(
                orchestrator, "command_result", return_value=command_ok
            ) as copied:
                result = orchestrator.stage_remote(
                    args,
                    value,
                    probe,
                    artifacts / "fixtures.zip",
                    artifacts / "controlled.rules",
                    artifacts / "isolated-config.toml",
                )
        self.assertTrue(result["policy"]["passed"])
        self.assertEqual(result["bootstrap_transport"], "staged_python_helper")
        self.assertEqual(copied.call_count, 5)
        setup_script = remote.call_args_list[1].args[1]
        policy_script = remote.call_args_list[2].args[1]
        self.assertIn("agent_eval_remote_bootstrap.py", setup_script)
        self.assertIn("--experiment-dir", setup_script)
        self.assertIn("--language-mode $languageMode", setup_script)
        self.assertNotIn("sourceAuth", setup_script)
        self.assertNotIn("Get-Content", setup_script)
        self.assertLessEqual(
            orchestrator.remote_powershell_command_chars(setup_script),
            orchestrator.WINDOWS_REMOTE_COMMAND_BUDGET,
        )
        for remote_call in remote.call_args_list:
            self.assertLessEqual(
                orchestrator.remote_powershell_command_chars(remote_call.args[1]),
                orchestrator.WINDOWS_REMOTE_COMMAND_BUDGET,
            )
        copied_destinations = [call.args[0][-1] for call in copied.call_args_list]
        self.assertIn(
            "remote:" + value["execution"]["remote_bootstrap"],
            copied_destinations,
        )
        self.assertIn(value["execution"]["remote_codex_home"], policy_script)


if __name__ == "__main__":
    unittest.main()
