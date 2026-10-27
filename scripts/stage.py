#!/usr/bin/env python3
"""Validate a pinned stage set and deploy one service at a time (FIN-149)."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

SERVICES = ("user", "bank-statement-data", "analytics", "assistant", "bank-bff", "fake-llm")
HEALTH = {"user": 19091, "bank-statement-data": 19092, "analytics": 19093,
          "assistant": 19094, "bank-bff": 9091}


def run(args):
    """Bound external operations; callers never pass secret material as arguments."""
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=180).stdout


def read_set(path):
    data = json.loads(path.read_text())
    if set(data) != set(SERVICES):
        raise ValueError("set must pin exactly five services and fake-llm")
    for name, pin in data.items():
        if set(pin) != {"sha", "run_id", "run_attempt"}:
            raise ValueError("pin requires sha, run_id, run_attempt")
        if not isinstance(pin["sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", pin["sha"]) or pin["sha"] == "0" * 40:
            raise ValueError("expected a real full SHA")
        if any(type(pin[k]) is not int or pin[k] < 1 for k in ("run_id", "run_attempt")):
            raise ValueError("expected positive Actions run and attempt")
    if data["fake-llm"] != data["assistant"]:
        raise ValueError("fake-llm must come from the same assistant run")
    return data


def repository(service):
    return "fin-assist/" + ("assistant" if service == "fake-llm" else service)


def verify_artifact(service, pin, directory):
    if service not in SERVICES or directory.is_symlink() or not directory.is_dir():
        raise ValueError("invalid artifact directory")
    for filename in (service, "manifest.json", "SHA256SUMS", "run.json"):
        p = directory / filename
        if p.is_symlink() or not p.is_file():
            raise ValueError("artifact requires regular files")
    manifest = json.loads((directory / "manifest.json").read_text())
    expected = dict(schema_version=1, service=service, repository=repository(service), source_sha=pin["sha"],
                    run_id=pin["run_id"], run_attempt=pin["run_attempt"], platform="linux/amd64",
                    event="push", ref="refs/heads/main")
    if any(manifest.get(k) != v for k, v in expected.items()):
        raise ValueError("artifact provenance mismatch")
    evidence = json.loads((directory / "run.json").read_text())
    check_run(service, pin, evidence)
    binary = directory / service
    hasher = hashlib.sha256()
    with binary.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if not binary.stat().st_size or manifest.get("binary_sha256") != digest:
        raise ValueError("binary checksum mismatch")
    if (directory / "SHA256SUMS").read_text() != f"{digest}  {service}\n":
        raise ValueError("checksum file mismatch")
    return manifest


def check_run(service, pin, evidence):
    if (evidence.get("head_sha") != pin["sha"] or evidence.get("id") != pin["run_id"]
            or evidence.get("run_attempt") != pin["run_attempt"] or evidence.get("event") != "push"
            or evidence.get("head_branch") != "main" or evidence.get("conclusion") != "success"
            or evidence.get("path") != ".github/workflows/ci.yml"
            or evidence.get("repository", {}).get("full_name") != repository(service)):
        raise ValueError("only successful source CI push/main runs are eligible")


def fetch(pins, destination):
    destination.mkdir(parents=True, exist_ok=False)
    for service, pin in pins.items():
        repo = repository(service)
        evidence = json.loads(run(["gh", "api", f"repos/{repo}/actions/runs/{pin['run_id']}"]))
        check_run(service, pin, evidence)
        directory = destination / service
        run(["gh", "run", "download", str(pin["run_id"]), "--repo", repo,
             "--name", f"stage-{service}-{pin['sha']}-linux-amd64", "--dir", str(directory)])
        (directory / "run.json").write_text(json.dumps(evidence, indent=2) + "\n")
        verify_artifact(service, pin, directory)


def verify_set(pins, directory):
    manifests = {name: verify_artifact(name, pins[name], directory / name) for name in SERVICES}
    versions = {m.get("modules", {}).get("contracts/gen/go") for name, m in manifests.items() if name != "fake-llm"}
    if len(versions) != 1 or None in versions:
        raise ValueError("five services must pin one contracts/gen/go version")
    return manifests


def atomic_link(path, target):
    temporary = path.with_name(path.name + ".new")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("stale deployment link needs investigation")
    temporary.symlink_to(target)
    temporary.replace(path)


MIGRATION_OVERRIDE_ROOT = Path("/run/systemd/system")


def managed_directory(root, *parts):
    """Reject symlinks before following any managed writable directory."""
    if root.is_symlink():
        raise ValueError("managed root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    current = root.resolve()
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("managed directory must not be a symlink")
        current.mkdir(exist_ok=True)
    return current


def migrate(service, target):
    """Use the new immutable executable while automatic restarts still select the old one."""
    directory = managed_directory(MIGRATION_OVERRIDE_ROOT, service + "-migrate.service.d")
    override = directory / "release.conf"
    # Never overwrite an operator override or a stale interrupted migration selection.
    with override.open("x") as stream:
        stream.write(f"[Service]\nExecStart=\nExecStart={target / service} migrate up\n")
    try:
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "start", service + "-migrate.service"])
    except BaseException:
        try:
            override.unlink()
            run(["systemctl", "daemon-reload"])
        except Exception as cleanup_error:
            print(f"migration override cleanup failed: {cleanup_error}", file=sys.stderr)
        raise
    else:
        override.unlink()
        run(["systemctl", "daemon-reload"])


def deploy(service, pin, directory, root=Path("/opt/finassist")):
    """Install a verified immutable release; never downgrade or roll back the database."""
    manifest = verify_artifact(service, pin, directory)
    root = managed_directory(root)
    releases = managed_directory(root, "releases", service)
    target = releases / pin["sha"]
    current = root / service
    if current.exists() and not current.is_symlink():
        raise ValueError("current release must be a managed symlink")
    previous = current.resolve(strict=True) if current.is_symlink() else None
    if previous is not None and previous.parent != releases:
        raise ValueError("current release escaped managed directory")
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix=".install-", dir=releases) as tmp:
            staging = Path(tmp) / "release"
            staging.mkdir(mode=0o755)
            shutil.copyfile(directory / service, staging / service)
            (staging / service).chmod(0o755)
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            staging.rename(target)
    else:
        if target.is_symlink() or not target.is_dir():
            raise ValueError("existing release is not a regular directory")
        if (target / service).is_symlink() or not (target / service).is_file():
            raise ValueError("installed binary must be a regular file")
        if hashlib.sha256((target / service).read_bytes()).hexdigest() != manifest["binary_sha256"]:
            raise ValueError("immutable release checksum mismatch")
    if service not in ("bank-bff", "fake-llm"):
        migrate(service, target)
    atomic_link(current, target)
    try:
        run(["systemctl", "restart", service + ".service"])
        # curl retries are bounded and do not print response bodies or credentials.
        if service in HEALTH:
            run(["curl", "--fail", "--silent", "--show-error", "--retry", "20", "--retry-all-errors",
                 "--retry-delay", "1", "--max-time", "2", "--output", "/dev/null",
                 f"http://127.0.0.1:{HEALTH[service]}/readyz"])
        else:
            run(["systemctl", "is-active", "--quiet", "fake-llm.service"])
    except Exception:
        if previous is not None:
            atomic_link(current, previous)
            cleanup_command = ["systemctl", "restart", service + ".service"]
        else:
            # Remove the selection before stop: even a failed stop cannot select this release again.
            current.unlink()
            cleanup_command = ["systemctl", "stop", service + ".service"]
        try:
            run(cleanup_command)
        except Exception as cleanup_error:
            print(f"rollback service operation failed: {cleanup_error}", file=sys.stderr)
        raise
    if previous is not None and previous != target:
        history = managed_directory(root, "previous")
        atomic_link(history / service, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "verify", "deploy"])
    parser.add_argument("pins", type=Path)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--service", choices=SERVICES)
    args = parser.parse_args()
    pins = read_set(args.pins)
    if args.command == "fetch":
        fetch(pins, args.artifacts)
    else:
        verify_set(pins, args.artifacts)
        if args.command == "deploy":
            if os.geteuid() != 0 or os.uname().sysname != "Linux" or not args.service:
                parser.error("deploy requires Linux root and --service")
            from check_env import check
            if args.service != "fake-llm":
                check(args.service, Path("/etc/finassist") / args.service / "env",
                      Path(__file__).resolve().parents[1] / "stage/systemd")
            # Set and artifacts must be operator-controlled, copied to root-owned private staging.
            with Path("/run/finassist-deploy.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                deploy(args.service, pins[args.service], args.artifacts / args.service)


if __name__ == "__main__":
    main()
