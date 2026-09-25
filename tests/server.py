"""Local fixture HTTP server for tests (no internet needed).

Serves tests/site/ plus two dynamic endpoints:
  /flaky      -> 503 twice, then 200 (exercises retry/backoff)
  /mutable    -> body controlled by FixtureServer.mutable (change detection),
                 with an ETag that honors If-None-Match
  /limited    -> 429 with `Retry-After: FixtureServer.retry_after` once, then 200
  /redirect/P -> 302 to /P on this server
  /away       -> 302 to about.html on "localhost" (a different host name, same server)
Set FixtureServer.robots_status to make /robots.txt answer with that error code.
Every request path is recorded in FixtureServer.log. FixtureServer(tls=True) serves
HTTPS with a throwaway self-signed certificate (see make_cert).
"""
import base64
import hashlib
import os
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).parent / "site"


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server.fixture
        with srv.lock:
            srv.log.append((self.path, self.headers.get("User-Agent")))
        if self.path == "/robots.txt" and srv.robots_status:
            self.send_error(srv.robots_status)
            return
        if self.path.startswith("/redirect/"):
            return self._redirect(self.path[len("/redirect"):])
        if self.path == "/away":
            return self._redirect(f"http://localhost:{self.server.server_address[1]}/about.html")
        if self.path == "/limited":
            with srv.lock:
                srv.limited_hits += 1
                first = srv.limited_hits == 1
            if first:
                self.send_response(429)
                self.send_header("Retry-After", srv.retry_after)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send("<html><title>Limited OK</title></html>")
        if self.path == "/flaky":
            with srv.lock:
                srv.flaky_hits += 1
                fail = srv.flaky_hits <= 2
            if fail:
                self.send_error(503)
                return
            return self._send("<html><title>Flaky OK</title></html>")
        if self.path == "/mutable":
            etag = '"%s"' % hashlib.sha1(srv.mutable.encode()).hexdigest()[:16]
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            return self._send(srv.mutable, {"ETag": etag})
        super().do_GET()

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, body, headers=None):
        data = body.encode()
        self.send_response(200)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_cert(directory: str) -> tuple[str, str]:
    """Self-signed cert and key for 127.0.0.1 in directory. Needs the openssl CLI."""
    if not shutil.which("openssl"):
        raise unittest.SkipTest("openssl CLI not found")
    cert, key = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", key, "-out", cert], check=True, capture_output=True)
    return cert, key


class FixtureServer:
    def __init__(self, tls: bool = False):
        self.log = []
        self.lock = threading.Lock()
        self.flaky_hits = 0
        self.limited_hits = 0
        self.retry_after = "1"
        self.robots_status = None
        self.mutable = "<html><title>v1</title><body><p>price 10</p></body></html>"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(SITE)))
        self.httpd.fixture = self
        self.cert = None
        if tls:
            self._tmp = tempfile.TemporaryDirectory()
            self.cert, key = make_cert(self._tmp.name)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.cert, key)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        scheme = "https" if tls else "http"
        self.url = f"{scheme}://127.0.0.1:{self.httpd.server_address[1]}/"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.cert:
            self._tmp.cleanup()

    def paths(self):
        return [p for p, _ in self.log]


class _ProxyHandler(SimpleHTTPRequestHandler):
    """Answers plain-HTTP proxied requests (absolute-URI request lines) itself and
    tunnels CONNECT requests to the real target. Optionally requires Basic auth."""

    def log_message(self, *a):
        pass

    def _authorized(self) -> bool:
        owner = self.server.owner
        got = self.headers.get("Proxy-Authorization")
        with owner.lock:
            owner.auth_seen.append(got)
        if owner.auth is None or got == "Basic " + base64.b64encode(owner.auth.encode()).decode():
            return True
        self.send_response(407)
        self.send_header("Proxy-Authenticate", 'Basic realm="test"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        with self.server.owner.lock:
            self.server.owner.log.append(self.path)
        if not self._authorized():
            return
        if self.path.endswith("/robots.txt"):
            self.send_error(404)
            return
        data = f"<html><title>via proxy</title><body><p>{self.path}</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_CONNECT(self):
        with self.server.owner.lock:
            self.server.owner.log.append("CONNECT " + self.path)
        if not self._authorized():
            return
        host, _, port = self.path.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)), timeout=5)
        except OSError:
            self.send_error(502)
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        conns = [self.connection, upstream]
        with upstream:
            while True:  # pipe bytes both ways until either side closes
                ready, _, _ = select.select(conns, [], [], 5)
                if not ready:
                    return
                for c in ready:
                    data = c.recv(65536)
                    if not data:
                        return
                    (upstream if c is self.connection else self.connection).sendall(data)


class ProxyServer:
    """A fake forward proxy. Logs the absolute URLs (plain HTTP) and "CONNECT host:port"
    lines it was asked for in .log, and each request's Proxy-Authorization in .auth_seen.
    With auth="user:pass" it answers 407 unless that Basic auth is sent."""

    def __init__(self, auth: str | None = None):
        self.log = []
        self.auth = auth
        self.auth_seen = []
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self.httpd.owner = self
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
