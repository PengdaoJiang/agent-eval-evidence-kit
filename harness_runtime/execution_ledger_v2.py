"""Controller-derived ledger from one terminal freeze, including v2 input versions.

No candidate execution, scratch materialization, directory discovery or API call.
Missing/unselected/drifted v2 recovery dependencies refuse certification.
CLI: --job JOB --freeze <workspace>/receipts/freeze/JOB.json
Output: receipts/measurements/JOB.execution-ledger-v2.json (exclusive/idempotent).
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import execution_ledger as old
import selected_execution as se
from versioned_inputs_v2 import VersionStore, digest


def build_ledger(job, freeze_path, *, lab=se.LAB):
    se.require(isinstance(job, str) and len(job) <= 100 and se.JOB_RE.fullmatch(job), 'INVALID_JOB_NAME')
    lab=se.checked_path(Path(lab).absolute()); root=se.checked_path(lab/'runs'/job)
    exact_freeze=lab/'receipts/freeze'/f'{job}.json'
    se.require(Path(freeze_path).is_absolute() and se.key(freeze_path)==se.key(exact_freeze), 'EXACT_JOB_FREEZE_REQUIRED')
    freeze,freeze_identity=old.read_json(exact_freeze)
    session=freeze.get('session_id')
    se.require(freeze.get('schema')=='rea.harness.constructor-freeze.v1' and freeze.get('job_id')==job
               and freeze.get('workspace_id')==se.WORKSPACE and isinstance(session,str) and se.SESSION_RE.fullmatch(session), 'FREEZE_BINDING_MISMATCH')
    se.require(freeze.get('owner_turn_terminal') is True and freeze.get('all_recorded_tools_ended') is True, 'TERMINAL_FREEZE_REQUIRED')
    catalog_ref=old.descriptor(freeze.get('catalog')); contract_ref=old.descriptor(freeze.get('contract'))
    se.require(se.key(catalog_ref['path'])==se.key(root/'evidence/INPUT_CATALOG.json')
               and se.key(contract_ref['path'])==se.key(root/'evidence/TASK_CONTRACT.json'), 'EXACT_JOB_CONTROL_INPUTS_REQUIRED')
    catalog,catalog_identity=old.read_json(catalog_ref['path'],catalog_ref)
    contract,contract_identity=old.read_json(contract_ref['path'],contract_ref)
    se.require(catalog.get('job_id')==job and isinstance(catalog.get('references'),list), 'CATALOG_BINDING_MISMATCH')
    se.require(contract.get('job_id')==job and contract.get('session_id')==session and contract.get('workspace_id')==se.WORKSPACE, 'CONTRACT_BINDING_MISMATCH')
    se.require(isinstance(freeze.get('candidate_files'),list), 'EXPLICIT_FREEZE_CANDIDATES_REQUIRED')
    candidates={}; selected={}; verified={}
    for raw in freeze['candidate_files']:
        ref=old.descriptor(raw); k=se.key(ref['path'])
        se.require(se.inside(ref['path'],root) and k not in candidates, 'INVALID_OR_DUPLICATE_FROZEN_CANDIDATE')
        candidates[k]=ref
    for raw in catalog['references']:
        ref=old.descriptor(raw); k=se.key(ref['path'])
        se.require(k not in selected, 'DUPLICATE_CATALOG_REFERENCE'); selected[k]=ref

    def verify(ref):
        k=se.key(ref['path'])
        if k not in verified:
            try: actual=se.identity(Path(ref['path']))
            except (OSError,se.Refused) as exc: raise se.Refused('FROZEN_RECOVERY_DEPENDENCY_MISSING:'+ref['path']) from exc
            se.require(old.version_key(actual)==old.version_key(ref), 'FROZEN_RECOVERY_DEPENDENCY_DRIFT:'+ref['path'])
            verified[k]=ref
        else:
            se.require(old.version_key(verified[k])==old.version_key(ref), 'CONFLICTING_FROZEN_IDENTITY')
        return Path(ref['path'])

    def frozen_file(path, expected=None):
        ref=candidates.get(se.key(path))
        se.require(ref is not None, 'RECOVERY_DEPENDENCY_NOT_IN_FREEZE:'+str(path))
        if expected is not None:
            se.require(old.version_key(ref)==old.version_key(expected), 'FROZEN_RECOVERY_DESCRIPTOR_MISMATCH')
        verify(ref); return ref

    def catalog_file(ref):
        ref=old.descriptor(ref); chosen=selected.get(se.key(ref['path']))
        se.require(chosen is not None and old.version_key(chosen)==old.version_key(ref), 'BASE_NOT_EXACT_CATALOG_REFERENCE')
        return verify(chosen)

    class ReadOnlyContext:
        # Only VersionStore.reconstruct/reference need this finite interface.
        work=root/'work'
        def selected(self,source):
            se.require(isinstance(source,str) and Path(source).is_absolute(), 'EXACT_ABSOLUTE_SOURCE_REQUIRED')
            ref=selected.get(se.key(source))
            se.require(ref is not None, 'BASE_NOT_EXACT_CATALOG_REFERENCE')
            return catalog_file(ref),ref

    store=VersionStore(ReadOnlyContext())
    execution_root=root/'work/execution_records'
    receipt_refs={}
    for k,ref in candidates.items():
        p=Path(ref['path'])
        if not se.inside(p,execution_root):continue
        rel=Path(se.key(p)).relative_to(Path(se.key(execution_root)))
        if len(rel.parts)==2 and rel.name=='receipt.json':
            se.require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',rel.parts[0]), 'INVALID_EXECUTION_RECORD_NAME')
            receipt_refs[k]=ref

    versions={}; items=[]; operations=Counter(); compile_exits=Counter(); run_exits=Counter(); unrecorded=Counter()

    def bind_version(ref, item, alias, role, evidence):
        ref=old.descriptor(ref); vk=old.version_key(ref)
        row=versions.setdefault(vk,{'version_key':vk,'bytes':ref['bytes'],'sha256':ref['sha256'],
                                    'recorded_paths':[],'uses':[],'recovery':{'status':'VERIFIED_RECONSTRUCTABLE','evidence':[]}})
        if ref['path'] not in row['recorded_paths']:row['recorded_paths'].append(ref['path'])
        row['uses'].append({'run_id':item['run_id'],'alias':alias,'role':role})
        if evidence not in row['recovery']['evidence']:row['recovery']['evidence'].append(evidence)
        item['input_bindings'].append({'alias':alias,'role':role,'recorded_identity':ref,'version_key':vk})

    def validate_storage(entry):
        ref=old.descriptor(entry.get('source')); storage=entry.get('storage')
        se.require(isinstance(storage,dict) and type(entry.get('owned')) is bool, 'INVALID_SNAPSHOT_INPUT')
        if entry['owned']:
            se.require(se.inside(ref['path'],root/'work'), 'SNAPSHOT_SOURCE_OUTSIDE_WORK')
            relative=entry.get('work_relative')
            se.require(se.key(se.child(root/'work',relative))==se.key(ref['path']), 'SNAPSHOT_WORK_RELATIVE_MISMATCH')
        else:
            se.require(storage.get('kind')=='catalog_reference', 'SELECTED_INPUT_MUST_REMAIN_REFERENCE')
            catalog_file(ref)
        kind=storage.get('kind'); chain=[]
        if kind=='catalog_reference':
            reference=old.descriptor(storage.get('reference')); catalog_file(reference)
            se.require(old.version_key(reference)==old.version_key(ref), 'REFERENCE_TARGET_MISMATCH')
            chain.append({'kind':'catalog_reference','identity':reference})
        elif kind in ('cas','json_delta'):
            content=storage.get('object' if kind=='cas' else 'recipe')
            se.require(isinstance(content,dict) and type(content.get('bytes')) is int and content['bytes']>=0
                       and isinstance(content.get('sha256'),str) and re.fullmatch(r'[0-9a-f]{64}',content['sha256']), 'INVALID_CONTENT_IDENTITY')
            path=store.object_path('objects' if kind=='cas' else 'recipes',content['sha256'])
            frozen=frozen_file(path,content); chain.append({'kind':kind,'identity':frozen})
            if kind=='json_delta':
                recipe,_=old.read_json(path,frozen)
                se.require(recipe.get('schema')=='dsh.lab.json-delta.v2' and old.version_key(recipe.get('target',{}))==old.version_key(ref), 'DELTA_TARGET_MISMATCH')
                base=old.descriptor(recipe.get('base')); catalog_file(base)
                chain.append({'kind':'catalog_base','identity':base})
        else:raise se.Refused('UNKNOWN_INPUT_STORAGE_KIND')
        try:data=store.reconstruct(entry)
        except (OSError,se.Refused,ValueError,TypeError,KeyError,IndexError) as exc:
            raise se.Refused('INPUT_VERSION_NOT_RECONSTRUCTABLE:'+ref['sha256']) from exc
        se.require(old.version_key(digest(data))==old.version_key(ref), 'RECONSTRUCTED_BYTES_MISMATCH')
        return ref,chain

    for k in sorted(receipt_refs):
        ref=receipt_refs[k]; verify(ref); row,actual=old.read_json(ref['path'],ref); path=Path(ref['path']); run_id=path.parent.name
        se.require(row.get('job_id')==job and row.get('workspace_id')==se.WORKSPACE and row.get('session_id')==session, 'EXECUTION_RECEIPT_BINDING_MISMATCH')
        se.require(row.get('run_id')==run_id and se.key(row.get('retained_root',''))==se.key(path.parent)
                   and se.key(row.get('scratch_root',''))==se.key(root/'scratch'/run_id), 'EXECUTION_ROOT_BINDING_MISMATCH')
        operation=row.get('operation'); schema=row.get('schema')
        se.require((schema=='dsh.lab.selected_execution.receipt.v1' and operation=='copy') or
                   (schema=='dsh.lab.selected_execution.receipt.v2' and operation=='build-run-v2'), 'UNSUPPORTED_EXECUTION_RECEIPT_SCHEMA')
        operations[operation]+=1
        item={'run_id':run_id,'operation':operation,'source_receipt':actual,'recorded_status':row.get('status'),'input_bindings':[]}
        if operation=='copy':
            se.require(isinstance(row.get('files'),list), 'COPY_FILES_REQUIRED')
            for index,copied in enumerate(row['files']):
                source=old.descriptor(copied.get('source')); catalog_file(source)
                se.require(isinstance(copied.get('destination'),str) and se.inside(copied['destination'],root/'work'), 'COPY_DESTINATION_OUTSIDE_WORK')
                bind_version(source,item,str(index),'copy_source',{'kind':'catalog_reference','identity':source})
        else:
            iv=row.get('input_versions')
            if iv is None:
                se.require(row.get('status')=='EXECUTION_REFUSED_OR_FAILED' and row.get('compile') is None and row.get('run') is None, 'EXECUTED_INPUT_SNAPSHOT_MISSING')
                item.update(snapshot=None,compile_exit_raw=None,run_exit_raw=None,
                            input_version_status='NO_EXECUTION_SNAPSHOT_FAILED_BEFORE_SUBPROCESS',recorded_failure=row.get('failure'))
                unrecorded['compile']+=1; unrecorded['run']+=1; items.append(item); continue
            se.require(isinstance(iv,dict) and isinstance(iv.get('snapshot'),dict), 'V2_SNAPSHOT_REQUIRED')
            snapshot_identity=iv['snapshot']; snapshot_path=store.object_path('snapshots',snapshot_identity.get('sha256'))
            se.require(isinstance(iv.get('snapshot_path'),str) and se.key(iv['snapshot_path'])==se.key(snapshot_path), 'SNAPSHOT_PATH_BINDING_MISMATCH')
            snapshot_ref=frozen_file(snapshot_path,snapshot_identity); snapshot,_=old.read_json(snapshot_path,snapshot_ref)
            se.require(snapshot.get('schema')=='dsh.lab.input-snapshot.v2' and snapshot.get('job_id')==job
                       and snapshot.get('session_id')==session and snapshot.get('workspace_id')==se.WORKSPACE, 'SNAPSHOT_JOB_BINDING_MISMATCH')
            se.require(isinstance(snapshot.get('bindings'),list) and snapshot['bindings']==row.get('binding_files'), 'SNAPSHOT_RECEIPT_BINDING_MISMATCH')
            expected_bindings={se.key(catalog_ref['path']):catalog_ref['sha256'],se.key(contract_ref['path']):contract_ref['sha256']}
            seen={}
            for binding in snapshot['bindings']:
                se.require(isinstance(binding,dict) and isinstance(binding.get('path'),str) and isinstance(binding.get('sha256'),str)
                           and re.fullmatch(r'[0-9a-f]{64}',binding['sha256']), 'INVALID_SNAPSHOT_CONTROL_BINDING')
                bkey=se.key(binding['path']); se.require(bkey not in seen, 'DUPLICATE_SNAPSHOT_CONTROL_BINDING'); seen[bkey]=binding['sha256']
            se.require(all(seen.get(k)==v for k,v in expected_bindings.items()) and
                       set(seen)==set(expected_bindings)|{se.key(root/'.control/prepare.json')}, 'SNAPSHOT_CATALOG_CONTRACT_BINDING_MISMATCH')
            se.require(isinstance(snapshot.get('inputs'),dict) and snapshot['inputs'], 'SNAPSHOT_INPUTS_REQUIRED')
            materialized=row.get('materialized_inputs')
            se.require(isinstance(materialized,dict) and materialized.get('snapshot_sha256')==snapshot_identity['sha256']
                       and isinstance(materialized.get('inputs'),dict) and set(materialized['inputs'])==set(snapshot['inputs']), 'MATERIALIZED_SNAPSHOT_BINDING_MISMATCH')
            item['snapshot']=snapshot_ref
            for alias,entry in sorted(snapshot['inputs'].items()):
                source,chain=validate_storage(entry); consumed=old.descriptor(materialized['inputs'][alias])
                se.require(old.version_key(consumed)==old.version_key(source), 'MATERIALIZED_INPUT_VERSION_MISMATCH')
                expected_path=se.child(root/'scratch'/run_id/'inputs',entry['work_relative']) if entry['owned'] else Path(source['path'])
                se.require(se.key(consumed['path'])==se.key(expected_path), 'MATERIALIZED_INPUT_PATH_MISMATCH')
                bind_version(source,item,alias,'versioned_input',{'snapshot':snapshot_ref,'dependencies':chain,'verified_in_memory':True})
            spec_entry=snapshot['inputs'].get('ExecutionSpecV2'); spec=row.get('spec')
            se.require(isinstance(spec_entry,dict) and isinstance(spec,dict) and
                       se.key(spec_entry['source']['path'])==se.key(spec.get('path','')) and spec_entry['source']['sha256']==spec.get('sha256'), 'EXACT_EXECUTION_SPEC_NOT_RETAINED')
            compiler=old.descriptor(row.get('compiler')); catalog_file(compiler)
            bind_version(compiler,item,'compiler','compiler',{'kind':'catalog_reference','identity':compiler})
            for phase,histogram in (('compile',compile_exits),('run',run_exits)):
                phase_row=row.get(phase); se.require(phase_row is None or isinstance(phase_row,dict), 'INVALID_EXECUTION_PHASE')
                code=phase_row.get('exit') if phase_row is not None else None
                se.require(code is None or type(code) is int, 'INTEGER_RAW_EXIT_REQUIRED'); item[phase+'_exit_raw']=code
                if code is None:unrecorded[phase]+=1
                else:histogram[str(code)]+=1
                if phase_row is not None:
                    for stream in ('stdout','stderr'):
                        log=phase_row.get(stream); se.require(isinstance(log,str) and se.key(log)==se.key(path.parent/f'{phase}.{stream}.bin'), 'LOG_ROOT_BINDING_MISMATCH')
                        frozen_file(log)
        items.append(item)

    se.require(se.identity(exact_freeze)['sha256']==freeze_identity['sha256'], 'FREEZE_CHANGED_DURING_DERIVATION')
    for ref in [catalog_identity,contract_identity,*verified.values()]:
        se.require(old.version_key(se.identity(ref['path']))==old.version_key(ref), 'EVIDENCE_CHANGED_DURING_DERIVATION')
    ordered=[versions[k] for k in sorted(versions)]
    return {'schema':'dsh.lab.execution_ledger.v2','job_id':job,'session_id':session,'workspace_id':se.WORKSPACE,
            'status':'FROZEN_EXECUTION_LEDGER_DERIVED',
            'provenance':{'role':'ARCHITECTURE_DERIVED_BOOKKEEPING','constructor_original_delivery':False,
                          'does_not_repair_frozen_worker_ledger':True,'generator':se.identity(Path(__file__).absolute()),
                          'counting_helpers':se.identity(Path(old.__file__).absolute()),'freeze':freeze_identity,'catalog':catalog_identity,'contract':contract_identity},
            'selection':{'receipt_rule':'Only explicit freeze candidate work/execution_records/<one-id>/receipt.json; v1 copy and v2 build-run',
                         'recovery_rule':'Every snapshot/recipe/CAS is exactly frozen and hash verified; every external base is exactly catalog selected; reconstruction is in memory only',
                         'directory_scans':0,'scratch_writes':0,'api_calls':0,'model_calls':0},
            'counts':{'receipts':len(items),'copy':operations['copy'],'build_run':operations['build-run-v2'],
                      'compile_exit_raw_histogram':dict(sorted(compile_exits.items())),'run_exit_raw_histogram':dict(sorted(run_exits.items())),
                      'compile_exit_not_recorded_or_not_executed':unrecorded['compile'],'run_exit_not_recorded_or_not_executed':unrecorded['run'],
                      'unique_input_versions':len(ordered),'reconstructable_input_versions':len(ordered),'unavailable_input_versions':0},
            'semantic_boundary':'Raw exit histograms only: 3/4 are not automatically scientific PASS/FAIL. Missing or unselected v2 recovery chains refuse derivation rather than silently reporting recoverable. No OS sandbox or whole-machine absence claim.',
            'executions':items,'input_versions':ordered}


def publish(job,freeze_path,*,lab=se.LAB):
    report=build_ledger(job,freeze_path,lab=lab)
    destination=Path(lab).absolute()/'receipts/measurements'/f'{job}.execution-ledger-v2.json'
    se.checked_path(destination,missing=True)
    if destination.exists():
        existing,_=old.read_json(destination); se.require(existing==report,'EXISTING_LEDGER_DIFFERS_HISTORY_PRESERVED')
        status='IDENTICAL_EXISTING_REPORT'
    else:
        se.mkdir(destination.parent); se.write_json(destination,report); status='DERIVED_REPORT_CREATED'
    return {'status':status,'report':se.identity(destination),'counts':report['counts']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--job',required=True); parser.add_argument('--freeze',required=True)
    args=parser.parse_args(argv)
    try:
        result=publish(args.job,args.freeze); code=0
    except (se.Refused,OSError,ValueError,TypeError,KeyError,IndexError) as exc:
        result={'status':'REFUSED','code':str(exc) if isinstance(exc,se.Refused) else 'MISSING_OR_INVALID_RECOVERY_EVIDENCE','exception_type':type(exc).__name__}; code=1
    print(json.dumps(result,ensure_ascii=False)); return code


if __name__=='__main__':raise SystemExit(main())
