"""Local UI/API. Run on 127.0.0.1; never expose this job runner publicly."""
import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import unquote, urlsplit
import webbrowser

from qa.engine import Engine

ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    server_version = "Azmayar/1.0"

    def __init__(self, *args, engine=None, preview=False, token=None, **kwargs):
        self.engine, self.preview, self.token = engine, preview, token
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # Browser polling is intentionally quiet.
        pass

    def host_allowed(self):
        host = self.headers.get("Host", "").split(":")[0].lower()
        return self.preview or host in {"127.0.0.1", "localhost", "["}

    def respond(self, body, kind="application/json; charset=utf-8", status=200, download=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self.host_allowed():
            return self.respond({"error": "Invalid Host"}, status=403)
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/api/health":
                return self.respond(dict(self.engine.health(), session_token=self.token, preview=self.preview))
            if path == "/api/jobs":
                return self.respond(self.engine.list())
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{16})", path)
            if match:
                return self.respond(self.engine.view(match[1]))
            artifact = re.fullmatch(r"/api/jobs/([0-9a-f]{16})/files/([a-zA-Z0-9_.-]+)", path)
            if artifact:
                job_id, name = artifact.groups()
                job = self.engine.view(job_id)
                allowed = {"report.json": "application/json", "report.html": "text/html; charset=utf-8", "resources.csv": "text/csv; charset=utf-8"}
                if name in allowed:
                    target = self.engine.data / job_id / name
                    return self.respond(target.read_bytes(), allowed[name], download=name)
                if name in job.get("screenshots", []) and name.endswith(".png"):
                    target = self.engine.data / job_id / "artifacts" / name
                    if target.is_symlink():
                        raise KeyError(name)
                    return self.respond(target.read_bytes(), "image/png")
                raise KeyError(name)
            files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in files:
                name, kind = files[path]
                return self.respond((ROOT / "web" / name).read_bytes(), kind)
            if path == "/favicon.ico":
                return self.respond(b"", "image/x-icon", status=204)
            raise KeyError(path)
        except (KeyError, FileNotFoundError):
            self.respond({"error": "موردی پیدا نشد."}, status=404)

    def do_POST(self):
        if not self.host_allowed() or self.headers.get("X-QA-Token", "") != self.token:
            return self.respond({"error": "درخواست معتبر نیست؛ صفحه را دوباره باز کنید."}, status=403)
        # Preview is read-only; it cannot execute code through a remotely accessible proxy.
        if self.preview:
            return self.respond({"error": "این پیش‌نمایش فقط خواندنی است. برای اجرا، نسخهٔ محلی را باز کنید."}, status=403)
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{self.headers.get('Host')}", f"https://{self.headers.get('Host')}"}:
            return self.respond({"error": "Origin نامعتبر است."}, status=403)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 256000:
                return self.respond({"error": "حجم درخواست معتبر نیست."}, status=413)
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.respond({"error": "Content-Type باید application/json باشد."}, status=415)
            payload = json.loads(self.rfile.read(size))
            path = urlsplit(self.path).path
            if path == "/api/jobs":
                return self.respond(self.engine.start(payload), status=201)
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{16})/cancel", path)
            if match:
                return self.respond(self.engine.cancel(match[1]))
            return self.respond({"error": "مسیر پیدا نشد."}, status=404)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            self.respond({"error": str(exc)}, status=400)
        except KeyError:
            self.respond({"error": "آزمون پیدا نشد."}, status=404)


def make_server(port=8877, data_dir=None, preview=False):
    engine = Engine(ROOT, data_dir)
    handler = partial(Handler, engine=engine, preview=preview, token=secrets.token_urlsafe(32))
    server = ThreadingHTTPServer(("0.0.0.0" if preview else "127.0.0.1", port), handler)
    server.daemon_threads = True
    return server, engine


def main():
    parser = argparse.ArgumentParser(description="Azmayar local repository test lab")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8877")))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--preview", action="store_true", help="Read-only UI preview; disables all job execution")
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--strictPort", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        server, engine = make_server(args.port, preview=args.preview)
    except OSError as exc:
        print(f"Cannot start: {exc}. Try: python app.py --port 8878")
        raise SystemExit(1)
    address = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Azmayar is ready: {address}", flush=True)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        if engine.active:
            engine.cancel(engine.active)
            print("Stopping the active worker…", flush=True)
            import time
            deadline = time.monotonic() + 45
            while engine.active and time.monotonic() < deadline:
                time.sleep(0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
