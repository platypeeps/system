"""A later attempt of one assignment counts the commits an earlier attempt pushed (sd:1802)."""
import os
import subprocess
import sys
import unittest

from sd_db import runner as store
from . import test_runtime as fixtures
from .test_runtime import git

COMMIT = '''import subprocess,sys
from pathlib import Path
Path('work.txt').write_text('authored work\\n')
subprocess.run(['git','add','work.txt'],check=True)
subprocess.run(['git','commit','-qm','Implement fixture\\n\\nAuthored-with: fixture/fixture'],check=True)
sys.exit(1)
'''
NOTHING = 'import sys\nsys.exit(1)\n'
IDLE = 'pass\n'


class ResumedAttempt(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.db, self.config = self.fixture.root, self.fixture.db, self.fixture.config
        self.runner, self.remote, self.provider = self.fixture.runner, self.fixture.remote, self.fixture.provider
        self.claim = self.fixture.claim

    def attempt(self, request, body):
        self.provider.write_text(body)
        return self.runner.execute(self.db, request, command=[sys.executable, str(self.provider)],
                                   environment={'PATH': os.environ['PATH'], 'HOME': str(self.root)})

    def again(self, request):
        current = store.queue_state(self.db, request['id'])
        store.requeue(self.db, request['id'], expected_revision=current['revision'], who='operator')
        return store.claim(self.db, request['id'], owner=self.runner.owner,
                           work_root=self.config.work, retention_root=self.config.retention)

    def test_a_later_attempt_counts_the_commit_an_earlier_attempt_pushed(self):
        request = self.claim()
        first = self.attempt(request, COMMIT)
        self.assertIn('provider exited 1', first['detail'])
        pushed = git(self.remote, 'rev-parse', 'refs/heads/work/item')
        self.assertIn('Implement fixture', git(self.remote, 'log', '-1', '--format=%s', pushed))
        # The default branch moves on, so the second attempt's merge step
        # brings in a commit nobody on this assignment authored.
        other = self.root / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(other)], check=True)
        git(other, 'config', 'user.email', 'other@example.invalid'); git(other, 'config', 'user.name', 'Other')
        (other / 'MAIN').write_text('landed elsewhere\n')
        git(other, 'add', 'MAIN'); git(other, 'commit', '-qm', 'land elsewhere'); git(other, 'push', '-q', 'origin', 'main')
        second = self.attempt(self.again(request), IDLE)
        self.assertEqual(second['run'], 2)
        self.assertEqual(second['outcome'], 'done', second['detail'])
        git(self.remote, 'merge-base', '--is-ancestor', pushed, second['authored_head'])  # raises when not
        self.assertEqual(git(self.remote, 'show', second['authored_head'] + ':MAIN'), 'landed elsewhere')

    def test_a_later_attempt_still_refuses_when_no_attempt_committed(self):
        request = self.claim()
        self.attempt(request, NOTHING)
        second = self.attempt(self.again(request), IDLE)
        self.assertEqual(second['outcome'], 'blocked')
        self.assertIn('authored no commits', second['detail'])

    def test_a_new_assignment_does_not_inherit_an_earlier_assignments_commits(self):
        request = self.claim()
        self.attempt(request, COMMIT)
        self.assertTrue(git(self.remote, 'rev-parse', 'refs/heads/work/item'))
        other = self.claim()
        self.assertNotEqual(other['id'], request['id'])
        result = self.attempt(other, IDLE)
        self.assertEqual(result['outcome'], 'blocked')
        self.assertIn('authored no commits', result['detail'])

    def test_a_later_attempt_refuses_a_carried_commit_without_attribution(self):
        request = self.claim()
        self.attempt(request, COMMIT.replace('Authored-with: fixture/fixture', 'no trailer'))
        second = self.attempt(self.again(request), IDLE)
        self.assertEqual(second['outcome'], 'blocked')
        self.assertIn('lacks actual-provider attribution', second['detail'])


if __name__ == '__main__':
    unittest.main()
