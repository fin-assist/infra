from contextlib import redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("check_units", Path(__file__).with_name("check_units.py"))
units = importlib.util.module_from_spec(spec)
spec.loader.exec_module(units)


class UnitVerificationTests(unittest.TestCase):
    def test_unknown_ignored_directive_fails_even_with_exit_zero(self):
        for diagnostic in ["Unknown key name 'ConditionPathIsExecutable' in section 'Unit', ignoring.",
                           "Unknown lvalue 'Typo' in section 'Service'", "Unknown section 'Typos'. Ignoring."]:
            with self.subTest(diagnostic=diagnostic), patch.object(units.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, stdout="", stderr=diagnostic)):
                with self.assertRaisesRegex(ValueError, "rejected configuration"):
                    units.analyze(["verify", "synthetic.service"])

    def test_other_diagnostics_remain_visible_and_nonzero_verify_fails(self):
        warning = "synthetic.service: warning: Document path is not available.\n"
        output = io.StringIO()
        with patch.object(units.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", warning)) as command, \
                redirect_stderr(output):
            units.analyze(["verify", "synthetic.service"])
        self.assertEqual(output.getvalue(), warning)
        self.assertEqual(command.call_args.kwargs["timeout"], 60)
        with patch.object(units.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "bad unit")):
            with self.assertRaises(ValueError):
                units.analyze(["verify", "synthetic.service"])

    def test_synthetic_execstart_and_timer_survive_stricter_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "synthetic.service").write_text("[Service]\nExecStart=/not-installed/binary serve\n")
            (root / "synthetic.timer").write_text("[Timer]\nOnActiveSec=4h\n")
            def analyze(args):
                self.assertEqual(args[:2], ["verify", "--man=no"])
                self.assertEqual({Path(p).suffix for p in args[2:]}, {".service", ".timer"})
                for p in args[2:]:
                    text = Path(p).read_text()
                    if p.endswith(".service"):
                        self.assertIn("ExecStart=/usr/bin/true", text)
                        self.assertNotIn("/not-installed/", text)
                    else:
                        self.assertIn("OnActiveSec=4h", text)
            with patch.object(units, "analyze", side_effect=analyze) as command:
                units.verify_units(root)
            command.assert_called_once()

    def test_guard_checks_executable_nonexecutable_missing_and_directory(self):
        states = []
        def analyze(args, expected=0):
            self.assertEqual(args[0], "condition")
            name, value = args[1].split("=", 1)
            self.assertEqual(name, "ConditionFileIsExecutable")
            path = Path(value)
            executable = path.is_file() and os.access(path, os.X_OK)
            states.append((path.exists(), path.is_dir(), executable, expected))
            self.assertEqual(expected, 0 if executable else 1)
        with patch.object(units, "analyze", side_effect=analyze):
            units.verify_refresh_guard()
        self.assertEqual(states, [(True, False, True, 0), (True, False, False, 1),
                                  (False, False, False, 1), (True, True, False, 1)])

    def test_guard_verification_uses_actual_directive_and_refuses_ignored_condition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "finassist-iam-refresh.service").write_text("[Unit]\nConditionPathIsExecutable=/not-installed\n")
            with patch.object(units.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "",
                    "Unknown condition ConditionPathIsExecutable")) as command:
                with self.assertRaises(ValueError):
                    units.verify_refresh_guard(root)
            self.assertIn("ConditionPathIsExecutable=", command.call_args.args[0][-1])
