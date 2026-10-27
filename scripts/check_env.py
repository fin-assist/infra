#!/usr/bin/env python3
"""Refuse stage EnvironmentFile overrides of unit-owned identity and credential settings."""
import argparse
from pathlib import Path
import re


def check(service, env_path, unit_root=Path("stage/systemd")):
    if service not in ("user", "bank-statement-data", "analytics", "assistant", "bank-bff"):
        raise ValueError("unknown service")
    units = [unit_root / (service + ".service"), *sorted((unit_root / (service + ".service.d")).glob("*.conf"))]
    owned = set()
    for unit in units:
        for line in unit.read_text().splitlines():
            if line.startswith("Environment="):
                owned.add(line.split("=", 2)[1])
    names = set()
    for line in env_path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in names:
            raise ValueError("use unique plain NAME=value entries")
        if any(c in value for c in ('"', "'", "\\", "\t", " ")):
            raise ValueError("stage env supports only plain unquoted values")
        if key in owned or key.endswith("_FILE") or key in {"DB_PRIMARY_DSN", "DB_REPLICA_DSN", "SERVICE_TOKEN"}:
            raise ValueError("unit-owned identity/credential setting cannot be overridden: " + key)
        if "REPLACE_" in value or ".invalid" in value:
            raise ValueError("replace deployment placeholders before installation")
        names.add(key)
    return names


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service")
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args()
    check(args.service, args.env_file)
