"""Exact-input copy/build/run with retained input versions and scratch reconstruction.

No OS sandbox is claimed. No quotas, time limits or approval policy are added.
The original v1 module is imported read-only and is never modified.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import selected_execution as se
from versioned_inputs_v2 import VersionStore

SPEC_ALIAS = 'ExecutionSpecV2'


def load_spec(context, relative, *, execution=True):
    path = se.child(context.work, relative)
    spec, sha = se.load_json(path)
    schemas = ('dsh.lab.selected_execution.build-run.v1', 'dsh.lab.selected_execution.build-run.v2') if execution else ('dsh.lab.selected_execution.inputs.v2',)
    se.require(isinstance(spec, dict) and spec.get('schema') in schemas, 'V2_SPEC_SCHEMA_REQUIRED')
    fields = {'schema','inputs','json_bases'} | ({'compiler','compile_args','artifact','run_args'} if execution else set())
    se.require(set(spec) <= fields, 'UNKNOWN_V2_SPEC_FIELD')
    se.require(isinstance(spec.get('inputs'), dict) and spec['inputs'], 'EXPLICIT_INPUTS_REQUIRED')
    se.require(SPEC_ALIAS not in spec['inputs'], 'RESERVED_INPUT_ALIAS')
    for alias in spec['inputs']:
        se.require(isinstance(alias, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', alias), 'INVALID_INPUT_ALIAS')
    return path, spec, sha


def freeze_spec(context, relative):
    path, spec, sha = load_spec(context, relative, execution=False)
    frozen = VersionStore(context).freeze({**spec['inputs'], SPEC_ALIAS: str(path)}, spec.get('json_bases'))
    se.require(se.identity(path)['sha256'] == sha, 'SPEC_CHANGED_DURING_FREEZE')
    return {'status': 'INPUTS_FROZEN', **frozen}, 0


def build_run(context, relative):
    path, spec, spec_sha = load_spec(context, relative)
    compiler, compiler_identity = context.selected(spec.get('compiler'))
    se.require(isinstance(spec.get('compile_args'), list) and isinstance(spec.get('run_args', []), list), 'EXPLICIT_ARGV_REQUIRED')
    se.child(context.scratch, spec.get('artifact'))
    context.unchanged()
    retained, scratch, record = context.new_record('build-run-v2')
    record.update(schema='dsh.lab.selected_execution.receipt.v2', compiler=compiler_identity,
                  spec={'path':str(path),'sha256':spec_sha}, compile=None, run=None, artifact=None,
                  input_versions=None, materialized_inputs=None)
    code = 1
    try:
        store = VersionStore(context)
        frozen = store.freeze({**spec['inputs'], SPEC_ALIAS:str(path)}, spec.get('json_bases'))
        record['input_versions'] = frozen
        se.require(se.identity(path)['sha256'] == spec_sha, 'SPEC_CHANGED_DURING_FREEZE')
        materialized = store.restore(frozen['snapshot']['sha256'], se.child(scratch, 'inputs'))
        record['materialized_inputs'] = materialized
        inputs = {name:se.checked_path(row['path']) for name,row in materialized['inputs'].items() if name != SPEC_ALIAS}
        artifact = se.child(scratch, spec['artifact'])
        se.mkdir(artifact.parent)
        se.require(not artifact.exists(), 'FRESH_ARTIFACT_REQUIRED')
        compile_argv = [str(compiler)] + [se.expand_arg(arg, inputs, scratch, artifact) for arg in spec['compile_args']]
        run_argv = [str(artifact)] + [se.expand_arg(arg, inputs, scratch, artifact) for arg in spec.get('run_args', [])]
        record['planned_run_argv'] = run_argv
        context.unchanged()
        se.require(se.same_identity(se.identity(compiler), compiler_identity), 'COMPILER_IDENTITY_CHANGED')
        for row in materialized['inputs'].values():
            se.require(se.same_identity(se.identity(row['path']), row), 'MATERIALIZED_INPUT_CHANGED')
        code = se.invoke(compile_argv, scratch, retained, 'compile', record)
        if code != 0:
            record['status'] = 'COMPILE_FAILED_RUN_SKIPPED'
            if artifact.exists():
                record['artifact'] = {**se.identity(artifact), 'qualification':'Failed compilation output; never executed'}
        else:
            record['artifact'] = se.identity(artifact)
            context.unchanged()
            for row in materialized['inputs'].values():
                se.require(se.same_identity(se.identity(row['path']), row), 'MATERIALIZED_INPUT_CHANGED_AFTER_COMPILE')
            se.require(se.same_identity(se.identity(artifact), record['artifact']), 'ARTIFACT_CHANGED_BEFORE_RUN')
            code = se.invoke(run_argv, scratch, retained, 'run', record)
            record['status'] = 'RUN_FINISHED'
    except (se.Refused, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        record.update(status='EXECUTION_REFUSED_OR_FAILED', failure=se.failure(exc))
        code = 1
    receipt = retained/'receipt.json'
    se.write_json(receipt, record)
    return {'status':record['status'],'receipt':str(receipt),
            'snapshot_sha256':record['input_versions']['snapshot']['sha256'] if record['input_versions'] else None,
            'compile_exit':record['compile']['exit'] if record['compile'] else None,
            'run_exit':record['run']['exit'] if record['run'] else None}, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',required=True)
    sub = parser.add_subparsers(dest='operation',required=True)
    copy = sub.add_parser('copy')
    copy.add_argument('--item',nargs=2,action='append',required=True,metavar=('EXACT_SOURCE','WORK_RELATIVE_DEST'))
    build = sub.add_parser('build-run')
    build.add_argument('--spec',required=True)
    freeze = sub.add_parser('freeze-inputs')
    freeze.add_argument('--spec',required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('--snapshot',required=True,help='64-character snapshot SHA returned by freeze/build-run')
    args=parser.parse_args(argv)
    try:
        context=se.Context(args.job)
        if args.operation=='copy':
            result,code=se.copy_selected(context,args.item)
        elif args.operation=='build-run':
            result,code=build_run(context,args.spec)
        elif args.operation=='freeze-inputs':
            result,code=freeze_spec(context,args.spec)
        else:
            result,code={'status':'INPUTS_RESTORED',**VersionStore(context).restore(args.snapshot)},0
    except (se.Refused,OSError,ValueError,TypeError,KeyError,IndexError) as exc:
        result,code={'status':'REFUSED','failure':se.failure(exc)},1
    print(json.dumps(result,ensure_ascii=False))
    return code


if __name__=='__main__':
    sys.exit(main())
