"""Execute the review-origin workflow script against live-API boundary fixtures."""
import json
from pathlib import Path
import re
import subprocess
import textwrap
import unittest


class ClaudeOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/claude.yml').read_text()
        # Exercise the deployed inline script rather than a reimplementation of its policy.
        match = re.search(r'          script: \|\n((?:            .*\n|\n)+)', workflow)
        if match is None:
            raise AssertionError('review origin script is missing')
        cls.script = textwrap.dedent(match.group(1))

    def check_origin(self, payload, pr=None, api_error=False):
        harness = r'''
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const outputs = {};
const calls = [];
const core = { setOutput: (name, value) => { outputs[name] = value; } };
const github = { rest: { pulls: { get: async (args) => {
  calls.push(args);
  if (input.api_error) throw new Error('API unavailable');
  return { data: input.pr };
} } } };
const context = { payload: input.payload, repo: { owner: 'fin-assist', repo: 'infra' } };
(async () => {
  let failed = false;
  try {
    await new (Object.getPrototypeOf(async function(){}).constructor)(
      'core', 'github', 'context', input.script)(core, github, context);
  } catch { failed = true; }
  process.stdout.write(JSON.stringify({ outputs, calls, failed }));
})();
'''
        result = subprocess.run(['node', '-e', harness], text=True, capture_output=True,
                                input=json.dumps(dict(script=self.script, payload=payload, pr=pr, api_error=api_error)),
                                check=True, timeout=10)
        return json.loads(result.stdout)

    @staticmethod
    def pr(state='open', head='fin-assist/infra', base='fin-assist/infra'):
        return dict(state=state, head=dict(repo=dict(full_name=head) if head else None),
                    base=dict(repo=dict(full_name=base) if base else None))

    def test_open_same_repository_pr_from_comment_and_review(self):
        for payload in ({'issue': {'number': 7, 'pull_request': {}}}, {'pull_request': {'number': 7}}):
            with self.subTest(payload=payload):
                result = self.check_origin(payload, self.pr())
                self.assertEqual(result['outputs']['allowed'], 'true')
                self.assertEqual(result['calls'], [{'owner': 'fin-assist', 'repo': 'infra', 'pull_number': 7}])
                self.assertFalse(result['failed'])

    def test_standalone_issue_never_queries_or_enables_write_job(self):
        result = self.check_origin({'issue': {'number': 7}})
        self.assertEqual(result['outputs']['allowed'], 'false')
        self.assertEqual(result['calls'], [])

    def test_live_pr_overrides_stale_payload_and_refuses_fork_closed_or_missing_repo(self):
        payload = {'pull_request': self.pr() | {'number': 7}}
        for pr in (self.pr(head='external/infra'), self.pr(base='external/infra'),
                   self.pr(state='closed'), self.pr(head=None), self.pr(base=None)):
            with self.subTest(pr=pr):
                result = self.check_origin(payload, pr)
                self.assertEqual(result['outputs']['allowed'], 'false')
                self.assertEqual(len(result['calls']), 1)

    def test_api_error_fails_closed(self):
        result = self.check_origin({'pull_request': {'number': 7}}, api_error=True)
        self.assertTrue(result['failed'])
        self.assertEqual(result['outputs']['allowed'], 'false')
