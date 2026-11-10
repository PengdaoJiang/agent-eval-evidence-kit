"""Independent-behavior tests for exact context projection; no production writes."""
from pathlib import Path
import contextlib
import importlib.util
import io
import json
import os
import shutil
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / 'context_view.py'
spec = importlib.util.spec_from_file_location('context_view_under_test', ENTRY)
cv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cv)


class ContextProjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'fixtures' / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=True)
        self.long_clause = '科学条款完整返回，不能任意截断。' * 1000
        self.definition = {'schema': 'rea.architecture.action_continuation_definition.v1',
            'goal': 'one complete lemma', 'assumptions': ['complete-domain'],
            'state_relation': {'S': 'arbitrary'}, 'acceptance': ['independent'],
            'independent_verification': {'separate': True}, 'public_export_contract': {'type': 'typed'},
            'new_unrecognized_scientific_clause': self.long_clause}
        di = self.write('definition.json', self.definition)
        mi = self.write('method.json', {'schema': 'rea.architecture.source_semantics_changed_method_contract.v1',
                                      'V2': {'full': self.long_clause}, 'external': '/not/selected.json'})
        ii = self.write('inputs.json', {'schema': 'rea.architecture.action_exact_inputs.v1', 'catalog': '/catalog',
             'references': [{'path': str(self.root / 'not_opened.bin'), 'bytes': 2, 'sha256': 'a' * 64, 'role': 'RULE',
                             'usage': 'retain qualifier', 'new_scientific_constraint': 'must read'},
                            {'path': str(self.root / 'other.bin'), 'bytes': 3, 'sha256': 'b' * 64, 'role': 'PROFILE'}]})
        si = self.write('storage.json', {'schema': 'rea.storage.dependencies.v1',
                   'files': [{'path': 'duplicate-only'}] * 100,
                   'read_closure': [{'path': 'duplicate-only'}] * 100})
        self.order = {'目标': 'job/a001 scientific work', '选中输入': {
            'definition': di, 'method_contract': mi, 'selected_index': ii, 'storage_binding': si,
            'audit_writer': {'owner': 'fixture-worker'}},
            '独立可写根': {'work': str(self.root / 'work'), 'evidence': str(self.root / 'evidence'),
                          'supervisor_only': 'not-a-worker-write-root'}, '交付': ['proof'], '验收': ['full proof']}
        self.ref = self.write('order.json', self.order)
        self.ref_file = self.root / 'order_ref.json'
        self.ref_file.write_text(json.dumps(self.ref), encoding='utf-8')

    def write(self, name, value):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return cv.read(p)[1]

    def call(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.dict(os.environ, {'CODEX_THREAD_ID': 'fixture-worker'}):
            result = cv.main(list(args))
        return result, json.loads(out.getvalue())

    def tearDown(self):
        target = self.root.resolve()
        expected_parent = (ROOT / 'fixtures').resolve()
        assert target.parent == expected_parent and target.is_relative_to(ROOT.resolve())
        assert target.name == self._testMethodName
        shutil.rmtree(target)

    def test_exact_path_hash_and_optional_size(self):
        code, result = self.call('worker', '--order', self.ref['path'], '--sha256', self.ref['sha256'],
                                 '--bytes', str(self.ref['bytes']))
        self.assertEqual(code, 0)
        self.assertEqual(result['order'], self.ref)
        self.assertNotIn('supervisor_only', result['owned_roots'])

    def test_wrong_hash_refused(self):
        code, result = self.call('worker', '--order', self.ref['path'], '--sha256', '0' * 64)
        self.assertEqual(code, 1)
        self.assertIn('EXACT_IDENTITY_MISMATCH', result['error'])

    def test_wrong_size_refused(self):
        with self.assertRaises(ValueError):
            cv.order_context(self.ref['path'], self.ref['sha256'], self.ref['bytes'] + 1)

    def test_descriptor_file_supported(self):
        order, ref = cv.order_context(str(self.ref_file))
        self.assertEqual(order, self.order)
        self.assertEqual(ref, self.ref)

    def test_full_science_core_and_unknown_field_preserved(self):
        code, result = self.call('core', '--order', str(self.ref_file), '--document', 'definition')
        self.assertEqual(code, 0)
        self.assertEqual(result['value'], self.definition)
        self.assertFalse(result['truncated'])
        self.assertEqual(len(result['value']['new_unrecognized_scientific_clause']), len(self.long_clause))

    def test_science_navigation_lists_unknown_field_and_no_storage_dump(self):
        value = cv.worker(self.order, self.ref)
        self.assertIn('/new_unrecognized_scientific_clause', value['documents']['definition']['top_level_pointers'])
        self.assertNotIn('duplicate-only', json.dumps(value))
        self.assertEqual(value['machine_index_counts']['storage']['files'], 100)

    def test_unknown_schema_refused(self):
        changed = self.write('new-schema.json', {'schema': 'unrecognized.v99', 'science': 'cannot discard'})
        self.order['选中输入']['definition'] = changed
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_SCHEMA'):
            cv.worker(self.order, self.ref)

    def test_unselected_document_refused_no_nested_follow(self):
        with self.assertRaisesRegex(ValueError, 'DOCUMENT_NOT_SELECTED'):
            cv.document(self.order, 'read_scope')
        value, _ = cv.document(self.order, 'method')
        self.assertEqual(cv.pointer(value, '/external'), '/not/selected.json')

    def test_section_complete_and_pointer_escapes(self):
        self.assertEqual(cv.pointer({'a/b': {'~x': ['full']}}, '/a~1b/~0x/0'), 'full')
        code, result = self.call('section', '--order', str(self.ref_file), '--document', 'method', '--pointer', '/V2/full')
        self.assertEqual(code, 0)
        self.assertEqual(result['value'], self.long_clause)
        with self.assertRaises(ValueError):
            cv.pointer({}, '/bad~x')

    def test_inputs_query_only_existing_roles_without_content_read(self):
        value = cv.inputs(self.order, self.ref, 'RULE')
        self.assertEqual(value['matched_count'], 1)
        self.assertEqual(value['references'][0]['role'], 'RULE')
        self.assertEqual(value['references'][0]['exact_index_pointer'], '/references/0')
        self.assertIn('new_scientific_constraint', value['references'][0]['omitted_field_names'])
        self.assertFalse(Path(value['references'][0]['path']).exists())
        self.assertEqual(cv.inputs(self.order, self.ref, 'not-selected')['matched_count'], 0)

    def test_duplicate_key_rejected(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_JSON_KEY'):
            cv.parse('{"x":1,"x":2}')

    def test_cross_job_order_and_control_role_refused(self):
        with self.assertRaisesRegex(ValueError, 'OTHER_OR_UNBOUND_OWNER'):
            cv.authorize_order(self.order, 'different-worker')
        code, result = self.call('supervisor')
        self.assertEqual(code, 1)
        self.assertIn('CONTROL_ROLE_CALLER_REQUIRED', result['error'])

    def test_disagreeing_order_owners_refused(self):
        self.order['独立可写根']['storage_operations'] = {'owner': 'other-verifier'}
        with self.assertRaisesRegex(ValueError, 'OTHER_OR_UNBOUND_OWNER'):
            cv.authorize_order(self.order, 'fixture-worker')

    def controls(self):
        rows = [{'id': 'old', 'state': 'ACCEPTED_SCOPED_ARCHIVED', 'archived': True,
                 'gui_archive_receipt': 'recorded-exact-archive-receipt'}] * 50
        rows += [{'id': 'startup', 'state': 'STARTING', 'phase': 'SIDECAR_STARTUP',
                  'owner': {'thread_id': 'thread-start'}},
                 {'id': 'unknown', 'state': 'NEW_UNRECOGNIZED_STATE', 'archived': True,
                  'phase': 'unknown-phase', 'readiness': 'DO_NOT_ASSUME'},
                 {'id': 'hold', 'state': 'HOLD'},
                 {'id': 'ready', 'state': 'READY'},
                 {'id': 'needs-archive', 'state': 'COMPLETED', 'archived': True}]
        self.write('governance/supervisor/ready_queue.json', {'schema': 'rea.supervisor.ready_queue.v5.2',
                    'rows': rows, 'updated_at_utc': '2026-10-22T05:00:00Z', 'current_dispatch': {'active_gui': 0},
                    'research_complete': False})
        self.write('governance/supervisor/control_continuity.json', {
            'schema': 'rea.supervisor.control_continuity.v5.2', 'native_event_cursors': {'thread-start': 'cursor'},
            'latest_open_issue': {'path': 'exact-issue-reference'}, 'updated_at_utc': '2026-10-22T05:00:00Z'})

    def test_supervisor_history_aggregation_unknown_state_and_startup(self):
        self.controls()
        value = cv.supervisor(self.root)
        self.assertEqual(value['historical_rows']['count'], 50)
        self.assertEqual(value['active'][0]['phase'], 'SIDECAR_STARTUP')
        self.assertEqual(value['unclassified_frontier'][0]['state'], 'NEW_UNRECOGNIZED_STATE')
        self.assertEqual(len(value['recorded_ready']), 1)
        self.assertEqual(value['terminal_needing_archive_review'][0]['id'], 'needs-archive')
        self.assertEqual(value['wait_targets'][0]['afterCursor'], 'cursor')
        self.assertNotIn('scientific_acceptance', value)

    def test_architect_stale_runtime_explicit(self):
        self.controls()
        self.write('governance/architecture/CURRENT_EXECUTION.json', {
            'schema': 'rea.architecture.current_execution_view.v1', 'observed_at_utc': '2026-10-22T03:00:00Z',
            'runtime_snapshot': {'active_gui': 1}, 'current_blocker': {'status': 'OLD_RUNNING'},
            'delivery_groups': [], 'terminal_acceptance': {'three_universals': 'UNKNOWN'}})
        value = cv.architect(self.root)
        self.assertTrue(value['route_runtime_snapshot_stale'])
        self.assertFalse(value['route_runtime_snapshot_authoritative'])
        self.assertEqual(value['current_control']['recorded_dispatch']['active_gui'], 0)
        self.assertEqual(value['current_control']['latest']['latest_open_issue']['path'], 'exact-issue-reference')


if __name__ == '__main__':
    unittest.main(verbosity=2)
