import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("stage", Path(__file__).with_name("stage.py"))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def fixture(root, service="user", sha="a" * 40, run_id=123):
    directory = root / service
    directory.mkdir()
    pin = dict(sha=sha, run_id=run_id, run_attempt=1)
    binary = b"verified-binary"
    digest = hashlib.sha256(binary).hexdigest()
    manifest = dict(schema_version=1, service=service, repository=stage.repository(service), source_sha=sha,
                    run_id=run_id, run_attempt=1, platform="linux/amd64", event="push", ref="refs/heads/main",
                    binary_sha256=digest, modules={"contracts/gen/go": "v0.5.0"})
    run = dict(id=run_id, run_attempt=1, head_sha=sha, event="push", head_branch="main", conclusion="success",
               path=".github/workflows/ci.yml", repository={"full_name": stage.repository(service)})
    (directory / service).write_bytes(binary)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "SHA256SUMS").write_text(f"{digest}  {service}\n")
    (directory / "run.json").write_text(json.dumps(run))
    return pin, directory


class ValidationTests(unittest.TestCase):
    def test_verify_complete_set_and_same_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pins = {name: fixture(root, name)[0] for name in stage.SERVICES}
            lock = root / "set.json"
            lock.write_text(json.dumps(pins))
            self.assertEqual(stage.read_set(lock), pins)
            self.assertEqual(len(stage.verify_set(pins, root)), 6)
            p = root / "analytics/manifest.json"
            m = json.loads(p.read_text())
            m["modules"]["contracts/gen/go"] = "v0.6.0"
            p.write_text(json.dumps(m))
            with self.assertRaisesRegex(ValueError, "contracts"):
                stage.verify_set(pins, root)

    def test_pin_placeholders_or_wrong_fake_source_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pins = {name: fixture(root, name)[0] for name in stage.SERVICES}
            lock = root / "set.json"
            for invalid in [{}, pins | {"fake-llm": pins["fake-llm"] | {"sha": "b" * 40}},
                            pins | {"user": pins["user"] | {"sha": "0" * 40}},
                            pins | {"user": pins["user"] | {"run_id": True}}]:
                lock.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    stage.read_set(lock)

    def test_foreign_pr_failed_run_and_manifest_ref_refused(self):
        for field, value in [("event", "pull_request"), ("head_branch", "feature/x"),
                             ("head_sha", "b" * 40), ("conclusion", "failure"),
                             ("path", ".github/workflows/forged.yml"), ("run_attempt", 2),
                             ("repository", {"full_name": "evil/user"})]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                pin, directory = fixture(Path(tmp))
                evidence = json.loads((directory / "run.json").read_text())
                evidence[field] = value
                (directory / "run.json").write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):
                    stage.verify_artifact("user", pin, directory)
        with tempfile.TemporaryDirectory() as tmp:
            pin, directory = fixture(Path(tmp))
            m = json.loads((directory / "manifest.json").read_text())
            m["ref"] = "refs/pull/1/merge"
            (directory / "manifest.json").write_text(json.dumps(m))
            with self.assertRaises(ValueError):
                stage.verify_artifact("user", pin, directory)

    def test_tampered_binary_checksums_and_symlink_refused(self):
        for mode in ("binary", "checksum", "symlink", "missing"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                pin, directory = fixture(Path(tmp))
                if mode == "binary":
                    (directory / "user").write_bytes(b"tampered")
                elif mode == "checksum":
                    (directory / "SHA256SUMS").write_text("wrong")
                else:
                    (directory / "user").unlink()
                    if mode == "symlink":
                        (directory / "user").symlink_to(directory / "manifest.json")
                with self.assertRaises(ValueError):
                    stage.verify_artifact("user", pin, directory)

    def test_fetch_rejects_untrusted_run_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin, d = fixture(root)
            run = json.loads((d / "run.json").read_text()) | {"event": "pull_request"}
            with patch.object(stage, "run", return_value=json.dumps(run)) as invoke:
                with self.assertRaises(ValueError):
                    stage.fetch({"user": pin}, root / "download")
                self.assertEqual(invoke.call_count, 1)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory()
        self.override = patch.object(stage, "MIGRATION_OVERRIDE_ROOT", Path(self.runtime.name))
        self.override.start()
        self.addCleanup(self.override.stop)
        self.addCleanup(self.runtime.cleanup)

    def test_migration_before_restart_and_executable_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin, directory = fixture(root)
            install = root / "opt"
            calls = []
            with patch.object(stage, "run", side_effect=lambda args: calls.append(args)):
                stage.deploy("user", pin, directory, install)
            self.assertEqual(calls[:4], [["systemctl", "daemon-reload"], ["systemctl", "start", "user-migrate.service"], ["systemctl", "daemon-reload"], ["systemctl", "restart", "user.service"]])
            self.assertIn("/readyz", calls[4][-1])
            self.assertEqual((install / "user/user").stat().st_mode & 0o777, 0o755)

    def test_health_failure_restores_previous_binary_without_down_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin, directory = fixture(root)
            install = root / "opt"
            previous = install / "releases/user" / ("b" * 40)
            previous.mkdir(parents=True)
            (install / "user").symlink_to(previous)
            calls = []
            def run(args):
                calls.append(args)
                if args[0] == "curl":
                    raise RuntimeError("unhealthy")
            with patch.object(stage, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "unhealthy"):
                    stage.deploy("user", pin, directory, install)
            self.assertEqual((install / "user").resolve(), previous.resolve())
            self.assertEqual(calls[-1], ["systemctl", "restart", "user.service"])
            self.assertFalse(any("down" in call for call in calls))

    def test_failed_first_migration_never_selects_or_starts_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin, directory = fixture(root)
            calls = []
            def run(args):
                calls.append(args)
                if args[1] == "start":
                    raise RuntimeError("migration failure")
            with patch.object(stage, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "migration failure"):
                    stage.deploy("user", pin, directory, root / "opt")
            self.assertEqual(calls[-1], ["systemctl", "daemon-reload"])
            self.assertFalse((root / "opt/user").is_symlink())
            self.assertFalse(any(c[1] == "restart" for c in calls))

    def test_migration_uses_immutable_path_and_keeps_automatic_restart_on_old_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin, directory = fixture(root)
            install = root / "opt"
            previous = install / "releases/user" / ("b" * 40)
            previous.mkdir(parents=True)
            (install / "user").symlink_to(previous)
            observed = []
            def run(args):
                if args == ["systemctl", "start", "user-migrate.service"]:
                    self.assertEqual((install / "user").resolve(), previous.resolve())
                    text = (stage.MIGRATION_OVERRIDE_ROOT / "user-migrate.service.d/release.conf").read_text()
                    self.assertIn(str((install / "releases/user" / pin["sha"] / "user").resolve()), text)
                    observed.append(True)
                if args == ["systemctl", "restart", "user.service"]:
                    self.assertEqual((install / "user").resolve().name, pin["sha"])
            with patch.object(stage, "run", side_effect=run):
                stage.deploy("user", pin, directory, install)
            self.assertEqual(observed, [True])
            self.assertFalse((stage.MIGRATION_OVERRIDE_ROOT / "user-migrate.service.d/release.conf").exists())

    def test_symlink_managed_parents_refused_before_writing(self):
        for parent in ("releases", "releases/user"):
            with self.subTest(parent=parent), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pin, directory = fixture(root)
                install = root / "opt"
                (install / parent).parent.mkdir(parents=True, exist_ok=True)
                outside = root / "outside"
                outside.mkdir()
                (install / parent).symlink_to(outside)
                with patch.object(stage, "run") as command:
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        stage.deploy("user", pin, directory, install)
                    command.assert_not_called()
                self.assertEqual(list(outside.iterdir()), [])

    def test_first_health_failure_and_failed_stop_still_unselects_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin, directory = fixture(root)
            def run(args):
                if args[0] == "curl":
                    raise RuntimeError("primary health failure")
                if args[1] == "stop":
                    raise RuntimeError("secondary stop failure")
            with patch.object(stage, "run", side_effect=run), patch("sys.stderr") as stderr:
                with self.assertRaisesRegex(RuntimeError, "primary health failure"):
                    stage.deploy("user", pin, directory, root / "opt")
            self.assertFalse((root / "opt/user").is_symlink())
            self.assertTrue(stderr.write.called)

    def test_migration_error_survives_failed_reload_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin, directory = fixture(root)
            calls = []
            def run(args):
                calls.append(args)
                if args[1] == "start":
                    raise RuntimeError("primary migration failure")
                if args[1] == "daemon-reload" and len(calls) > 1:
                    raise RuntimeError("secondary reload failure")
            with patch.object(stage, "run", side_effect=run), patch("sys.stderr"):
                with self.assertRaisesRegex(RuntimeError, "primary migration failure"):
                    stage.deploy("user", pin, directory, root / "opt")
            self.assertFalse((root / "opt/user").is_symlink())

    def test_bff_has_no_migration_and_old_directory_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin, directory = fixture(root, "bank-bff")
            calls = []
            with patch.object(stage, "run", side_effect=lambda args: calls.append(args)):
                stage.deploy("bank-bff", pin, directory, root / "opt")
            self.assertFalse(any("migrate" in " ".join(c) for c in calls))
            (root / "opt/bank-bff").unlink()
            (root / "opt/bank-bff").mkdir()
            with self.assertRaises(ValueError):
                stage.deploy("bank-bff", pin, directory, root / "opt")

    def test_all_commands_are_bounded(self):
        with patch.object(stage.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")) as command:
            stage.run(["systemctl", "is-active", "user"])
            self.assertEqual(command.call_args.kwargs["timeout"], 180)
