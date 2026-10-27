"""Check unit syntax without pretending that credentials or a VPS exist."""
from pathlib import Path
import subprocess
import tempfile

with tempfile.TemporaryDirectory() as tmp:
    files = []
    for source in Path("stage/systemd").glob("*.service"):
        text = source.read_text()
        # CI has no deployed binaries: validate directives, not their installation.
        text = "\n".join("ExecStart=/usr/bin/true" if line.startswith("ExecStart=") else line for line in text.splitlines())
        target = Path(tmp) / source.name
        target.write_text(text + "\n")
        files.append(str(target))
    if not files:
        raise SystemExit("no units to verify")
    subprocess.run(["systemd-analyze", "verify", "--man=no", *files], check=True, timeout=60)
