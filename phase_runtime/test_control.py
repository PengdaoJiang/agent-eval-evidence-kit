"""Exercise caller identity and exact release selection at the wrapper boundary."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import phase_control as control


class BoundControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        entry = Path(__file__).with_name('phase_guard.py').resolve()
        spec = importlib.util.spec_from_file_location('control_test_guard', entry)
        self.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.guard)
        self.release = self.root / 'release.json'
        self.release.write_text(json.dumps({'schema': 'rea.phase_control.release.v1',
            'sources': [self.guard.file_identity(entry)],
            'guard_entry': self.guard.file_identity(entry)}), encoding='utf-8')
        self.policy = self.root / 'policy.json'
        self.policy.write_text(json.dumps({'schema': 'rea.phase_continuity.policy.v1',
            'release_descriptor': self.guard.file_identity(self.release),
            'registry_root': str(self.root / 'registry'), 'controller_owner': 'controller-fixture'}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_worker_cannot_choose_sibling_or_controller_operation(self):
        for operation, request in [('register', {}), ('validate-delivery', {}),
                                   ('recover', {'thread_id': 'other-worker'}),
                                   ('publish-progress', {'thread_id': 'other-worker'})]:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                control.operate(operation, self.policy, request, 'worker-fixture')
        self.assertFalse((self.root / 'registry').exists())

    def test_missing_current_binding_never_returns_startup(self):
        result = control.operate('context', self.policy, native_thread='worker-fixture')
        self.assertEqual(result['status'], 'CURRENT_FORMAL_ASSIGNMENT_REQUIRED')
        self.assertFalse(result['startup_fallback'])
        self.assertFalse(result['owner_transfer'])
        leader = control.operate('context', self.policy, native_thread='controller-fixture')
        self.assertEqual(leader['status'], 'SUPERVISOR_CONTROL_ENTRY')
        self.assertFalse(leader['scientific_acceptance'])

    def test_changed_release_and_missing_native_caller_are_rejected(self):
        with self.assertRaises(ValueError):
            control.operate('context', self.policy)
        self.release.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            control.operate('context', self.policy, native_thread='worker-fixture')


if __name__ == '__main__':
    unittest.main()
