import importlib.util
from pathlib import Path
import re
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("check_env", Path(__file__).with_name("check_env.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ConfigurationTests(unittest.TestCase):
    def test_no_port_collisions_or_wildcard_listeners(self):
        ports = []
        for path in Path("stage/systemd").glob("*.service"):
            if path.stem not in ("user", "bank-statement-data", "analytics", "assistant", "bank-bff"):
                continue
            listeners = re.findall(r"^Environment=(?:GRPC_ADDR|HTTP_ADDR|PUBLIC_ADDR)=(.*)$", path.read_text(), re.M)
            self.assertEqual(len(listeners), 2, path)
            self.assertTrue(all(v.startswith("127.0.0.1:") for v in listeners), path)
            ports.extend(listeners)
        self.assertEqual(len(ports), len(set(ports)))

    def test_migrations_share_credentials_and_do_not_restart(self):
        for name in ("user", "bank-statement-data", "analytics", "assistant"):
            base = Path(f"stage/systemd/{name}.service").read_text()
            migration = Path(f"stage/systemd/{name}-migrate.service").read_text()
            self.assertIn("Type=oneshot", migration)
            self.assertIn("Restart=no", migration)
            self.assertIn(f"ExecStart=/opt/finassist/{name}/{name} migrate up", migration)
            self.assertEqual(re.findall(r"^LoadCredential=.*", base, re.M), re.findall(r"^LoadCredential=.*", migration, re.M))

    def test_env_cannot_override_unit_identity_or_credentials(self):
        for line in ["ENV=dev", "HTTP_ADDR=0.0.0.0:9091", "DB_PRIMARY_DSN_FILE=/tmp/secret", "DB_PRIMARY_DSN=secret"]:
            with tempfile.TemporaryDirectory() as tmp:
                file = Path(tmp) / "env"
                file.write_text(line)
                with self.assertRaises(ValueError):
                    module.check("user", file)
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "env"
            file.write_text("AUTH_STAGE_ISSUER=finassist-stage\nPHONE_PROVIDER=fake\n")
            self.assertEqual(module.check("user", file), {"AUTH_STAGE_ISSUER", "PHONE_PROVIDER"})

    def test_placeholders_and_duplicates_refused(self):
        for text in ["AUTH_STAGE_ISSUER=REPLACE_VALUE", "JWT_ISSUER=https://stage.example.invalid", "PHONE_PROVIDER=fake\nPHONE_PROVIDER=zvonok"]:
            with tempfile.TemporaryDirectory() as tmp:
                file = Path(tmp) / "env"
                file.write_text(text)
                with self.assertRaises(ValueError):
                    module.check("user", file)

    def test_optional_model_failure_cannot_stop_assistant(self):
        text = Path("stage/systemd/assistant.service.d/fake-llm.conf").read_text()
        self.assertIn("Wants=fake-llm.service", text)
        self.assertIn("After=fake-llm.service", text)
        self.assertNotRegex(text, r"(?m)^(?:Requires|BindsTo)=.*fake-llm")

    def test_nginx_covers_all_sse_and_upload_envelope(self):
        text = Path("stage/nginx/finassist.conf").read_text()
        self.assertEqual(text.count("proxy_buffering off;"), 3)
        for path in ["/v1/auth/phone/events", "/v1/statements/[^/]+/progress", "/v1/assistant/messages/[^/]+/events"]:
            self.assertIn(path, text)
        self.assertIn("client_max_body_size 10551296;", text)
        self.assertIn("proxy_request_buffering off;", text)
        self.assertIn("X-Forwarded-For $remote_addr;", text)
        self.assertNotIn("$proxy_add_x_forwarded_for", text)
        self.assertIn("proxy_next_upstream off;", text)
