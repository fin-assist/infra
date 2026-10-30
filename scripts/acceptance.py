#!/usr/bin/env python3
"""Run bounded synthetic public-API checks; never equate smoke with FIN-362 acceptance."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import ssl
import sys
import time
from urllib.parse import quote, urlencode, urlsplit
import uuid

import stage

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY = 4 * 1024 * 1024
MAX_LINE = 64 * 1024
SUPPORTS = "heading@1,tile_group@1,chart@1,ranked_list@1,insight_card@1,rows@1,text@1"
# contracts 87e8200 openapi/openapi.yaml, components.parameters.XClientVersion.
CLIENT_VERSION_PATTERN = r"[a-z][a-z0-9_]*/(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\+[0-9]+"
DEFAULT_CLIENT_VERSION = "android/0.2.14+1"


class CheckFailed(Exception):
    """Only fixed, non-sensitive messages may leave the runner."""


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


@contextmanager
def alarm_budget(deadline):
    """Bound slow-drip/DNS too, preserving an enclosing alarm's absolute deadline."""
    now = time.monotonic()
    old_delay, old_interval = signal.getitimer(signal.ITIMER_REAL)
    delay = min(deadline - now, old_delay) if old_delay > 0 else deadline - now
    require(delay > 0, "request budget exceeded")
    old_deadline = now + old_delay
    def expired(_signum, _frame):
        raise CheckFailed("request budget exceeded")
    old_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, delay)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_delay > 0:
            remaining = old_deadline - time.monotonic()
            require(remaining > 0, "suite deadline exceeded")
            signal.setitimer(signal.ITIMER_REAL, remaining, old_interval)


def origin(value):
    parsed = urlsplit(value)
    require(parsed.scheme in ("http", "https") and parsed.hostname and
            not parsed.username and not parsed.password and not parsed.query and
            not parsed.fragment and parsed.path in ("", "/"), "invalid API origin")
    if parsed.scheme == "http":
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = False
        require(local, "HTTP is allowed only on numeric loopback")
    try:
        port = parsed.port
    except ValueError:
        raise CheckFailed("invalid API port") from None
    return parsed, port


class Client:
    """No proxy, redirect, cookies, automatic retries or unbounded response reads."""

    def __init__(self, base_url, deadline, timeout=15, client_version=DEFAULT_CLIENT_VERSION):
        self.base, self.port = origin(base_url)
        self.deadline, self.timeout = deadline, timeout
        require(len(client_version) <= 64 and re.fullmatch(CLIENT_VERSION_PATTERN, client_version),
                "invalid client version header")
        self.client_version = client_version
        self.token = None

    def remaining(self):
        value = self.deadline - time.monotonic()
        require(value > 0, "suite deadline exceeded")
        return min(value, self.timeout)

    def open(self, method, path, body=None, key=None, headers=None):
        require(path.startswith("/v1/") and "\r" not in path and "\n" not in path,
                "invalid API path")
        request_headers = {"X-Client-Version": self.client_version, "Accept": "application/json"}
        if self.token:
            request_headers["Authorization"] = "Bearer " + self.token
        if key:
            request_headers["Idempotency-Key"] = key
        if headers:
            request_headers.update(headers)
        if isinstance(body, dict):
            body = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
            request_headers["Content-Type"] = "application/json"
        cls = http.client.HTTPSConnection if self.base.scheme == "https" else http.client.HTTPConnection
        options = {"timeout": self.remaining()}
        if self.base.scheme == "https":
            options["context"] = ssl.create_default_context()
        connection = cls(self.base.hostname, self.port, **options)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return connection, response
        except Exception:
            connection.close()
            raise

    def request(self, method, path, expected=200, body=None, key=None, headers=None):
        connection, response = self.open(method, path, body, key, headers)
        try:
            require(response.status == expected, "unexpected HTTP status")
            raw = bounded_read(response, connection, self.remaining)
            if expected == 204:
                require(raw == b"", "204 response has a body")
                return None
            require(response.getheader("Content-Type", "").split(";")[0].strip() == "application/json",
                    "JSON content type required")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise CheckFailed("invalid JSON response") from None
            require(isinstance(data, dict), "JSON object required")
            return data
        finally:
            connection.close()

    def stream(self, path, terminal, headers=None):
        connection, response = self.open("GET", path, headers={"Accept": "text/event-stream", **(headers or {})})
        try:
            require(response.status == 200, "unexpected SSE HTTP status")
            require(response.getheader("Content-Type", "").split(";")[0].strip() == "text/event-stream",
                    "SSE content type required")
            for event, data in sse_events(response, connection, self.remaining):
                if event == "error":
                    raise CheckFailed("terminal SSE error")
                if terminal(event, data):
                    return event, data
            raise CheckFailed("SSE closed without expected terminal event")
        finally:
            connection.close()


def set_timeout(connection, remaining):
    timeout = remaining()
    if connection.sock is not None:
        connection.sock.settimeout(timeout)


def bounded_read(response, connection, remaining):
    chunks, size = [], 0
    while True:
        set_timeout(connection, remaining)
        chunk = response.read1(min(65536, MAX_BODY + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        require(size <= MAX_BODY, "response exceeds byte limit")
        chunks.append(chunk)


def sse_events(response, connection, remaining):
    event, lines, size = "message", [], 0
    while True:
        set_timeout(connection, remaining)
        raw = response.readline(MAX_LINE + 1)
        size += len(raw)
        require(size <= MAX_BODY and len(raw) <= MAX_LINE, "SSE exceeds byte limit")
        if not raw:
            return
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise CheckFailed("invalid SSE encoding") from None
        if not line:
            if lines:
                try:
                    data = json.loads("\n".join(lines))
                except ValueError:
                    raise CheckFailed("invalid SSE JSON") from None
                require(isinstance(data, dict), "SSE JSON object required")
                yield event, data
            event, lines = "message", []
        elif line.startswith("event:"):
            event = line[6:].lstrip(" ")
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))


def multipart(content, filename):
    boundary = "fin362-" + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            'Content-Type: application/x-ofx\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": "multipart/form-data; boundary=" + boundary}


def text_field(data, name):
    require(isinstance(data, dict), "JSON object required")
    value = data.get(name)
    require(isinstance(value, str) and bool(value), "missing string field")
    return value


def list_field(data, name):
    require(isinstance(data, dict), "JSON object required")
    value = data.get(name)
    require(isinstance(value, list), "missing list field")
    return value


def object_field(data, name):
    require(isinstance(data, dict), "JSON object required")
    value = data.get(name)
    require(isinstance(value, dict), "missing object field")
    return value


def bool_field(data, name):
    require(isinstance(data, dict), "JSON object required")
    value = data.get(name)
    require(type(value) is bool, "boolean field required")
    return value


def int_field(data, name):
    require(isinstance(data, dict), "JSON object required")
    value = data.get(name)
    require(type(value) is int, "integer field required")
    return value


def same_json(left, right):
    """JSON replay identity must not equate Python's True/1 or 0.0/0."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


def analytics_ready(data):
    if not bool_field(data, "has_data"):
        return False
    return not bool_field(object_field(data, "state"), "recalculating")


class Suite:
    def __init__(self, client, report, phone):
        self.client, self.report, self.phone = client, report, phone

    def check(self, case, action):
        self.report["cases"][case] = "running"
        try:
            value = action()
        except Exception:
            self.report["cases"][case] = "failed"
            raise
        self.report["cases"][case] = "passed"
        return value

    def login(self):
        c = self.client
        consent = c.request("GET", "/v1/consents/personal_data")
        version = text_field(consent, "version")
        start_body = {"phone": self.phone, "pd_consent_accepted": True,
                      "pd_consent_version": version, "age_confirmed": True}
        key = str(uuid.uuid4())
        start = c.request("POST", "/v1/auth/phone", body=start_body, key=key)
        replay = c.request("POST", "/v1/auth/phone", body=start_body, key=key)
        require(text_field(start, "verification_token") == text_field(replay, "verification_token"),
                "phone idempotency mismatch")
        headers = {"X-Verification-Token": text_field(start, "verification_token")}
        _, verified = c.stream("/v1/auth/phone/events", lambda e, d: e == "status" and d.get("status") == "verified", headers)
        require(verified.get("is_new_user") is True, "synthetic account must be new for this run")
        _, again = c.stream("/v1/auth/phone/events", lambda e, d: e == "status" and d.get("status") == "verified", headers)
        require(text_field(verified, "registration_token") == text_field(again, "registration_token"),
                "phone SSE replay mismatch")
        body = {"registration_token": text_field(verified, "registration_token"),
                "pd_consent_accepted": True, "pd_consent_version": version, "timezone": "Europe/Moscow"}
        key = str(uuid.uuid4())
        pair = c.request("POST", "/v1/auth/register", 201, body, key)
        require(same_json(pair, c.request("POST", "/v1/auth/register", 201, body, key)), "registration replay mismatch")
        c.token = text_field(pair, "access_token")
        self.refresh = text_field(pair, "refresh_token")

    def refresh_session(self):
        body, key = {"refresh_token": self.refresh}, str(uuid.uuid4())
        c = self.client
        suite_deadline = c.deadline
        # A used refresh token is safe to replay only within the contractual 60s
        # cache window. Reserve 15s for transit/clock uncertainty; never issue the
        # replay after this absolute budget, even if slow-drip I/O did not time out.
        c.deadline = min(suite_deadline, time.monotonic() + 45)
        try:
            with alarm_budget(c.deadline):
                pair = c.request("POST", "/v1/auth/token", body=body, key=key)
                c.remaining()
                require(same_json(pair, c.request("POST", "/v1/auth/token", body=body, key=key)),
                        "refresh replay mismatch")
        finally:
            c.deadline = suite_deadline
        self.client.token = text_field(pair, "access_token")
        self.refresh = text_field(pair, "refresh_token")
        self.client.request("GET", "/v1/profile")

    def upload(self, name):
        content = (ROOT / "stage/acceptance" / name).read_bytes()
        body, headers = multipart(content, name)
        key = str(uuid.uuid4())
        result = self.client.request("POST", "/v1/statements", 202, body, key, headers)
        require(same_json(result, self.client.request("POST", "/v1/statements", 202, body, key, headers)),
                "upload replay mismatch")
        upload_id = quote(text_field(result, "upload_id"), safe="")
        path = f"/v1/statements/{upload_id}/progress"
        _, complete = self.client.stream(path, lambda e, d: e == "complete")
        _, replay = self.client.stream(path, lambda e, d: e == "complete")
        require(same_json(complete, replay), "import terminal replay mismatch")
        return complete

    def import_fixture(self):
        complete = self.upload("basic.ofx")
        require(int_field(complete, "operation_count") == 2 and int_field(complete, "new_count") == 2 and
                int_field(complete, "unread_count") == 0, "unexpected initial import counters")
        totals = object_field(complete, "totals")
        require(int_field(totals, "expense") == 10000 and
                int_field(totals, "income") == 100000, "unexpected fixture financial totals")
        duplicate = self.upload("basic.ofx")
        require(int_field(duplicate, "new_count") == 0 and int_field(duplicate, "duplicate_count") == 2,
                "cross-upload deduplication failed")
        empty = self.upload("empty.ofx")
        require(int_field(empty, "operation_count") == 0 and int_field(empty, "unread_count") == 0,
                "unexpected empty import counters")

    def operations(self):
        c = self.client
        feed = c.request("GET", "/v1/operations")
        require(len(list_field(feed, "items")) == 2, "fixture feed differs")
        result = c.request("GET", "/v1/operations?all_time=true")
        require(int_field(result, "total_count") == 2 and len(list_field(result, "items")) == 2,
                "fixture search differs")
        identifiers = {text_field(item, "id") for item in result["items"]}
        require(len(identifiers) == 2 and identifiers == {text_field(item, "id") for item in feed["items"]},
                "feed/search identities differ")
        for mode in ("with", "without"):
            for kind, expected in (("expense", 10000), ("income", 100000)):
                filtered = c.request("GET", "/v1/operations?" + urlencode({"all_time": "true", "kind": kind, "transfer_mode": mode}))
                items = list_field(filtered, "items")
                require(len(items) == 1 and int_field(filtered, "total_count") == 1 and int_field(items[0], "amount") == expected,
                        "fixture financial filter differs")
        for item in result["items"]:
            path = "/v1/operations/" + quote(text_field(item, "id"), safe="") + "?" + urlencode({"occurred_at": text_field(item, "occurred_at")})
            detail = c.request("GET", path)
            require(text_field(detail, "id") == item["id"] and int_field(detail, "amount") == int_field(item, "amount"),
                    "operation detail differs from search")
        categories = list_field(c.request("GET", "/v1/categories"), "categories")
        require(bool(categories), "categories empty")
        for category in categories:
            text_field(category, "id")
            bool_field(category, "assignable")
            bool_field(category, "is_system")

    def analytics(self):
        c = self.client
        for period, key in (("month", "2026-09"), ("quarter", "2026-Q3"), ("year", "2026")):
            periods = list_field(c.request("GET", "/v1/analytics/periods?" + urlencode({"period": period})), "periods")
            while key not in [text_field(p, "key") for p in periods]:
                time.sleep(min(1, c.remaining()))
                periods = list_field(c.request("GET", "/v1/analytics/periods?" + urlencode({"period": period})), "periods")
            for mode in ("with", "without"):
                path = "/v1/analytics?" + urlencode({"period": period, "date": key,
                    "transfer_mode": mode, "supports": SUPPORTS})
                data = c.request("GET", path)
                while not analytics_ready(data):
                    time.sleep(min(1, c.remaining()))
                    data = c.request("GET", path)
                require(data.get("params") == {"period": period, "date": key, "transfer_mode": mode},
                        "analytics parameters differ")
                blocks = list_field(data, "blocks")
                require(bool(blocks), "analytics blocks missing")
                for block in blocks:
                    text_field(block, "type")
                    require(int_field(block, "v") == 1, "unsupported analytics block version")

    def assistant_read(self):
        c = self.client
        limit = c.request("GET", "/v1/assistant/limit")
        require(0 <= int_field(limit, "remaining") <= int_field(limit, "daily_max"), "invalid assistant limit")
        text_field(limit, "resets_at")
        data = c.request("GET", "/v1/assistant/messages")
        require(list_field(data, "messages") == [], "new account has assistant history")
        require(bool_field(data, "has_older") is False, "new account has older assistant history")

    def logout(self):
        self.client.request("POST", "/v1/auth/logout", 204, {"refresh_token": self.refresh})
        self.client.request("POST", "/v1/auth/logout", 204, {"refresh_token": self.refresh})
        self.client.token = None

    def run(self):
        for case, action in (("auth_registration_replay_sse", self.login),
                             ("auth_refresh_replay", self.refresh_session),
                             ("import_basic_duplicate_empty_sse", self.import_fixture),
                             ("operations_basic_search_details_categories", self.operations),
                             ("analytics_basic_periods_modes", self.analytics),
                             ("assistant_empty_history_limit", self.assistant_read),
                             ("auth_logout_repeat", self.logout)):
            self.check(case, action)


def open_report(path):
    # Create exclusively, without following a pre-existing symlink or overwriting evidence.
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def write_report(path, report, fd=None):
    if fd is None:
        fd = open_report(path)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--phone", required=True, help="unused synthetic +7000000xxxx account")
    parser.add_argument("--set", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--client-version", default=DEFAULT_CLIENT_VERSION)
    parser.add_argument("--timeout", type=int, default=300, choices=range(30, 601), metavar="30..600")
    parser.add_argument("--confirm-stage-synthetic", action="store_true", required=True,
                        help="operator confirms isolated stage, fake phone/LLM and synthetic account")
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "task": "FIN-362", "started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "public_api_smoke", "full_acceptance": False, "deployed_set_verified": False, "cases": {},
              "remaining": json.loads((ROOT / "stage/acceptance/matrix.json").read_text())}
    report_fd = None
    try:
        require(re.fullmatch(r"\+7000000[0-9]{4}", args.phone), "synthetic phone required")
        origin(args.base_url)
        # Reserve evidence before any HTTP mutations; retain the descriptor so a later
        # path replacement cannot redirect private output to a different file.
        report_fd = open_report(args.report)
        pins = stage.read_set(args.set)
        manifests = stage.verify_set(pins, args.artifacts)
        report["source_set"] = pins
        report["contracts_module"] = manifests["bank-bff"]["modules"]["contracts/gen/go"]
        report["fixtures"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (ROOT / "stage/acceptance").glob("*.ofx")}
        report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        # SIGALRM bounds total runtime, including slow-drip headers/lines. Socket timeouts
        # separately bound idle operations. No subprocess or shell is run by this suite.
        def alarm(_signum, _frame):
            raise CheckFailed("suite deadline exceeded")
        previous = signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, args.timeout)
        try:
            Suite(Client(args.base_url, time.monotonic() + args.timeout, client_version=args.client_version), report, args.phone).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        report["smoke_result"] = "passed"
        code = 0
    except Exception:
        # Never serialize exception messages: transports and parsers may include tokens,
        # private paths or returned financial content in their diagnostics.
        report["smoke_result"] = "failed"
        code = 1
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    try:
        if report_fd is None:
            raise OSError("no private report descriptor")
        write_report(args.report, report, report_fd)
    except OSError:
        print("Could not create private report", file=sys.stderr)
        return 1
    print("Public API smoke: " + report["smoke_result"] + "; FIN-362 full acceptance: NOT CONFIRMED")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
