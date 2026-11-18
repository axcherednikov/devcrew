#!/usr/bin/env python3
"""Independent H4 reproduction using only public CLI commands and real Git/evidence.

Run with --cli pointing to another revision to compare the same bypass.
"""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parents[1] / 'scripts/devcrew.py')
    parser.add_argument('--expect', choices=('PASS', 'REJECT'), default='REJECT')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='h4-independent-') as directory:
        project = pathlib.Path(directory)
        def cli(*arguments):
            return subprocess.run([sys.executable, str(args.cli.resolve()), *map(str, arguments)],
                                  capture_output=True, text=True)
        def git(*arguments):
            subprocess.run(['git', '-C', str(project), *arguments], check=True, capture_output=True)
        git('init', '-q')
        (project / 'required-test.py').write_text("raise SystemExit('confirmed project test failure')\n")
        git('add', 'required-test.py')
        git('-c', 'user.name=H4', '-c', 'user.email=h4@example.com', 'commit', '-qm', 'fixture')
        checks = project / '.codex/checks'
        checks.mkdir(parents=True)
        plan, results = checks / 'plan.json', checks / 'results.json'
        argv = [sys.executable, '-B', 'required-test.py']
        plan.write_text(json.dumps({'waves': [{'id': 'project', 'slices': [
            {'id': 'implementation', 'files': ['required-test.py']}],
            'verification': ['mandatory project test'], 'verification_bindings': [
                {'index': 0, 'verification': 'mandatory project test', 'argv': argv}]}]}))
        results.write_text(json.dumps({'waves': []}))
        approved = cli('wave-gate', '--project', project, '--plan', plan, '--results', results, '--wave', 0)
        if approved.returncode:
            raise RuntimeError(approved.stderr)
        required = cli('run-check', '--project', project, '--name', 'mandatory', '--', *argv)
        unrelated = cli('run-check', '--project', project, '--name', 'mandatory', '--',
                        sys.executable, '-c', "print('irrelevant command passed')")
        if required.returncode == 0 or unrelated.returncode != 0:
            raise RuntimeError('fixture did not reproduce failing required / passing unrelated commands')
        results.write_text(json.dumps({'waves': [{'id': 'project', 'slices': [
            {'id': 'implementation', 'done': True, 'problems': []}],
            'checks': [unrelated.stdout.splitlines()[0]]}]}))
        gate = cli('wave-gate', '--project', project, '--plan', plan, '--results', results, '--wave', 1)
        verdict = 'PASS' if gate.returncode == 0 else 'REJECT'
        print(json.dumps({'mandatory_exit': required.returncode, 'unrelated_exit': unrelated.returncode,
                          'bypass': verdict, 'gate_stderr': gate.stderr.strip()}))
        return 0 if verdict == args.expect else 1


if __name__ == '__main__':
    raise SystemExit(main())
