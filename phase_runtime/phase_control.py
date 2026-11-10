"""Project phase entry: exact release, native identity, and current binding.

No scheduling, process control, resource release, or scientific acceptance.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

DEFAULT_POLICY = Path(os.environ.get('PHASE_POLICY', Path(__file__).with_name('policy.json')))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def exact(descriptor):
    path = Path(descriptor['path'])
    if not path.is_absolute():
        raise ValueError('Absolute bound path required')
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            stat = item.lstat()
            if item.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Reparse path refused')
    raw = path.read_bytes()
    if len(raw) != descriptor['bytes'] or hashlib.sha256(raw).hexdigest() != descriptor['sha256']:
        raise ValueError('Bound release identity mismatch')
    return path


def load(policy):
    if policy.get('schema') != 'rea.phase_continuity.policy.v1':
        raise ValueError('Unsupported phase policy')
    release = read(exact(policy['release_descriptor']))
    if release.get('schema') != 'rea.phase_control.release.v1':
        raise ValueError('Unsupported phase release')
    for descriptor in release['sources']:
        exact(descriptor)
    entry = exact(release['guard_entry'])
    spec = importlib.util.spec_from_file_location('rea_bound_phase_guard', entry)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def operate(operation, policy_path, request=None, native_thread=None):
    policy = read(policy_path)
    module = load(policy)
    if not native_thread:
        raise ValueError('CODEX_THREAD_ID required; never infer identity from chat history')
    request = dict(request or {})
    registry = str(Path(policy['registry_root']).resolve())
    if request.get('registry_root') and str(Path(request['registry_root']).resolve()) != registry:
        raise ValueError('Registry differs from project policy')
    request['registry_root'] = registry
    controller = policy['controller_owner']
    if operation == 'context':
        if native_thread == controller:
            return {'status': 'SUPERVISOR_CONTROL_ENTRY',
                    'registry_root': registry,
                    'recovery': 'recover with the selected queue row thread_id',
                    'intake': 'validate-delivery with independently captured native observation',
                    'scientific_acceptance': False}
        request = {'registry_root': registry, 'thread_id': native_thread}
        operation = 'recover'
    elif operation in ('register', 'validate-delivery'):
        if native_thread != controller:
            raise ValueError('Only current supervisor calls controller operations')
        request['controller_owner'] = controller
    elif operation == 'publish-progress':
        if request.get('thread_id') != native_thread:
            raise ValueError('Progress writer differs from native thread')
        request['caller_owner'] = native_thread
    elif operation == 'recover':
        if native_thread != controller:
            if request.get('thread_id', native_thread) != native_thread:
                raise ValueError('Worker cannot select sibling recovery context')
            request['thread_id'] = native_thread
    else:
        raise ValueError('Unsupported phase operation')
    request['operation'] = operation
    return module.run(request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['context', 'recover', 'register', 'publish-progress', 'validate-delivery'])
    parser.add_argument('--policy', default=str(DEFAULT_POLICY))
    parser.add_argument('--request')
    args = parser.parse_args()
    try:
        result = operate(args.operation, args.policy,
                         read(args.request) if args.request else None,
                         os.environ.get('CODEX_THREAD_ID'))
        print(json.dumps(result, ensure_ascii=False))
        successes = {'SUPERVISOR_CONTROL_ENTRY', 'CURRENT_PHASE',
                     'REGISTERED', 'PROGRESS_PUBLISHED',
                     'CURRENT_FORMAL_IDENTITY_VALID'}
        return 0 if result.get('status') in successes else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'REFUSED', 'error': str(exc),
                          'scientific_acceptance': False,
                          'owner_transfer': False, 'resource_release': False}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
