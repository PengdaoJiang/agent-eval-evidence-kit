"""Offline synthetic tests. No model, Harness API, compiler or candidate is run."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import selected_execution as se
import selected_execution_v2 as v2
import versioned_inputs_v2 as vi


class VersionedInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='rea-versioned-input-v2-')
        self.fixture=Path(self.temp.name).resolve(); self.lab=self.fixture/'lab'
        self.job='DS1017-XPM-TEST-V2'; self.root=self.lab/'runs'/self.job
        self.work=self.root/'work'; self.scratch=self.root/'scratch'
        for relative in ('.control','evidence','work','scratch'):
            (self.root/relative).mkdir(parents=True)
        session='session-11111111-2222-3333-4444-555555555555'
        binding={'job_id':self.job,'session_id':session,'workspace_id':se.WORKSPACE}
        self.put(self.root/'.control/prepare.json',{**binding,'job':self.job,'schema':'rea.harness.task-storage.prepare.v1','root':str(self.root),'target':str(self.lab)})
        self.put(self.root/'evidence/TASK_CONTRACT.json',binding)
        self.catalog={'job_id':self.job,'references':[]}
        self.compiler=self.fixture/'compiler.exe'; self.compiler.write_bytes(b'not executable; subprocess mocked')
        self.select(self.compiler)
        self.base=self.fixture/'base.json'
        self.base_value={'schema':'synthetic.base','items':[{'id':i,'payload':'abcdef0123456789'*6,'value':i} for i in range(2000)],'tail':{'z':0,'a':1}}
        self.base.write_bytes(self.json_bytes(self.base_value)); self.select(self.base)

    def tearDown(self):
        # Only this TemporaryDirectory owns these paths; no project roots are removed.
        self.assertTrue(self.fixture.name.startswith('rea-versioned-input-v2-'))
        self.temp.cleanup()

    @staticmethod
    def json_bytes(value):
        return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')

    @staticmethod
    def put(path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value),encoding='utf-8')

    def select(self,path):
        self.catalog['references'].append({'path':str(path),**vi.digest(path.read_bytes())})
        self.put(self.root/'evidence/INPUT_CATALOG.json',self.catalog)

    def context(self):return se.Context(self.job,lab=self.lab)
    def store(self):return vi.VersionStore(self.context())
    def variant(self,index=1):
        value=json.loads(self.base.read_text()); value['items'][index]['value']=-index
        return self.json_bytes(value)
    def owned(self,name,data):
        path=self.work/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data); return path
    def clear_materialization(self,path):
        resolved=Path(path).resolve(); self.assertTrue(resolved.is_relative_to(self.scratch.resolve()))
        self.assertNotEqual(resolved,self.scratch.resolve()); shutil.rmtree(resolved)

    def test_json_variants_use_small_deltas_and_do_not_copy_base(self):
        store=self.store(); original_base=self.base.read_bytes()
        source=self.owned('inputs/model.json',self.variant(1)); first_bytes=source.read_bytes()
        first=store.freeze({'model':str(source)},{'model':str(self.base)})
        source.write_bytes(self.variant(2)); second=store.freeze({'model':str(source)},{'model':str(self.base)})
        for snap in (first,second):
            record=json.loads(Path(snap['snapshot_path']).read_text())
            self.assertEqual(record['inputs']['model']['storage']['kind'],'json_delta')
            self.assertEqual(snap['selected_input_retained_payload_copy_bytes'],0)
            self.assertLess(snap['retained_increment']['logical_bytes'],4096)
        self.assertFalse((store.root/'objects').exists())
        self.assertEqual(self.base.read_bytes(),original_base)
        restored=store.restore(first['snapshot']['sha256'])
        self.assertEqual(Path(restored['inputs']['model']['path']).read_bytes(),first_bytes)
        self.clear_materialization(restored['materialization_root'])
        restored_again=store.restore(first['snapshot']['sha256'])
        self.assertEqual(Path(restored_again['inputs']['model']['path']).read_bytes(),first_bytes)

    def test_json_member_order_add_remove_splice_signed_zero_unicode_and_crlf(self):
        value=json.loads(self.base.read_text())
        value['tail']={'new':'é','z':-0.0}; value['items'][2:3]=[{'b':2,'a':1},{'b':3,'a':4}]
        value={'tail':value['tail'],'items':value['items'],'schema':value['schema']}
        data=b'\xef\xbb\xbf'+self.json_bytes(value).replace(b'\n',b'\r\n')
        source=self.owned('model.json',data); store=self.store()
        frozen=store.freeze({'model':str(source)},{'model':str(self.base)})
        snapshot=json.loads(Path(frozen['snapshot_path']).read_text())
        self.assertEqual(snapshot['inputs']['model']['storage']['kind'],'json_delta')
        restored=store.restore(frozen['snapshot']['sha256'])
        self.assertEqual(Path(restored['inputs']['model']['path']).read_bytes(),data)

    def test_source_binary_and_unusual_json_use_one_content_addressed_object(self):
        store=self.store(); raw=b'\x00\xff\r\nsource bytes'
        source=self.owned('source.bin',raw)
        first=store.freeze({'source':str(source)}); second=store.freeze({'source':str(source)})
        self.assertEqual(len(list((store.root/'objects').iterdir())),1)
        for snap in (first,second):
            restored=store.restore(snap['snapshot']['sha256'])
            self.assertEqual(Path(restored['inputs']['source']['path']).read_bytes(),raw)
        unusual=self.owned('unusual.json',b'{  "x":1, "x":2 }\n')
        frozen=store.freeze({'data':str(unusual)},{'data':str(self.base)})
        record=json.loads(Path(frozen['snapshot_path']).read_text())
        self.assertEqual(record['inputs']['data']['storage']['kind'],'cas')
        self.assertEqual(Path(store.restore(frozen['snapshot']['sha256'])['inputs']['data']['path']).read_bytes(),unusual.read_bytes())

    def test_selected_original_and_identical_copy_remain_hash_references(self):
        owned=self.owned('copy/base.json',self.base.read_bytes()); store=self.store()
        frozen=store.freeze({'selected':str(self.base),'owned':str(owned)})
        record=json.loads(Path(frozen['snapshot_path']).read_text())
        self.assertTrue(all(x['storage']['kind']=='catalog_reference' for x in record['inputs'].values()))
        self.assertFalse((store.root/'objects').exists()); self.assertFalse((store.root/'recipes').exists())
        restored=store.restore(frozen['snapshot']['sha256'])
        self.assertEqual(se.key(restored['inputs']['selected']['path']),se.key(self.base))
        self.assertTrue(Path(restored['inputs']['owned']['path']).is_relative_to(self.scratch))

    def test_base_hash_drift_refused_before_freeze_and_after_freeze(self):
        source=self.owned('model.json',self.variant()); store=self.store()
        frozen=store.freeze({'model':str(source)},{'model':str(self.base)})
        self.base.write_bytes(b'changed')
        with self.assertRaises(se.Refused):store.freeze({'model':str(source)},{'model':str(self.base)})
        with self.assertRaises(se.Refused):store.restore(frozen['snapshot']['sha256'])

    def test_content_address_tampering_and_contract_drift_are_refused(self):
        source=self.owned('source.bin',b'unusual'); store=self.store()
        frozen=store.freeze({'source':str(source)})
        record=json.loads(Path(frozen['snapshot_path']).read_text())
        obj=record['inputs']['source']['storage']['object']
        store.object_path('objects',obj['sha256']).write_bytes(b'tampered')
        with self.assertRaises(se.Refused):store.restore(frozen['snapshot']['sha256'])
        with self.assertRaises(se.Refused):store.freeze({'source':str(source)})
        Path(frozen['snapshot_path']).write_bytes(b'{}')
        with self.assertRaises(se.Refused):store.restore(frozen['snapshot']['sha256'])
        self.put(self.root/'evidence/TASK_CONTRACT.json',{'changed':True})
        with self.assertRaises(se.Refused):store.context.unchanged()

    def test_path_escape_unselected_base_and_restore_outside_scratch_refused(self):
        store=self.store(); source=self.owned('model.json',self.variant())
        for item in ('../outside.json',str(self.fixture/'unselected.json')):
            with self.assertRaises((se.Refused,FileNotFoundError)):store.freeze({'x':item})
        with self.assertRaises(se.Refused):store.freeze({'x':str(source)},{'x':str(source)})
        frozen=store.freeze({'x':str(source)})
        with self.assertRaises(se.Refused):store.restore('../escape')
        with self.assertRaises(se.Refused):store.restore(frozen['snapshot']['sha256'],self.fixture/'outside')
        with self.assertRaises(se.Refused):store.restore(frozen['snapshot']['sha256'],self.scratch)
        restored=store.restore(frozen['snapshot']['sha256'])
        with self.assertRaises(se.Refused):store.restore(frozen['snapshot']['sha256'],Path(restored['materialization_root']))

    def build_spec(self):
        self.owned('consumer/main.cpp',b'#include "local.hpp"\nint main(){}\n')
        self.owned('consumer/local.hpp',b'// private mutable header\n')
        self.owned('inputs/model.json',self.variant())
        spec={'schema':'dsh.lab.selected_execution.build-run.v2','compiler':str(self.compiler),
              'inputs':{'main':'consumer/main.cpp','header':'consumer/local.hpp','data':'inputs/model.json'},
              'json_bases':{'data':str(self.base)},'compile_args':['-std=c++17','{input:main}','-o','{artifact}'],
              'artifact':'bin/checker.exe','run_args':['{input:data}']}
        self.put(self.work/'build.json',spec); return spec

    def test_compile_failure_keeps_first_input_and_logs_after_overwrite_and_scratch_cleanup(self):
        self.build_spec(); first=(self.work/'inputs/model.json').read_bytes()
        def failure(argv,**kw):
            main=Path(argv[-3]); self.assertTrue(main.is_relative_to(self.scratch))
            self.assertTrue((main.parent/'local.hpp').exists())
            kw['stderr'].write(b'original compiler failure\n'); kw['stdout'].write(b'full output\x00')
            Path(argv[-1]).write_bytes(b'partial binary')
            return subprocess.CompletedProcess(argv,2)
        with patch.object(se.subprocess,'run',side_effect=failure) as run:
            result,code=v2.build_run(self.context(),'build.json')
        self.assertEqual(code,2); self.assertEqual(run.call_count,1); self.assertEqual(result['status'],'COMPILE_FAILED_RUN_SKIPPED')
        receipt=json.loads(Path(result['receipt']).read_text()); (self.work/'inputs/model.json').write_bytes(self.variant(99))
        self.clear_materialization(receipt['scratch_root'])
        self.assertEqual(Path(receipt['compile']['stderr']).read_bytes(),b'original compiler failure\n')
        restored=self.store().restore(result['snapshot_sha256'])
        self.assertEqual(Path(restored['inputs']['data']['path']).read_bytes(),first)
        self.assertEqual(receipt['input_versions']['snapshot']['sha256'],result['snapshot_sha256'])

    def test_success_runs_fresh_artifact_and_snapshot_not_changed_source(self):
        self.build_spec(); first=(self.work/'inputs/model.json').read_bytes(); observed=[]
        def execute(argv,**kw):
            if not observed:
                artifact=Path(argv[-1]); self.assertFalse(artifact.exists()); artifact.write_bytes(b'fresh')
                observed.append(artifact); (self.work/'inputs/model.json').write_bytes(self.variant(8))
                return subprocess.CompletedProcess(argv,0)
            self.assertEqual(Path(argv[0]),observed[0]); self.assertEqual(Path(argv[1]).read_bytes(),first)
            kw['stdout'].write(b'UNKNOWN'); return subprocess.CompletedProcess(argv,3)
        with patch.object(se.subprocess,'run',side_effect=execute) as run:
            result,code=v2.build_run(self.context(),'build.json')
        self.assertEqual(run.call_count,2); self.assertEqual(code,3); self.assertEqual(result['run_exit'],3)

    def test_input_modification_by_compiler_and_missing_artifact_prevent_run(self):
        for mutate in (False,True):
            with self.subTest(mutate=mutate):
                self.build_spec()
                def compile(argv,**kw):
                    if mutate:
                        Path(argv[-1]).write_bytes(b'new'); Path(argv[-3]).write_bytes(b'compiler changed source')
                    return subprocess.CompletedProcess(argv,0)
                with patch.object(se.subprocess,'run',side_effect=compile) as run:
                    result,code=v2.build_run(self.context(),'build.json')
                self.assertEqual(run.call_count,1); self.assertEqual(code,1); self.assertIsNone(result['run_exit'])

    def test_real_file_allocation_and_logical_net_savings_against_naive_two_copies(self):
        store=self.store(); naive=self.fixture/'naive'; naive.mkdir(); source=self.work/'model.json'
        variants=[self.variant(1),self.variant(2)]
        for index,data in enumerate(variants):
            source.write_bytes(data); (naive/f'{index}.json').write_bytes(data)
            report=store.freeze({'data':str(source)},{'data':str(self.base)})
            self.assertEqual(report['retained_increment']['logical_bytes'],report['storage_after']['logical_bytes']-report['storage_before']['logical_bytes'])
        measured=vi.inventory(store.root); baseline=vi.inventory(naive)
        self.assertLess(measured['logical_bytes'],baseline['logical_bytes']//20)
        if measured['allocated_bytes'] is not None and baseline['allocated_bytes'] is not None:
            self.assertLess(measured['allocated_bytes'],baseline['allocated_bytes'])
        print('V2_STORAGE_MEASUREMENT '+json.dumps({'baseline':baseline,'version_store':measured,
              'net_logical_bytes_avoided':baseline['logical_bytes']-measured['logical_bytes'],
              'net_allocated_bytes_avoided':baseline['allocated_bytes']-measured['allocated_bytes'] if baseline['allocated_bytes'] is not None and measured['allocated_bytes'] is not None else None}))


if __name__=='__main__':unittest.main(verbosity=2)
