from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_eval_remote_bootstrap as bootstrap  # noqa: E402


class RemoteBootstrapTests(unittest.TestCase):
    def test_first_line_prefers_login_status_after_codex_warning(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout="",
            stderr=(
                "WARNING: could not create PATH aliases\n"
                "Logged in using ChatGPT\n"
            ),
        )
        with mock.patch.object(bootstrap.subprocess, "run", return_value=completed):
            result = bootstrap.first_line(
                ["codex", "login", "status"],
                environment={},
                label="isolated-home login",
                preferred_prefix="Logged in",
            )
        self.assertEqual(result, "Logged in using ChatGPT")

    def test_prepare_remote_rejects_helper_outside_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            experiment = root / "experiment"
            experiment.mkdir()
            outside_helper = root / "agent_eval_remote_bootstrap.py"
            outside_helper.write_text("outside", encoding="utf-8")
            with mock.patch.object(bootstrap, "__file__", str(outside_helper)):
                with self.assertRaisesRegex(
                    bootstrap.BootstrapError,
                    "bootstrap must run from the experiment directory",
                ):
                    bootstrap.prepare_remote(
                        experiment,
                        codex=sys.executable,
                        language_mode="ConstrainedLanguage",
                    )

    def test_prepare_remote_uses_fixed_files_and_never_reports_auth_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            experiment = root / "experiment"
            experiment.mkdir()
            staged_bootstrap = experiment / "agent_eval_remote_bootstrap.py"
            staged_bootstrap.write_text("frozen bootstrap", encoding="utf-8")
            (experiment / "agent_eval_probe.py").write_text(
                "probe", encoding="utf-8"
            )
            (experiment / "controlled.rules").write_text(
                "rules", encoding="utf-8"
            )
            (experiment / "isolated-config.toml").write_text(
                'cli_auth_credentials_store = "file"\n', encoding="utf-8"
            )
            with zipfile.ZipFile(experiment / "fixtures.zip", "w") as archive:
                archive.writestr("f1/input.txt", "fixture")

            profile = root / "profile"
            source_auth = profile / ".codex" / "auth.json"
            source_auth.parent.mkdir(parents=True)
            secret = b'{"access_token":"must-not-appear"}'
            source_auth.write_bytes(secret)
            environment = {
                **os.environ,
                "USERPROFILE": str(profile),
            }

            def fake_first_line(
                command: list[str], *, label: str, **_: object
            ) -> str:
                if label == "isolated-home Codex":
                    return "codex-cli 0.144.4"
                if label == "isolated-home login":
                    return "Logged in using ChatGPT"
                return "Python 3.14.0"

            with mock.patch.object(
                bootstrap, "__file__", str(staged_bootstrap)
            ), mock.patch.object(bootstrap, "first_line", side_effect=fake_first_line):
                result = bootstrap.prepare_remote(
                    experiment,
                    codex=sys.executable,
                    language_mode="ConstrainedLanguage",
                    environ=environment,
                )

            destination_auth = experiment / "codex-home" / "auth.json"
            self.assertEqual(destination_auth.read_bytes(), secret)
            self.assertEqual(
                (experiment / "fixtures" / "f1" / "input.txt").read_text(
                    encoding="utf-8"
                ),
                "fixture",
            )
            self.assertEqual(result["auth"], "present")
            self.assertEqual(result["auth_action"], "copied")
            self.assertEqual(result["language_mode"], "ConstrainedLanguage")
            self.assertNotIn("must-not-appear", repr(result))
            self.assertFalse(any("auth" in key and "hash" in key for key in result))

            destination_auth.write_bytes(b'{"existing":"keep"}')
            source_auth.write_bytes(b'{"replacement":"ignore"}')
            with mock.patch.object(
                bootstrap, "__file__", str(staged_bootstrap)
            ), mock.patch.object(bootstrap, "first_line", side_effect=fake_first_line):
                resumed = bootstrap.prepare_remote(
                    experiment,
                    codex=sys.executable,
                    language_mode="ConstrainedLanguage",
                    environ=environment,
                )
            self.assertEqual(destination_auth.read_bytes(), b'{"existing":"keep"}')
            self.assertEqual(resumed["auth_action"], "preserved")

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            archive_path = root / "fixtures.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "no")
            with self.assertRaisesRegex(bootstrap.BootstrapError, "unsafe path"):
                bootstrap.safe_extract(archive_path, root / "fixtures")
            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
