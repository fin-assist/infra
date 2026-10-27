import base64
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

spec = importlib.util.spec_from_file_location("refresh_iam", Path(__file__).with_name("refresh_iam.py"))
iam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iam)


class IAMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crypto = tempfile.TemporaryDirectory(prefix=".iam-crypto-test-", dir=Path.cwd())
        cls.root = Path(cls.crypto.name)
        cls.private = cls.root / "synthetic.pem"
        cls.public = cls.root / "public.pem"
        subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(cls.private)], check=True, capture_output=True, timeout=15)
        subprocess.run(["/usr/bin/openssl", "pkey", "-in", str(cls.private), "-pubout", "-out", str(cls.public)],
                       check=True, capture_output=True, timeout=10)
        cls.key = {"id": "synthetic-key", "service_account_id": "synthetic-account", "private_key": cls.private.read_text()}

    @classmethod
    def tearDownClass(cls):
        cls.crypto.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".iam-test-", dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.keyfile = self.directory / "authorized-key.json"
        self.keyfile.write_text(json.dumps(self.key))
        self.keyfile.chmod(0o600)
        self.token = self.directory / "object-storage-iam"
        self.token.write_bytes(b"old-synthetic-token")
        self.token.chmod(0o600)

    def verify_jwt(self, jwt, now):
        parts = jwt.split(".")
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        self.assertEqual(json.loads(decode(parts[0])), {"typ": "JWT", "alg": "PS256", "kid": self.key["id"]})
        self.assertEqual(json.loads(decode(parts[1])), {"iss": self.key["service_account_id"], "aud": iam.IAM_URL,
                                                      "iat": int(now), "exp": int(now) + 300})
        message, signature = self.directory / "message", self.directory / "signature"
        message.write_bytes(".".join(parts[:2]).encode())
        signature.write_bytes(decode(parts[2]))
        subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(self.public), "-signature", str(signature),
                        "-sigopt", "rsa_padding_mode:pss", "-sigopt", "rsa_pss_saltlen:32", str(message)],
                       check=True, capture_output=True, timeout=10)

    def http_iam(self, status=200, value=None, redirect=False):
        test = self
        requests = []
        value = value if value is not None else {"iamToken": "new-synthetic-token", "expiresAt":
                 datetime.fromtimestamp(time.time() + 12 * 3600, timezone.utc).isoformat()}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body, self.headers["Content-Type"]))
                self.send_response(302 if redirect else status)
                if redirect:
                    self.send_header("Location", "/credential-leak")
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        network = urllib.request.build_opener(urllib.request.ProxyHandler({}), iam.NoRedirect())

        class LocalIAM:
            def open(self, request, timeout):
                test.assertEqual(request.full_url, "https://iam.api.cloud.yandex.net/iam/v1/tokens")
                test.assertEqual(request.method, "POST")
                test.assertEqual(timeout, 15)
                local = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/iam/v1/tokens",
                                               data=request.data, headers=dict(request.headers), method="POST")
                return network.open(local, timeout=timeout)

        return LocalIAM(), requests

    def test_authorized_json_and_real_ps256_signature(self):
        key = iam.read_key(self.keyfile)
        self.assertEqual(key, self.key)
        self.verify_jwt(iam.signed_jwt(key, 1700000000), 1700000000)

    def test_real_crypto_http_and_initial_never_touch_systemd(self):
        self.token.unlink()  # First bootstrap has no credential and no service binary.
        opener, requests = self.http_iam()
        exchange = iam.exchange
        now = time.time()
        with patch.object(iam.time, "time", return_value=now), patch.object(iam, "systemctl") as service, \
                patch.object(iam, "exchange", side_effect=lambda jwt, n: exchange(jwt, n, opener=opener)):
            iam.refresh(self.keyfile, self.token, initial=True)
        service.assert_not_called()
        self.assertEqual(self.token.read_bytes(), b"new-synthetic-token")
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.assertEqual(requests[0][0], "/iam/v1/tokens")
        self.assertEqual(requests[0][2], "application/json")
        self.verify_jwt(requests[0][1]["jwt"], now)

    def test_refresh_restarts_only_after_replace_and_restart_failure_is_failure(self):
        events = []
        def service(action):
            events.append((action, self.token.read_bytes()))
        with patch.object(iam, "systemctl", side_effect=service), patch.object(iam, "exchange", return_value=b"new-token"):
            iam.refresh(self.keyfile, self.token)
        self.assertEqual(events, [("is-active", b"old-synthetic-token"), ("try-restart", b"new-token"), ("is-active", b"new-token")])
        def fail_restart(action):
            if action == "try-restart":
                raise iam.RefreshError("bank-statement-data service operation failed")
        with patch.object(iam, "systemctl", side_effect=fail_restart), patch.object(iam, "exchange", return_value=b"newer-token"):
            with self.assertRaises(iam.RefreshError):
                iam.refresh(self.keyfile, self.token)
        self.assertEqual(self.token.read_bytes(), b"newer-token")

    def test_stopped_service_does_not_exchange_or_replace(self):
        with patch.object(iam, "systemctl", side_effect=iam.RefreshError("inactive")), patch.object(iam, "exchange") as network:
            with self.assertRaises(iam.RefreshError):
                iam.refresh(self.keyfile, self.token)
        network.assert_not_called()
        self.assertEqual(self.token.read_bytes(), b"old-synthetic-token")

    def test_http_failure_redirect_and_retry_leave_old_token(self):
        for status, redirect in [(403, False), (503, False), (200, True)]:
            with self.subTest(status=status, redirect=redirect):
                opener, requests = self.http_iam(status=status, redirect=redirect)
                exchange = iam.exchange
                with patch.object(iam, "systemctl") as service, \
                        patch.object(iam, "exchange", side_effect=lambda jwt, now: exchange(jwt, now, opener, sleep=lambda _: None)):
                    with self.assertRaises(iam.RefreshError):
                        iam.refresh(self.keyfile, self.token)
                self.assertEqual(len(requests), 3)
                self.assertTrue(all(r[0] == "/iam/v1/tokens" for r in requests))
                self.assertEqual(self.token.read_bytes(), b"old-synthetic-token")
                self.assertEqual([c.args[0] for c in service.call_args_list], ["is-active"])

    def test_response_refusals_and_nanoseconds(self):
        now = time.time()
        valid_expiry = datetime.fromtimestamp(now + 12 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
        for value in [{}, [], {"iamToken": "", "expiresAt": valid_expiry}, {"iamToken": "secret\nheader", "expiresAt": valid_expiry},
                      {"iamToken": "x", "expiresAt": "not-a-date"}, {"iamToken": "x", "expiresAt": 42},
                      {"iamToken": "x", "expiresAt": datetime.fromtimestamp(now + 4 * 3600, timezone.utc).isoformat()},
                      {"iamToken": "x", "expiresAt": "2099-01-01T00:00:00Z"}]:
            opener, _ = self.http_iam(value=value)
            with self.subTest(value=value), self.assertRaises(iam.RefreshError):
                iam.exchange("synthetic-jwt", now, opener)
        opener, _ = self.http_iam(value={"iamToken": "x", "expiresAt": valid_expiry})
        self.assertEqual(iam.exchange("synthetic-jwt", now, opener), b"x")

    def test_key_signing_and_subprocess_failure_preserve_old_token(self):
        self.keyfile.write_text(json.dumps(self.key | {"private_key": "-----BEGIN PRIVATE KEY-----\ninvalid"}))
        with patch.object(iam, "systemctl"), self.assertRaisesRegex(iam.RefreshError, "signing failed"):
            iam.refresh(self.keyfile, self.token)
        self.assertEqual(self.token.read_bytes(), b"old-synthetic-token")
        with patch.object(iam.subprocess, "run", side_effect=subprocess.TimeoutExpired("openssl", 10)):
            with self.assertRaises(iam.RefreshError):
                iam.signed_jwt(self.key, time.time())

    def test_secret_files_and_ancestors_reject_symlinks_hardlinks_permissions_fifo(self):
        alias = self.directory / "alias"
        alias.symlink_to(self.keyfile)
        with self.assertRaises(OSError):
            iam.read_key(alias)
        alias.unlink()
        os.link(self.keyfile, alias)
        with self.assertRaises(iam.RefreshError):
            iam.read_key(alias)
        alias.unlink()
        self.keyfile.chmod(0o644)
        with self.assertRaises(iam.RefreshError):
            iam.read_key(self.keyfile)
        fifo = self.directory / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(iam.RefreshError):
            iam.read_key(fifo)
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(OSError):
            iam.replace_token(alias / "token", b"x")
        self.directory.chmod(0o777)
        try:
            with self.assertRaises(iam.RefreshError):
                iam.replace_token(self.token, b"x")
        finally:
            self.directory.chmod(0o700)

    def test_atomic_replace_failures_and_target_symlink_preserve_old_token(self):
        unrelated = self.directory / "other-secret"
        unrelated.write_bytes(b"unrelated-synthetic-key")
        for operation in ("fsync", "replace"):
            with patch.object(iam.os, operation, side_effect=OSError("synthetic write failure")):
                with self.assertRaises(OSError):
                    iam.replace_token(self.token, b"new-token")
            self.assertEqual(self.token.read_bytes(), b"old-synthetic-token")
            self.assertFalse(list(self.directory.glob(".iam-*")))
            self.assertEqual(unrelated.read_bytes(), b"unrelated-synthetic-key")
        alias = self.directory / "alias"
        alias.symlink_to(self.token)
        with self.assertRaises(iam.RefreshError):
            iam.replace_token(alias, b"new-token")
        self.assertEqual(self.token.read_bytes(), b"old-synthetic-token")

    def test_refresh_write_failure_never_restarts_service(self):
        with patch.object(iam, "systemctl") as service, patch.object(iam, "exchange", return_value=b"new-token"), \
                patch.object(iam.os, "replace", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                iam.refresh(self.keyfile, self.token)
        self.assertEqual([call.args[0] for call in service.call_args_list], ["is-active"])
        self.assertEqual(self.token.read_bytes(), b"old-synthetic-token")

    def test_lock_refuses_overlap_or_symlink(self):
        path = self.directory / "refresh.lock"
        with iam.lock(path):
            with self.assertRaisesRegex(iam.RefreshError, "already running"):
                with iam.lock(path):
                    self.fail("overlap entered")
        path.unlink()
        path.symlink_to(self.token)
        with self.assertRaises(OSError):
            with iam.lock(path):
                self.fail("symlink entered")

    def test_cli_never_logs_credentials_or_claims_success_on_failure(self):
        for error in (RuntimeError("private-key-and-provider-secret"), iam.RefreshError("IAM signing failed")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(iam.sys, "argv", ["refresh_iam.py", "refresh"]), patch.object(iam.os, "geteuid", return_value=0), \
                    patch.object(iam, "lock", return_value=nullcontext()), patch.object(iam, "refresh", side_effect=error), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(iam.main(), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn("private-key-and-provider-secret", stderr.getvalue())

    def test_non_root_cli_cannot_read_key_or_exchange(self):
        with patch.object(iam.sys, "argv", ["refresh_iam.py", "initial"]), patch.object(iam.os, "geteuid", return_value=1000), \
                patch.object(iam, "refresh") as refresh, redirect_stderr(io.StringIO()):
            self.assertEqual(iam.main(), 1)
        refresh.assert_not_called()

    def test_timer_and_commands_are_bounded_without_frequent_restart(self):
        timer = Path("stage/systemd/finassist-iam-refresh.timer").read_text()
        self.assertIn("OnActiveSec=4h", timer)
        self.assertIn("OnUnitActiveSec=4h", timer)
        self.assertGreaterEqual(iam.MIN_VALIDITY, 5 * 3600)
        service = Path("stage/systemd/finassist-iam-refresh.service").read_text()
        self.assertIn("ConditionPathIsExecutable=/opt/finassist/bank-statement-data/bank-statement-data", service)
        self.assertIn("RuntimeDirectoryPreserve=yes", service)
        with patch.object(iam.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as command:
            iam.systemctl("try-restart")
            self.assertEqual(command.call_args.args[0], ["/usr/bin/systemctl", "try-restart", "--quiet", "bank-statement-data.service"])
            self.assertEqual(command.call_args.kwargs["timeout"], 60)

    def test_production_transport_uses_system_tls_no_proxy_and_no_redirect(self):
        now = time.time()
        class Response(io.BytesIO):
            status = 200
        response = Response(json.dumps({"iamToken": "synthetic-token", "expiresAt":
                            datetime.fromtimestamp(now + 12 * 3600, timezone.utc).isoformat()}).encode())
        with patch.object(iam.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(iam.exchange("synthetic-jwt", now), b"synthetic-token")
            handlers = build.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], iam.NoRedirect)
            context = handlers[2]._context
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, iam.IAM_URL)
            self.assertEqual(json.loads(request.data), {"jwt": "synthetic-jwt"})

    def test_oversized_slow_or_malformed_response_refused(self):
        class Response(io.BytesIO):
            status = 200
        for data in (b"not-json", b"x" * (iam.MAX_BYTES + 1)):
            with patch.object(iam.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = Response(data)
                with self.assertRaises(iam.RefreshError):
                    iam.exchange("synthetic-jwt", time.time())
        with patch.object(iam.urllib.request, "build_opener") as build, \
                patch.object(iam.time, "monotonic", side_effect=[0, 16]):
            build.return_value.open.return_value = Response(b"{}")
            with self.assertRaisesRegex(iam.RefreshError, "timed out"):
                iam.exchange("synthetic-jwt", time.time())
