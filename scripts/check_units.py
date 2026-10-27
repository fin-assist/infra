"""Check syntax and execute the recurring IAM deployment guard, without deploying."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def analyze(args, expected=0):
    result = subprocess.run(["systemd-analyze", *args], check=False, capture_output=True, text=True, timeout=60)
    diagnostics = result.stdout + result.stderr
    # verify may return 0 even after ignoring an unknown directive (systemd 255).
    unknown = re.search(r"\bUnknown\s+(?:key|lvalue|section|condition)\b", diagnostics, re.I)
    if unknown or result.returncode != expected:
        raise ValueError("systemd-analyze rejected configuration or condition result:\n" + diagnostics)
    if diagnostics:
        print(diagnostics, file=sys.stderr, end="" if diagnostics.endswith("\n") else "\n")


def verify_units(root=Path("stage/systemd")):
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for source in sorted(list(root.glob("*.service")) + list(root.glob("*.timer"))):
            text = source.read_text()
            # CI has no deployed binaries: validate directives, not their installation.
            text = "\n".join("ExecStart=/usr/bin/true" if line.startswith("ExecStart=") else line for line in text.splitlines())
            target = Path(tmp) / source.name
            target.write_text(text + "\n")
            files.append(str(target))
        if not files:
            raise ValueError("no units to verify")
        analyze(["verify", "--man=no", *files])


def verify_refresh_guard(root=Path("stage/systemd")):
    unit = (root / "finassist-iam-refresh.service").read_text()
    guards = re.findall(r"(?m)^(Condition\w+)=.*$", unit)
    if len(guards) != 1:
        raise ValueError("recurring IAM unit must have one executable deployment guard")
    # Evaluate the actual unit directive, not a separately hardcoded correct spelling.
    # systemd-analyze condition does not start units or require systemd as PID 1.
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "binary"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        analyze(["condition", f"{guards[0]}={binary}"])
        binary.chmod(0o600)
        analyze(["condition", f"{guards[0]}={binary}"], expected=1)
        binary.unlink()
        analyze(["condition", f"{guards[0]}={binary}"], expected=1)
        binary.mkdir(mode=0o700)
        analyze(["condition", f"{guards[0]}={binary}"], expected=1)


if __name__ == "__main__":
    verify_units()
    verify_refresh_guard()
