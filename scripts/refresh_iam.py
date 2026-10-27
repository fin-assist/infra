#!/usr/bin/env python3
"""Root-only external IAM credential refresh; never prints provider responses or keys."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

IAM_URL = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
MIN_VALIDITY = 5 * 60 * 60  # Four-hour timer avoids interrupting imports every 30 minutes.
MAX_BYTES = 65536
SERVICE = "bank-statement-data.service"


class RefreshError(Exception):
    """Only constant, credential-free messages may cross the CLI boundary."""


@contextmanager
def directory(path, private=False):
    """Walk by descriptor: no symlinks, writable ancestors, or untrusted owners."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RefreshError("untrusted credential directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise RefreshError("untrusted credential directory")
        if private and (os.fstat(fd).st_uid != os.geteuid() or stat.S_IMODE(os.fstat(fd).st_mode) != 0o700):
            raise RefreshError("credential directory must be owner-only")
        yield fd
    finally:
        os.close(fd)


def regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise RefreshError("credential must be a regular owner-only file")


def read_key(path):
    path = Path(path)
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            regular(os.fstat(source.fileno()))
            data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RefreshError("authorized key is too large")
    try:
        value = json.loads(data)
        for field in ("id", "service_account_id"):
            if not isinstance(value[field], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value[field]):
                raise ValueError()
        pem = value["private_key"]
        if not isinstance(pem, str) or len(pem) > 16384 or "-----BEGIN PRIVATE KEY-----" not in pem:
            raise ValueError()
        pem.encode("ascii")
        return value
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise RefreshError("invalid authorized key") from None


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def signed_jwt(key, now):
    header = {"typ": "JWT", "alg": "PS256", "kid": key["id"]}
    claims = {"iss": key["service_account_id"], "aud": IAM_URL, "iat": int(now), "exp": int(now) + 300}
    payload = b".".join(encoded(json.dumps(x, separators=(",", ":")).encode()) for x in (header, claims))
    # Message is bounded (<1 KiB). Private PEM goes to stdin, never argv/env/a temporary file.
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = None
        result = subprocess.run([
            "/usr/bin/openssl", "dgst", "-sha256", "-sign", "/dev/stdin",
            "-sigopt", "rsa_padding_mode:pss", "-sigopt", "rsa_pss_saltlen:32", f"/dev/fd/{read_fd}",
        ], input=key["private_key"].encode("ascii"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            pass_fds=(read_fd,), timeout=10, check=True)
        if not 256 <= len(result.stdout) <= 1024:
            raise RefreshError("invalid RSA signature")
        return (payload + b"." + encoded(result.stdout)).decode("ascii")
    except (OSError, subprocess.SubprocessError):
        raise RefreshError("IAM signing failed") from None
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def exchange(jwt, now, opener=None, sleep=time.sleep):
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    body = json.dumps({"jwt": jwt}, separators=(",", ":")).encode()
    for attempt in range(3):
        request = urllib.request.Request(IAM_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with opener.open(request, timeout=15) as response:
                if response.status != 200:
                    raise RefreshError("IAM exchange failed")
                deadline = time.monotonic() + 15
                data = bytearray()
                while len(data) <= MAX_BYTES:
                    if time.monotonic() > deadline:
                        raise RefreshError("IAM response timed out")
                    chunk = response.read1(MAX_BYTES + 1 - len(data))
                    if not chunk:
                        break
                    data.extend(chunk)
            break
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise RefreshError("IAM exchange failed") from None
            sleep(5 * (attempt + 1))
    try:
        if len(data) > MAX_BYTES:
            raise ValueError()
        value = json.loads(data)
        token = value["iamToken"]
        expiry = value["expiresAt"]
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,16384}", token):
            raise ValueError()
        if not isinstance(expiry, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)", expiry):
            raise ValueError()
        # IAM allows nanoseconds; Python 3.9 parses at most microseconds.
        expiry = re.sub(r"(\.\d{6})\d+", r"\1", expiry)
        expires = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
        if expires < now + MIN_VALIDITY or expires > now + 13 * 60 * 60:
            raise ValueError()
        return token.encode("ascii")
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError):
        raise RefreshError("invalid IAM response or insufficient token lifetime") from None


def replace_token(path, token):
    path = Path(path)
    with directory(path.parent, private=True) as parent:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            regular(info)
        except FileNotFoundError:
            pass
        temporary = ".iam-" + secrets.token_hex(16)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as target:
                target.write(token)
                target.flush()
                os.fsync(target.fileno())
            os.fsync(parent)
            # Rename is the commit point; all fallible writes/fsyncs precede it.
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def systemctl(action):
    try:
        result = subprocess.run(["/usr/bin/systemctl", action, "--quiet", SERVICE], check=False,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if action == "is-active" and result.returncode == 3:
            return False
        if result.returncode != 0:
            raise RefreshError("bank-statement-data service operation failed")
        return True
    except (OSError, subprocess.SubprocessError):
        raise RefreshError("bank-statement-data service operation failed") from None


@contextmanager
def lock(path):
    path = Path(path)
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            regular(os.fstat(fd))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RefreshError("IAM refresh already running") from None
            yield
        finally:
            os.close(fd)


def refresh(key_file, token_file, initial=False):
    was_active = not initial and systemctl("is-active")
    now = time.time()
    token = exchange(signed_jwt(read_key(key_file), now), now)
    replace_token(token_file, token)
    if was_active:
        # Unlike restart, try-restart cannot start a service stopped during the exchange.
        systemctl("try-restart")
        if not systemctl("is-active"):
            raise RefreshError("bank-statement-data service operation failed")
    return bool(was_active)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("initial", "refresh"))
    parser.add_argument("--key-file", type=Path, default=Path("/etc/finassist/iam-refresh/authorized-key.json"))
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise RefreshError("IAM refresh requires root")
        with lock(Path("/run/finassist-iam-refresh/refresh.lock")):
            restarted = refresh(args.key_file, Path("/etc/finassist/bank-statement-data/object-storage-iam"), initial=args.mode == "initial")
    except RefreshError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # File/HTTP/crypto exceptions may contain credential bytes, response bodies or paths.
        print("IAM refresh failed", file=sys.stderr)
        return 1
    print("IAM credential initialized" if args.mode == "initial" else
          "IAM credential refreshed and service restarted" if restarted else
          "IAM credential refreshed; service remains inactive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
