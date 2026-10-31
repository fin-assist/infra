"""Deployment protocol tests without credentials, network or live systemd."""
import copy
import fcntl
import io
import json
import shutil
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import stage
import stage_action as action
import stage_remote as remote
from test_stage import fixture


def bundle_fixture(root):
    artifacts = root / "artifacts"
    artifacts.mkdir()
    pins = {name: fixture(artifacts, name)[0] for name in stage.SERVICES}
    (root / "set.json").write_text(json.dumps(pins))
    (root / "request.json").write_text(json.dumps(dict(protocol=1, selection="all", run_key="123-1", implementation=remote.implementation())))
    return pins


def archive_fixture(root):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path in root.rglob("*"):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
    output.seek(0)
    return output


class RemoteTests(unittest.TestCase):
    def test_archive_real_roundtrip_and_malicious_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, dest = root / "source", root / "dest"
            source.mkdir(); dest.mkdir()
            pins = bundle_fixture(source)
            remote.unpack(archive_fixture(source), dest)
            self.assertEqual(stage.read_set(dest / "set.json"), pins)
            stage.verify_set(pins, dest / "artifacts")
        for name, kind, size in [("../escape", tarfile.REGTYPE, 1), ("set.json", tarfile.SYMTYPE, 1),
                                 ("set.json", tarfile.REGTYPE, remote.MAX_FILE + 1)]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as tmp:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as archive:
                    member = tarfile.TarInfo(name); member.type = kind; member.size = size
                    # Only a header is required: validation must precede reading the body.
                    archive.fileobj.write(member.tobuf())
                data.seek(0)
                with self.assertRaises(ValueError):
                    remote.unpack(data, Path(tmp))

    def test_selection_pairs_assistant_and_fake(self):
        self.assertEqual(remote.selection("assistant"), ("fake-llm", "assistant"))
        self.assertEqual(remote.selection("fake-llm"), ("fake-llm", "assistant"))
        with self.assertRaises(ValueError):
            remote.selection("user;id")

    def test_complete_deploy_uses_existing_migration_restart_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pins = bundle_fixture(root)
            install = root / "opt"; runtime = root / "runtime"
            report = dict(status="failed", steps=[])
            calls = []
            with patch.object(remote, "check"), patch.object(stage, "MIGRATION_OVERRIDE_ROOT", runtime), \
                    patch.object(stage, "run", side_effect=lambda args: calls.append(args)), patch.object(stage, "wait_fake"):
                remote.execute(root, report, install)
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["after"], pins)
            self.assertEqual([s["service"] for s in report["steps"]], list(remote.ORDER))
            self.assertLess(calls.index(["systemctl", "start", "user-migrate.service"]),
                            calls.index(["systemctl", "restart", "user.service"]))

    def test_preflight_refuses_contract_checksum_implementation_and_unselected_mismatch(self):
        for defect in ("contract", "checksum", "implementation", "unselected", "env"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); bundle_fixture(root)
                if defect == "contract":
                    p = root / "artifacts/user/manifest.json"; data = json.loads(p.read_text())
                    data["modules"]["contracts/gen/go"] = "v99.0.0"; p.write_text(json.dumps(data))
                elif defect == "checksum":
                    (root / "artifacts/user/user").write_text("corrupt")
                elif defect in ("implementation", "unselected"):
                    p = root / "request.json"; data = json.loads(p.read_text())
                    if defect == "implementation": data["implementation"] = {}
                    else: data["selection"] = "user"
                    p.write_text(json.dumps(data))
                with patch.object(stage, "deploy") as deploy, patch.object(remote, "check", side_effect=ValueError("env") if defect == "env" else None):
                    with self.assertRaises(ValueError):
                        remote.execute(root, dict(steps=[]), root / "opt")
                    deploy.assert_not_called()
                self.assertFalse((root / "opt").exists())

    def test_partial_deploy_requires_and_preserves_actual_unselected_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pins = bundle_fixture(root)
            install = root / "opt"
            with patch.object(remote, "check"), patch.object(stage, "MIGRATION_OVERRIDE_ROOT", root / "runtime"), \
                    patch.object(stage, "run"), patch.object(stage, "wait_fake"):
                remote.execute(root, dict(steps=[]), install)
                request = json.loads((root / "request.json").read_text()); request["selection"] = "user"
                (root / "request.json").write_text(json.dumps(request))
                report = dict(steps=[])
                remote.execute(root, report, install)
            self.assertEqual(report["after"], pins)
            self.assertEqual([s["service"] for s in report["steps"]], ["user"])

    def test_readiness_failure_reports_rollback_and_stops_remaining_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pins = bundle_fixture(root); install = root / "opt"
            with patch.object(remote, "check"), patch.object(stage, "MIGRATION_OVERRIDE_ROOT", root / "runtime"), \
                    patch.object(stage, "run"), patch.object(stage, "wait_fake"):
                remote.execute(root, dict(steps=[]), install)
            # A second verified release fails readiness after its migration and switch.
            old = copy.deepcopy(pins)
            shutil.rmtree(root / "artifacts")
            (root / "artifacts").mkdir()
            pins = {name: fixture(root / "artifacts", name, sha="b" * 40)[0] for name in stage.SERVICES}
            (root / "set.json").write_text(json.dumps(pins))
            calls = []
            def command(args):
                calls.append(args)
                if args[0] == "curl": raise RuntimeError("private output must not enter report")
            report = dict(status="failed", steps=[])
            with patch.object(remote, "check"), patch.object(stage, "MIGRATION_OVERRIDE_ROOT", root / "runtime"), \
                    patch.object(stage, "run", side_effect=command):
                with self.assertRaises(RuntimeError): remote.execute(root, report, install)
            self.assertEqual(report["after"], old)
            self.assertEqual(len(report["steps"]), 1)
            self.assertEqual(report["steps"][0]["phase"], "readiness")
            self.assertEqual(report["steps"][0]["rollback"], "previous-restarted-unverified")
            self.assertNotIn("private output", json.dumps(report))
            self.assertFalse(any("down" in call for call in calls))

    def test_new_attempt_cannot_reuse_old_immutable_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pins = bundle_fixture(root)
            install = root / "opt"
            with patch.object(remote, "check"), patch.object(stage, "MIGRATION_OVERRIDE_ROOT", root / "runtime"), \
                    patch.object(stage, "run"), patch.object(stage, "wait_fake"):
                remote.execute(root, dict(steps=[]), install)
            pins["user"]["run_id"] = 124
            (root / "set.json").write_text(json.dumps(pins))
            for filename, key in (("manifest.json", "run_id"), ("run.json", "id")):
                path = root / "artifacts/user" / filename
                data = json.loads(path.read_text())
                data[key] = 124
                path.write_text(json.dumps(data))
            with patch.object(stage, "deploy") as deploy:
                with self.assertRaisesRegex(ValueError, "provenance"):
                    remote.execute(root, dict(steps=[]), install)
                deploy.assert_not_called()

    def test_migration_failure_is_reported_without_selecting_new_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_fixture(root)
            report = dict(status="failed", steps=[])
            def command(args):
                if args == ["systemctl", "start", "user-migrate.service"]:
                    raise RuntimeError("migration failed")
            with patch.object(remote, "check"), patch.object(stage, "MIGRATION_OVERRIDE_ROOT", root / "runtime"), \
                    patch.object(stage, "run", side_effect=command):
                with self.assertRaises(RuntimeError):
                    remote.execute(root, report, root / "opt")
            self.assertEqual(report["steps"][0]["phase"], "migration")
            self.assertEqual(report["steps"][0]["rollback"], "not-needed")
            self.assertEqual(report["after"], report["before"])
            self.assertEqual(len(report["steps"]), 1)

    def test_real_cross_process_lock_conflicts_with_manual_deploy_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "deploy.lock"
            with lock.open("w") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                code = "import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                result = subprocess.run(["python3", "-c", code, str(lock)], capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"BlockingIOError", result.stderr)


class ActionTests(unittest.TestCase):
    def test_full_ci_requires_all_successful_gates_and_completed_jobs(self):
        jobs = [{"name": n, "status": "completed", "conclusion": "success"} for n in ("go", "lint", "image")]
        stage.check_jobs([{"jobs": jobs}])
        for changed in (jobs[:2], jobs + [jobs[0]], jobs + [{"name": "extra", "status": "in_progress"}],
                        [j | {"conclusion": "failure"} for j in jobs]):
            with self.assertRaises(ValueError): stage.check_jobs([{"jobs": changed}])

    def test_fetch_rechecks_attempt_after_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pin, directory = fixture(root)
            evidence = json.loads((directory / "run.json").read_text())
            jobs = [{"jobs": [{"name": n, "status": "completed", "conclusion": "success"} for n in ("go", "lint", "image")]}]
            def command(args):
                if args[1] == "run":
                    shutil.copytree(directory, root / "download/user")
                    return ""
                if "--slurp" in args: return json.dumps(jobs)
                command.count += 1
                return json.dumps(evidence if command.count == 1 else evidence | {"run_attempt": 2})
            command.count = 0
            with patch.object(stage, "run", side_effect=command):
                with self.assertRaises(ValueError): stage.fetch({"user": pin}, root / "download")

    def test_ssh_is_strict_and_failures_leave_safe_report_and_remove_key(self):
        for outcome in ("success", "deploy-failed", "transport-failed", "timeout"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); pins = bundle_fixture(root)
                # deploy owns these paths and fetch creates the artifact directory.
                shutil.rmtree(root / "artifacts")
                def fetch(p, d):
                    d.mkdir()
                    for name in stage.SERVICES: fixture(d, name)
                def ssh(args, **kwargs):
                    self.assertIn("StrictHostKeyChecking=yes", args)
                    self.assertIn("GlobalKnownHostsFile=/dev/null", args)
                    self.assertEqual(args[-1], "python3 /opt/finassist-infra/scripts/stage_remote.py")
                    self.assertEqual(kwargs["timeout"], 2000)
                    if outcome == "timeout": raise subprocess.TimeoutExpired(args, 2000)
                    status = "success" if outcome == "success" else "failed"
                    return subprocess.CompletedProcess(args, 255 if outcome == "transport-failed" else (0 if status == "success" else 1),
                                                       json.dumps(dict(protocol=1, status=status)).encode(), b"SENSITIVE ssh error")
                env = dict(GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1", STAGE_SSH_HOST="stage.example.invalid",
                           STAGE_SSH_KEY="SENSITIVE key", STAGE_SSH_KNOWN_HOSTS="SENSITIVE hosts")
                with patch.dict(action.os.environ, env), patch.object(stage, "fetch", side_effect=fetch), patch.object(action.subprocess, "run", side_effect=ssh):
                    self.assertEqual(action.deploy(root, json.dumps(pins), "all"), outcome == "success")
                self.assertFalse((root / "ssh/key").exists())
                self.assertNotIn("SENSITIVE", (root / "report.json").read_text())

    def test_invalid_input_or_failed_fetch_never_connects_to_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pins = bundle_fixture(root)
            with patch.object(stage, "fetch", side_effect=ValueError("private error")), patch.object(action.subprocess, "run") as ssh:
                self.assertFalse(action.deploy(root, json.dumps(pins), "all"))
                ssh.assert_not_called()
            self.assertNotIn("private error", (root / "report.json").read_text())


class EndpointTests(unittest.TestCase):
    def invoke(self, root, payload):
        # Real protocol/stdin/stdout/persistence and flock, with only privileged service commands faked.
        source = str(Path(__file__).resolve().parent)
        code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import stage_remote as remote
root = Path(sys.argv[2])
remote.LOCK = root / 'lock'
remote.REPORTS = root / 'reports'
remote.os.geteuid = lambda: 0
remote.sys.platform = 'linux'
execute = remote.execute
remote.execute = lambda d, r: execute(d, r, root / 'opt')
remote.check = lambda *a: None
remote.stage.MIGRATION_OVERRIDE_ROOT = root / 'runtime'
remote.stage.run = lambda *a: None
remote.stage.wait_fake = lambda: None
sys.exit(remote.main())
"""
        return subprocess.run(["python3", "-c", code, source, str(root)], input=payload,
                              capture_output=True, timeout=15)

    def test_endpoint_report_persistence_duplicate_and_lock_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / "source"; source.mkdir()
            pins = bundle_fixture(source)
            payload = archive_fixture(source).getvalue()
            result = self.invoke(root, payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["after"], pins)
            persisted = root / "reports/123-1.json"
            self.assertEqual(json.loads(persisted.read_text()), report)
            self.assertEqual(persisted.stat().st_mode & 0o777, 0o600)
            again = self.invoke(root, payload)
            self.assertEqual(again.returncode, 1)
            self.assertEqual(json.loads(again.stdout)["error"], "FileExistsError")
            self.assertEqual(json.loads(persisted.read_text()), report)
            with (root / "lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = self.invoke(root, payload)
            self.assertEqual(busy.returncode, 1)
            self.assertEqual(json.loads(busy.stdout)["error"], "BlockingIOError")

    def test_report_persistence_failure_cannot_return_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            bundle_fixture(source)
            (root / "reports").mkdir()
            (root / "reports/123-1.tmp").write_text("interrupted report")
            result = self.invoke(root, archive_fixture(source).getvalue())
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["error"], "FileExistsError")
            self.assertEqual(report["phase"], "complete")
