#!/usr/bin/env python3
"""Prepare verified artifacts and invoke the preinstalled stage SSH endpoint."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

import stage
import stage_remote


def deploy(work, pins_text, selected):
    stage_remote.selection(selected)
    pins_path = work / "set.json"
    pins_path.write_text(pins_text)
    pins = stage.read_set(pins_path)
    report = dict(protocol=1, status="failed", phase="fetch", selected=list(stage_remote.selection(selected)), requested=pins)
    report_path = work / "report.json"
    try:
        artifacts = work / "artifacts"
        stage.fetch(pins, artifacts)
        stage.verify_set(pins, artifacts)
        report["phase"] = "ssh"
        report["remote_outcome"] = "unknown"
        run_key = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
        if not re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*", run_key):
            raise ValueError("invalid infra run identity")
        request = dict(protocol=1, selection=selected, implementation=stage_remote.implementation(), run_key=run_key)
        (work / "request.json").write_text(json.dumps(request))
        bundle = work / "bundle.tar"
        with tarfile.open(bundle, "w") as archive:
            for name in ("request.json", "set.json"):
                archive.add(work / name, arcname=name, recursive=False)
            for service in stage.SERVICES:
                for name in (service, "manifest.json", "SHA256SUMS", "run.json"):
                    archive.add(artifacts / service / name, arcname=f"artifacts/{service}/{name}", recursive=False)
        host = os.environ["STAGE_SSH_HOST"]
        port = os.environ.get("STAGE_SSH_PORT", "22")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host) or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("invalid SSH endpoint")
        ssh = work / "ssh"
        ssh.mkdir(mode=0o700)
        key, hosts = ssh / "key", ssh / "known_hosts"
        key.write_text(os.environ["STAGE_SSH_KEY"] + "\n")
        hosts.write_text(os.environ["STAGE_SSH_KNOWN_HOSTS"] + "\n")
        key.chmod(0o600)
        hosts.chmod(0o600)
        try:
            with bundle.open("rb") as stream:
                result = subprocess.run([
                    "ssh", "-F", "/dev/null", "-T", "-i", str(key), "-p", port,
                    "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "UserKnownHostsFile=" + str(hosts), "-o", "GlobalKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                    "root@" + host, "python3 /opt/finassist-infra/scripts/stage_remote.py"],
                    stdin=stream, capture_output=True, timeout=2000)
            # Never propagate SSH banners/errors, host identifiers or server process output.
            if result.returncode not in (0, 1):
                raise RuntimeError("SSH transport failed; remote state unknown")
            remote = json.loads(result.stdout)
            if remote.get("protocol") != 1 or remote.get("status") not in ("success", "failed"):
                raise ValueError("invalid remote report")
            remote.setdefault("requested", pins)
            remote.setdefault("selected", list(stage_remote.selection(selected)))
            report = remote
            report["remote_outcome"] = "reported"
            if result.returncode != 0 or report["status"] != "success":
                raise RuntimeError("remote deployment failed")
        finally:
            key.unlink(missing_ok=True)
            hosts.unlink(missing_ok=True)
    except Exception as error:
        report["status"] = "failed"
        report.setdefault("error", type(error).__name__)
    finally:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report["status"] == "success"


def main():
    # The workflow uploads only this report. Credentials and binaries stay in private temporary storage.
    report_path = Path("stage-deploy-report.json")
    report_path.write_text('{"status":"failed","phase":"input"}\n')
    try:
        with tempfile.TemporaryDirectory(prefix="stage-action-") as tmp:
            work = Path(tmp)
            success = deploy(work, os.environ["STAGE_SET_JSON"], os.environ["STAGE_SELECTION"])
            report_path.write_bytes((work / "report.json").read_bytes())
        return 0 if success else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
