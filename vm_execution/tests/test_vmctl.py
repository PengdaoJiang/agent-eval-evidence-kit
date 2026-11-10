import argparse
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import types
import unittest
import uuid

MODULE = Path(__file__).resolve().parents[1] / 'tools/vmctl.py'
spec = importlib.util.spec_from_file_location('vmctl', MODULE)
vmctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vmctl)


class FakeRemote:
    def __init__(self, cfg, *, lost=False, receipt_terminal=True, unsafe_archive=False):
        self.cfg = cfg
        self.lost = lost
        self.receipt_terminal = receipt_terminal
        self.unsafe_archive = unsafe_archive
        self.files = {}

    def probe(self):
        return {'boot_id': 'boot-fixture-01', 'uid': 1000, 'hostname': 'test-vm'}

    def upload(self, local, remote):
        self.files[remote] = Path(local).read_bytes()

    def command(self, command, *, stdout=None):
        if command.startswith('python3 '):
            if self.lost:
                raise OSError('simulated connection loss after dispatch')
            req = json.loads(next(v for k, v in self.files.items() if k.endswith('/request.json')))
            receipt = {k: req[k] for k in ('job_id', 'vm_id', 'epoch', 'boot_id')}
            receipt.update(schema='vivado.vm.guest.receipt.v1', terminal=self.receipt_terminal,
                           status='PASS', exit_code=0, execution_started=True,
                           requested_source_sha256=req['source']['sha256'], source_sha256=req['source']['sha256'],
                           workers=req['workers'], timed_out=False, cleanup_signals=[],
                           cleanup_errors=[], remaining_descendants=[])
            self.files[req['guest_root'] + '/receipt.json'] = json.dumps(receipt).encode()
            self.files[req['guest_root'] + '/stdout.log'] = b'VIVADO_STUB_OK\n'
            self.files[req['guest_root'] + '/stderr.log'] = b''
        elif command.startswith('tar '):
            with tarfile.open(fileobj=stdout, mode='w') as tar:
                info = tarfile.TarInfo('../escape' if self.unsafe_archive else 'output/result.txt')
                data = b'actual fixture output'
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return subprocess.CompletedProcess(command, 0, b'{}', b'')

    def download(self, remote, local):
        if remote not in self.files:
            raise vmctl.LabError('missing remote receipt')
        Path(local).write_bytes(self.files[remote])


class VmctlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.script = self.root / 'input.sh'
        self.script.write_text('echo hello\n')
        self.key = self.root / 'key'
        self.key.write_text('fixture-not-a-key')
        self.hosts = self.root / 'known_hosts'
        self.hosts.write_text('fixture')
        self.cfg = {'vm_name': 'Vivado-Linux', 'vm_id': str(uuid.uuid4()), 'epoch': 1,
                    'host_ip': '192.0.2.10', 'user': 'research',
                    'key_path': str(self.key), 'known_hosts': str(self.hosts)}
        vmctl.write_json(self.root / 'state/config.json', self.cfg)
        self.args = argparse.Namespace(project=str(self.project), task='test-task',
                                       script=str(self.script), workers=2, timeout=None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dynamic_vm_state_overrides_config(self):
        vmctl.write_json(self.root / 'state/vm.json', {'vm_id': self.cfg['vm_id'], 'vm_name': 'New-Name',
                          'epoch': 2, 'ip_addresses': ['fe80::123', '192.0.2.20']})
        cfg = vmctl.load_config(self.root)
        self.assertEqual((cfg['vm_name'], cfg['epoch'], cfg['host_ip']), ('New-Name', 2, '192.0.2.20'))

    def test_exact_host_key_required(self):
        options = vmctl.ssh_options(self.cfg)
        self.assertIn('StrictHostKeyChecking=yes', options)
        self.assertIn('HostKeyAlias=Vivado-Linux', options)
        self.assertIn('BatchMode=yes', options)
        self.assertNotIn('StrictHostKeyChecking=no', options)

    def test_live_task_exclusive_but_projects_independent(self):
        vmctl.admit(self.root, self.project, 'same', self.script, 1, None)
        with self.assertRaises(vmctl.LabError):
            vmctl.admit(self.root, self.project, 'same', self.script, 1, None)
        other = self.root / 'project-2'
        other.mkdir()
        vmctl.admit(self.root, other, 'same', self.script, 1, None)
        self.assertEqual(len(vmctl.leases(self.root)), 2)

    def test_pass_requires_export_and_then_releases(self):
        result = vmctl.run_job(self.root, self.args, FakeRemote)
        self.assertEqual(result['status'], 'PASS')
        lease = vmctl.leases(self.root)[0]
        self.assertEqual(lease['status'], 'RELEASED')
        job = self.root / 'jobs' / result['job_id']
        self.assertTrue((job / 'OUTPUT.tar').is_file())
        self.assertFalse((job / 'output').exists())
        self.assertEqual(vmctl.read_json(job / 'REQUEST.json')['timeout_seconds'], None)

    def test_disconnect_retains_unknown_lease(self):
        result = vmctl.run_job(self.root, self.args, lambda cfg: FakeRemote(cfg, lost=True))
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(vmctl.leases(self.root)[0]['status'], 'UNKNOWN')
        with self.assertRaises(vmctl.LabError):
            vmctl.admit(self.root, self.project, 'test-task', self.script, 1, None)

    def test_nonterminal_receipt_cannot_release(self):
        result = vmctl.run_job(self.root, self.args, lambda cfg: FakeRemote(cfg, receipt_terminal=False))
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(vmctl.leases(self.root)[0]['status'], 'UNKNOWN')

    def test_export_path_escape_not_extracted_and_not_accepted(self):
        result = vmctl.run_job(self.root, self.args, lambda cfg: FakeRemote(cfg, unsafe_archive=True))
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertFalse((self.root / 'escape').exists())

    def test_epoch_change_rejects_recover_without_remote_use(self):
        result = vmctl.run_job(self.root, self.args, lambda cfg: FakeRemote(cfg, lost=True))
        vmctl.write_json(self.root / 'state/vm.json', {'vm_id': self.cfg['vm_id'], 'epoch': 9})
        def forbidden(_):
            self.fail('Remote connection must not be made for stale incarnation')
        with self.assertRaises(vmctl.LabError):
            vmctl.recover_job(self.root, result['job_id'], forbidden)

    def test_invalid_task_fails_before_admission(self):
        with self.assertRaises(vmctl.LabError):
            vmctl.admit(self.root, self.project, '../wrong', self.script, 1, None)
        self.assertEqual(vmctl.leases(self.root), [])

    def test_recovery_requires_persisted_terminal_receipt(self):
        remote = FakeRemote(self.cfg, lost=True)
        result = vmctl.run_job(self.root, self.args, lambda cfg: remote)
        request = vmctl.read_json(self.root / 'jobs' / result['job_id'] / 'REQUEST.json')
        remote.lost = False
        remote.command('python3 fixture_completion')
        recovered = vmctl.recover_job(self.root, result['job_id'], lambda cfg: remote)
        self.assertEqual(recovered['status'], 'PASS')
        self.assertEqual(vmctl.leases(self.root)[0]['status'], 'RELEASED')

    def test_receipt_from_different_boot_cannot_close_lease(self):
        remote = FakeRemote(self.cfg, lost=True)
        result = vmctl.run_job(self.root, self.args, lambda cfg: remote)
        request = vmctl.read_json(self.root / 'jobs' / result['job_id'] / 'REQUEST.json')
        receipt = {k: request[k] for k in ('job_id', 'vm_id', 'epoch', 'boot_id')}
        receipt.update(boot_id='wrong-boot', terminal=True, status='PASS', exit_code=0)
        remote.files[request['guest_root'] + '/receipt.json'] = json.dumps(receipt).encode()
        with self.assertRaises(vmctl.LabError):
            vmctl.recover_job(self.root, result['job_id'], lambda cfg: remote)
        self.assertEqual(vmctl.leases(self.root)[0]['status'], 'UNKNOWN')

    def test_bash_staging_retains_original_and_normalizes_bom_crlf(self):
        self.script.write_bytes(b'\xef\xbb\xbfecho hello\r\n')
        cfg, job, req = vmctl.admit(self.root, self.project, 'normalized', self.script, 1, None)
        self.assertEqual((job / 'source.original').read_bytes(), b'\xef\xbb\xbfecho hello\r\n')
        self.assertEqual((job / 'source.sh').read_bytes(), b'echo hello\n')
        self.assertNotEqual(req['source']['sha256'], req['original_source_pin']['sha256'])

    def test_setup_error_has_no_running_guest_and_releases(self):
        class BadProbe(FakeRemote):
            def probe(self):
                raise OSError('no network before execution')
        result = vmctl.run_job(self.root, self.args, BadProbe)
        self.assertEqual(result['status'], 'SETUP_FAILED')
        self.assertEqual(vmctl.leases(self.root)[0]['status'], 'RELEASED')

    def test_unknown_lease_state_blocks_admission(self):
        _, _, req = vmctl.admit(self.root, self.project, 'unresolved', self.script, 1, None)
        path = self.root / 'state/leases' / (req['job_id'] + '.json')
        row = vmctl.read_json(path)
        row['status'] = 'UNRESOLVED'
        vmctl.write_json(path, row)
        with self.assertRaises(vmctl.LabError):
            vmctl.admit(self.root, self.project, 'unrelated', self.script, 1, None)

    def test_inconsistent_pass_and_source_receipts_rejected(self):
        for mutation in ('exit_code', 'source_sha256', 'missing_hash'):
            with self.subTest(mutation=mutation):
                args = argparse.Namespace(**vars(self.args))
                args.task = 'bad-' + mutation
                class BadReceipt(FakeRemote):
                    def command(self, command, *, stdout=None):
                        result = super().command(command, stdout=stdout)
                        if command.startswith('python3 '):
                            name = next(k for k in self.files if k.endswith('/receipt.json'))
                            receipt = json.loads(self.files[name])
                            if mutation == 'missing_hash':
                                receipt.pop('source_sha256')
                            else:
                                receipt[mutation] = 23 if mutation == 'exit_code' else 'wrong-source'
                            self.files[name] = json.dumps(receipt).encode()
                        return result
                result = vmctl.run_job(self.root, args, BadReceipt)
                self.assertEqual(result['status'], 'UNKNOWN')

    def test_export_reacquires_active_lease_and_detects_midtransfer_epoch_change(self):
        remote = FakeRemote(self.cfg)
        result = vmctl.run_job(self.root, self.args, lambda cfg: remote)
        original_command = remote.command
        observed = []
        def during_export(command, *, stdout=None):
            if command.startswith('tar '):
                observed.append(vmctl.leases(self.root)[0]['status'])
                vmctl.write_json(self.root / 'state/vm.json', {'vm_id': self.cfg['vm_id'], 'epoch': 8})
            return original_command(command, stdout=stdout)
        remote.command = during_export
        with self.assertRaises(vmctl.LabError):
            vmctl.export_job(self.root, result['job_id'], remote)
        self.assertEqual(observed, ['ACTIVE'])
        self.assertEqual(vmctl.leases(self.root)[0]['status'], 'RELEASED')

    def test_export_detects_midtransfer_guest_reboot(self):
        remote = FakeRemote(self.cfg)
        result = vmctl.run_job(self.root, self.args, lambda cfg: remote)
        original_command = remote.command
        def during_export(command, *, stdout=None):
            if command.startswith('tar '):
                remote.probe = lambda: {'boot_id': 'unexpected-reboot'}
            return original_command(command, stdout=stdout)
        remote.command = during_export
        with self.assertRaises(vmctl.LabError):
            vmctl.export_job(self.root, result['job_id'], remote)

    def test_failed_reexport_preserves_all_previously_published_evidence(self):
        remote = FakeRemote(self.cfg)
        result = vmctl.run_job(self.root, self.args, lambda cfg: remote)
        job = self.root / 'jobs' / result['job_id']
        names = ('GUEST_RECEIPT.json', 'stdout.log', 'stderr.log', 'OUTPUT.tar', 'EXPORT.json', 'RESULT.json')
        before = {name: vmctl.sha256(job / name) for name in names}
        request = vmctl.read_json(job / 'REQUEST.json')
        remote.files[request['guest_root'] + '/stdout.log'] = b'changed guest output'
        with self.assertRaises(vmctl.LabError):
            vmctl.export_job(self.root, result['job_id'], remote)
        self.assertEqual(before, {name: vmctl.sha256(job / name) for name in names})
        self.assertEqual(list(job.glob('.export-*')), [])

    def test_identical_reexport_keeps_original_publication(self):
        remote = FakeRemote(self.cfg)
        result = vmctl.run_job(self.root, self.args, lambda cfg: remote)
        job = self.root / 'jobs' / result['job_id']
        before = (job / 'EXPORT.json').read_bytes()
        vmctl.export_job(self.root, result['job_id'], remote)
        self.assertEqual((job / 'EXPORT.json').read_bytes(), before)
        self.assertEqual(vmctl.leases(self.root)[0]['status'], 'RELEASED')

    def guest_namespace(self, *, save_fault=False, stat_fault=False, zombie=False):
        namespace = {'__name__': 'guest_unit_test'}
        exec(compile(vmctl.GUEST_RUNNER, 'guest_runner.py', 'exec'), namespace)
        process = types.SimpleNamespace(pid=4242, returncode=None)
        process.poll = lambda: process.returncode
        def terminate():
            process.returncode = -15
        process.terminate = terminate
        process.kill = lambda: setattr(process, 'returncode', -9)
        def wait(timeout=None):
            process.returncode = 0
            return 0
        process.wait = wait
        namespace['subprocess'] = types.SimpleNamespace(Popen=lambda *a, **k: process,
                                                        DEVNULL=-3, TimeoutExpired=subprocess.TimeoutExpired)
        namespace['signal'] = types.SimpleNamespace(SIGHUP=1, SIGTERM=15, SIGKILL=9, SIG_IGN=0,
                                                    signal=lambda *a: None)
        namespace['enable_subreaper'] = lambda: None
        namespace['boot_id'] = lambda: 'boot-fixture-01'
        observed = {'signals': [], 'stat_calls': 0, 'zombie': zombie}
        def descendants():
            observed['stat_calls'] += 1
            if stat_fault and observed['stat_calls'] == 1:
                raise OSError('injected /proc failure')
            if process.returncode is None or observed['zombie']:
                return [{'pid': 4242, 'ppid': 1, 'state': 'Z' if observed['zombie'] else 'S', 'start_ticks': '123'}]
            return []
        namespace['descendants'] = descendants
        def signal_owned(rows, sig):
            observed['signals'].append(sig)
            process.returncode = -15 if sig == 15 else -9
        namespace['signal_owned'] = signal_owned
        namespace['reap'] = lambda: observed.update(zombie=False)
        original_save = namespace['save']
        count = [0]
        def save(root, value):
            count[0] += 1
            if save_fault and count[0] == 1:
                raise OSError('injected first receipt write failure')
            return original_save(root, value)
        namespace['save'] = save
        return namespace, process, observed

    def execute_guest_fixture(self, namespace):
        _, job, request = vmctl.admit(self.root, self.project, 'guest-runner', self.script, 1, None)
        request['boot_id'] = 'boot-fixture-01'
        return namespace['execute'](job, request), job, request

    def test_guest_post_popen_receipt_failure_cleans_children_in_finally(self):
        namespace, process, observed = self.guest_namespace(save_fault=True)
        receipt, job, request = self.execute_guest_fixture(namespace)
        self.assertEqual(receipt['status'], 'ERROR')
        self.assertTrue(receipt['terminal'])
        self.assertEqual(receipt['remaining_descendants'], [])
        self.assertEqual(process.returncode, -15)
        self.assertEqual(observed['signals'], [15])
        self.assertTrue(vmctl.read_json(job / 'receipt.json')['terminal'])
        vmctl.validate_receipt(receipt, request)

    def test_guest_process_scan_failure_still_reaps_and_reports_error(self):
        namespace, process, observed = self.guest_namespace(stat_fault=True)
        receipt, job, request = self.execute_guest_fixture(namespace)
        self.assertTrue(receipt['terminal'])
        self.assertEqual(receipt['status'], 'ERROR')
        self.assertTrue(receipt['cleanup_errors'])
        vmctl.validate_receipt(receipt, request)

    def test_guest_reaps_zombie_before_deciding_cleanup_failure(self):
        namespace, process, observed = self.guest_namespace(zombie=True)
        receipt, job, request = self.execute_guest_fixture(namespace)
        self.assertEqual(receipt['status'], 'PASS')
        self.assertEqual(observed['signals'], [])
        vmctl.validate_receipt(receipt, request)

    def test_guest_preexecution_error_can_close_without_fake_exit_or_source(self):
        namespace, process, observed = self.guest_namespace()
        def no_subreaper():
            raise OSError('unsupported supervisor capability')
        namespace['enable_subreaper'] = no_subreaper
        receipt, job, request = self.execute_guest_fixture(namespace)
        self.assertEqual(receipt['status'], 'ERROR')
        self.assertFalse(receipt['execution_started'])
        self.assertIsNone(receipt['exit_code'])
        vmctl.validate_receipt(receipt, request)


if __name__ == '__main__':
    unittest.main()
