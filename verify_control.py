"""Run the extracted phase, context and VM controllers with offline fixtures."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def main():
    source = Path(__file__).resolve().parent
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONUTF8'] = '1'
    # A public test must not inherit a real deployment's routing or caller.
    for key in ('PHASE_POLICY', 'PHASE_PROJECT_ROOT', 'PHASE_ARCHITECT_THREAD',
                'PHASE_SUPERVISOR_THREAD', 'VM_LAB_ROOT', 'CODEX_THREAD_ID'):
        env.pop(key, None)
    parent = Path(tempfile.gettempdir()).resolve()
    # Child tests also allocate temporary roots. Keep their recorded identities
    # consistent when Windows supplies an 8.3 alias through TEMP/TMP.
    env['TEMP'] = str(parent)
    env['TMP'] = str(parent)
    with tempfile.TemporaryDirectory(prefix='control-offline-', dir=parent) as directory:
        root = Path(directory).resolve()
        for name in ('phase_runtime', 'context_runtime', 'vm_execution'):
            shutil.copytree(source / name, root / name,
                            ignore=shutil.ignore_patterns('__pycache__', '.scratch', 'fixtures', 'lab-state'))
        for name, tests in (('phase_runtime', '.'), ('context_runtime', '.'), ('vm_execution', 'tests')):
            print(f'\nOffline suite: {name}', flush=True)
            subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                            '-s', tests, '-p', 'test*.py', '-v'],
                           cwd=root / name, env=env, check=True)
    print('Phase/context/VM offline checks passed. No model, SSH, VM or broker was started.')


if __name__ == '__main__':
    main()
