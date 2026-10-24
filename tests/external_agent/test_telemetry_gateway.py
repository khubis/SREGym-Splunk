import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from clients.external_agent.gateways.telemetry import allowed, authenticated, clean_path, read_body


class AccessTests(unittest.TestCase):
    def test_prometheus_reads(self):
        for path in [
            "/api/v1/query?query=up",
            "/api/v1/query_range",
            "/api/v1/labels",
            "/api/v1/label/job/values",
            "/api/v1/metadata",
            "/api/v1/rules",
            "/api/v1/alerts",
            "/api/v1/status/buildinfo",
            "/-/ready",
        ]:
            self.assertTrue(allowed("prometheus", "GET", path), path)

    def test_prometheus_writes_denied(self):
        for method, path in [
            ("POST", "/api/v1/admin/tsdb/delete_series"),
            ("POST", "/-/reload"),
            ("POST", "/api/v1/write"),
            ("DELETE", "/api/v1/series"),
            ("GET", "/api/v1/status/config"),
        ]:
            self.assertFalse(allowed("prometheus", method, path))

    def test_read_query_posts(self):
        self.assertTrue(allowed("prometheus", "POST", "/api/v1/query"))
        self.assertTrue(
            allowed(
                "grafana",
                "POST",
                "/api/ds/query",
                json.dumps({"queries": [{"datasource": {"uid": "sregym-prometheus"}}]}).encode(),
            )
        )

    def test_grafana_metadata(self):
        for path in [
            "/api/health",
            "/api/datasources",
            "/api/search",
            "/api/datasources/uid/sregym-loki/health",
            "/api/dashboards/uid/own-dashboard",
        ]:
            self.assertTrue(allowed("grafana", "GET", path), path)

    def test_grafana_proxies(self):
        self.assertTrue(
            allowed("grafana", "GET", "/api/datasources/proxy/uid/sregym-loki/loki/api/v1/query_range?query=x")
        )
        self.assertTrue(allowed("grafana", "GET", "/api/datasources/proxy/1/api/v1/query", datasource_ids=[1, 2]))
        self.assertFalse(allowed("grafana", "GET", "/api/datasources/proxy/3/api/v1/query", datasource_ids=[1, 2]))
        self.assertFalse(allowed("grafana", "GET", "/api/datasources/proxy/uid/foreign/api/v1/query"))

    def test_admin_and_mutations_denied(self):
        for method, path in [
            ("GET", "/api/admin/settings"),
            ("POST", "/api/serviceaccounts"),
            ("POST", "/api/dashboards/db"),
            ("DELETE", "/api/datasources/1"),
            ("GET", "/api/datasources/proxy/1/api/v1/status/config"),
        ]:
            self.assertFalse(allowed("grafana", method, path, datasource_ids=[1]))

    def test_traversal_and_arbitrary_origin_denied(self):
        for path in [
            "https://other.test/api/health",
            "//other.test/api/health",
            "/api/../admin",
            "/api/%2e%2e/admin",
            "/api/%252e%252e/admin",
            "/api\\admin",
            "/api//admin",
            "/api/health#fragment",
        ]:
            self.assertIsNone(clean_path(path), path)

    def test_query_datasource_bound(self):
        for doc in [
            {"queries": []},
            {"queries": [{"datasource": {"uid": "foreign"}}]},
            {"queries": [{}]},
            {"queries": "bad"},
        ]:
            self.assertFalse(allowed("grafana", "POST", "/api/ds/query", json.dumps(doc).encode()))
        self.assertFalse(allowed("grafana", "POST", "/api/ds/query", b"not-json"))

    def test_bounds(self):
        self.assertFalse(allowed("prometheus", "GET", "/api/v1/query?" + "x" * 17000))
        self.assertFalse(allowed("prometheus", "POST", "/api/v1/query", b"x" * 262145))

    def test_bearer_is_required_and_exact(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "token"
            p.write_text("private-test-value")
            p.chmod(0o600)
            cfg = {"mode": "grafana", "credential_file": str(p)}
            self.assertTrue(authenticated(cfg, "Bearer private-test-value"))
            self.assertFalse(authenticated(cfg, ""))
            self.assertFalse(authenticated(cfg, "Bearer wrong"))

    def test_basic_auth_is_required(self):
        import base64

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "password"
            p.write_text("private-test-value")
            p.chmod(0o600)
            cfg = {"mode": "prometheus", "credential_file": str(p), "username": "external"}
            self.assertTrue(authenticated(cfg, "Basic " + base64.b64encode(b"external:private-test-value").decode()))
            self.assertFalse(authenticated(cfg, "Bearer private-test-value"))

    def test_insecure_credential_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "token"
            p.write_text("test")
            p.chmod(0o644)
            with self.assertRaises(ValueError):
                authenticated({"mode": "grafana", "credential_file": str(p)}, "Bearer test")

    def test_cloudflare_chunked_read_query_body(self):
        self.assertEqual(
            read_body(BytesIO(b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"), {"Transfer-Encoding": "chunked"}), b"abcde"
        )
        self.assertEqual(read_body(BytesIO(b"0\r\n\r\n"), {"Transfer-Encoding": "chunked"}), b"")
        self.assertEqual(read_body(BytesIO(b"abc"), {"Content-Length": "3"}), b"abc")

    def test_ambiguous_and_malformed_framing_denied(self):
        for body, headers in [
            (b"", {"Transfer-Encoding": "chunked", "Content-Length": "0"}),
            (b"z\r\n", {"Transfer-Encoding": "chunked"}),
            (b"3\r\na", {"Transfer-Encoding": "chunked"}),
            (b"0\r\nx: y\r\n", {"Transfer-Encoding": "chunked"}),
            (b"", {"Content-Length": "-1"}),
        ]:
            with self.assertRaises(ValueError):
                read_body(BytesIO(body), headers)

    def test_chunked_body_size_bound(self):
        with self.assertRaises(OverflowError):
            read_body(BytesIO(b"40001\r\n"), {"Transfer-Encoding": "chunked"})


if __name__ == "__main__":
    unittest.main()
