#!/usr/bin/env python3
"""Shared, non-elevated SSH execution client for the Vivado Hyper-V laboratory.

The elevated broker and this client share state/admission.lock. The broker must
hold that lock while changing VM state and must refuse rollback when a lease is
ACTIVE or UNKNOWN. No private keys, credentials or license contents are logged.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

DEFAULT_ROOT = Path(os.environ.get('VM_LAB_ROOT', Path(__file__).resolve().parents[1] / 'lab-state'))
OPENSSH = Path('C:/Windows/System32/OpenSSH')
LIVE_LEASES = {'ACTIVE', 'UNKNOWN'}
TASK_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')


class LabError(RuntimeError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def pin(path):
    p = Path(path)
    return {'path': str(p), 'bytes': p.stat().st_size, 'sha256': sha256(p)}


@contextlib.contextmanager
def file_lock(path):
    """Byte-range lock compatible with .NET FileStream.Lock(0, 1)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (13, 36):
                        raise
                    time.sleep(0.1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def admission_lock(root):
    return file_lock(Path(root) / 'state/admission.lock')


def load_config(root, require_address=True):
    root = Path(root)
    cfg = read_json(root / 'state/config.json')
    dynamic_path = root / 'state/vm.json'
    if dynamic_path.exists():
        dynamic = read_json(dynamic_path)
        for key in ('vm_id', 'vm_name', 'epoch'):
            if key in dynamic:
                cfg[key] = dynamic[key]
        if dynamic.get('ip_addresses'):
            addresses = dynamic['ip_addresses']
            if isinstance(addresses, str):
                addresses = [addresses]
            usable = []
            for value in addresses:
                try:
                    address = ipaddress.ip_address(value)
                    if not (address.is_loopback or address.is_link_local or address.is_unspecified):
                        usable.append(address)
                except ValueError:
                    continue
            if usable:
                usable.sort(key=lambda value: value.version)
                cfg['host_ip'] = str(usable[0])
    required = ('vm_name', 'vm_id', 'user', 'key_path', 'known_hosts', 'epoch')
    if require_address:
        required += ('host_ip',)
    for key in required:
        if cfg.get(key) is None or str(cfg[key]) == '':
            raise LabError('Missing configuration: ' + key)
    if not TASK_RE.fullmatch(cfg['vm_name']):
        raise LabError('vm_name must be a safe HostKeyAlias')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', cfg['user']):
        raise LabError('Invalid SSH user')
    if cfg.get('host_ip'):
        try:
            ipaddress.ip_address(cfg['host_ip'])
        except ValueError as exc:
            raise LabError('host_ip must be an explicit IP address') from exc
    try:
        cfg['vm_id'] = str(uuid.UUID(str(cfg['vm_id'])))
    except ValueError as exc:
        raise LabError('Invalid VM ID') from exc
    for key in ('key_path', 'known_hosts'):
        path = Path(cfg[key])
        if not path.is_absolute() or not path.is_file():
            raise LabError(key + ' must name an existing absolute file')
    return cfg


def ssh_options(cfg, scp=False):
    options = ['-F', 'NUL' if os.name == 'nt' else '/dev/null',
               '-i', cfg['key_path'], '-o', 'IdentitiesOnly=yes',
               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'HostKeyAlias=' + cfg['vm_name'],
               '-o', 'UserKnownHostsFile=' + cfg['known_hosts'],
               '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=2']
    if cfg.get('port'):
        options += ['-P' if scp else '-p', str(int(cfg['port']))]
    return options


class Remote:
    def __init__(self, cfg):
        self.cfg = cfg
        host = cfg['host_ip']
        self.destination = cfg['user'] + '@' + host
        self.scp_destination = cfg['user'] + '@' + ('[' + host + ']' if ':' in host else host)

    def command(self, command, *, stdout=None):
        return subprocess.run([str(OPENSSH / 'ssh.exe'), *ssh_options(self.cfg),
                               self.destination, command], stdin=subprocess.DEVNULL,
                              stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False)

    def upload(self, local, remote):
        local = Path(local)
        result = subprocess.run([str(OPENSSH / 'scp.exe'), *ssh_options(self.cfg, True),
                                 local.name, self.scp_destination + ':' + remote],
                                cwd=local.parent, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise LabError('Upload failed: ' + result.stderr.decode(errors='replace')[-2000:])

    def download(self, remote, local):
        local = Path(local)
        result = subprocess.run([str(OPENSSH / 'scp.exe'), *ssh_options(self.cfg, True),
                                 self.scp_destination + ':' + remote, local.name],
                                cwd=local.parent, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise LabError('Download failed: ' + result.stderr.decode(errors='replace')[-2000:])

    def probe(self):
        code = "import json,os,socket;print(json.dumps({'boot_id':open('/proc/sys/kernel/random/boot_id').read().strip(),'hostname':socket.gethostname(),'uid':os.getuid()}))"
        result = self.command('python3 -c ' + shlex.quote(code))
        if result.returncode:
            raise LabError('Guest probe failed: ' + result.stderr.decode(errors='replace')[-2000:])
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise LabError('Guest probe returned no valid JSON') from exc


def leases(root):
    directory = Path(root) / 'state/leases'
    rows = []
    for path in sorted(directory.glob('*.json')):
        try:
            row = read_json(path)
            if row.get('status') not in LIVE_LEASES | {'RELEASED'}:
                raise LabError('Unknown lease state: ' + str(row.get('status')))
            rows.append(row)
        except Exception as exc:
            raise LabError('Unreadable lease prevents safe admission: ' + str(path)) from exc
    return rows


def admit(root, project, task, script, workers, timeout):
    root = Path(root)
    project, script = Path(project), Path(script)
    if not project.is_absolute() or not project.is_dir():
        raise LabError('--project must be an existing absolute directory')
    if not script.is_absolute() or not script.is_file():
        raise LabError('--script must be an existing absolute file')
    if not TASK_RE.fullmatch(task) or task in ('.', '..'):
        raise LabError('--task must be a simple name (letters, digits, _, -, .)')
    if workers < 1:
        raise LabError('--workers must be positive')
    if timeout is not None and timeout <= 0:
        raise LabError('--timeout must be positive when supplied')
    project = project.resolve()
    task_key = hashlib.sha256((os.path.normcase(str(project)) + '\n' + task).encode()).hexdigest()
    with admission_lock(root):
        cfg = load_config(root)
        for row in leases(root):
            if row.get('status') in LIVE_LEASES and row.get('task_key') == task_key:
                raise LabError('Task already has a live/unknown lease: ' + row['job_id'])
        job_id = str(uuid.uuid4())
        job = root / 'jobs' / job_id
        job.mkdir(parents=True, exist_ok=False)
        original_bytes = script.read_bytes()
        normalized = original_bytes.decode('utf-8-sig').replace('\r\n', '\n')
        (job / 'source.original').write_bytes(original_bytes)
        (job / 'source.sh').write_text(normalized, encoding='utf-8', newline='\n')
        request = {'schema': 'vivado.vm.job.v1', 'job_id': job_id, 'project': str(project),
                   'task': task, 'task_key': task_key, 'workers': workers, 'timeout_seconds': timeout,
                   'vm_name': cfg['vm_name'], 'vm_id': cfg['vm_id'], 'epoch': cfg['epoch'],
                   'host_ip': cfg['host_ip'], 'created_at': utc(), 'source': pin(job / 'source.sh'),
                   'original_source': str(script.resolve()), 'original_source_pin': pin(job / 'source.original'),
                   'staging': 'UTF-8 without BOM; CRLF normalized to LF for bash',
                   'guest_root': '/srv/experiments/' + job_id,
                   'host_owner_pid': os.getpid()}
        write_json(job / 'REQUEST.json', request)
        lease = {k: request[k] for k in ('job_id', 'project', 'task', 'task_key', 'workers',
                                        'vm_id', 'vm_name', 'epoch', 'host_owner_pid')}
        lease.update(schema='vivado.vm.lease.v1', status='ACTIVE', phase='PREPARING',
                     created_at=utc(), updated_at=utc())
        write_json(root / 'state/leases' / (job_id + '.json'), lease)
    return cfg, job, request


def update_lease(root, job_id, **changes):
    with admission_lock(root):
        path = Path(root) / 'state/leases' / (job_id + '.json')
        row = read_json(path)
        row.update(changes, updated_at=utc())
        write_json(path, row)


# The runner remains foreground with respect to SSH. Ignoring SIGHUP allows a
# disconnected caller to recover its receipt instead of starting a second job.
# Linux subreaper ownership also captures ordinary daemonized descendants after
# the main script exits. Scripts must not delegate work to independent services.
GUEST_RUNNER = r'''import ctypes, hashlib, json, os, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
def now(): return datetime.now(timezone.utc).isoformat()
def boot_id(): return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
def enable_subreaper():
    if ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)!=0:
        raise RuntimeError('Cannot establish Linux child subreaper')
def save(root,value):
    p=root/'receipt.tmp'
    p.write_text(json.dumps(value,indent=2)+'\n')
    os.replace(p,root/'receipt.json')
def descendants():
    rows={}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            text=(p/'stat').read_text(); fields=text[text.rfind(')')+2:].split()
            rows[int(p.name)]={'pid':int(p.name),'ppid':int(fields[1]),'state':fields[0],'start_ticks':fields[19]}
        except (FileNotFoundError,ProcessLookupError): pass
    selected={os.getpid()}; changed=True
    while changed:
        changed=False
        for pid,r in rows.items():
            if r['ppid'] in selected and pid not in selected: selected.add(pid); changed=True
    return [rows[p] for p in selected if p!=os.getpid() and p in rows]
def reap():
    while True:
        try:
            pid,_=os.waitpid(-1,os.WNOHANG)
            if pid==0: break
        except ChildProcessError: break
def signal_owned(rows,sig):
    current={r['pid']:r for r in descendants()}
    for r in rows:
        match=current.get(r['pid'])
        if match and match['start_ticks']==r['start_ticks'] and match['state']!='Z':
            try: os.kill(r['pid'],sig)
            except ProcessLookupError: pass
def cleanup_owned(process,receipt):
    # This is a termination grace interval, not a research runtime limit.
    started=time.monotonic()
    while True:
        try:
            code=process.poll()
            if code is not None: reap()
            remaining=descendants()
            if code is not None and not remaining:
                receipt['exit_code']=code
                receipt['remaining_descendants']=[]
                receipt['terminal']=True
                return
            active=[r for r in remaining if r['state']!='Z']
            sig=signal.SIGTERM if time.monotonic()-started<5 else signal.SIGKILL
            if active:
                signal_owned(active,sig)
                name='SIGTERM' if sig==signal.SIGTERM else 'SIGKILL'
                if name not in receipt['cleanup_signals']: receipt['cleanup_signals'].append(name)
            elif code is None:
                # Popen still owns this unreaped PID, so it cannot have been reused.
                process.terminate() if sig==signal.SIGTERM else process.kill()
                name='SIGTERM' if sig==signal.SIGTERM else 'SIGKILL'
                if name not in receipt['cleanup_signals']: receipt['cleanup_signals'].append(name)
        except BaseException as exc:
            message=type(exc).__name__+': '+str(exc)
            if message not in receipt['cleanup_errors']: receipt['cleanup_errors'].append(message)
            # A failed /proc scan must not skip main-process cleanup or claim release.
            try:
                if process.poll() is None:
                    process.terminate() if time.monotonic()-started<5 else process.kill()
            except ProcessLookupError: pass
            except BaseException as secondary:
                message=type(secondary).__name__+': '+str(secondary)
                if message not in receipt['cleanup_errors']: receipt['cleanup_errors'].append(message)
        # If kernel/process inspection is temporarily unavailable, retain this
        # supervisor until it can prove its descendants have actually exited.
        time.sleep(.1)
def execute(root,req):
    receipt={'schema':'vivado.vm.guest.receipt.v1','job_id':req['job_id'],'vm_id':req['vm_id'],'epoch':req['epoch'],
             'boot_id':boot_id(),'started_at':now(),'workers':req['workers'],
             'requested_source_sha256':req['source']['sha256'],'source_sha256':None,
             'execution_started':False,'terminal':False,'exit_code':None,'timed_out':False,
             'cleanup_signals':[],'cleanup_errors':[],'remaining_descendants':[]}
    process=None
    try:
        (root/'stdout.log').touch(); (root/'stderr.log').touch(); (root/'output').mkdir(exist_ok=True)
        if receipt['boot_id']!=req['boot_id']: raise RuntimeError('Guest boot changed before execution')
        source=root/'source.sh'
        receipt['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
        if receipt['source_sha256']!=req['source']['sha256']: raise RuntimeError('Source hash mismatch')
        enable_subreaper()
        signal.signal(signal.SIGHUP,signal.SIG_IGN)
        def interrupted(signum,frame): raise InterruptedError('Runner received signal '+str(signum))
        signal.signal(signal.SIGTERM,interrupted)
        env=os.environ.copy()
        env.update(LAB_JOB_ID=req['job_id'],LAB_VM_ID=req['vm_id'],LAB_VM_EPOCH=str(req['epoch']),
                   LAB_WORKERS=str(req['workers']),OMP_NUM_THREADS=str(req['workers']),MKL_NUM_THREADS=str(req['workers']),
                   LAB_OUTPUT_DIR=str(root/'output'))
        with (root/'stdout.log').open('wb') as out,(root/'stderr.log').open('wb') as err:
            process=subprocess.Popen(['bash',str(source)],cwd=root/'output',env=env,stdin=subprocess.DEVNULL,
                                     stdout=out,stderr=err,start_new_session=True)
            receipt['execution_started']=True; receipt['main_pid']=process.pid
            save(root,receipt)
            try: receipt['exit_code']=process.wait(timeout=req.get('timeout_seconds'))
            except subprocess.TimeoutExpired: receipt['timed_out']=True
    except BaseException as exc:
        receipt['error']=type(exc).__name__+': '+str(exc)
    finally:
        if process is not None:
            cleanup_owned(process,receipt)
        else:
            receipt['terminal']=True
        if receipt.get('error') or receipt['cleanup_errors']:
            receipt['status']='ERROR'
        else:
            receipt['status']='PASS' if receipt['terminal'] and receipt['exit_code']==0 and not receipt['timed_out'] and not receipt['cleanup_signals'] else 'FAIL'
        receipt['ended_at']=now()
        try: save(root,receipt)
        except BaseException as exc:
            receipt['receipt_persist_error']=type(exc).__name__+': '+str(exc)
            receipt['status']='ERROR'
    return receipt
if __name__=='__main__':
    root=Path(__file__).resolve().parent
    receipt=execute(root,json.loads((root/'request.json').read_text()))
    print(json.dumps(receipt))
    sys.exit(0 if receipt.get('status')=='PASS' else 1)
'''


def check_binding(cfg, request, probe=None):
    if cfg['vm_id'] != request['vm_id'] or cfg['epoch'] != request['epoch']:
        raise LabError('VM identity/epoch changed; do not attach this job to a new incarnation')
    if probe is not None and probe['boot_id'] != request.get('boot_id'):
        raise LabError('Guest boot identity changed; original job remains unresolved')


def validate_receipt(receipt, request):
    if receipt.get('schema') != 'vivado.vm.guest.receipt.v1':
        raise LabError('Unrecognized guest receipt schema')
    for key in ('job_id', 'vm_id', 'epoch', 'boot_id'):
        if receipt.get(key) != request.get(key):
            raise LabError('Guest receipt identity mismatch: ' + key)
    if receipt.get('terminal') is not True:
        raise LabError('Guest has not confirmed terminal owner release')
    status = receipt.get('status')
    if status not in {'PASS', 'FAIL', 'ERROR'}:
        raise LabError('Unrecognized guest terminal status')
    if receipt.get('requested_source_sha256') != request['source']['sha256']:
        raise LabError('Guest requested source hash mismatch')
    if receipt.get('workers') != request['workers']:
        raise LabError('Guest worker declaration mismatch')
    if type(receipt.get('execution_started')) is not bool or type(receipt.get('timed_out')) is not bool:
        raise LabError('Guest execution flags must be explicit booleans')
    if receipt.get('remaining_descendants') != []:
        raise LabError('Guest receipt does not prove an empty owned process tree')
    cleanup = receipt.get('cleanup_signals')
    errors = receipt.get('cleanup_errors')
    if not isinstance(cleanup, list) or any(x not in {'SIGTERM', 'SIGKILL'} for x in cleanup):
        raise LabError('Invalid guest cleanup signal record')
    if not isinstance(errors, list) or any(not isinstance(x, str) for x in errors):
        raise LabError('Invalid guest cleanup error record')
    started, code = receipt['execution_started'], receipt.get('exit_code')
    if started:
        if type(code) is not int or receipt.get('source_sha256') != request['source']['sha256']:
            raise LabError('Executed source hash or process exit code is missing/inconsistent')
    elif status != 'ERROR' or code is not None or receipt['timed_out'] or cleanup:
        raise LabError('Non-executed task can only return an explicit pre-execution ERROR')
    clean_success = started and code == 0 and not receipt['timed_out'] and not cleanup and not errors and not receipt.get('error')
    if status == 'PASS' and not clean_success:
        raise LabError('PASS contradicts guest exit/cleanup/error evidence')
    if status == 'FAIL' and (clean_success or errors or receipt.get('error')):
        raise LabError('FAIL contradicts guest execution evidence')
    if status == 'ERROR' and not (receipt.get('error') or errors):
        raise LabError('ERROR requires an explicit failure record')


@contextlib.contextmanager
def artifact_operation(root, job, request, phase):
    """Reacquire VM admission for export/recover, including released jobs."""
    with file_lock(job / 'export.lock'):
        lease_path = Path(root) / 'state/leases' / (request['job_id'] + '.json')
        operation_id = str(uuid.uuid4())
        with admission_lock(root):
            cfg = load_config(root)
            check_binding(cfg, request)
            previous = read_json(lease_path)
            if previous.get('status') not in LIVE_LEASES | {'RELEASED'}:
                raise LabError('Unknown lease state prevents artifact operation')
            row = dict(previous)
            row.update(status='ACTIVE', phase=phase, operation_id=operation_id,
                       operation_owner_pid=os.getpid(), updated_at=utc())
            write_json(lease_path, row)
        try:
            yield cfg
        finally:
            with admission_lock(root):
                row = read_json(lease_path)
                # finish() commits TERMINAL_EXPORTED; otherwise restore the exact
                # prior ownership state. No guest job is launched by this scope.
                if row.get('operation_id') == operation_id and row.get('phase') == phase:
                    previous['updated_at'] = utc()
                    write_json(lease_path, previous)


def _export_job(root, job_id, remote=None, cfg=None):
    job_id = str(uuid.UUID(job_id))
    job = Path(root) / 'jobs' / job_id
    request = read_json(job / 'REQUEST.json')
    cfg = cfg or load_config(root)
    check_binding(cfg, request)
    remote = remote or Remote(cfg)
    check_binding(cfg, request, remote.probe())
    # Transfer scratch never overwrites a previously published evidence file.
    # It contains only host-created files, not extracted guest archive members.
    with tempfile.TemporaryDirectory(prefix='.export-', dir=job) as temporary:
        stage = Path(temporary).resolve()
        if stage.parent != job.resolve():
            raise LabError('Export scratch escaped the exact job root')
        receipt_path = stage / 'GUEST_RECEIPT.json'
        remote.download(request['guest_root'] + '/receipt.json', receipt_path)
        receipt = read_json(receipt_path)
        validate_receipt(receipt, request)
        for name in ('stdout.log', 'stderr.log'):
            remote.download(request['guest_root'] + '/' + name, stage / name)
        archive = stage / 'OUTPUT.tar'
        with archive.open('wb') as stream:
            result = remote.command('tar -C ' + shlex.quote(request['guest_root']) + ' -cf - output', stdout=stream)
        if result.returncode:
            raise LabError('Output export failed: ' + result.stderr.decode(errors='replace')[-2000:])
        entries = []
        with tarfile.open(archive, 'r:') as tar:
            for member in tar:
                from pathlib import PurePosixPath
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or (path.parts and path.parts[0] != 'output'):
                    raise LabError('Unsafe archive member: ' + member.name)
                entries.append({'name': member.name, 'bytes': member.size,
                                'type': member.type.decode('ascii', errors='replace'),
                                'link_target': member.linkname if member.issym() or member.islnk() else None})
        # Broker rollback is excluded by the active artifact lease. This also
        # detects an out-of-protocol guest reboot during the transfer.
        check_binding(load_config(root), request, remote.probe())
        files = {'archive': 'OUTPUT.tar', 'receipt': 'GUEST_RECEIPT.json',
                 'stdout': 'stdout.log', 'stderr': 'stderr.log'}
        staged_pins = {kind: dict(pin(stage / name), path=str(job / name)) for kind, name in files.items()}
        existing = None
        if (job / 'EXPORT.json').exists():
            existing = read_json(job / 'EXPORT.json')
            if existing.get('job_id') != job_id:
                raise LabError('Published export job identity mismatch')
            for kind, name in files.items():
                old_pin = existing[kind]
                existing_path = Path(old_pin['path']).resolve()
                if existing_path != (job / name).resolve() or pin(existing_path) != old_pin:
                    raise LabError('Previously published evidence no longer matches its pin: ' + name)
                if staged_pins[kind] != old_pin:
                    raise LabError('Re-export differs from immutable accepted evidence: ' + name)
        # Expensive archive hashing is outside admission.lock: the live operation
        # lease already excludes rollback without blocking other jobs' admission.
        with admission_lock(root):
            check_binding(load_config(root), request)
            if existing is not None:
                return receipt, existing
            for name in files.values():
                os.replace(stage / name, job / name)
            manifest = {'schema': 'vivado.vm.export.v1', 'job_id': job_id, 'exported_at': utc(),
                        **staged_pins, 'extracted': False, 'members': entries}
            write_json(job / 'EXPORT.json', manifest)
        return receipt, manifest


def export_job(root, job_id, remote=None, cfg=None):
    job_id = str(uuid.UUID(job_id))
    job = Path(root) / 'jobs' / job_id
    request = read_json(job / 'REQUEST.json')
    with artifact_operation(root, job, request, 'EXPORTING') as current:
        return _export_job(root, job_id, remote, current)


def finish(root, job, request, receipt, export):
    validate_receipt(receipt, request)
    result = {'schema': 'vivado.vm.result.v1', 'job_id': request['job_id'],
              'status': receipt.get('status', 'FAIL'), 'exit_code': receipt.get('exit_code'),
              'guest_terminal': True, 'export_complete': True, 'ended_at': utc(),
              'receipt': pin(job / 'GUEST_RECEIPT.json'), 'export': pin(job / 'EXPORT.json')}
    with admission_lock(root):
        check_binding(load_config(root), request)
        write_json(job / 'RESULT.json', result)
        lease_path = Path(root) / 'state/leases' / (request['job_id'] + '.json')
        row = read_json(lease_path)
        row.update(status='RELEASED', phase='TERMINAL_EXPORTED', result_status=result['status'],
                   released_at=utc(), updated_at=utc())
        row.pop('operation_id', None)
        row.pop('operation_owner_pid', None)
        write_json(lease_path, row)
    return result


def collect_and_finish(root, job, request, remote, cfg):
    with artifact_operation(root, job, request, 'COLLECTING') as current:
        receipt, exported = _export_job(root, request['job_id'], remote, current)
        return finish(root, job, request, receipt, exported)


def run_job(root, args, remote_factory=Remote):
    cfg, job, request = admit(root, args.project, args.task, args.script, args.workers, args.timeout)
    dispatched = False
    remote = remote_factory(cfg)
    try:
        request['boot_id'] = remote.probe()['boot_id']
        write_json(job / 'REQUEST.json', request)
        update_lease(root, request['job_id'], boot_id=request['boot_id'])
        (job / 'runner.py').write_text(GUEST_RUNNER, encoding='utf-8', newline='\n')
        write_json(job / 'request.json', request)
        command = 'mkdir -p ' + shlex.quote(request['guest_root'])
        result = remote.command(command)
        if result.returncode:
            raise LabError('Guest job directory creation failed')
        for name in ('source.sh', 'runner.py', 'request.json'):
            remote.upload(job / name, request['guest_root'] + '/' + name)
        check_binding(load_config(root), request, remote.probe())
        update_lease(root, request['job_id'], phase='RUNNING')
        dispatched = True
        result = remote.command('python3 ' + shlex.quote(request['guest_root'] + '/runner.py'))
        (job / 'transport.stdout.log').write_bytes(result.stdout or b'')
        (job / 'transport.stderr.log').write_bytes(result.stderr or b'')
        write_json(job / 'TRANSPORT.json', {'exit_code': result.returncode, 'returned_at': utc(),
                                          'note': 'SSH exit alone is not a guest completion proof.'})
        return collect_and_finish(root, job, request, remote, cfg)
    except BaseException as exc:
        status = 'UNKNOWN' if dispatched else 'SETUP_FAILED'
        result = {'schema': 'vivado.vm.result.v1', 'job_id': request['job_id'], 'status': status,
                  'error': type(exc).__name__ + ': ' + str(exc), 'at': utc(),
                  'guest_terminal': False if dispatched else None,
                  'recovery': 'vmctl.py recover --job ' + request['job_id'] if dispatched else None}
        write_json(job / 'RESULT.json', result)
        update_lease(root, request['job_id'], status='UNKNOWN' if dispatched else 'RELEASED',
                     phase=status, error=result['error'])
        return result


def recover_job(root, job_id, remote_factory=Remote):
    job_id = str(uuid.UUID(job_id))
    job = Path(root) / 'jobs' / job_id
    request = read_json(job / 'REQUEST.json')
    cfg = load_config(root)
    check_binding(cfg, request)
    return collect_and_finish(root, job, request, remote_factory(cfg), cfg)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab-root', type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    doctor = commands.add_parser('doctor')
    doctor.add_argument('--offline', action='store_true')
    run = commands.add_parser('run')
    run.add_argument('--project', required=True)
    run.add_argument('--task', required=True)
    run.add_argument('--script', required=True)
    run.add_argument('--workers', required=True, type=int)
    run.add_argument('--timeout', type=float, default=None)
    for name in ('export', 'recover'):
        command = commands.add_parser(name)
        command.add_argument('--job', required=True)
    args = parser.parse_args(argv)
    try:
        root = args.lab_root.resolve()
        if args.command == 'status':
            cfg = load_config(root, require_address=False)
            result = {k: cfg.get(k) for k in ('vm_id', 'vm_name', 'epoch', 'host_ip')}
            rows = leases(root)
            result['live_leases'] = [x for x in rows if x.get('status') in LIVE_LEASES]
            result['lease_count'] = len(rows)
            result['released_count'] = sum(x.get('status') == 'RELEASED' for x in rows)
            result['live_workers'] = sum(x['workers'] for x in result['live_leases'])
        elif args.command == 'doctor':
            cfg = load_config(root, require_address=not args.offline)
            binaries = {name: (OPENSSH / name).is_file() for name in ('ssh.exe', 'scp.exe')}
            if not all(binaries.values()):
                raise LabError('Windows OpenSSH client tools are missing')
            result = {'status': 'PASS', 'vm_id': cfg['vm_id'], 'epoch': cfg['epoch'], 'tools': binaries,
                      'strict_host_key_checking': True, 'guest_probe': None if args.offline else Remote(cfg).probe()}
        elif args.command == 'run':
            result = run_job(root, args)
        elif args.command == 'recover':
            result = recover_job(root, args.job)
        else:
            receipt, exported = export_job(root, args.job)
            result = {'status': 'EXPORTED', 'job_id': args.job, 'guest_status': receipt.get('status'), 'export': exported}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status', 'PASS') in ('PASS', 'EXPORTED') else 1
    except (LabError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'status': 'ERROR', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
