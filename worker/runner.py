"""Runs only inside the disposable worker. No target code is executed by the UI host."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlencode
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

import psutil

BASE_URL = "http://127.0.0.1:8765"
SKIP_PATH = re.compile(r"(?:logout|signout|delete|remove|destroy|toggle|clear|reset|export|download|action|confirm)", re.I)
LOGIN_PATH = re.compile(r"/(?:accounts/)?(?:login|signin|sign-in)(?:/|$)", re.I)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(kind, **data):
    print("QA_EVENT " + json.dumps(dict(type=kind, **data), ensure_ascii=False), flush=True)


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 2)


def summarize_latency(values, errors, elapsed):
    total = len(values)
    return {"requests": total, "errors": errors, "error_percent": round(errors * 100 / total, 2) if total else None,
            "rps": round(total / elapsed, 2) if elapsed else None, "elapsed_s": round(elapsed, 2),
            "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95), "p99_ms": percentile(values, .99)}


def parse_django_tests(text):
    total = re.search(r"Ran (\d+) tests? in", text)
    def count(name):
        matches = re.findall(rf"\b{name}=(\d+)", text)
        return int(matches[-1]) if matches else 0
    if not total:
        return None
    result = {"total": int(total[1]), "failures": count("failures"), "errors": count("errors"), "skipped": count("skipped")}
    result["passed"] = max(0, result["total"] - result["failures"] - result["errors"] - result["skipped"])
    return result


def local_url(value, base=BASE_URL):
    absolute = urljoin(base, value)
    parsed = urlsplit(absolute)
    origin = urlsplit(BASE_URL)
    if (parsed.scheme, parsed.hostname, parsed.port) != (origin.scheme, origin.hostname, origin.port):
        return None
    if SKIP_PATH.search(parsed.path):
        return None
    return absolute.split("#")[0]


class LocalRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not local_url(newurl):
            raise HTTPError(newurl, 470, "External/destructive redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf = None
        self.password = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input":
            if values.get("name") == "csrfmiddlewaretoken":
                self.csrf = values.get("value")
            if values.get("type") == "password":
                self.password = True


class AppMonitor:
    def __init__(self, process, phase):
        self.process, self.phase = process, phase
        self.stop_event = threading.Event()
        self.samples = []
        self.error = None
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def loop(self):
        previous = {}
        started = time.monotonic()
        last = started
        while not self.stop_event.is_set():
            current = time.monotonic()
            try:
                parent = psutil.Process(self.process.pid)
                tree = [parent] + parent.children(recursive=True)
            except psutil.Error as exc:
                self.error = str(exc)
                break
            cpu_seconds = memory = reads = writes = 0
            active = {}
            for process in tree:
                try:
                    key = (process.pid, process.create_time())
                    times = process.cpu_times()
                    consumed = times.user + times.system
                    if key in previous:
                        cpu_seconds += max(0, consumed - previous[key])
                    active[key] = consumed
                    memory += process.memory_info().rss
                    try:
                        io = process.io_counters()
                        reads += io.read_bytes
                        writes += io.write_bytes
                    except (psutil.Error, AttributeError):
                        pass
                except psutil.Error:
                    continue
            if previous and current > last:
                sample = {"scope": "application", "timestamp": stamp(), "elapsed_s": round(current - started, 2),
                          "phase": self.phase(), "cpu_percent": round(cpu_seconds * 100 / (current - last), 2),
                          "memory_mb": round(memory / 1024**2, 2), "processes": len(active),
                          "read_mb": round(reads / 1024**2, 3), "write_mb": round(writes / 1024**2, 3)}
                self.samples.append(sample)
                emit("resource", sample=sample)
            previous, last = active, current
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)


class Runner:
    def __init__(self, config, source="/source", work="/work", output="/output"):
        self.cfg = config
        self.source, self.work, self.output = Path(source), Path(work), Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.repo = self.work / "repo"
        self.root = self.repo
        self.phase_name = "prepare"
        self.checks, self.screenshots, self.pages = [], [], []
        self.unit_tests = self.load = None
        self.server = self.monitor = None
        self.cancelled = False
        self.server_log = []
        self.username = "qa_" + secrets.token_hex(4)
        self.password = secrets.token_urlsafe(20)
        self.profile = None
        self.env = dict(os.environ, CI="true", PYTHONUNBUFFERED="1", PORT="8765", HOST="127.0.0.1",
                        DJANGO_SECRET_KEY=secrets.token_urlsafe(48), DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost,testserver",
                        DJANGO_DEBUG="true")
        self.python = sys.executable
        self.settings_module = None

    def phase(self, name, progress):
        self.phase_name = name
        emit("phase", phase=name, progress=progress)

    def log(self, line):
        line = str(line).replace(self.password, "[REDACTED]")
        emit("log", line=line[:6000])

    def check(self, name, status, details="", category="application", **extra):
        result = dict(id=f"check-{len(self.checks)+1}", name=name, status=status, details=str(details)[-16000:], category=category, **extra)
        self.checks.append(result)
        emit("check", check=result)
        return result

    @staticmethod
    def terminate(process):
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

    def run(self, command, timeout=300, env=None, label=None):
        if self.cancelled:
            raise InterruptedError("Execution cancelled")
        self.log("$ " + (label or " ".join(map(str, command))))
        process = subprocess.Popen(command, cwd=self.root, env=env or self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", start_new_session=True)
        lines = queue.Queue()
        def read():
            try:
                for line in iter(process.stdout.readline, ""):
                    lines.put(line[:20000])
            finally:
                lines.put(None)
        threading.Thread(target=read, daemon=True).start()
        output = []
        size = 0
        started = time.monotonic()
        try:
            while True:
                if self.cancelled:
                    raise InterruptedError("Execution cancelled")
                if time.monotonic() - started > timeout:
                    self.log(f"TIMEOUT after {timeout}s")
                    return 124, "".join(output) + f"\nTimeout after {timeout}s"
                try:
                    line = lines.get(timeout=.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                self.log(line.rstrip())
                if size < 2_000_000:
                    output.append(line)
                    size += len(line)
            return process.wait(timeout=max(.1, timeout - (time.monotonic() - started))), "".join(output)
        finally:
            self.terminate(process)
            process.stdout.close()

    def custom(self, value, timeout=300):
        return self.run(["/bin/sh", "-c", value], timeout=timeout)

    def prepare(self):
        shutil.copytree(self.source, self.repo, dirs_exist_ok=True, symlinks=False,
                        ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__", ".env", "*.sqlite3", "*.db"))
        chosen = self.cfg.get("subdir")
        if chosen:
            self.root = (self.repo / chosen).resolve()
            if not self.root.is_relative_to(self.repo.resolve()) or not self.root.is_dir():
                raise RuntimeError("زیرپوشهٔ پروژه پیدا نشد.")
        profile = self.cfg.get("profile", "auto")
        if profile == "auto":
            candidates = []
            for directory, dirs, files in os.walk(self.root):
                path = Path(directory)
                depth = len(path.relative_to(self.root).parts)
                dirs[:] = [x for x in dirs if not x.startswith(".") and x not in {"node_modules", "venv", "docs", "examples"}] if depth < 3 else []
                if "manage.py" in files:
                    candidates.append((depth, "django", path))
                elif "package.json" in files:
                    candidates.append((depth, "node", path))
            if candidates:
                candidates.sort(key=lambda x: (x[0], x[1] != "django", str(x[2])))
                depth, profile, self.root = candidates[0]
                if len([c for c in candidates if c[0] == depth]) > 1:
                    self.check("چند برنامه در مخزن", "warn", f"Selected {self.root.relative_to(self.repo)}. Set subdir to test another app.", "setup")
            elif (self.root / "requirements.txt").exists() or (self.root / "pyproject.toml").exists():
                profile = "python"
            else:
                raise RuntimeError("نوع پروژه شناسایی نشد؛ نوع و زیرپوشه را انتخاب کنید.")
        self.profile = profile
        self.check("شناسایی پروژه", "pass", f"Profile: {profile}; root: {self.root.relative_to(self.repo)}", "setup")
        emit("result", profile=profile, project_root=str(self.root.relative_to(self.repo)))
        self.phase("install", 24)
        if profile in {"django", "python"}:
            venv = self.work / "target-env"
            code, text = self.run([sys.executable, "-m", "venv", str(venv)], 90)
            if code:
                raise RuntimeError("ایجاد محیط Python ناموفق بود: " + text[-1500:])
            self.python = str(venv / "bin" / "python")
            self.env["PATH"] = str(venv / "bin") + os.pathsep + self.env.get("PATH", "")
            if (self.root / "requirements.txt").is_file():
                command = [self.python, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"]
            elif (self.root / "pyproject.toml").is_file():
                command = [self.python, "-m", "pip", "install", "."]
            else:
                command = None
            if command:
                code, text = self.run(command, 600)
                self.check("نصب وابستگی‌های Python", "pass" if code == 0 else "fail", text[-3500:], "setup")
                if code:
                    raise RuntimeError("نصب وابستگی‌ها ناموفق است؛ requirements.txt و خروجی pip را بررسی کنید.")
        elif profile == "node":
            self.package = json.loads((self.root / "package.json").read_text(encoding="utf-8"))
            command = ["npm", "ci" if (self.root / "package-lock.json").exists() else "install", "--no-audit", "--no-fund"]
            code, text = self.run(command, 600)
            self.check("نصب وابستگی‌های Node", "pass" if code == 0 else "fail", text[-3500:], "setup")
            if code:
                raise RuntimeError("نصب npm ناموفق بود. پروژه‌های pnpm/yarn را با فرمان آماده‌سازی تنظیم کنید.")
            if "build" in self.package.get("scripts", {}):
                code, text = self.run(["npm", "run", "build"], 600)
                self.check("ساخت برنامه", "pass" if code == 0 else "fail", text[-5000:], "setup")
                if code:
                    raise RuntimeError("Build پروژه ناموفق بود.")
        if self.cfg.get("setup_command"):
            code, text = self.custom(self.cfg["setup_command"], 600)
            self.check("آماده‌سازی اختصاصی", "pass" if code == 0 else "fail", text[-5000:], "setup")
            if code:
                raise RuntimeError("فرمان آماده‌سازی اختصاصی ناموفق بود.")

    def syntax_check(self):
        if self.profile not in {"django", "python"}:
            return
        errors = []
        checked = 0
        for directory, dirs, files in os.walk(self.root):
            dirs[:] = [x for x in dirs if x not in {".venv", "venv", "node_modules", ".git", "__pycache__"}]
            for name in files:
                path = Path(directory) / name
                if name.endswith(".py"):
                    checked += 1
                    try:
                        import tokenize
                        with tokenize.open(path) as file:
                            ast.parse(file.read(), filename=str(path.relative_to(self.root)))
                    except (SyntaxError, UnicodeError) as exc:
                        errors.append(str(exc))
                    if checked >= 5000:
                        break
            if checked >= 5000:
                break
        self.check("نحو فایل‌های Python", "fail" if errors else "pass", f"Parsed {checked} files.\n" + "\n".join(errors[:40]), "static")

    def configure_django(self):
        code = (self.root / "manage.py").read_text(encoding="utf-8")
        match = re.search(r"[\"']DJANGO_SETTINGS_MODULE[\"']\s*,\s*[\"']([\w.]+)[\"']", code)
        module = self.cfg.get("django_settings") or (match[1] if match else None)
        if not module:
            raise RuntimeError("ماژول تنظیمات Django پیدا نشد؛ django_settings را وارد کنید.")
        self.settings_module = module
        if self.cfg.get("run_deploy_check", True):
            deployment_env = dict(self.env, DJANGO_SETTINGS_MODULE=module, DJANGO_DEBUG="false")
            code, text = self.run([self.python, "manage.py", "check", "--deploy"], 90, env=deployment_env)
            warning = "WARNINGS:" in text or "warning" in text.lower()
            self.check("تنظیمات انتشار Django", "fail" if code else ("warn" if warning else "pass"),
                "Audit uses test environment variables, not live production configuration.\n" + text[-10000:], "configuration")
        # Force an ephemeral local SQLite database, independently of the project's DATABASE_URL.
        tools = self.work / "qa-tools"
        tools.mkdir(exist_ok=True)
        settings = f'''from {module} import *
DATABASES = {{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": {str(self.work / "qa.sqlite3")!r}}}}}
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
DEBUG = True
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
'''
        (tools / "azmayar_test_settings.py").write_text(settings, encoding="utf-8")
        self.env["PYTHONPATH"] = str(tools) + os.pathsep + str(self.root)
        self.env["DJANGO_SETTINGS_MODULE"] = "azmayar_test_settings"
        code, text = self.run([self.python, "manage.py", "check"], 90)
        self.check("بررسی ساختار Django", "pass" if code == 0 else "fail", text[-5000:], "configuration")
        if code:
            raise RuntimeError("Django system check ناموفق بود.")
        code, text = self.run([self.python, "manage.py", "migrate", "--noinput"], 180)
        self.check("ساخت دیتابیس آزمایشی", "pass" if code == 0 else "fail", text[-3000:], "setup")
        if code:
            raise RuntimeError("Migration روی دیتابیس آزمایشی ناموفق است؛ برنامه ممکن است به PostgreSQL نیاز داشته باشد.")

    def existing_tests(self):
        self.phase("tests", 43)
        self.syntax_check()
        if self.profile == "django":
            self.configure_django()
        if not self.cfg.get("run_tests", True):
            self.check("تست‌های مخزن", "skip", "Disabled by user.", "unit")
            return
        custom = self.cfg.get("test_command")
        if custom:
            code, text = self.custom(custom, 600)
        elif self.profile == "django":
            code, text = self.run([self.python, "manage.py", "test", "--noinput", "--verbosity", "2"], 600)
            self.unit_tests = parse_django_tests(text)
        elif self.profile == "node":
            if "test" not in self.package.get("scripts", {}):
                self.check("تست‌های مخزن", "skip", "package.json has no test script.", "unit")
                return
            code, text = self.run(["npm", "test"], 600)
        else:
            if not any(self.root.rglob("test*.py")):
                self.check("تست‌های مخزن", "skip", "No test*.py files found. Set a custom test command.", "unit")
                return
            installed, _ = self.run([self.python, "-m", "pip", "install", "pytest"], 180)
            if installed:
                self.check("تست‌های مخزن", "fail", "Could not install pytest.", "setup")
                return
            code, text = self.run([self.python, "-m", "pytest", "-q", f"--junitxml={self.output / 'junit.xml'}"], 600)
        state = "pass" if code == 0 else "fail"
        if (self.profile == "django" and not custom and (self.unit_tests is None or self.unit_tests["total"] == 0)) or code == 5:
            state = "skip" if code in {0, 5} else "fail"
        self.check("تست‌های مخزن", state, text[-15000:], "unit", unit_tests=self.unit_tests)
        emit("result", unit_tests=self.unit_tests)

    def create_test_user(self):
        if self.profile != "django":
            return
        helper = '''import django,os,json
django.setup()
from django.contrib.auth import get_user_model
U=get_user_model()
key=U.USERNAME_FIELD
name=os.environ['QA_USERNAME']
value=(name+'@example.test') if key=='email' else name
obj=U(**{key:value})
obj.set_password(os.environ['QA_PASSWORD'])
if hasattr(obj,'is_active'): obj.is_active=True
obj.save()
print('QA_LOGIN_ID='+value)
'''
        environment = dict(self.env, QA_USERNAME=self.username, QA_PASSWORD=self.password)
        code, text = self.run([self.python, "-c", helper], 60, env=environment, label="Create temporary regular test account")
        if code:
            self.check("حساب آزمایشی ورود", "warn", "Could not create a regular test account; provide a setup command and login scenario for custom auth.\n" + text[-3000:], "setup")
            self.username = None
        else:
            match = re.search(r"QA_LOGIN_ID=(.+)", text)
            self.username = match[1].strip() if match else self.username

    def start_server(self):
        self.phase("server", 58)
        if self.cfg.get("start_command"):
            command = ["/bin/sh", "-c", self.cfg["start_command"]]
        elif self.profile == "django":
            command = [self.python, "manage.py", "runserver", "127.0.0.1:8765", "--noreload"]
        elif self.profile == "node":
            scripts = self.package.get("scripts", {})
            dependencies = dict(self.package.get("dependencies", {}), **self.package.get("devDependencies", {}))
            if "start" in scripts:
                command = ["npm", "start"]
                if "next" in dependencies:
                    command += ["--", "--hostname", "127.0.0.1", "--port", "8765"]
                elif "vite" in dependencies:
                    command += ["--", "--host", "127.0.0.1", "--port", "8765"]
            elif "dev" in scripts:
                command = ["npm", "run", "dev"]
                if "vite" in dependencies:
                    command += ["--", "--host", "127.0.0.1", "--port", "8765"]
            else:
                self.check("اجرای سرور", "skip", "No npm start/dev script; specify start_command.", "setup")
                return False
        else:
            self.check("اجرای سرور", "skip", "برای پروژهٔ Python عمومی، فرمان شروع با پورت 8765 وارد کنید.", "setup")
            return False
        self.log("$ " + " ".join(command))
        self.server = subprocess.Popen(command, cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", start_new_session=True)
        def read_server():
            for line in iter(self.server.stdout.readline, ""):
                self.server_log.append(line[:6000])
                if len(self.server_log) > 2000:
                    self.server_log.pop(0)
                self.log("[server] " + line.rstrip())
        threading.Thread(target=read_server, daemon=True).start()
        self.monitor = AppMonitor(self.server, lambda: self.phase_name)
        self.monitor.start()
        begin = time.monotonic()
        while time.monotonic() - begin < 90:
            if self.cancelled:
                raise InterruptedError("Execution cancelled")
            if self.server.poll() is not None:
                self.check("اجرای سرور", "fail", "Server exited.\n" + "".join(self.server_log)[-7000:], "runtime")
                return False
            try:
                with build_opener(LocalRedirect()).open(BASE_URL, timeout=2) as response:
                    status = response.status
                self.check("اجرای سرور", "pass", f"HTTP {status}; ready after {time.monotonic()-begin:.2f}s", "runtime")
                return True
            except HTTPError as exc:
                if exc.code < 500:
                    self.check("اجرای سرور", "pass", f"Server reachable (HTTP {exc.code}); routes checked separately.", "runtime")
                    return True
                self.check("اجرای سرور", "fail", f"Root returns HTTP {exc.code}; see server log.", "runtime")
                return False
            except OSError:
                time.sleep(.3)
        self.check("اجرای سرور", "fail", "Server not ready at 127.0.0.1:8765 within 90s. Check start_command/PORT.", "runtime")
        return False

    def save_screenshot(self, page, name):
        try:
            filename = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:120] + ".png"
            page.screenshot(path=str(self.output / filename), full_page=True, timeout=10000)
            self.screenshots.append(filename)
            return filename
        except Exception as exc:
            self.log(f"Screenshot unavailable: {exc}")
            return None

    def browser(self):
        self.phase("browser", 68)
        from playwright.sync_api import sync_playwright, expect
        cookie_header = ""
        authenticated = False
        urls = [BASE_URL + path for path in self.cfg.get("paths", ["/"]) if local_url(BASE_URL + path)]
        visited = []
        if not self.cfg.get("run_browser", True):
            self.check("بررسی مرورگر", "skip", "Disabled by user. Django HTTP login is attempted independently for the load test.", "browser")
            cookie_header, authenticated = self.http_session()
            return cookie_header, urls, authenticated
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="fa-IR")
            # Prevent browser navigation/request to non-local destinations, including redirects.
            blocked = []
            def route_request(route):
                if self.same_origin(route.request.url) or route.request.url.startswith(("data:", "blob:")):
                    route.continue_()
                else:
                    blocked.append(route.request.url)
                    route.abort()
            context.route("**/*", route_request)
            page = context.new_page()
            errors = []
            console_errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            try:
                page.goto(BASE_URL, wait_until="networkidle", timeout=20000)
                password = page.locator('input[type="password"]')
                if password.count() and self.username:
                    user = page.locator('input[name="username"], input[name="email"], input[autocomplete="username"]').first
                    user.fill(self.username)
                    password.first.fill(self.password)
                    page.locator('button[type="submit"], input[type="submit"]').first.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                    authenticated = not page.locator('input[type="password"]').count() and not LOGIN_PATH.search(urlsplit(page.url).path)
                    self.check("ورود با حساب آزمایشی", "pass" if authenticated else "fail", f"Temporary regular account. Final path: {urlsplit(page.url).path}", "authentication")
                    if not authenticated:
                        self.save_screenshot(page, "login-failed")
                elif password.count():
                    self.check("ورود با حساب آزمایشی", "skip", "Login form exists but no compatible test account was created.", "authentication")
                else:
                    self.check("ورود خودکار", "skip", "No compatible password login found at the root redirect; configure a login scenario if needed.", "authentication")
            except Exception as exc:
                self.check("ورود با حساب آزمایشی", "fail", str(exc)[:3000], "authentication")
            if authenticated:
                urls = list(dict.fromkeys([page.url] + urls))
            cursor = 0
            while cursor < len(urls) and len(visited) < self.cfg.get("max_pages", 12):
                if self.cancelled:
                    raise InterruptedError("Execution cancelled")
                url = urls[cursor]
                cursor += 1
                if not local_url(url) or url in visited:
                    continue
                visited.append(url)
                path = urlsplit(url).path
                errors.clear()
                console_errors.clear()
                started = time.monotonic()
                try:
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    response = page.goto(url, wait_until="networkidle", timeout=20000)
                    elapsed = round((time.monotonic() - started) * 1000, 2)
                    code = response.status if response else 0
                    redirected_login = bool(LOGIN_PATH.search(urlsplit(page.url).path)) and not bool(LOGIN_PATH.search(path))
                    status = "fail" if code >= 400 or not code else ("skip" if redirected_login else "pass")
                    self.check(f"صفحه {path}", status, f"HTTP {code}; {elapsed} ms; final path {urlsplit(page.url).path}; authenticated={authenticated}." +
                        (" Protected content not exercised; redirected to login." if redirected_login else ""), "http", url=path, elapsed_ms=elapsed)
                    if not redirected_login and code < 400:
                        self.pages.append(path)
                    if elapsed > self.cfg.get("slow_ms", 1500):
                        self.check(f"زمان بارگذاری {path}", "warn", f"Browser navigation + network-idle: {elapsed} ms; threshold {self.cfg.get('slow_ms',1500)} ms. This includes Playwright network-idle wait.", "performance")
                    if errors:
                        self.check(f"خطای JavaScript {path}", "fail", "\n".join(dict.fromkeys(errors))[:6000], "javascript")
                    if console_errors:
                        self.check(f"پیام کنسول {path}", "warn", "console.error output; review alongside blocked external resources:\n" + "\n".join(dict.fromkeys(console_errors))[:6000], "javascript")
                    self.save_screenshot(page, f"desktop-{len(visited):02d}")
                    if not redirected_login and code < 400:
                        links = page.locator("a[href]").evaluate_all("els => els.map(x => x.href).slice(0,200)")
                        for link in links:
                            good = local_url(link)
                            if good and good not in urls and not urlsplit(good).path.startswith(("/admin/", "/static/", "/media/")):
                                urls.append(good)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.wait_for_timeout(150)
                    overflow = page.evaluate("({scroll: document.documentElement.scrollWidth, viewport: innerWidth})")
                    self.check(f"نمای موبایل {path}", "warn" if overflow["scroll"] > overflow["viewport"] + 4 else "pass",
                        f"Horizontal width {overflow['scroll']}px; viewport {overflow['viewport']}px. This checks overflow only, not complete visual correctness.", "responsive")
                    if len(visited) <= 4 or status == "fail":
                        self.save_screenshot(page, f"mobile-{len(visited):02d}")
                except Exception as exc:
                    self.check(f"صفحه {path}", "fail", str(exc)[:4000], "browser", url=path)
                    self.save_screenshot(page, f"failed-{len(visited):02d}")
            self.run_scenarios(page, expect)
            if blocked:
                self.check("منابع بیرونی مرورگر", "warn", "External browser requests were blocked by test policy, not an application defect:\n" + "\n".join(dict.fromkeys(blocked))[:5000], "infrastructure")
            # Custom scenarios may establish cookies; cookie presence alone is not proof of login.
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in context.cookies(BASE_URL))
            browser.close()
        return cookie_header, [BASE_URL + p for p in self.pages] or [BASE_URL], authenticated

    def http_session(self):
        if self.profile != "django" or not self.username:
            return "", False
        jar = CookieJar()
        opener = build_opener(LocalRedirect(), HTTPCookieProcessor(jar))
        try:
            with opener.open(BASE_URL, timeout=10) as response:
                login_url = response.url
                form = LoginForm()
                form.feed(response.read(500000).decode("utf-8", "replace"))
            if not form.password:
                return "", False
            data = {"username": self.username, "password": self.password}
            if form.csrf:
                data["csrfmiddlewaretoken"] = form.csrf
            request = Request(login_url, data=urlencode(data).encode(), headers={"Referer": login_url})
            with opener.open(request, timeout=10) as response:
                final_form = LoginForm()
                final_form.feed(response.read(500000).decode("utf-8", "replace"))
                authenticated = not final_form.password and not bool(LOGIN_PATH.search(urlsplit(response.url).path))
            self.check("ورود HTTP برای فشار", "pass" if authenticated else "warn", "Temporary Django account; no browser interaction was performed.", "authentication")
            return "; ".join(f"{c.name}={c.value}" for c in jar), authenticated
        except Exception as exc:
            self.check("ورود HTTP برای فشار", "warn", str(exc)[:3000], "authentication")
            return "", False

    @staticmethod
    def same_origin(url):
        try:
            parsed = urlsplit(url)
            base = urlsplit(BASE_URL)
            return (parsed.scheme, parsed.hostname, parsed.port) == (base.scheme, base.hostname, base.port)
        except ValueError:
            return False

    def run_scenarios(self, page, expect):
        for number, scenario in enumerate(self.cfg.get("scenarios", []), 1):
            if self.cancelled:
                raise InterruptedError("Execution cancelled")
            step_index = 0
            try:
                for step_index, step in enumerate(scenario["steps"], 1):
                    action = step["action"]
                    value = step.get("value", "")
                    locator = page.locator(step.get("selector", "body"))
                    if action == "goto":
                        url = BASE_URL + value
                        if not self.same_origin(url):
                            raise ValueError("Scenario navigation must stay local.")
                        response = page.goto(url, wait_until="networkidle", timeout=20000)
                        if response and response.status >= 400:
                            raise AssertionError(f"HTTP {response.status}")
                    elif action == "fill":
                        locator.fill(value, timeout=8000)
                    elif action == "click":
                        locator.click(timeout=8000)
                    elif action == "select":
                        locator.select_option(value, timeout=8000)
                    elif action == "check":
                        locator.check(timeout=8000)
                    elif action == "expect_text":
                        expect(locator).to_contain_text(value, timeout=8000)
                    elif action == "expect_url":
                        expect(page).to_have_url(BASE_URL + value, timeout=8000)
                screenshot = self.save_screenshot(page, f"scenario-{number:02d}-pass")
                self.check(scenario["name"], "pass", f"{len(scenario['steps'])} scenario steps completed.", "scenario", screenshot=screenshot)
            except Exception as exc:
                screenshot = self.save_screenshot(page, f"scenario-{number:02d}-failed")
                self.check(scenario["name"], "fail", f"Step {step_index}: {str(exc)[:5000]}", "scenario", screenshot=screenshot)

    def load_test(self, cookie, urls, authenticated):
        self.phase("load", 88)
        if not self.cfg.get("run_load", True):
            self.check("آزمون فشار HTTP", "skip", "Disabled by user.", "load")
            return
        duration, users = self.cfg.get("duration", 15), self.cfg.get("users", 5)
        begin = time.monotonic()
        deadline = begin + duration
        lock = threading.Lock()
        latencies = []
        failures = []
        codes = {}
        urls = list(dict.fromkeys(u for u in urls if local_url(u)))[:5] or [BASE_URL]
        def client(index):
            opener = build_opener(LocalRedirect())
            count = 0
            while time.monotonic() < deadline and not self.cancelled:
                start = time.monotonic()
                url = urls[(index + count) % len(urls)]
                code = 0
                error = None
                try:
                    headers = {"User-Agent": "Azmayar-Local-Test/1.0"}
                    if cookie:
                        headers["Cookie"] = cookie
                    with opener.open(Request(url, headers=headers), timeout=5) as response:
                        response.read(512000)
                        code = response.status
                        if LOGIN_PATH.search(urlsplit(response.url).path) and not LOGIN_PATH.search(urlsplit(url).path):
                            error = "Redirected to login; protected route was not exercised."
                        elif code >= 400:
                            error = f"HTTP {code}"
                except HTTPError as exc:
                    code, error = exc.code, f"HTTP {exc.code}"
                except OSError as exc:
                    error = str(exc)[:300]
                elapsed = (time.monotonic() - start) * 1000
                with lock:
                    latencies.append(elapsed)
                    codes[str(code)] = codes.get(str(code), 0) + 1
                    if error:
                        failures.append({"path": urlsplit(url).path, "error": error})
                count += 1
                time.sleep(.15)
        with ThreadPoolExecutor(max_workers=users) as pool:
            list(pool.map(client, range(users)))
        elapsed = time.monotonic() - begin
        self.load = dict(summarize_latency(latencies, len(failures), elapsed), users=users,
                         requested_duration_s=duration, authenticated=authenticated,
                         session_cookies_sent=bool(cookie),
                         paths=[urlsplit(x).path for x in urls], status_codes=codes, error_examples=failures[:12])
        if not latencies:
            state = "skip" if self.cancelled else "fail"
        elif failures:
            state = "fail"
        elif self.load["p95_ms"] > self.cfg.get("slow_ms", 1500):
            state = "warn"
        else:
            state = "pass"
        self.check("آزمون فشار HTTP", state, json.dumps(self.load, ensure_ascii=False, indent=2), "load")
        emit("result", load=self.load)

    def execute(self):
        exit_code = 0
        try:
            self.prepare()
            self.existing_tests()
            if self.cfg.get("run_browser", True) or self.cfg.get("run_load", True):
                self.create_test_user()
                if self.start_server():
                    try:
                        cookie, urls, authenticated = self.browser()
                    except Exception as exc:
                        self.check("موتور مرورگر", "fail", str(exc)[:4000], "infrastructure")
                        cookie, urls, authenticated = "", [BASE_URL + p for p in self.cfg.get("paths", ["/"])], False
                    self.load_test(cookie, urls, authenticated)
                else:
                    self.check("مرورگر و فشار", "skip", "Server failed to start; no successful browser/load result is claimed.", "browser")
            else:
                self.check("مرورگر و فشار", "skip", "Disabled by user.", "browser")
        except InterruptedError as exc:
            exit_code = 2
            self.check("توقف اجرا", "skip", str(exc), "infrastructure")
        except Exception as exc:
            exit_code = 1
            self.check("ادامهٔ مراحل تست", "fail", str(exc)[:8000], "infrastructure")
        finally:
            self.phase("report", 97)
            if self.monitor:
                self.monitor.stop()
                if not self.monitor.samples:
                    self.check("پایش منابع برنامه", "warn", "No process samples were available. Values are unknown, not zero. " + (self.monitor.error or "The server may have run for less than one sampling interval."), "infrastructure")
            self.terminate(self.server)
            if self.server and self.server.stdout:
                self.server.stdout.close()
            result = dict(profile=self.profile, checks=self.checks, unit_tests=self.unit_tests, load=self.load,
                          screenshots=self.screenshots, browser_pages=self.pages,
                          resources=self.monitor.samples if self.monitor else [])
            (self.output / "worker-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            emit("result", **{k: v for k, v in result.items() if k not in {"checks", "resources"}})
        return exit_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/runner/config.json")
    parser.add_argument("--source", default="/source")
    parser.add_argument("--work", default="/work")
    parser.add_argument("--output", default="/output")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    runner = Runner(config, args.source, args.work, args.output)
    signal.signal(signal.SIGTERM, lambda *args: setattr(runner, "cancelled", True))
    signal.signal(signal.SIGINT, lambda *args: setattr(runner, "cancelled", True))
    return runner.execute()


if __name__ == "__main__":
    sys.exit(main())
