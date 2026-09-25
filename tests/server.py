"""Local fixture HTTP server for tests (no internet needed).

Serves tests/site/ plus two dynamic endpoints:
  /flaky      -> 503 twice, then 200 (exercises retry/backoff)
  /mutable    -> body controlled by FixtureServer.mutable (change detection)
Set FixtureServer.robots_status to make /robots.txt answer with that error code.
Every request path is recorded in FixtureServer.log.
"""
import threading
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
        if self.path == "/flaky":
            with srv.lock:
                srv.flaky_hits += 1
                fail = srv.flaky_hits <= 2
            if fail:
                self.send_error(503)
                return
            return self._send("<html><title>Flaky OK</title></html>")
        if self.path == "/mutable":
            return self._send(srv.mutable)
        super().do_GET()

    def _send(self, body):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FixtureServer:
    def __init__(self):
        self.log = []
        self.lock = threading.Lock()
        self.flaky_hits = 0
        self.robots_status = None
        self.mutable = "<html><title>v1</title><body><p>price 10</p></body></html>"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(SITE)))
        self.httpd.fixture = self
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def paths(self):
        return [p for p, _ in self.log]
