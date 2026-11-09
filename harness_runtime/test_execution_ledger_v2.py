"""Three targeted offline ledger tests; all candidates and processes are synthetic."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import execution_ledger_v2 as ledger
import selected_execution as se
import selected_execution_v2 as v2
import test_selected_execution_v2 as fixtures
from versioned_inputs_v2 import VersionStore


class LedgerV2Tests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.VersionedInputTests('test_json_variants_use_small_deltas_and_do_not_copy_base')
        self.fixture.setUp(); f=self.fixture
        se.copy_selected(f.context(),[(str(f.base),'copy/base.json')])
        f.build_spec(); self.first_data=(f.work/'inputs/model.json').read_bytes()
        self.results=[]
        for index,exit_code in enumerate((3,4)):
            if index:(f.work/'inputs/model.json').write_bytes(f.variant(2))
            call_count=[]
            def execute(argv,**kwargs):
                call_count.append(argv)
                if len(call_count)==1:
                    Path(argv[-1]).write_bytes(b'fresh synthetic binary')
                    return subprocess.CompletedProcess(argv,0)
                kwargs['stdout'].write(b'raw consumer output')
                return subprocess.CompletedProcess(argv,exit_code)
            with patch.object(se.subprocess,'run',side_effect=execute):
                result,code=v2.build_run(f.context(),'build.json')
            self.assertEqual(code,exit_code); self.results.append(result)
        self.freeze_path=f.lab/'receipts/freeze'/f'{f.job}.json'
        self.freeze={'schema':'rea.harness.constructor-freeze.v1','job_id':f.job,
                     'session_id':json.loads((f.root/'evidence/TASK_CONTRACT.json').read_text())['session_id'],
                     'workspace_id':se.WORKSPACE,'owner_turn_terminal':True,'all_recorded_tools_ended':True,
                     'catalog':se.identity(f.root/'evidence/INPUT_CATALOG.json'),
                     'contract':se.identity(f.root/'evidence/TASK_CONTRACT.json'),
                     'candidate_files':[se.identity(p) for p in sorted(f.work.rglob('*')) if p.is_file()]}
        self.save_freeze()
        self.assertTrue(f.scratch.resolve().is_relative_to(f.fixture)); shutil.rmtree(f.scratch); f.scratch.mkdir()

    def save_freeze(self):self.fixture.put(self.freeze_path,self.freeze)
    def tearDown(self):self.fixture.tearDown()
    def derive(self):return ledger.build_ledger(self.fixture.job,str(self.freeze_path),lab=self.fixture.lab)

    def test_overwritten_first_input_reconstructs_in_memory_without_scratch(self):
        f=self.fixture
        with patch.object(VersionStore,'restore',side_effect=AssertionError('Ledger must not materialize scratch')):
            report=self.derive()
        first_sha=__import__('hashlib').sha256(self.first_data).hexdigest()
        version=next(v for v in report['input_versions'] if v['sha256']==first_sha)
        self.assertEqual(version['recovery']['status'],'VERIFIED_RECONSTRUCTABLE')
        self.assertNotEqual((f.work/'inputs/model.json').read_bytes(),self.first_data)
        self.assertEqual(list(f.scratch.iterdir()),[])
        self.assertEqual(report['counts']['unavailable_input_versions'],0)
        published=ledger.publish(f.job,str(self.freeze_path),lab=f.lab)
        self.assertEqual(published['status'],'DERIVED_REPORT_CREATED')
        self.assertEqual(ledger.publish(f.job,str(self.freeze_path),lab=f.lab)['status'],'IDENTICAL_EXISTING_REPORT')

    def test_unselected_or_missing_recipe_refuses_recoverability_certification(self):
        recipes=[r for r in self.freeze['candidate_files'] if '/input_versions_v2/recipes/' in r['path'].replace('\\','/')]
        self.assertTrue(recipes); target=recipes[0]
        self.freeze['candidate_files'].remove(target); self.save_freeze()
        with self.assertRaisesRegex(se.Refused,'RECOVERY_DEPENDENCY_NOT_IN_FREEZE'):self.derive()
        self.freeze['candidate_files'].append(target); self.save_freeze()
        path=Path(target['path']).resolve(); self.assertTrue(path.is_relative_to(self.fixture.fixture)); path.unlink()
        with self.assertRaisesRegex(se.Refused,'FROZEN_RECOVERY_DEPENDENCY_MISSING'):self.derive()

    def test_counts_come_from_raw_v1_copy_v2_execution_receipts(self):
        report=self.derive(); counts=report['counts']
        self.assertEqual(counts['receipts'],3); self.assertEqual(counts['copy'],1); self.assertEqual(counts['build_run'],2)
        self.assertEqual(counts['compile_exit_raw_histogram'],{'0':2}); self.assertEqual(counts['run_exit_raw_histogram'],{'3':1,'4':1})
        self.assertNotIn('accepted',counts); self.assertIn('not automatically scientific PASS/FAIL',report['semantic_boundary'])
        self.assertEqual(report['selection']['directory_scans'],0); self.assertEqual(report['selection']['scratch_writes'],0)


if __name__=='__main__':unittest.main(verbosity=2)
