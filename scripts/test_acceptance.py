import contextlib
import http.server
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import test_stage

spec = importlib.util.spec_from_file_location("acceptance", Path(__file__).with_name("acceptance.py"))
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class Response:
    def __init__(self, raw):
        self.stream = io.BytesIO(raw)

    def readline(self, n):
        return self.stream.readline(n)

    def read1(self, n):
        return self.stream.read(n)


class Connection:
    sock = None


class Boundaries(unittest.TestCase):
    def test_origin_requires_tls_or_numeric_loopback_without_credentials(self):
        for value in ("https://stage.example.invalid", "http://127.0.0.1:8080", "http://[::1]:8080"):
            acceptance.origin(value)
        for value in ("http://stage.example.invalid", "http://localhost", "https://a:secret@host",
                      "https://host/path", "https://host/?token=secret", "https://host/#secret", "file:///tmp/x"):
            with self.subTest(value=value), self.assertRaises(acceptance.CheckFailed):
                acceptance.origin(value)

    def test_sse_comments_multiline_crlf_and_unknown_event(self):
        raw = (b": ping\r\nevent: future\r\ndata: {\r\ndata: \"new\":true}\r\n\r\n"
               b"event: complete\ndata: {\"new_count\":2}\n\n")
        result = list(acceptance.sse_events(Response(raw), Connection(), lambda: 1))
        self.assertEqual(result, [("future", {"new": True}), ("complete", {"new_count": 2})])

    def test_sse_discards_incomplete_last_event(self):
        self.assertEqual(list(acceptance.sse_events(Response(b"event: done\ndata: {}"), Connection(), lambda: 1)), [])

    def test_sse_rejects_bad_json_encoding_non_object_and_long_line(self):
        for raw in (b"data: secret-token\n\n", b"data: []\n\n", b"data: \xff\n\n",
                    b"data: " + b"a" * acceptance.MAX_LINE + b"\n\n"):
            with self.subTest(size=len(raw)), self.assertRaises(acceptance.CheckFailed):
                list(acceptance.sse_events(Response(raw), Connection(), lambda: 1))

    def test_body_limit_and_total_deadline(self):
        with patch.object(acceptance, "MAX_BODY", 3):
            with self.assertRaises(acceptance.CheckFailed):
                acceptance.bounded_read(Response(b"1234"), Connection(), lambda: 1)
        with self.assertRaises(acceptance.CheckFailed):
            acceptance.Client("http://127.0.0.1", time.monotonic() - 1).remaining()

    def test_report_is_private_exclusive_and_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "report.json"
            acceptance.write_report(p, {"result": "failed"})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                acceptance.write_report(p, {})
            link = Path(tmp) / "link.json"
            link.symlink_to(p)
            with self.assertRaises(FileExistsError):
                acceptance.write_report(link, {})
            self.assertEqual(json.loads(p.read_text()), {"result": "failed"})

    def test_case_failure_cannot_be_reported_as_passed(self):
        report = {"cases": {}}
        suite = acceptance.Suite(None, report, "+70000001234")
        def fail():
            raise acceptance.CheckFailed("fixed error")
        with self.assertRaises(acceptance.CheckFailed):
            suite.check("case", fail)
        self.assertEqual(report["cases"], {"case": "failed"})

    def test_matrix_preserves_all_seven_unverified_groups(self):
        matrix = json.loads((acceptance.ROOT / "stage/acceptance/matrix.json").read_text())
        self.assertEqual({x["group"] for x in matrix}, set(range(1, 8)))
        self.assertTrue(all(x["status"] == "not_run" and x["evidence"] for x in matrix))
        self.assertEqual(len({x["case"] for x in matrix}), len(matrix))

    def test_multipart_has_single_synthetic_file_and_matching_boundary(self):
        raw, headers = acceptance.multipart(b"<OFX/>", "basic.ofx")
        boundary = headers["Content-Type"].split("boundary=")[1].encode()
        self.assertEqual(raw.count(b"--" + boundary), 2)
        self.assertIn(b'name="file"; filename="basic.ofx"', raw)

    def test_financial_fixtures_have_explicit_overlap_pair_and_repair_identity(self):
        root = acceptance.ROOT / "stage/acceptance"
        financial = ET.fromstring((root / "financial.ofx").read_text())
        transactions = financial.findall(".//STMTTRN")
        self.assertEqual(len(transactions), 7)
        pair = [x for x in transactions if x.findtext("NAME") == "Между своими счетами"]
        self.assertEqual({x.findtext("TRNAMT") for x in pair}, {"-500.00", "500.00"})
        ids = {x.findtext("FITID") for x in transactions}
        overlap = ET.fromstring((root / "financial-overlap.ofx").read_text())
        overlap_ids = {x.findtext("FITID") for x in overlap.findall(".//STMTTRN")}
        self.assertEqual(len(ids & overlap_ids), 2)
        self.assertEqual(len(overlap_ids - ids), 1)
        unread = ET.fromstring((root / "unread.ofx").read_text())
        fixed = ET.fromstring((root / "unread-fixed.ofx").read_text())
        self.assertEqual(unread.findtext(".//FITID"), fixed.findtext(".//FITID"))
        self.assertEqual(unread.findtext(".//ACCTID"), fixed.findtext(".//ACCTID"))
        expected = json.loads((root / "expected.json").read_text())
        self.assertEqual(expected["financial.ofx"]["with"]["expense"], 100000 - 20000 + 30000)
        self.assertEqual(expected["financial.ofx"]["without"]["expense"], 100000 - 20000)


class HTTPChecks(unittest.TestCase):
    def serve(self, status, body, content_type="application/json", extra=None):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.paths.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.paths = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = acceptance.Client("http://127.0.0.1:" + str(server.server_port), time.monotonic() + 5)
        return client, server

    def test_reads_json_over_http(self):
        client, _ = self.serve(200, b'{"categories":[]}')
        self.assertEqual(client.request("GET", "/v1/categories"), {"categories": []})

    def test_redirect_is_not_followed_even_same_origin(self):
        client, server = self.serve(302, b"", extra={"Location": "/v1/profile?token=private"})
        with self.assertRaisesRegex(acceptance.CheckFailed, "HTTP status"):
            client.request("GET", "/v1/categories")
        self.assertEqual(server.paths, ["/v1/categories"])

    def test_error_response_does_not_expose_server_body(self):
        client, _ = self.serve(500, b'{"access_token":"secret-token"}')
        with self.assertRaises(acceptance.CheckFailed) as result:
            client.request("GET", "/v1/profile")
        self.assertNotIn("secret-token", str(result.exception))

    def test_rejects_wrong_json_content_type(self):
        client, _ = self.serve(200, b"{}", "text/plain")
        with self.assertRaisesRegex(acceptance.CheckFailed, "content type"):
            client.request("GET", "/v1/profile")

    def test_terminal_sse_error_and_truncated_stream_fail(self):
        for raw in (b'event: error\ndata: {"code":"secret"}\n\n',
                    b'event: progress\ndata: {"percent":1}\n\n'):
            client, _ = self.serve(200, raw, "text/event-stream")
            with self.assertRaises(acceptance.CheckFailed):
                client.stream("/v1/statements/id/progress", lambda e, d: e == "complete")

    def test_sse_returns_terminal_event(self):
        client, _ = self.serve(200, b'event: complete\ndata: {"new_count":2}\n\n', "text/event-stream")
        self.assertEqual(client.stream("/v1/statements/id/progress", lambda e, d: e == "complete"),
                         ("complete", {"new_count": 2}))


class MainChecks(unittest.TestCase):
    def inputs(self, root):
        pins = {name: test_stage.fixture(root, name)[0] for name in acceptance.stage.SERVICES}
        path = root / "set.json"
        path.write_text(json.dumps(pins))
        return ["--base-url", "http://127.0.0.1:8090", "--phone", "+70000001234",
                "--set", str(path), "--artifacts", str(root), "--report", str(root / "report.json"),
                "--confirm-stage-synthetic"]

    def test_main_records_pins_but_never_claims_full_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.inputs(root)
            with patch.object(acceptance.Suite, "run"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(acceptance.main(args), 0)
            report = json.loads((root / "report.json").read_text())
            self.assertFalse(report["full_acceptance"])
            self.assertEqual(report["scope"], "public_api_smoke")
            self.assertEqual(set(report["source_set"]), set(acceptance.stage.SERVICES))
            self.assertEqual(len(report["fixtures"]), 6)
            self.assertEqual({x["status"] for x in report["remaining"]}, {"not_run"})

    def test_no_network_before_provenance_or_synthetic_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.inputs(root)
            (root / "user/user").write_bytes(b"tampered")
            with patch.object(acceptance.Suite, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(acceptance.main(args), 1)
                run.assert_not_called()

    def test_main_never_serializes_exception_with_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.inputs(root)
            with patch.object(acceptance.Suite, "run", side_effect=OSError("secret-token")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(acceptance.main(args), 1)
            self.assertNotIn("secret-token", (root / "report.json").read_text())

    def test_main_rejects_real_phone_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.inputs(root)
            args[args.index("--phone") + 1] = "+79991234567"
            with patch.object(acceptance.Suite, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(acceptance.main(args), 1)
                run.assert_not_called()

    def test_output_must_be_reservable_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.inputs(root)
            args[args.index("--report") + 1] = str(root / "missing/report.json")
            with patch.object(acceptance.Suite, "run") as run, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(acceptance.main(args), 1)
                run.assert_not_called()


class SuiteChecks(unittest.TestCase):
    class Peer:
        """Tooling test peer only; never used as evidence of deployed neighbors."""
        token = None

        def __init__(self):
            self.calls, self.upload_keys, self.upload_requests = [], {}, []
            self.bad_duplicate = False

        def request(self, method, path, expected=200, body=None, key=None, headers=None):
            self.calls.append((method, path, key))
            parsed = urlsplit(path)
            query = parse_qs(parsed.query)
            path = parsed.path
            if path == "/v1/consents/personal_data":
                return {"version": "2026-10-30"}
            if path == "/v1/auth/phone":
                assert body["pd_consent_accepted"] and body["age_confirmed"]
                assert body["pd_consent_version"] == "2026-10-30"
                return {"verification_token": "vt-private"}
            if path == "/v1/auth/register":
                assert body["pd_consent_version"] == "2026-10-30"
                return {"access_token": "private-access", "refresh_token": "private-refresh"}
            if path == "/v1/auth/token":
                return {"access_token": "private-access-2", "refresh_token": "private-refresh-2"}
            if path == "/v1/statements":
                self.upload_requests.append((key, body, headers))
                if key not in self.upload_keys:
                    self.upload_keys[key] = len(self.upload_keys) + 1
                return {"upload_id": str(self.upload_keys[key]), "file_name": "fixture.ofx"}
            if path == "/v1/operations":
                items = [{"id": "expense", "occurred_at": "2026-09-10T12:00:00+03:00", "amount": 10000},
                         {"id": "income", "occurred_at": "2026-09-15T12:00:00+03:00", "amount": 100000}]
                if "kind" in query:
                    items = [item for item in items if item["id"] == query["kind"][0]]
                return {"items": items, "total_count": len(items)}
            if path.startswith("/v1/operations/"):
                identifier = path.rsplit("/", 1)[1]
                return {"id": identifier, "amount": 10000 if identifier == "expense" else 100000}
            if path == "/v1/categories":
                return {"categories": [{"id": "cat_supermarkets"}]}
            if path == "/v1/analytics/periods":
                return {"periods": [{"key": {"month": "2026-09", "quarter": "2026-Q3", "year": "2026"}[query["period"][0]]}]}
            if path == "/v1/analytics":
                return {"has_data": True, "state": {"recalculating": False}, "blocks": [{"type": "text"}],
                        "params": {k: query[k][0] for k in ("period", "date", "transfer_mode")}}
            if path == "/v1/assistant/messages":
                return {"messages": []}
            return {}

        def stream(self, path, terminal, headers=None):
            if path == "/v1/auth/phone/events":
                data = {"status": "verified", "is_new_user": True, "registration_token": "reg-private"}
                assert terminal("status", data)
                return "status", data
            number = path.split("/")[3]
            data = {"operation_count": 2, "new_count": 2, "unread_count": 0,
                    "totals": {"expense": 10000, "income": 100000}}
            if number == "2":
                data.update(new_count=0, duplicate_count=1 if self.bad_duplicate else 2)
            if number == "3":
                data.update(operation_count=0, new_count=0)
            assert terminal("complete", data)
            return "complete", data

    def test_suite_uses_same_key_body_and_boundary_for_replays(self):
        peer, report = self.Peer(), {"cases": {}}
        acceptance.Suite(peer, report, "+70000001234").run()
        self.assertEqual(len(report["cases"]), 7)
        self.assertEqual(set(report["cases"].values()), {"passed"})
        self.assertEqual(len(peer.upload_keys), 3)
        self.assertEqual(len(peer.upload_requests), 6)
        for n in range(0, 6, 2):
            self.assertEqual(peer.upload_requests[n], peer.upload_requests[n + 1])
        for path in ("/v1/auth/phone", "/v1/auth/register", "/v1/auth/token"):
            calls = [call for call in peer.calls if call[1] == path]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(peer.calls[-2][0:2], ("POST", "/v1/auth/logout"))
        self.assertFalse(any(method == "DELETE" for method, _, _ in peer.calls))

    def test_dedup_error_stops_before_later_scenarios(self):
        peer, report = self.Peer(), {"cases": {}}
        peer.bad_duplicate = True
        with self.assertRaisesRegex(acceptance.CheckFailed, "deduplication"):
            acceptance.Suite(peer, report, "+70000001234").run()
        self.assertEqual(report["cases"]["import_basic_duplicate_empty_sse"], "failed")
        self.assertNotIn("operations_basic_search_details_categories", report["cases"])


if __name__ == "__main__":
    unittest.main()
