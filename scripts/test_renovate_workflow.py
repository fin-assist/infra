"""Execute the workflow's real activation gate before privileged job admission."""
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


class WorkflowGateTests(unittest.TestCase):
    def gate_script(self):
        workflow = (Path(__file__).resolve().parents[1]
                    / '.github/workflows/renovate-policy.yml').read_text()
        gate = re.search(r'^  gate:\n(.*?)(?=^  reconcile:)', workflow, re.M | re.S)
        self.assertIsNotNone(gate, 'No independent gate protects Environment admission for TRUE')
        # The gate must not acquire privileges before it decides whether to run.
        self.assertNotIn('secrets.', gate[1])
        self.assertNotRegex(gate[1], re.compile(r'^    environment:', re.M))
        self.assertIn('    permissions: {}\n', gate[1])
        self.assertIn('RENOVATE_POLICY_ENABLED: ${{ vars.RENOVATE_POLICY_ENABLED }}', gate[1])
        self.assertIn('    needs: gate\n', workflow)
        self.assertIn("needs.gate.outputs.enabled == 'true'", workflow)
        run = re.search(r'^        run: \|\n((?:          .*\n|\n)+)', gate[1], re.M)
        self.assertIsNotNone(run, 'Workflow gate must contain an executable shell block')
        return textwrap.dedent(run[1])

    def assert_gate(self, value, expected):
        script = self.gate_script()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            env = {'GITHUB_OUTPUT': str(output)}
            if value is not None:
                env['RENOVATE_POLICY_ENABLED'] = value
            result = subprocess.run(['/bin/bash', '-e', '-o', 'pipefail', '-c', script],
                                    env=env, cwd=directory, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), f'enabled={expected}\n')

    def test_uppercase_true_does_not_admit_privileged_job(self):
        self.assert_gate('TRUE', 'false')

    def test_only_exact_lowercase_true_admits_privileged_job(self):
        for value in (None, '', 'false', 'TRUE', 'True', 'true', '1'):
            with self.subTest(value=value):
                self.assert_gate(value, 'true' if value == 'true' else 'false')

    def test_value_is_data_and_cannot_inject_shell_commands_or_outputs(self):
        self.assert_gate('true\nenabled=true\n$(exit 1)', 'false')


class InfraWorkflowContractTests(unittest.TestCase):
    def test_production_config_and_trigger_select_validation_not_deployment(self):
        import json
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / '.github/renovate-policy.json').read_text())
        self.assertEqual(config['ciWorkflow'], 'stage.yml')
        self.assertEqual(config['ciChecks'], ['stage'])
        self.assertEqual(config['requiredChecks'],
                         ['stage', 'Renovate policy tests', 'Renovate runtime contract'])
        workflow = (root / '.github/workflows/renovate-policy.yml').read_text()
        self.assertIn('workflows: [Stage configuration, Renovate policy tests]', workflow)
        self.assertNotIn('stage-deploy', workflow)
        self.assertNotRegex(workflow, r'(?m)^    environment: stage$')
        self.assertNotIn('STAGE_SSH', workflow)
        self.assertIn('          ref: main\n', workflow)
        self.assertIn('          persist-credentials: false\n', workflow)

    def test_baseline_preserves_existing_recommended_policy(self):
        import json
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(json.loads((root / 'renovate-base.json').read_text()),
                         {'extends': ['config:recommended']})
        config = json.loads((root / 'renovate.json').read_text())
        self.assertEqual(config['extends'],
                         ['local>fin-assist/infra:base#renovate-policy',
                          'local>fin-assist/infra:policy#renovate-policy'])
        self.assertFalse(config['automerge'])
        self.assertTrue(config['dependencyDashboard'])
