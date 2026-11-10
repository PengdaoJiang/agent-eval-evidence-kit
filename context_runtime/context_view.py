"""Read-only, exact-input context projections. No dispatch or acceptance authority."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys

PROJECT = Path(os.environ.get('PHASE_PROJECT_ROOT', Path(__file__).resolve().parent / 'example-project'))
ARCHITECT_THREAD = os.environ.get('PHASE_ARCHITECT_THREAD', '')
SUPERVISOR_THREAD = os.environ.get('PHASE_SUPERVISOR_THREAD', '')
FIELDS = {'目标', '选中输入', '独立可写根', '交付', '验收'}
SCHEMAS = {
    'route': {'rea.architecture.current_execution_view.v1'},
    'queue': {'rea.supervisor.ready_queue.v5.2'},
    'continuity': {'rea.supervisor.control_continuity.v5.2'},
    'definition': {'rea.architecture.action_continuation_definition.v1',
                   'rea.architecture.delivery_recovery_definition.v1'},
    'method': {'rea.architecture.source_semantics_changed_method_contract.v1'},
    'inputs': {'rea.architecture.action_exact_inputs.v1'},
    'storage': {'rea.storage.dependencies.v1', 'rea.storage.contract.v1'},
    'read_scope': {'rea.architecture.action_continuation_read_scope.v1'},
    'process_scope': {'rea.architecture.process_scope.v1'},
    'terminal_index': {'rea.runtime.pending_native.v1'},
    'consumed_index': {'rea.consumed_native_events.v1'},
}
DOCUMENT_KEYS = {'definition': 'definition', 'method': 'method_contract',
                 'inputs': 'selected_index', 'storage': 'storage_binding',
                 'read_scope': 'read_scope', 'process_scope': 'process_scope'}
ACTIVE_STATES = {'RUNNING', 'STARTING', 'IN_PROGRESS', 'FORMAL_GUI_CONSTRUCTION',
                 'FORMAL_GUI_VERIFICATION', 'GUI_RUNNING', 'STARTUP_RUNNING',
                 'WAITING_GUI_REGISTRATION', 'FORMAL_RUNNING'}
READY_STATES = {'READY', 'READY_TO_DISPATCH', 'ELIGIBLE_FOR_DISPATCH'}
TERMINAL_STATES = {'ACCEPTED_SCOPED_ARCHIVED', 'NOT_ACCEPTED_PROCESS_NONCONFORMING',
    'PRECISE_GAP_INDEPENDENTLY_CONFIRMED', 'PRECISE_GAPS_INDEPENDENTLY_CONFIRMED',
    'FROZEN_CANDIDATE_SCOPED_ACCEPTED_BY_FRESH_RECHECK_RETAINED',
    'TERMINAL_PROCESS_SCOPE_GAP_NOT_ACCEPTED', 'NOT_ACCEPTED_INDEPENDENT_FAILURE',
    'RETIRED_BY_USER_EXTERNAL_HOLD_REPORT_REGISTERED', 'RETIRED_UNISSUED_RESERVATION',
    'TERMINAL_NONACCEPTED_GUI_ARCHIVED', 'COMPLETED', 'FAILED', 'CANCELLED',
    'ARCHIVED', 'SCOPED_ACCEPTED', 'ACCEPTED'}
DEFERRED_STATES = {'WAITING_ACCEPTED_PUBLIC_BINDING', 'HOLD', 'DEFERRED',
    'WAITING_DEPENDENCIES', 'WAITING_EXACT_EXTERNAL_INPUT', 'BLOCKED',
    'WAITING_NATIVE_PLATFORM_ACCESS', 'WAITING_USER_PLATFORM_ACCESS'}
BOUNDARY = ('Read-only projection of selected records. It changes no input scope, owner, '
            'token, readiness, scientific acceptance, phase or sealed artifact. '
            'Byte measurements are output bytes, not context tokens.')


def pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError('DUPLICATE_JSON_KEY:' + key)
        result[key] = value
    return result


def parse(raw):
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError('NONFINITE_JSON')))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def regular(path):
    p = Path(path)
    if not p.is_absolute():
        raise ValueError('ABSOLUTE_PATH_REQUIRED')
    for item in (p, *p.parents):
        if item.exists() or item.is_symlink():
            stat = item.lstat()
            if item.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                raise ValueError('REPARSE_PATH_REFUSED')
    if not p.is_file() or p.stat().st_nlink != 1:
        raise ValueError('REGULAR_SINGLE_LINK_FILE_REQUIRED')
    return p


def read(path):
    p = regular(path)
    raw = p.read_bytes()
    return parse(raw.decode('utf-8-sig')), {'path': str(p), 'bytes': len(raw),
                                          'sha256': hashlib.sha256(raw).hexdigest()}


def descriptor_structure(ref):
    if (not isinstance(ref, dict) or not {'path', 'bytes', 'sha256'} <= set(ref)
            or not isinstance(ref['path'], str) or type(ref['bytes']) is not int
            or ref['bytes'] < 0 or not isinstance(ref['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])):
        raise ValueError('EXACT_DESCRIPTOR_REQUIRED')
    if not Path(ref['path']).is_absolute():
        raise ValueError('ABSOLUTE_PATH_REQUIRED')


def exact(ref):
    descriptor_structure(ref)
    value, observed = read(ref['path'])
    if any(observed[key] != ref[key] for key in ('bytes', 'sha256')):
        raise ValueError('EXACT_IDENTITY_MISMATCH:' + ref['path'])
    return value, observed


def schema(value, kind):
    if not isinstance(value, dict) or value.get('schema') not in SCHEMAS[kind]:
        raise ValueError('UNSUPPORTED_SCHEMA:' + kind)
    return value


def pointer(value, selector):
    if selector == '':
        return value
    if not selector.startswith('/'):
        raise ValueError('JSON_POINTER_REQUIRED')
    current = value
    for raw in selector[1:].split('/'):
        if re.search('~(?![01])', raw):
            raise ValueError('INVALID_JSON_POINTER_ESCAPE')
        key = raw.replace('~1', '/').replace('~0', '~')
        if isinstance(current, dict):
            if key not in current:
                raise ValueError('POINTER_NOT_FOUND:' + selector)
            current = current[key]
        elif isinstance(current, list) and re.fullmatch('0|[1-9][0-9]*', key):
            index = int(key)
            if index >= len(current):
                raise ValueError('POINTER_NOT_FOUND:' + selector)
            current = current[index]
        else:
            raise ValueError('POINTER_NOT_FOUND:' + selector)
    return current


def order_context(argument, expected_sha256=None, expected_bytes=None):
    if expected_sha256 is not None:
        p = regular(argument)
        # The expected hash is supplied by the unchanged phase binding. Size is optional.
        ref = {'path': str(p), 'bytes': p.stat().st_size if expected_bytes is None else expected_bytes,
               'sha256': expected_sha256}
    else:
        if expected_bytes is not None:
            raise ValueError('SHA256_REQUIRED_WITH_BYTES')
        ref = parse(argument) if argument.lstrip().startswith('{') else read(argument)[0]
    order, descriptor = exact(ref)
    if not isinstance(order, dict) or set(order) != FIELDS:
        raise ValueError('FIVE_FIELD_FORMAL_ORDER_REQUIRED')
    if not isinstance(order['选中输入'], dict) or not isinstance(order['独立可写根'], dict):
        raise ValueError('INVALID_ORDER_FIELDS')
    if not isinstance(order['目标'], str) or not isinstance(order['交付'], list) or not isinstance(order['验收'], list):
        raise ValueError('INVALID_ORDER_FIELDS')
    return order, descriptor


def authorize_order(order, caller):
    """Protocol-level identity check, not an OS security boundary."""
    if not caller:
        raise ValueError('NATIVE_THREAD_ID_REQUIRED')
    selected, roots = order['选中输入'], order['独立可写根']
    candidates = []
    for value in (selected.get('audit_writer'), roots.get('storage_operations')):
        if isinstance(value, dict) and value.get('owner'):
            candidates.append(value['owner'])
    if not candidates or any(not isinstance(x, str) or x != caller for x in candidates):
        raise ValueError('ORDER_BELONGS_TO_OTHER_OR_UNBOUND_OWNER')


def document(order, name):
    selected = order['选中输入']
    key = DOCUMENT_KEYS[name]
    if name == 'storage' and key not in selected:
        key = 'storage_contract'
    if key not in selected:
        raise ValueError('DOCUMENT_NOT_SELECTED:' + name)
    value, descriptor = exact(selected[key])
    schema(value, name)
    return value, descriptor


def nav(order):
    result = {}
    for name, key in DOCUMENT_KEYS.items():
        if key not in order['选中输入'] and not (name == 'storage' and 'storage_contract' in order['选中输入']):
            continue
        value, ref = document(order, name)
        result[name] = {'identity': ref, 'schema': value['schema'],
                        'top_level_pointers': ['/' + k.replace('~', '~0').replace('/', '~1') for k in value]}
    return result


def worker(order, descriptor):
    documents = nav(order)
    counts = {}
    for name in ('inputs', 'storage'):
        if name not in documents:
            continue
        value, _ = document(order, name)
        counts[name] = {key: len(value[key]) for key in ('references', 'dependencies', 'files', 'read_closure')
                        if isinstance(value.get(key), list)}
        if name == 'inputs':
            counts[name]['roles'] = dict(Counter(x.get('role', 'UNSPECIFIED') for x in value.get('references', [])))
    shell_quote = lambda value: "'" + value.replace("'", "''") + "'"
    setup = ["$reaPython = " + shell_quote(sys.executable),
             "$reaReader = " + shell_quote(str(Path(__file__).resolve())),
             '$reaOrder = ' + shell_quote(descriptor['path']),
             '$reaOrderHash = ' + shell_quote(descriptor['sha256'])]
    definition_pointers = documents.get('definition', {}).get('top_level_pointers', [])
    examples = ['/goal', '/assumptions', '/state_relation', '/acceptance',
                '/independent_verification', '/public_export_contract']
    reading_commands = ['& $reaPython -B $reaReader section --order $reaOrder --sha256 $reaOrderHash --document definition --pointer ' + p
                        for p in examples if p in definition_pointers]
    reading_commands += ['& $reaPython -B $reaReader section --order $reaOrder --sha256 $reaOrderHash --document order --pointer /交付',
                         '& $reaPython -B $reaReader section --order $reaOrder --sha256 $reaOrderHash --document order --pointer /验收']
    return {'status': 'SELECTED_ORDER_NAVIGATION', 'order': descriptor, 'goal': order['目标'],
            'owned_roots': {k: v for k, v in order['独立可写根'].items() if k != 'supervisor_only'},
            'documents': documents, 'machine_index_counts': counts,
            'science_reading_required': {
                'rule': 'This navigation is not a scientific contract substitute. Read complete scientific sections: goal, assumptions, state_relation, acceptance, independent_verification, exports and ALL additional scientific fields. Review every listed key, including unknown ones. Read relevant complete method chapters as needed; relevance does not remove any applicable proof requirement. Review complete order deliverables and acceptance. Read/process scope remain effective. Core is an optional complete-document fallback, not a required duplicate dump.',
                'powershell_setup': setup,
                'section_examples_not_exhaustive': reading_commands,
                'complete_key_review_required': definition_pointers,
                'method_chapter_navigation': documents.get('method', {}).get('top_level_pointers', []),
                'optional_full_document': 'core --order EXACT --document definition|method',
                'section': 'section returns the complete selected JSON pointer; no automatic size truncation or recursive reference reads.'},
            'boundary': BOUNDARY}


def inputs(order, descriptor, role=None, match=None):
    value, ref = document(order, 'inputs')
    rows = value.get('references')
    if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
        raise ValueError('EXPLICIT_INPUT_REFERENCES_REQUIRED')
    for row in rows:
        descriptor_structure(row)
    selected = [(index, x) for index, x in enumerate(rows) if (role is None or x.get('role') == role)
                and (match is None or match.casefold() in str(x.get('path', '')).casefold())]
    return {'status': 'SELECTED_INPUT_INDEX_QUERY', 'order': descriptor, 'index': ref,
            'query': {'exact_role': role, 'path_substring': match}, 'total_count': len(rows),
            'matched_count': len(selected),
            'references': [{**{k: x[k] for k in ('role', 'path', 'bytes', 'sha256') if k in x},
                            'exact_index_pointer': '/references/' + str(index),
                            'omitted_field_names': [k for k in x if k not in ('role', 'path', 'bytes', 'sha256')]}
                           for index, x in selected],
            'required_before_use': 'Read each complete selected record through section --document inputs --pointer /references/INDEX before scientific use. Usage, compatibility, qualifications and every unknown omitted field remain binding; this listing does not remove them.',
            'boundary': 'Only existing initial index records; no directory enumeration, content reads, manifest expansion or supplemental access. ' + BOUNDARY}


def row_projection(row):
    return {k: row[k] for k in ('id', 'attempt', 'state', 'phase', 'owner', 'work_order',
                               'readiness', 'next_action', 'formal_turn_id', 'native_cursor') if k in row}


def supervisor(root):
    control_root = root / 'governance/supervisor'
    q, qi = read(control_root / 'ready_queue.json')
    c, ci = read(control_root / 'control_continuity.json')
    schema(q, 'queue'); schema(c, 'continuity')
    if not isinstance(q.get('rows'), list):
        raise ValueError('QUEUE_ROWS_REQUIRED')
    rows = q['rows']
    if not all(isinstance(r, dict) and isinstance(r.get('state'), str) for r in rows):
        raise ValueError('QUEUE_ROW_STATE_REQUIRED')
    active = [row_projection(r) for r in rows if r['state'] in ACTIVE_STATES]
    ready = [row_projection(r) for r in rows if r['state'] in READY_STATES]
    other = [r for r in rows if r['state'] not in ACTIVE_STATES | READY_STATES]
    def archive_reference(row):
        return any((isinstance(row.get(key), str) and bool(row[key]))
                   or (isinstance(row.get(key), dict) and bool(row[key].get('path')))
                   for key in ('gui_archive_receipt', 'archive_receipt'))
    history = [r for r in other if r['state'] in TERMINAL_STATES and archive_reference(r)]
    terminal_pending = [row_projection(r) for r in other
                        if r['state'] in TERMINAL_STATES and not archive_reference(r)]
    deferred = [r for r in other if r['state'] in DEFERRED_STATES]
    unclassified = [row_projection(r) for r in other
                    if r['state'] not in TERMINAL_STATES | DEFERRED_STATES]
    waits = [{'threadId': r['owner']['thread_id'], 'hostId': r['owner'].get('host_id', 'local'),
              'afterCursor': c.get('native_event_cursors', {}).get(r['owner']['thread_id'])}
             for r in active if isinstance(r.get('owner'), dict) and r['owner'].get('thread_id')]
    pending_root = control_root / '.runtime-helper/transactions'
    pending = [str(p.parent) for p in pending_root.glob('*/payload.json') if not (p.parent / 'COMMITTED.json').exists()]
    index = control_root / '.runtime-helper/terminal_index.json'
    events = schema(read(index)[0], 'terminal_index').get('events', {}) if index.exists() else {}
    if not isinstance(events, dict) or not all(isinstance(e, dict) for e in events.values()):
        raise ValueError('INVALID_TERMINAL_INDEX_EVENTS')
    consumed_path = c.get('consumed_event_index')
    consumed = []
    if consumed_path:
        consumed_file = regular(consumed_path)
        if not consumed_file.is_relative_to(control_root):
            raise ValueError('CONSUMED_EVENT_INDEX_OUTSIDE_CONTROL_ROOT')
        consumed = schema(read(consumed_file)[0], 'consumed_index').get('events', [])
    if not isinstance(consumed, list) or not all(isinstance(e, dict) for e in consumed):
        raise ValueError('INVALID_CONSUMED_INDEX_EVENTS')
    consumed_keys = {str(x.get('thread_id')) + '|' + str(x.get('turn_id')) for x in consumed}
    unseen = [{'event_key': key, **{k: e[k] for k in ('thread_id', 'turn_id', 'status', 'kind', 'path',
               'receipt', 'observation', 'evidence', 'final_text_evidence', 'latest_observation') if k in e}}
              for key, e in events.items() if key not in consumed_keys]
    latest = {key: q.get(key, c.get(key)) for key in (
        'latest_research_disposition', 'latest_research_unknown', 'latest_open_issue',
        'latest_research_progress', 'current_research_plan')
        if q.get(key, c.get(key)) is not None}
    summary = q.get('current_research_summary', {})
    return {'status': 'RECOVERY_REQUIRED' if pending else 'CURRENT_CONTROL_PROJECTION',
            'sources': {'queue': qi, 'continuity': ci},
            'recorded_at_utc': {'queue': q.get('updated_at_utc'), 'continuity': c.get('updated_at_utc')},
            'supervisor_turn_id': c.get('supervisor_turn_id'),
            'execution_switch': c.get('execution_switch'), 'recorded_status': c.get('status'),
            'active': active, 'recorded_ready': ready, 'wait_targets': waits,
            'historical_rows': {'count': len(history), 'states': dict(Counter(r['state'] for r in history))},
            'terminal_needing_archive_review': terminal_pending,
            'other_nonready_rows': {'count': len(deferred), 'states': dict(Counter(r['state'] for r in deferred))},
            'unclassified_frontier': unclassified,
            'recorded_dispatch': q.get('current_dispatch'),
            'pending_transactions': pending, 'unconsumed_terminal_events': unseen,
            'gates': {k: {a: v[a] for a in ('status', 'decision') if a in v} for k, v in q.get('gates', {}).items()},
            'latest': latest,
            'recorded_route_issue_reference': q.get('current_research_issue', c.get('current_research_issue')),
            'issue_selection_rule': 'Latest disposition/UNKNOWN and latest_open_issue remain separate from the recorded route-issue reference; the latter does not override the latest terminal result.',
            'next_action': q.get('next_action', c.get('next_action')),
            'next_trigger': c.get('next_trigger'), 'research_complete': q.get('research_complete'),
            'scientific_frontier': {k: summary[k] for k in ('three_universals', 'full_S2_accepted', 'actual_S3',
                'source_semantics_order', 'source_semantics_order_verification', 'frontier') if k in summary},
            'readiness_rule': 'Existing state labels only; no dependency evaluation or inferred eligibility. Unknown states remain in unclassified_frontier even if an archive flag exists. Known deferred rows are counted by state. Consult exact queue row before dispatch.',
            'observation_scope': 'Sequential control-file observations with exact observed identities, not an atomic snapshot or native platform poll.',
            'boundary': BOUNDARY}


def architect(root):
    route, ri = read(root / 'governance/architecture/CURRENT_EXECUTION.json')
    schema(route, 'route')
    live = supervisor(root)
    historical = route.get('runtime_snapshot', {})
    recorded = live.get('recorded_dispatch') or {}
    stale = bool(route.get('observed_at_utc', '') < (live['recorded_at_utc']['queue'] or ''))
    if 'active_gui' in historical and 'active_gui' in recorded:
        stale = stale or historical['active_gui'] != recorded['active_gui']
    return {'status': 'ARCHITECT_ROUTE_AND_CURRENT_CONTROL_PROJECTION', 'route_source': ri,
            'route_observed_at_utc': route.get('observed_at_utc'),
            'route_runtime_snapshot_stale': stale,
            'route_runtime_snapshot_authoritative': False,
            'route_current_blocker_is_historical_reading_only': stale,
            'route': {k: route[k] for k in ('active_contracts', 'current_blocker', 'delivery_groups',
                                           'grouping_rule', 'terminal_acceptance') if k in route},
            'current_control': live, 'boundary': BOUNDARY}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for operation in ('architect', 'supervisor'):
        sub.add_parser(operation)
    for operation in ('worker', 'inputs', 'section', 'core'):
        cmd = sub.add_parser(operation)
        cmd.add_argument('--order', required=True,
                         help='Formal order path with --sha256, exact descriptor JSON, or descriptor JSON file.')
        cmd.add_argument('--sha256', help='Expected formal order SHA256 from the unchanged phase binding.')
        cmd.add_argument('--bytes', dest='expected_bytes', type=int)
        if operation == 'inputs':
            cmd.add_argument('--role'); cmd.add_argument('--match')
        if operation in ('section', 'core'):
            choices = ['definition', 'method'] if operation == 'core' else ['order', *DOCUMENT_KEYS]
            cmd.add_argument('--document', required=True, choices=choices)
            if operation == 'section':
                cmd.add_argument('--pointer', required=True)
    args = parser.parse_args(argv)
    try:
        caller = os.environ.get('CODEX_THREAD_ID')
        if args.command in ('architect', 'supervisor'):
            expected_caller = ARCHITECT_THREAD if args.command == 'architect' else SUPERVISOR_THREAD
            if caller != expected_caller:
                raise ValueError('CONTROL_ROLE_CALLER_REQUIRED:' + args.command)
            result = globals()[args.command](PROJECT)
        else:
            order, ref = order_context(args.order, args.sha256, args.expected_bytes)
            authorize_order(order, caller)
            if args.command == 'worker':
                result = worker(order, ref)
            elif args.command == 'inputs':
                result = inputs(order, ref, args.role, args.match)
            else:
                value, identity = (order, ref) if args.document == 'order' else document(order, args.document)
                selected_pointer = args.pointer if args.command == 'section' else ''
                result = {'status': 'COMPLETE_SELECTED_FIELD', 'order': ref, 'document': args.document,
                          'identity': identity, 'pointer': selected_pointer,
                          'value': pointer(value, selected_pointer), 'truncated': False,
                          'recursive_reference_reads': False, 'boundary': BOUNDARY}
        print(encoded(result).decode('utf-8'))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(encoded({'status': 'REFUSED', 'error': str(exc), 'boundary': BOUNDARY}).decode('utf-8'))
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
