"""Run source-derived offline tests with canonical, disposable temporary roots."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='public-harness-') as temporary:
        canonical = str(Path(temporary).resolve())
        env = {**os.environ, 'TMP': canonical, 'TEMP': canonical,
               'TMPDIR': canonical, 'PYTHONDONTWRITEBYTECODE': '1'}
        # Fixtures supply their own fake transports; inherited live settings are
        # irrelevant and removed so a test cannot accidentally select them.
        for name in ('DSH_LAB', 'DSH_LOG_ROOT', 'DSH_BOOTSTRAP_LOG', 'DSH_PACKAGE_JSON'):
            env.pop(name, None)
        subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                        '-s', 'harness_runtime', '-p', 'test_*.py', '-q'], cwd=ROOT, env=env, check=True)
        subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                        '-s', 'cli_parallel/tests', '-q'], cwd=ROOT, env=env, check=True)
        names = ['test_continuation_v2.mjs', 'test_finalize_v2_1.mjs',
                 'test_usage_metrics.mjs', 'test_observation_metrics.mjs',
                 'astra_loop_test_continuation_high_v1.mjs', 'astra_loop_test_continuation_max_v1.mjs',
                 'astra_loop_test_finalize_high_v1.mjs', 'astra_loop_test_finalize_max_v1.mjs']
        subprocess.run([args.node, '--test', '--test-concurrency=1',
                        *[str(ROOT / 'harness_runtime' / name) for name in names]],
                       cwd=ROOT, env=env, check=True)


if __name__ == '__main__': main()
