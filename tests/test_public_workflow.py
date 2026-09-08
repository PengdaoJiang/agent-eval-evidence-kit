from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import agent_eval_orchestrator as orchestrator
import offline_replay


class PublicWorkflowTests(unittest.TestCase):
    def test_prepare_is_offline_and_does_not_stage_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = orchestrator.build_parser().parse_args([
                "--run-dir", str(pathlib.Path(temporary) / "plan"), "--prepare-only"
            ])
            with mock.patch.object(orchestrator, "prepare_isolated_codex_home") as auth, \
                 mock.patch.object(orchestrator, "preflight_local") as preflight, \
                 mock.patch.object(orchestrator, "stage_remote") as remote:
                self.assertEqual(orchestrator.execute(args), 0)
                auth.assert_not_called()
                preflight.assert_not_called()
                remote.assert_not_called()

    def test_live_execution_needs_explicit_opt_in(self):
        args = orchestrator.build_parser().parse_args(["--run-dir", "unused"])
        with mock.patch.object(orchestrator, "initialize") as initialize:
            with self.assertRaises(orchestrator.OrchestratorError):
                orchestrator.execute(args)
            initialize.assert_not_called()

    def test_real_analyzer_handles_generated_faults_and_missing_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = offline_replay.replay(pathlib.Path(temporary) / "run", omit_last=True)
        self.assertEqual(summary["planned_calls"], 48)
        self.assertEqual(summary["planned_pairs"], 24)
        self.assertEqual(summary["missing_results"], 1)
        self.assertEqual(summary["evidence_class"], "synthetic_pipeline_test")


if __name__ == "__main__":
    unittest.main()
