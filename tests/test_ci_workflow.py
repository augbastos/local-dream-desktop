"""Offline contracts for the unified workflow, aggregate gate and base safety policy."""
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = (shutil.which('bash') if os.name != 'nt' else
        shutil.which('bash', path=str(Path(shutil.which('git') or '').resolve().parents[1] / 'bin')))
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class WorkflowTests(unittest.TestCase):
    def test_workflow_contract(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertTrue(workflow.startswith('# CI profile: public\nname: CI\n'))
        self.assertEqual({path.name for path in (ROOT / '.github/workflows').glob('*.yml')},
                         {'ci.yml', 'package.yml', 'dependabot-automerge.yml'})
        self.assertIn('  push:\n    branches: [main]', workflow)
        self.assertIn('types: [opened, synchronize, reopened, ready_for_review]', workflow)
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}', workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", workflow)
        jobs = dict(re.findall(r'^  ([\w-]+):\n(.*?)(?=^  [\w-]+:|\Z)',
                               workflow.split('jobs:\n', 1)[1], re.M | re.S))
        self.assertEqual(set(jobs), {'safety', 'test', 'ci-passed'})
        self.assertEqual(re.findall(r'^    name: (.+)$', workflow, re.M),
                         ['Safety', 'Test (${{ matrix.platform }}, Python ${{ matrix.python }})', 'CI passed'])
        for job in jobs.values():
            self.assertIn('timeout-minutes:', job)
        for line in workflow.splitlines():
            if 'uses:' in line:
                self.assertRegex(line.strip(), r'uses: actions/[\w-]+@[0-9a-f]{40} # v[\d.]+$')
        safety = jobs['safety']
        self.assertIn("if: github.event_name == 'pull_request'", safety)
        self.assertIn('ref: ${{ github.event.pull_request.base.sha }}', safety)
        self.assertIn('sparse-checkout: .github/scripts', safety)
        self.assertIn("if: hashFiles('base-policy/.github/scripts/safety.sh') == ''", safety)
        self.assertIn('One-time bootstrap:', safety)
        self.assertEqual(safety.count('uses: actions/checkout@'), 3)
        self.assertEqual(safety.count('persist-credentials: false'), 3)
        self.assertIn('fetch-depth: 0', safety)
        self.assertIn('bash base-policy/.github/scripts/safety.sh', safety)
        self.assertIn('bash bootstrap-policy/.github/scripts/safety.sh', safety)
        self.assertIn('needs: [safety, test]', jobs['ci-passed'])
        self.assertIn('if: always()', jobs['ci-passed'])
        self.assertNotIn('pull_request_target', workflow)
        test = jobs['test']
        self.assertIn('os: [ubuntu-latest, windows-latest]', test)
        self.assertIn("python: ['3.10', '3.13']", test)
        self.assertIn('os: ubuntu-latest\n            platform: Linux', test)
        self.assertIn('os: windows-latest\n            platform: Windows', test)
        self.assertIn('cache: pip', test)
        self.assertIn('requirements.txt\n            requirements-ci.txt', test)
        self.assertIn("node-version: '22'", test)
        for command in ('python -m pip install -r requirements-ci.txt',
                        'python tools/test_fast.py', 'node tests/check_page.js'):
            self.assertIn('run: ' + command, test)

    @unittest.skipUnless(BASH and shutil.which('jq'), 'Bash and jq required')
    def test_aggregate_rejects_failure_skip_and_cancel_except_non_pr_safety(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        job = workflow.split('  ci-passed:\n', 1)[1].split('  merge-when-green:\n', 1)[0]
        gate = textwrap.dedent(job.split('        run: |\n', 1)[1]).strip()
        # Exercise the actual workflow command in one shell, including all result pairs.
        commands = ['gate() {\n' + gate + '\n}']
        for event in ('pull_request', 'push', 'workflow_dispatch'):
            for safety, tests in itertools.product(('success', 'failure', 'skipped', 'cancelled'), repeat=2):
                expected = tests == 'success' and (safety == 'success' or
                                                  (safety == 'skipped' and event == 'push'))
                needs = json.dumps({'safety': {'result': safety}, 'test': {'result': tests}})
                commands.append(f"EVENT_NAME='{event}' NEEDS_JSON='{needs}' gate >/dev/null 2>&1")
                commands.append(f'[ "$?" -eq {0 if expected else 1} ] || exit 99')
        result = subprocess.run([BASH, '-c', '\n'.join(commands)], cwd=ROOT,
                                capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(BASH, 'Bash required')
class SafetyTests(unittest.TestCase):
    def run_policy(self, **changes):
        parent = ROOT / '.test-tmp'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory)
            fake = root / 'bin'
            fake.mkdir()
            if changes.pop('PLANT_GITLEAKS', None):
                for name in ('.gitleaksignore', '.gitleaks.toml'):
                    (root / name).write_text('allow everything\n')
            scripts = {
                'curl': 'exit "${DOWNLOAD_STATUS:-0}"\n',
                'sha256sum': 'cat >/dev/null\nexit "${CHECKSUM_STATUS:-0}"\n',
                'tar': 'cp "$FAKE_GITLEAKS" "$4/gitleaks"\nchmod +x "$4/gitleaks"\n',
                'gitleaks': ('printf "%s\\n" "$*" > "$SCAN_ARGS"\n'
                             '[ -e .gitleaksignore ] || [ -e .gitleaks.toml ] && echo present >> "$SCAN_ARGS"\n'
                             'exit "${SCAN_STATUS:-0}"\n'),
                'git': 'printf "%s\\n" "${HEAD_SHA_FAKE:-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}"\n',
                'gh': '''
                    case "$*" in
                      *'/pulls/'*) printf '%s\\n' "$CHANGED_FILES" ;;
                      *'/contents/.github?ref='*)
                        [ "${API_STATUS:-0}" -eq 0 ] || exit "$API_STATUS"
                        printf '%s\\n' "$BASE_ENTRIES" ;;
                      *'/contents/.github/safety-critical.txt?ref='*)
                        [ "${RULE_STATUS:-0}" -eq 0 ] || exit "$RULE_STATUS"
                        printf '%s\\n' "$BASE_RULES" ;;
                      *) exit 99 ;;
                    esac
                ''',
            }
            for name, content in scripts.items():
                path = fake / name
                path.write_text('#!/usr/bin/env bash\nset -eu\n' + textwrap.dedent(content), newline='\n')
                path.chmod(0o755)
            env = dict(os.environ, FAKE_BIN=fake.as_posix(),
                       PR_DIR=root.as_posix(), TMPDIR=root.as_posix(),
                       REPO='test/repo', PR='123', BASE='a' * 40, COMMITS='3',
                       FAKE_GITLEAKS=(fake / 'gitleaks').as_posix(),
                       SCAN_ARGS=(root / 'scan-args').as_posix(), CHANGED_FILES='docs/test.md',
                       BASE_ENTRIES='scripts\nworkflows', BASE_RULES='')
            env.update(changes)
            # Resolve inside Bash so Windows drive colons never split the mock PATH.
            command = 'export PATH="$(cd "$FAKE_BIN" && pwd):$PATH"; exec bash .github/scripts/safety.sh'
            result = subprocess.run([BASH, '-c', command], cwd=ROOT,
                                    env=env, capture_output=True, text=True, timeout=15,
                                    creationflags=NO_WINDOW)
            args = (root / 'scan-args').read_text() if (root / 'scan-args').exists() else ''
            return result.returncode, result.stdout + result.stderr, args

    def test_scan_pins_and_commit_range(self):
        script = (ROOT / '.github/scripts/safety.sh').read_text()
        self.assertIn('/v8.21.2/gitleaks_8.21.2_linux_x64.tar.gz', script)
        self.assertIn('5bc41815076e6ed6ef8fbecc9d9b75bcae31f39029ceb55da08086315316e3ba', script)
        code, output, args = self.run_policy()
        self.assertEqual(code, 0, output)
        self.assertIn('--ignore-gitleaks-allow', args)
        self.assertIn('--config ', args)
        self.assertIn('--gitleaks-ignore-path ', args)
        self.assertIn('--log-opts=--text --no-textconv --no-ext-diff --diff-merges=first-parent '
                      + 'a' * 40 + '..' + 'b' * 40, args)
        self.assertIn('no critical effect', output)

    def test_pr_cannot_configure_its_own_scanner(self):
        # The PR's .gitleaksignore/.gitleaks.toml are gone before gitleaks runs,
        # and adding or changing them (or .gitattributes) needs the owner.
        code, output, args = self.run_policy(PLANT_GITLEAKS='1')
        self.assertEqual(code, 0, output)
        self.assertNotIn('present', args)
        for path in ('.gitleaksignore', 'sub/.gitleaks.toml', '.gitattributes'):
            code, output, _ = self.run_policy(CHANGED_FILES=path)
            self.assertEqual(code, 1, output)
            self.assertIn('changes what the secret scan sees', output)

    def test_invalid_commit_ids_fail_closed(self):
        for changes in (dict(BASE='main'), dict(HEAD_SHA_FAKE='HEAD')):
            code, output, _ = self.run_policy(**changes)
            self.assertEqual(code, 1, output)
            self.assertIn('invalid base or head commit', output)

    def test_public_self_protection(self):
        for path in ('.github/scripts/safety.sh', '.github/safety-critical.txt',
                     '.github/workflows/ci.yml'):
            code, output, _ = self.run_policy(CHANGED_FILES=path)
            self.assertEqual(code, 1, output)
            self.assertIn('needs owner:', output)

    def test_base_rules_apply_to_all_pages_and_rename_sources(self):
        code, output, _ = self.run_policy(
            CHANGED_FILES='docs/new.md\ntests/critical.py', BASE_ENTRIES='safety-critical.txt',
            BASE_RULES='# comment\n\n^tests/critical\\.py$ :: critical effect')
        self.assertEqual(code, 1, output)
        self.assertIn('tests/critical.py (critical effect)', output)
        script = (ROOT / '.github/scripts/safety.sh').read_text()
        self.assertIn('gh api --paginate', script)
        self.assertIn('.previous_filename', script)
        self.assertIn('safety-critical.txt?ref=$BASE', script)

    def test_scan_api_and_rule_failures_fail_closed(self):
        for changes in (dict(DOWNLOAD_STATUS='2'), dict(CHECKSUM_STATUS='2'),
                        dict(SCAN_STATUS='1'), dict(API_STATUS='1'),
                        dict(BASE_ENTRIES='safety-critical.txt', RULE_STATUS='1'),
                        dict(BASE_ENTRIES='safety-critical.txt', BASE_RULES='[ :: invalid regex')):
            with self.subTest(changes=changes):
                code, output, _ = self.run_policy(**changes)
                self.assertNotEqual(code, 0, output)


if __name__ == '__main__':
    unittest.main()
