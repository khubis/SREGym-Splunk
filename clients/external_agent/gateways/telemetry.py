"""Authenticated, bounded, read-only gateways for the pilot telemetry Services."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import stat
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

MAX_BODY = 262144
MAX_RESPONSE = 8 * 1024 * 1024
UIDS = {"sregym-prometheus", "sregym-loki"}
PROM_READ = re.compile(
    r"^/api/v1/(query|query_range|query_exemplars|series|labels|label/[^/]+/values|metadata|rules|alerts|targets|status/buildinfo|status/runtimeinfo|format_query|parse_query)$"
)


def secret(path):
    p = Path(path)
    s = p.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
        raise ValueError("Private regular mode-0600 file required")
    value = p.read_text().strip()
    if not value:
        raise ValueError("Empty private credential")
    return value


def clean_path(target):
    u = urlsplit(target)
    if u.scheme or u.netloc or u.fragment or not u.path.startswith("/"):
        return None
    p = unquote(u.path)
    if "%" in p or chr(92) in p or "//" in p or any(s in [".", ".."] for s in p.split("/")):
        return None
    return p


def allowed(mode, method, target, body=b"", datasource_ids=()):
    p = clean_path(target)
    if p is None or len(target) > 16384 or len(body) > MAX_BODY:
        return False
    if mode == "prometheus":
        if method in ["GET", "HEAD"]:
            return p in ["/-/ready", "/-/healthy"] or bool(PROM_READ.fullmatch(p))
        return method == "POST" and p in [
            "/api/v1/query",
            "/api/v1/query_range",
            "/api/v1/series",
            "/api/v1/labels",
            "/api/v1/query_exemplars",
            "/api/v1/format_query",
            "/api/v1/parse_query",
        ]
    if method in ["GET", "HEAD"] and p in [
        "/api/health",
        "/api/datasources",
        "/api/search",
        "/api/org",
        "/api/user",
        "/api/folders",
        "/api/annotations",
        "/api/access-control/user/permissions",
    ]:
        return True
    if method in ["GET", "HEAD"] and re.fullmatch(r"/api/(dashboards/uid|folders)/[A-Za-z0-9_-]+", p):
        return True
    if method in ["GET", "HEAD"] and re.fullmatch(
        r"/api/datasources/(uid/(sregym-prometheus|sregym-loki)|name/[^/]+)(/health)?", p
    ):
        return True
    match = re.fullmatch(r"/api/datasources/proxy/(uid/(sregym-prometheus|sregym-loki)|([0-9]+))(/.*)?", p)
    if match:
        if match[3] is not None and int(match[3]) not in datasource_ids:
            return False
        suffix = match[4] or "/"
        if "/admin" in suffix or "/delete" in suffix or "/config" in suffix:
            return False
        if method in ["GET", "HEAD"]:
            return True
        return method == "POST" and suffix in [
            "/api/v1/query",
            "/api/v1/query_range",
            "/api/v1/series",
            "/loki/api/v1/query",
            "/loki/api/v1/query_range",
        ]
    if method == "POST" and p == "/api/ds/query":
        try:
            doc = json.loads(body)
            queries = doc["queries"]
            return (
                isinstance(queries, list)
                and 0 < len(queries) <= 32
                and all(q.get("datasource", {}).get("uid") in UIDS for q in queries)
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
    return False


def authenticated(config, header):
    if config["mode"] == "grafana":
        expected = "Bearer " + secret(config["credential_file"])
    else:
        expected = (
            "Basic "
            + base64.b64encode((config["username"] + ":" + secret(config["credential_file"])).encode()).decode()
        )
    return hmac.compare_digest(header.encode(), expected.encode())


def read_body(stream, headers):
    transfer = headers.get("Transfer-Encoding", "").lower().strip()
    length = headers.get("Content-Length")
    if transfer:
        if transfer != "chunked" or length is not None:
            raise ValueError("Unsupported or ambiguous framing")
        chunks, total = [], 0
        while True:
            line = stream.readline(128)
            if not line.endswith(b"\r\n") or not re.fullmatch(b"[0-9A-Fa-f]+\r\n", line):
                raise ValueError("Invalid chunk header")
            size = int(line.strip(), 16)
            total += size
            if total > MAX_BODY:
                raise OverflowError("Body too large")
            if size == 0:
                if stream.readline(128) != b"\r\n":
                    raise ValueError("Trailers not accepted")
                return b"".join(chunks)
            data = stream.read(size)
            if len(data) != size or stream.read(2) != b"\r\n":
                raise ValueError("Truncated chunk")
            chunks.append(data)
    if length is None:
        return b""
    if not length.isdigit():
        raise ValueError("Invalid content length")
    size = int(length)
    if size > MAX_BODY:
        raise OverflowError("Body too large")
    body = stream.read(size)
    if len(body) != size:
        raise ValueError("Truncated body")
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(secret(args.config))
    if cfg["mode"] not in ["grafana", "prometheus"] or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", cfg["upstream"]):
        raise ValueError("A supported mode and fixed loopback upstream are required")
    client = httpx.Client(timeout=30, trust_env=False, follow_redirects=False)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def reply(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def handle_request(self):
            code, size, authorized = 500, 0, False
            try:
                authorized = authenticated(cfg, self.headers.get("Authorization", ""))
                if not authorized:
                    code = 401
                    self.reply(code, b'{"error":"authentication required"}')
                    return
                try:
                    body = read_body(self.rfile, self.headers)
                except ValueError:
                    code = 400
                    self.reply(code, b'{"error":"unsupported body framing"}')
                    return
                except OverflowError:
                    code = 413
                    self.reply(code, b'{"error":"request too large"}')
                    return
                if not allowed(cfg["mode"], self.command, self.path, body, cfg.get("datasource_ids", [])):
                    code = 403
                    self.reply(code, b'{"error":"read-only route denied"}')
                    return
                guard = json.loads(Path(cfg["guard_file"]).read_text())
                if guard.get("valid_until", 0) <= time.time():
                    code = 503
                    self.reply(code, b'{"error":"upstream identity not currently verified"}')
                    return
                headers = {"Accept": self.headers.get("Accept", "application/json")}
                if self.headers.get("Content-Type"):
                    headers["Content-Type"] = self.headers["Content-Type"]
                if cfg["mode"] == "grafana":
                    if cfg["viewer_expires_at"] <= time.time():
                        code = 503
                        self.reply(code, b'{"error":"upstream viewer token expired"}')
                        return
                    headers["Authorization"] = "Bearer " + secret(cfg["viewer_token_file"])
                # Caller Host, cookies, and authorization never reach the origin.
                with client.stream(
                    self.command, cfg["upstream"] + self.path, headers=headers, content=body
                ) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE:
                            raise ValueError("Response exceeds bound")
                        chunks.append(chunk)
                    code = response.status_code
                    self.reply(code, b"".join(chunks), response.headers.get("content-type", "application/json"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                code = 502
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.reply(code, b'{"error":"upstream unavailable"}')
            finally:
                with Path(cfg["audit_file"]).open("a") as audit:
                    audit.write(
                        json.dumps(
                            {
                                "at": time.time(),
                                "method": self.command,
                                "path_sha256": hashlib.sha256(urlsplit(self.path).path.encode()).hexdigest(),
                                "status": code,
                                "response_bytes": size,
                                "authenticated": authorized,
                            }
                        )
                        + "\n"
                    )

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", cfg["listen_port"]), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
