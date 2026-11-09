"""Per-job immutable input records with exact selected bases and JSON deltas.

Immutability is exclusive publication plus identity checking, not an OS sandbox.
Selected bases must remain available; restore rejects changed or missing bases.
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

import selected_execution as se

STORE = 'input_versions_v2'
HEX = re.compile(r'[0-9a-f]{64}\Z')


def digest(data):
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def canonical(value):
    # Preserve nested JSON member order: sorting a patch value would change bytes.
    return (json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def read_exact(path, expected=None):
    with se.open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        data = stream.read()
        after = os.fstat(stream.fileno())
    se.require((before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino), 'INPUT_CHANGED_DURING_SNAPSHOT')
    got = digest(data)
    if expected is not None:
        se.require(se.same_identity(got, expected), 'RETAINED_OR_INPUT_IDENTITY_CHANGED')
    return data


def allocated_bytes(path):
    """Regular-file allocation, excluding directory/volume metadata; None if unavailable."""
    path = se.checked_path(path)
    if os.name != 'nt':
        info = path.stat()
        return info.st_blocks * 512 if hasattr(info, 'st_blocks') else None
    from ctypes import wintypes
    class FileStandardInfo(ctypes.Structure):
        _fields_ = [('AllocationSize', ctypes.c_longlong), ('EndOfFile', ctypes.c_longlong),
                    ('NumberOfLinks', wintypes.DWORD), ('DeletePending', wintypes.BOOLEAN), ('Directory', wintypes.BOOLEAN)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        return None
    try:
        info = FileStandardInfo()
        if not kernel.GetFileInformationByHandleEx(handle, 1, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return int(info.AllocationSize)
    finally:
        kernel.CloseHandle(handle)


def inventory(root):
    root = se.checked_path(root, missing=True)
    result = {'files': 0, 'logical_bytes': 0, 'allocated_bytes': 0,
              'allocation_scope': 'regular files only; directory/volume metadata excluded'}
    if not root.exists():
        return result
    def visit(directory):
        with os.scandir(se.checked_path(directory)) as entries:
            for entry in entries:
                path = se.checked_path(Path(entry.path))
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                else:
                    se.require(stat.S_ISREG(path.stat().st_mode), 'NONREGULAR_STORE_ENTRY')
                    result['files'] += 1
                    result['logical_bytes'] += path.stat().st_size
                    allocated = allocated_bytes(path)
                    if allocated is None:
                        result['allocated_bytes'] = None
                    elif result['allocated_bytes'] is not None:
                        result['allocated_bytes'] += allocated
    visit(root)
    return result


def publish(path, data):
    """Never overwrite; compare an existing content address before reusing it."""
    path = se.checked_path(path, missing=True)
    se.mkdir(path.parent)
    expected = digest(data)
    if path.exists():
        read_exact(path, expected)
        return False
    staged = se.child(path.parent, '.publish-' + uuid.uuid4().hex)
    try:
        with open(staged, 'xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        se.checked_path(path, missing=True)
        try:
            os.link(staged, path, follow_symlinks=False)
        except FileExistsError:
            read_exact(path, expected)
            return False
        return True
    finally:
        staged.unlink(missing_ok=True)


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def parse_json(data):
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=reject_duplicates,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def serialize(value, profile):
    text = json.dumps(value, ensure_ascii=profile['ensure_ascii'], sort_keys=profile['sort_keys'],
                      indent=profile['indent'], separators=tuple(profile['separators']), allow_nan=False)
    text += profile['ending']
    if profile['crlf']:
        text = text.replace('\n', '\r\n')
    return (b'\xef\xbb\xbf' if profile['bom'] else b'') + text.encode('utf-8')


def serialization_profile(value, data):
    """Exact-byte matching only; unusual formatting falls back to one CAS blob."""
    for indent in (2, 4, None, '\t'):
        for ensure_ascii in (False, True):
            for sort_keys in (False, True):
                for separators in ((',', ': ') if indent is not None else (', ', ': '), (',', ':')):
                    profile = {'indent': indent, 'ensure_ascii': ensure_ascii, 'sort_keys': sort_keys,
                               'separators': list(separators), 'ending': '\n' if data.endswith(b'\n') else '',
                               'crlf': b'\r\n' in data, 'bom': data.startswith(b'\xef\xbb\xbf')}
                    if serialize(value, profile) == data:
                        return profile
    return None


def value_equal(left, right):
    return json.dumps(left, ensure_ascii=False, separators=(',', ':'), allow_nan=False) == json.dumps(right, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def difference(base, target, path=()):
    if type(base) is not type(target):
        return [{'op': 'set', 'path': list(path), 'value': target}]
    if isinstance(base, dict):
        ops = []
        for key in base:
            if key not in target:
                ops.append({'op': 'delete', 'path': list(path) + [key]})
        for key in target:
            if key not in base:
                ops.append({'op': 'set', 'path': list(path) + [key], 'value': target[key]})
            else:
                ops.extend(difference(base[key], target[key], path + (key,)))
        resulting_order = [key for key in base if key in target] + [key for key in target if key not in base]
        if resulting_order != list(target):
            ops.append({'op': 'order', 'path': list(path), 'keys': list(target)})
        return ops
    if isinstance(base, list):
        if len(base) == len(target):
            return [op for index, (left, right) in enumerate(zip(base, target)) for op in difference(left, right, path + (index,))]
        start = 0
        while start < min(len(base), len(target)) and value_equal(base[start], target[start]):
            start += 1
        tail = 0
        while tail < min(len(base), len(target)) - start and value_equal(base[-tail-1], target[-tail-1]):
            tail += 1
        return [{'op': 'splice', 'path': list(path), 'start': start,
                 'delete': len(base)-start-tail, 'values': target[start:len(target)-tail if tail else len(target)]}]
    return [] if value_equal(base, target) else [{'op': 'set', 'path': list(path), 'value': target}]


def apply_difference(base, operations):
    value = copy.deepcopy(base)
    for op in operations:
        path = op['path']
        se.require(isinstance(path, list) and all(type(k) in (str, int) for k in path), 'INVALID_DELTA_PATH')
        target = value
        if op['op'] in ('order', 'splice'):
            for key in path:
                target = target[key]
        if op['op'] == 'set':
            if not path:
                value = copy.deepcopy(op['value'])
            else:
                parent = value
                for key in path[:-1]: parent = parent[key]
                parent[path[-1]] = copy.deepcopy(op['value'])
        elif op['op'] == 'delete':
            parent = value
            for key in path[:-1]: parent = parent[key]
            del parent[path[-1]]
        elif op['op'] == 'order':
            se.require(isinstance(target, dict) and set(op['keys']) == set(target) and len(op['keys']) == len(target), 'INVALID_DELTA_ORDER')
            ordered = {k: target[k] for k in op['keys']}
            target.clear(); target.update(ordered)
        elif op['op'] == 'splice':
            se.require(isinstance(target, list) and type(op['start']) is int and type(op['delete']) is int
                       and 0 <= op['start'] <= len(target) and 0 <= op['delete'] <= len(target)-op['start'], 'INVALID_DELTA_SPLICE')
            target[op['start']:op['start']+op['delete']] = copy.deepcopy(op['values'])
        else:
            raise se.Refused('UNKNOWN_DELTA_OPERATION')
    return value


class VersionStore:
    def __init__(self, context):
        self.context = context
        self.root = se.child(context.work, STORE)

    def object_path(self, kind, sha):
        se.require(kind in ('objects', 'recipes', 'snapshots') and isinstance(sha, str) and HEX.fullmatch(sha), 'INVALID_CONTENT_ADDRESS')
        return se.child(self.root, f'{kind}/{sha}' + ('.blob' if kind == 'objects' else '.json'))

    def save_object(self, kind, data):
        identity = digest(data)
        publish(self.object_path(kind, identity['sha256']), data)
        return identity

    def reference(self, identity):
        path, actual = self.context.selected(identity['path'])
        se.require(se.same_identity(actual, identity), 'BASE_REFERENCE_IDENTITY_CHANGED')
        return path

    def freeze(self, inputs, json_bases=None):
        se.require(isinstance(inputs, dict) and inputs, 'EXPLICIT_INPUT_MAP_REQUIRED')
        json_bases = json_bases or {}
        se.require(isinstance(json_bases, dict) and set(json_bases) <= set(inputs), 'BASE_ALIAS_NOT_DECLARED')
        before = inventory(self.root)
        entries = {}
        for alias, source in inputs.items():
            se.require(isinstance(alias, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', alias), 'INVALID_INPUT_ALIAS')
            path, identity = self.context.input_file(source)
            owned = se.inside(path, self.context.work)
            entry = {'source': identity, 'owned': owned,
                     'work_relative': path.relative_to(self.context.work).as_posix() if owned else None}
            if not owned:
                entry['storage'] = {'kind': 'catalog_reference', 'reference': identity}
            else:
                data = read_exact(path, identity)
                matches = [ref for ref in self.context.references.values() if se.same_identity(identity, ref)]
                if matches:
                    ref = matches[0]
                    self.reference(ref)
                    entry['storage'] = {'kind': 'catalog_reference', 'reference': {k: ref[k] for k in ('path','bytes','sha256')}}
                else:
                    stored = None
                    reason = 'no_explicit_json_base'
                    if alias in json_bases:
                        base_path, base_identity = self.context.selected(json_bases[alias])
                        base_data = read_exact(base_path, base_identity)
                        try:
                            target = parse_json(data); base = parse_json(base_data)
                            profile = serialization_profile(target, data)
                            if profile is None:
                                reason = 'json_serialization_not_exactly_representable'
                            else:
                                recipe = {'schema': 'dsh.lab.json-delta.v2', 'base': base_identity,
                                          'target': digest(data), 'serialization': profile,
                                          'operations': difference(base, target)}
                                reconstructed = serialize(apply_difference(base, recipe['operations']), profile)
                                se.require(reconstructed == data, 'DELTA_RECONSTRUCTION_MISMATCH')
                                encoded = canonical(recipe)
                                if len(encoded) < len(data):
                                    stored = {'kind': 'json_delta', 'recipe': self.save_object('recipes', encoded)}
                                else:
                                    reason = 'delta_record_not_smaller_than_exact_object'
                        except (UnicodeError, ValueError, OverflowError, RecursionError):
                            reason = 'json_value_or_encoding_requires_exact_object'
                    entry['storage'] = stored or {'kind': 'cas', 'object': self.save_object('objects', data), 'reason': reason}
            entries[alias] = entry
        self.context.unchanged()
        snapshot = {'schema': 'dsh.lab.input-snapshot.v2', 'job_id': self.context.job,
                    'session_id': self.context.session, 'workspace_id': se.WORKSPACE,
                    'attempt_nonce': uuid.uuid4().hex,
                    'bindings': [{'path': str(p), 'sha256': h} for p,h in self.context.bindings],
                    'inputs': entries, 'claim_boundary': 'Exact explicit input versions; no transitive OS I/O or scientific acceptance claim.'}
        identity = self.save_object('snapshots', canonical(snapshot))
        after = inventory(self.root)
        delta = {k: after[k]-before[k] if after[k] is not None and before[k] is not None else None
                 for k in ('files','logical_bytes','allocated_bytes')}
        return {'snapshot': identity, 'snapshot_path': str(self.object_path('snapshots', identity['sha256'])),
                'storage_before': before, 'storage_after': after, 'retained_increment': delta,
                'owned_input_logical_bytes': sum(x['source']['bytes'] for x in entries.values() if x['owned']),
                'selected_input_retained_payload_copy_bytes': 0}

    def reconstruct(self, entry):
        storage = entry['storage']; kind = storage['kind']
        if kind == 'catalog_reference':
            data = read_exact(self.reference(storage['reference']), storage['reference'])
        elif kind == 'cas':
            identity = storage['object']; data = read_exact(self.object_path('objects', identity['sha256']), identity)
        elif kind == 'json_delta':
            identity = storage['recipe']
            recipe = json.loads(read_exact(self.object_path('recipes', identity['sha256']), identity))
            se.require(recipe.get('schema') == 'dsh.lab.json-delta.v2', 'INVALID_DELTA_SCHEMA')
            base = parse_json(read_exact(self.reference(recipe['base']), recipe['base']))
            try:
                data = serialize(apply_difference(base, recipe['operations']), recipe['serialization'])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise se.Refused('INVALID_DELTA_RECORD') from exc
            se.require(se.same_identity(digest(data), recipe['target']), 'DELTA_TARGET_IDENTITY_CHANGED')
        else:
            raise se.Refused('UNKNOWN_INPUT_STORAGE_KIND')
        se.require(se.same_identity(digest(data), entry['source']), 'RESTORED_INPUT_IDENTITY_CHANGED')
        return data

    def restore(self, snapshot_sha, destination=None):
        encoded = read_exact(self.object_path('snapshots', snapshot_sha))
        se.require(digest(encoded)['sha256'] == snapshot_sha, 'SNAPSHOT_IDENTITY_CHANGED')
        snapshot = json.loads(encoded)
        se.require(snapshot.get('schema') == 'dsh.lab.input-snapshot.v2' and snapshot.get('job_id') == self.context.job
                   and snapshot.get('session_id') == self.context.session and snapshot.get('workspace_id') == se.WORKSPACE, 'SNAPSHOT_JOB_BINDING_MISMATCH')
        se.require(snapshot.get('bindings') == [{'path': str(p), 'sha256': h} for p,h in self.context.bindings], 'SNAPSHOT_CONTRACT_BINDING_MISMATCH')
        self.context.unchanged()
        destination = destination or se.child(self.context.scratch, 'restore-inputs-v2-' + uuid.uuid4().hex)
        destination = se.checked_path(destination, missing=True)
        se.require(se.inside(destination, self.context.scratch) and se.key(destination) != se.key(self.context.scratch), 'RESTORE_MUST_USE_OWN_SCRATCH')
        se.require(not destination.exists(), 'RESTORE_ROOT_ALREADY_EXISTS')
        planned = []
        for alias, entry in snapshot['inputs'].items():
            se.require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', alias), 'INVALID_INPUT_ALIAS')
            if entry['owned']:
                target = se.child(destination, entry['work_relative'])
                planned.append((alias, entry, target))
            else:
                se.require(entry['storage']['kind'] == 'catalog_reference', 'SELECTED_INPUT_MUST_REMAIN_REFERENCE')
                target = self.reference(entry['storage']['reference'])
                se.require(se.same_identity(se.identity(target), entry['source']), 'SELECTED_INPUT_IDENTITY_CHANGED')
                planned.append((alias, entry, target))
        destination.mkdir(parents=True)
        restored = {}
        for alias, entry, target in planned:
            if entry['owned']:
                data = self.reconstruct(entry)
                publish(target, data)
            restored[alias] = {'path': str(target), **digest(read_exact(target, entry['source']))}
        return {'snapshot_sha256': snapshot_sha, 'materialization_root': str(destination), 'inputs': restored}
