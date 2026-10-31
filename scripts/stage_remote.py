#!/usr/bin/env python3
"""Trusted, preinstalled SSH endpoint for manual stage deployments (FIN-410)."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tarfile
import tempfile

import stage
from check_env import check

ORDER = ("user", "analytics", "bank-statement-data", "fake-llm", "assistant", "bank-bff")
IMPLEMENTATION = ("scripts/stage_remote.py", "scripts/stage.py", "scripts/check_env.py")
MAX_FILE = 160 * 1024 * 1024
MAX_BUNDLE = 800 * 1024 * 1024
REPORTS = Path("/var/lib/finassist-deploy/reports")
LOCK = Path("/run/finassist-deploy.lock")
ROOT = Path("/opt/finassist")
SOURCE = Path(__file__).resolve().parents[1]


def selection(value):
    if value == "all":
        return ORDER
    if value in ("assistant", "fake-llm"):
        return ("fake-llm", "assistant")
    if value not in stage.SERVICES:
        raise ValueError("unsupported selection")
    return (value,)


def implementation(source=SOURCE):
    return {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in IMPLEMENTATION}


def unpack(stream, destination):
    """Extract only the exact regular-file protocol; never use extractall on SSH input."""
    allowed = {"request.json", "set.json"} | {
        f"artifacts/{service}/{name}" for service in stage.SERVICES
        for name in (service, "manifest.json", "SHA256SUMS", "run.json")}
    seen, total = set(), 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            total += member.size
            if (member.name not in allowed or member.name in seen or not member.isfile()
                    or member.size < 1 or member.size > MAX_FILE or total > MAX_BUNDLE):
                raise ValueError("invalid deployment bundle")
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.extractfile(member) as src, target.open("xb") as dst:
                remaining = member.size
                while remaining:
                    block = src.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("truncated deployment bundle")
                    dst.write(block)
                    remaining -= len(block)
            seen.add(member.name)
    if seen != allowed:
        raise ValueError("incomplete deployment bundle")


def installed(root=ROOT):
    """Report only validated binary identities, never env, paths or process output."""
    root = root.resolve()
    versions = {}
    for service in stage.SERVICES:
        current = root / service
        if not current.is_symlink():
            if current.exists():
                raise ValueError("unmanaged installed service")
            versions[service] = None
            continue
        target = current.resolve(strict=True)
        if target.parent != root / "releases" / service or not re.fullmatch(r"[0-9a-f]{40}", target.name):
            raise ValueError("unmanaged installed release")
        binary, manifest_path = target / service, target / "manifest.json"
        if binary.is_symlink() or manifest_path.is_symlink():
            raise ValueError("invalid installed release")
        manifest = json.loads(manifest_path.read_text())
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        expected = dict(service=service, repository=stage.repository(service), platform="linux/amd64",
                        event="push", ref="refs/heads/main", source_sha=target.name, binary_sha256=digest)
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise ValueError("installed release integrity mismatch")
        pin = {key: manifest.get(key) for key in ("run_id", "run_attempt")}
        pin["sha"] = manifest["source_sha"]
        # Return only validated pin fields, not arbitrary manifest content.
        if any(type(pin[k]) is not int or pin[k] < 1 for k in ("run_id", "run_attempt")):
            raise ValueError("invalid installed identity")
        versions[service] = pin
    return versions


def execute(directory, report, root=ROOT):
    report["phase"] = "implementation"
    request = json.loads((directory / "request.json").read_text())
    if request.get("protocol") != 1 or request.get("implementation") != implementation():
        raise ValueError("preinstalled infra implementation differs from workflow")
    chosen = selection(request["selection"])
    report["phase"] = "artifact-verification"
    pins = stage.read_set(directory / "set.json")
    stage.verify_set(pins, directory / "artifacts")
    report.update(selected=list(chosen), requested=pins, verification="passed")
    report["phase"] = "installed-set"
    before = installed(root)
    report["before"] = before
    if any(before[name] != pins[name] for name in stage.SERVICES if name not in chosen):
        raise ValueError("unselected installed services must match the requested set")
    # Immutable paths also pin provenance: a new attempt cannot silently reuse an older manifest.
    for service in chosen:
        target = root / "releases" / service / pins[service]["sha"]
        if target.is_symlink():
            raise ValueError("invalid existing immutable release")
        if target.exists():
            candidate = directory / "artifacts" / service
            binary, manifest_path = target / service, target / "manifest.json"
            if (not target.is_dir() or binary.is_symlink() or manifest_path.is_symlink()
                    or not binary.is_file() or not manifest_path.is_file()):
                raise ValueError("invalid immutable release")
            if json.loads(manifest_path.read_text()) != json.loads((candidate / "manifest.json").read_text()):
                raise ValueError("immutable release provenance differs")
            if hashlib.sha256(binary.read_bytes()).hexdigest() != json.loads(manifest_path.read_text())["binary_sha256"]:
                raise ValueError("immutable release checksum differs")
    # Validate every selected env before the first migration or service restart.
    report["phase"] = "env-preflight"
    for service in chosen:
        if service != "fake-llm":
            check(service, Path("/etc/finassist") / service / "env", SOURCE / "stage/systemd")
    report["preflight"] = "passed"
    report["phase"] = "deployment"
    try:
        for service in chosen:
            result = {"service": service, "status": "failed"}
            report["steps"].append(result)
            stage.deploy(service, pins[service], directory / "artifacts" / service, root, result)
            result["status"] = "success"
    finally:
        report["after"] = installed(root)
    if report["after"] != pins:
        raise ValueError("installed set differs from requested identities")
    report["phase"] = "complete"
    report["status"] = "success"


def emit(report):
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    sys.stdout.flush()


def main():
    os.umask(0o077)
    report = dict(protocol=1, status="failed", verification="not-completed", steps=[])
    try:
        if os.geteuid() != 0 or sys.platform != "linux":
            raise ValueError("requires Linux root")
        # Ignore SSH disconnects; finish the bounded operation and persist its report.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        def deadline(*_):
            raise TimeoutError("deployment deadline")
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(1800)
        # One lock for upload, whole-set preflight and every service. Manual stage.py uses it too.
        with LOCK.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with tempfile.TemporaryDirectory(prefix="finassist-deploy-") as tmp:
                directory = Path(tmp)
                unpack(sys.stdin.buffer, directory)
                request = json.loads((directory / "request.json").read_text())
                run_key = request.get("run_key", "")
                if not re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*", run_key):
                    raise ValueError("invalid infra run identity")
                report["run_key"] = run_key
                reports = stage.managed_directory(REPORTS)
                destination = reports / (run_key + ".json")
                # Reserve the identity so a repeated transport cannot deploy twice.
                with destination.open("x") as stream:
                    stream.write(json.dumps(report) + "\n")
                try:
                    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                        execute(directory, report)
                except Exception as error:
                    report["error"] = type(error).__name__
                finally:
                    temporary = destination.with_suffix(".tmp")
                    with temporary.open("x") as stream:
                        json.dump(report, stream, sort_keys=True)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(destination)
    except Exception as error:
        report["status"] = "failed"
        report["error"] = type(error).__name__
    finally:
        signal.alarm(0)
    emit(report)
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
