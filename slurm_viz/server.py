"""HTTP server: routing, token check, static files."""

import errno
import hmac
import mimetypes
import os
import re
import secrets
import socket
import sys
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from . import views
from .render import error_page
from .slurm import SlurmError

STATIC_DIR = Path(__file__).parent / "static"

ROUTES = [
    (re.compile(r"^/$"), views.overview),
    (re.compile(r"^/running/?$"), views.running),
    (re.compile(r"^/pending/?$"), views.pending),
    (re.compile(r"^/completed/?$"), views.completed),
    (re.compile(r"^/job/([^/]+)/?$"), views.job_detail),
    (re.compile(r"^/job/([^/]+)/log$"), views.job_log),
]

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class Handler(BaseHTTPRequestHandler):
    app: views.App
    token: str
    cookie_name: str
    server_version = "slurm-viz"
    sys_version = ""

    def do_GET(self):
        url = urlsplit(self.path)
        params = dict(parse_qsl(url.query))

        if url.path == "/favicon.ico":
            return self.send_static("favicon.svg")
        if url.path.startswith("/static/"):
            return self.send_static(url.path.removeprefix("/static/"))

        # ?token=... -> set cookie and redirect to the same URL without the token
        given = params.pop("token", None)
        if given is not None:
            if not hmac.compare_digest(given, self.token):
                return self.send_html(403, error_page(403, "Wrong token. Use the URL printed when the server started."))
            location = url.path + ("?" + urlencode(params) if params else "")
            return self.send(302, b"", "text/plain", {
                "Location": location,
                "Set-Cookie": f"{self.cookie_name}={self.token}; HttpOnly; SameSite=Strict; Path=/",
            })
        if not self.authorized():
            return self.send_html(403, error_page(
                403, "Missing token. Open the URL with ?token=... printed when the server started."))

        if params.pop("fresh", None):  # refresh button: skip the cache for this request
            self.app.client.cache.clear()

        for pattern, view in ROUTES:
            m = pattern.match(url.path)
            if m:
                try:
                    page = view(self.app, params, *(unquote(g) for g in m.groups()))
                except SlurmError as exc:
                    page = views.Page(503, error_page(503, f"Could not read from Slurm: {exc}"))
                except Exception:
                    traceback.print_exc()
                    page = views.Page(500, error_page(500, "Internal error; see the server terminal."))
                return self.send(page.status, page.html.encode(), page.content_type)
        self.send_html(404, error_page(404, "Page not found"))

    def authorized(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(self.cookie_name)
        return morsel is not None and hmac.compare_digest(morsel.value, self.token)

    def send_static(self, name: str):
        path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in path.parents or not path.is_file():
            return self.send_html(404, error_page(404, "Not found"))
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send(200, path.read_bytes(), ctype, {"Cache-Control": "max-age=300"})

    def send_html(self, status: int, html: str):
        self.send(status, html.encode(), "text/html; charset=utf-8")

    def send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(status)
        merged = {**SECURITY_HEADERS, **(headers or {})}
        merged["Content-Type"] = ctype
        merged["Content-Length"] = str(len(body))
        for k, v in merged.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # never log the token
        line = re.sub(r"token=[^&\s]+", "token=***", fmt % args)
        sys.stderr.write(f"{self.log_date_time_string()} {line}\n")


def bind(host: str, port: int, handler, tries: int = 20) -> ThreadingHTTPServer:
    """Bind to port, or the next free one if someone else on the node already uses it."""
    for p in range(port, port + tries):
        try:
            return ThreadingHTTPServer((host, p), handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    raise SystemExit(f"No free port in {port}-{port + tries - 1}")


def serve(client, args) -> int:
    token = secrets.token_urlsafe(18)
    app = views.App(client=client, page_size=args.page_size, refresh=args.refresh)
    handler = type("BoundHandler", (Handler,), {"app": app, "token": token, "cookie_name": ""})
    httpd = bind(args.host, args.port, handler)
    port = httpd.server_address[1]
    handler.cookie_name = f"slurmviz_{port}"  # one cookie per port, so two instances don't clash

    try:
        client.all_jobs()  # fail fast if Slurm is unreachable
    except SlurmError as exc:
        print(f"Warning: Slurm query failed: {exc}", file=sys.stderr)

    url = f"http://localhost:{port}/?token={token}"
    host = socket.gethostname()
    busy = f" (port {args.port} was busy)" if port != args.port else ""
    if os.environ.get("SLURM_JOB_ID"):
        # inside a Slurm job on a compute node: hop through the login node
        tunnel = f"ssh -N -J {client.user}@login.ncshare.org -L {port}:localhost:{port} {client.user}@{host}"
    else:
        tunnel = f"ssh -N -L {port}:localhost:{port} {client.user}@login.ncshare.org   (the login node is {host})"
    print(f"""
Slurm job dashboard for {client.user} on {host}, {args.host}:{port}{busy}

  1. On your laptop:  {tunnel}
  2. Open:            {url}

Press Ctrl+C to stop.
""", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0
