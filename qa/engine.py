"""One active job, ephemeral Docker workers, persistent real evidence."""
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    psutil = None

from .config import validate
from .report import resource_summary, write_reports

IMAGE = "azmayar-worker:1.0.0"
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
LIMITATIONS = [
    "بررسی خودکار همهٔ خطاها و قواعد کسب‌وکار را کشف نمی‌کند؛ برای گردش کار اختصاصی، سناریو اضافه کنید.",
    "کشف خودکار صفحات فقط لینک‌های محلی و خواندنی را بررسی می‌کند؛ فرم‌ها بدون سناریوی صریح ارسال نمی‌شوند.",
    "ابزار کد را کپی می‌کند و در Django دیتابیس SQLite تازه می‌سازد؛ کد و فرمان سفارشی همچنان می‌توانند به سرویس‌های بیرونی متصل شوند.",
    "مرورگر فقط به سرور محلی آزمایش دسترسی دارد؛ منابع CDN و API بیرونی در بررسی مرورگر مسدود می‌شوند.",
    "پایش برنامه فقط در بازهٔ اجرای سرور است؛ نصب و تست واحد در آمار کل کانتینر دیده می‌شود.",
    "این ابزار ممیزی امنیتی جامع یا اندازه‌گیری پوشش کد انجام نمی‌دهد.",
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_size(value):
    match = re.fullmatch(r"\s*([\d.]+)\s*([kMGT]?i?B)\s*", str(value), flags=re.I)
    if not match:
        return None
    unit = match.group(2).lower()
    factors = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1e6, "mib": 1024**2,
               "gb": 1e9, "gib": 1024**3, "tb": 1e12, "tib": 1024**4}
    return round(float(match.group(1)) * factors[unit] / 1024**2, 3)


def snapshot(source, destination):
    """Copy bytes only, no symlinks or local databases, with a bounded input."""
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".next", "dist", ".env", ".env.local", ".env.production"}
    total = count = 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        current = Path(directory)
        dirs[:] = [d for d in dirs if d not in ignored and not (current / d).is_symlink()]
        target = destination / current.relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            original = current / name
            if original.is_symlink() or name in ignored or name.endswith((".sqlite3", ".db", ".pyc")):
                continue
            count += 1
            total += original.stat().st_size
            if count > 15000 or total > 250 * 1024**2:
                raise ValueError("حجم کد از سقف ۲۵۰ MiB یا ۱۵۰۰۰ فایل بیشتر است.")
            shutil.copyfile(original, target / name)
            (target / name).chmod(0o644)
    return count


class Cancelled(Exception):
    pass


class Engine:
    def __init__(self, root, data_dir=None):
        self.root = Path(root)
        self.data = Path(data_dir or self.root / "data")
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.cancel_events = {}
        self.deadlines = {}
        self.active = None
        self.health_cache = None
        self.health_at = 0
        for file in self.data.glob("*/state.json"):
            try:
                value = json.loads(file.read_text(encoding="utf-8"))
                if value["status"] not in TERMINAL:
                    value["status"] = "interrupted"
                    value["phase"] = "interrupted"
                    value["limitations"] = value.get("limitations", []) + ["سرویس هنگام این اجرا بسته شده؛ گزارش ناقص است."]
                    write_reports(file.parent, value)
                    file.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                self.jobs[value["id"]] = value
            except (ValueError, OSError, KeyError):
                continue

    def health(self, refresh=False):
        if not refresh and self.health_cache and time.monotonic() - self.health_at < 12:
            return self.health_cache
        docker = shutil.which("docker")
        git = shutil.which("git")
        ready = False
        detail = "Docker نصب نیست؛ Docker Desktop یا Docker Engine را نصب و اجرا کنید."
        version = None
        if docker:
            try:
                response = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=8)
                ready = response.returncode == 0
                version = response.stdout.strip() if ready else None
                detail = "Docker آماده است." if ready else "Docker نصب است اما موتور آن اجرا نیست یا دسترسی ندارید."
            except (OSError, subprocess.TimeoutExpired):
                detail = "پاسخی از Docker دریافت نشد؛ وضعیت Docker Desktop را بررسی کنید."
        result = {"docker": ready, "docker_version": version, "git": bool(git), "host_monitor": bool(psutil),
                  "ready": ready and bool(git) and bool(psutil), "detail": detail,
                  "active_job": self.active, "image": IMAGE}
        if not git:
            result["detail"] += " Git هم باید نصب شود."
        if not psutil:
            result["detail"] += " وابستگی‌ها را با فایل راه‌انداز نصب کنید."
        self.health_cache = result
        self.health_at = time.monotonic()
        return result

    def save(self, job):
        with self.lock:
            directory = self.data / job["id"]
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / "state.tmp"
            temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            temporary.replace(directory / "state.json")

    def view(self, job_id, compact=False):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            value = json.loads(json.dumps(self.jobs[job_id]))
        value["summary"] = {s: sum(1 for c in value.get("checks", []) if c.get("status") == s) for s in ["pass", "fail", "warn", "skip"]}
        value["resource_summary"] = resource_summary(value.get("resources", []))
        if compact:
            for key in ["logs", "resources", "checks", "scenarios"]:
                value.pop(key, None)
        else:
            value["logs"] = value.get("logs", [])[-500:]
            value["resources"] = value.get("resources", [])[-1200:]
        return value

    def list(self):
        with self.lock:
            ids = sorted(self.jobs, key=lambda k: self.jobs[k]["created_at"], reverse=True)[:50]
        return [self.view(k, compact=True) for k in ids]

    def start(self, payload):
        config = validate(payload)
        if not self.health()["ready"]:
            raise ValueError(self.health()["detail"])
        with self.lock:
            if self.active:
                raise ValueError("یک آزمون در حال اجراست؛ ابتدا آن را تمام یا متوقف کنید.")
            job_id = uuid.uuid4().hex[:16]
            job = {"id": job_id, "created_at": now(), "status": "running", "phase": "clone", "progress": 2,
                   "config": config, "checks": [], "resources": [], "logs": [], "limitations": list(LIMITATIONS),
                   "load": None, "commit": None, "profile": None, "screenshots": [], "unit_tests": None}
            self.jobs[job_id] = job
            self.cancel_events[job_id] = threading.Event()
            self.active = job_id
            self.save(job)
        threading.Thread(target=self._execute, args=(job_id,), daemon=True, name=f"qa-{job_id}").start()
        return self.view(job_id)

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            if self.jobs[job_id]["status"] in TERMINAL:
                return self.view(job_id)
            self.cancel_events[job_id].set()
            self.jobs[job_id]["phase"] = "stopping"
        return self.view(job_id)

    def log(self, job, line):
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(line)).strip()[:6000]
        if not clean:
            return
        with self.lock:
            job["logs"].append(clean)
            if len(job["logs"]) > 3500:
                job["logs"] = job["logs"][-3500:]

    def event(self, job, line):
        if not line.startswith("QA_EVENT "):
            self.log(job, line)
            return
        try:
            event = json.loads(line[9:])
            kind = event.get("type")
            with self.lock:
                if kind == "phase":
                    job["phase"] = str(event["phase"])[:40]
                    job["progress"] = max(job["progress"], min(99, int(event.get("progress", 0))))
                elif kind == "check":
                    check = event["check"]
                    if isinstance(check, dict) and check.get("status") in {"pass", "fail", "warn", "skip"}:
                        job["checks"].append(check)
                elif kind == "resource":
                    sample = event["sample"]
                    if isinstance(sample, dict) and len(job["resources"]) < 18000:
                        sample["scope"] = "application"
                        job["resources"].append(sample)
                elif kind == "result":
                    for key in ["load", "profile", "unit_tests", "screenshots", "project_root", "browser_pages"]:
                        if key in event:
                            job[key] = event[key]
                elif kind == "log":
                    self.log(job, event.get("line", ""))
                if kind in {"phase", "check", "result"}:
                    self.save(job)
        except (ValueError, KeyError, TypeError):
            self.log(job, line)

    def command(self, job, args, timeout, cwd=None, events=False, env=None):
        """Drain pipes without blocking cancellation, including quiet child processes."""
        timeout = min(timeout, self.deadlines.get(job["id"], time.monotonic() + timeout) - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("زمان کل آزمون به پایان رسید.")
        self.log(job, "$ " + " ".join(map(str, args)))
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=os.name != "nt")
        lines = queue.Queue()
        def reader():
            try:
                for line in iter(process.stdout.readline, ""):
                    lines.put(line[:30000])
            finally:
                lines.put(None)
        threading.Thread(target=reader, daemon=True).start()
        begin = time.monotonic()
        output = []
        output_size = 0
        try:
            done = False
            while not done:
                if self.cancel_events[job["id"]].is_set():
                    raise Cancelled()
                if time.monotonic() - begin > timeout:
                    raise TimeoutError("زمان مجاز فرمان به پایان رسید.")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None:
                        continue
                    continue
                if line is None:
                    done = True
                else:
                    output.append(line)
                    output_size += len(line)
                    while output_size > 2_000_000 and len(output) > 1:
                        output_size -= len(output.pop(0))
                    if events:
                        self.event(job, line.rstrip())
                    else:
                        self.log(job, line)
            remaining = max(0.1, timeout - (time.monotonic() - begin))
            code = process.wait(timeout=remaining)
            return code, "".join(output)[-2_000_000:]
        finally:
            if process.poll() is None:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif psutil:
                    try:
                        children = psutil.Process(process.pid).children(recursive=True)
                        for child in children:
                            try:
                                child.kill()
                            except psutil.Error:
                                pass
                    except psutil.Error:
                        pass
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()

    def _monitor(self, job, container, stop):
        start = time.monotonic()
        psutil.cpu_percent(interval=None) if psutil else None
        initial_disk = psutil.disk_io_counters() if psutil else None
        initial_net = psutil.net_io_counters() if psutil else None
        while not stop.wait(1):
            elapsed = round(time.monotonic() - start, 2)
            if psutil:
                memory = psutil.virtual_memory()
                sample = {"scope": "host", "timestamp": now(), "elapsed_s": elapsed, "phase": job["phase"],
                          "cpu_percent": psutil.cpu_percent(interval=None), "memory_mb": round(memory.used / 1024**2, 2),
                          "memory_percent": memory.percent}
                disk, net = psutil.disk_io_counters(), psutil.net_io_counters()
                if disk and initial_disk:
                    sample.update(read_mb=round((disk.read_bytes-initial_disk.read_bytes)/1024**2, 3), write_mb=round((disk.write_bytes-initial_disk.write_bytes)/1024**2, 3))
                if net and initial_net:
                    sample.update(network_rx_mb=round((net.bytes_recv-initial_net.bytes_recv)/1024**2, 3), network_tx_mb=round((net.bytes_sent-initial_net.bytes_sent)/1024**2, 3))
                with self.lock:
                    job["resources"].append(sample)
            if int(elapsed) % 3:
                continue
            try:
                result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
                                        capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    stats = json.loads(result.stdout.splitlines()[0])
                    network = str(stats.get("NetIO", "0B / 0B")).split("/")
                    sample = {"scope": "container", "timestamp": now(), "elapsed_s": elapsed, "phase": job["phase"],
                              "cpu_percent": float(stats["CPUPerc"].removesuffix("%")),
                              "memory_mb": parse_size(stats["MemUsage"].split("/")[0]),
                              "memory_percent": float(stats["MemPerc"].removesuffix("%")),
                              "network_rx_mb": parse_size(network[0]), "network_tx_mb": parse_size(network[-1])}
                    disk = str(stats.get("BlockIO", "0B / 0B")).split("/")
                    sample.update(read_mb=parse_size(disk[0]), write_mb=parse_size(disk[-1]))
                    with self.lock:
                        job["resources"].append(sample)
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                continue

    def _execute(self, job_id):
        job = self.jobs[job_id]
        directory = self.data / job_id
        work = directory / "work"
        work.mkdir()
        container = "azmayar-" + job_id
        monitor_stop = threading.Event()
        monitor = None
        created = False
        begin = time.monotonic()
        try:
            cfg = job["config"]
            self.deadlines[job_id] = begin + cfg["timeout_minutes"] * 60
            git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT="0")
            command = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false", "-c", f"core.hooksPath={os.devnull}",
                       "clone", "--depth", "1", "--single-branch", "--no-recurse-submodules"]
            if cfg["branch"]:
                command += ["--branch", cfg["branch"]]
            code, _ = self.command(job, command + ["--", cfg["repo_url"], str(work / "checkout")], 180, env=git_env)
            if code:
                raise RuntimeError("دریافت مخزن ناموفق بود؛ آدرس، عمومی‌بودن مخزن، شاخه و اتصال اینترنت را بررسی کنید.")
            sha = subprocess.run(["git", "-C", str(work / "checkout"), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            job["commit"] = sha
            count = snapshot(work / "checkout", work / "source")
            self.event(job, "QA_EVENT " + json.dumps({"type": "check", "check": {"name": "دریافت و کپی مخزن", "status": "pass", "category": "setup", "details": f"Commit {sha}; {count} files; symlinks and existing databases excluded."}}))
            job["phase"], job["progress"] = "image", 10
            inspect = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=15)
            if inspect.returncode:
                self.log(job, "ساخت محیط اجرا در اولین استفاده؛ دانلود تصویر مرورگر ممکن است چند دقیقه طول بکشد.")
                code, _ = self.command(job, ["docker", "build", "--tag", IMAGE, str(self.root / "worker")], 1200)
                if code:
                    raise RuntimeError("ساخت محیط Docker ناموفق بود؛ خروجی نصب را بررسی کنید.")
            job["phase"], job["progress"] = "prepare", 18
            (work / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            args = ["docker", "create", "--name", container, "--init", "--cpus", str(cfg["cpus"]),
                    "--memory", f'{cfg["memory_mb"]}m', "--memory-swap", f'{cfg["memory_mb"]}m',
                    "--pids-limit", "256", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--shm-size", "256m", IMAGE]
            code, _ = self.command(job, args, 30)
            if code:
                raise RuntimeError("کانتینر ساخته نشد؛ ظرفیت Docker و تنظیمات محدودیت منابع را بررسی کنید.")
            created = True
            for src, dst in [(work / "source" / ".", "/source"), (work / "config.json", "/runner/config.json")]:
                # Path('/source/.') normalizes its suffix; append '/.' explicitly for docker cp semantics.
                actual = str(work / "source") + "/." if dst == "/source" else str(src)
                code, _ = self.command(job, ["docker", "cp", actual, f"{container}:{dst}"], 60)
                if code:
                    raise RuntimeError("انتقال نسخهٔ آزمایشی به کانتینر ناموفق بود.")
            monitor = threading.Thread(target=self._monitor, args=(job, container, monitor_stop), daemon=True)
            monitor.start()
            remaining = cfg["timeout_minutes"] * 60 - (time.monotonic() - begin)
            if remaining <= 0:
                raise TimeoutError("زمان کل اجرا تمام شد؛ زمان بیشتری در تنظیمات انتخاب کنید.")
            code, _ = self.command(job, ["docker", "start", "--attach", container], remaining, events=True)
            inspect_result = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", container], capture_output=True, text=True, timeout=10)
            if inspect_result.returncode == 0:
                state = json.loads(inspect_result.stdout)
                if state.get("OOMKilled"):
                    job["checks"].append({"name": "محدودیت حافظه", "status": "fail", "category": "infrastructure", "details": "Docker فرایند را به علت کمبود حافظه متوقف کرد؛ memory_mb را افزایش دهید."})
                actual_exit = state.get("ExitCode", code)
            else:
                actual_exit = code
            if actual_exit:
                raise RuntimeError(f"اجرای محیط تست با کد {actual_exit} پایان یافت؛ گزارش ممکن است ناقص باشد.")
            job["status"] = "completed"
        except Cancelled:
            job["status"] = "cancelled"
            job["limitations"].append("کاربر اجرا را متوقف کرده است؛ مراحل بعدی اجرا نشده‌اند.")
        except Exception as exc:
            job["status"] = "failed"
            job["checks"].append({"name": "ادامهٔ اجرای آزمون", "status": "fail", "category": "infrastructure", "details": str(exc)[:3000]})
            self.log(job, str(exc))
        finally:
            monitor_stop.set()
            if monitor:
                monitor.join(timeout=7)
            if created:
                try:
                    subprocess.run(["docker", "stop", "--time", "5", container], capture_output=True, timeout=12)
                    collected = directory / "artifacts"
                    collected.mkdir(exist_ok=True)
                    subprocess.run(["docker", "cp", f"{container}:/output/.", str(collected)], capture_output=True, timeout=25)
                    shots = []
                    for path in collected.glob("*.png"):
                        if path.is_symlink() or path.stat().st_size > 8 * 1024**2:
                            path.unlink(missing_ok=True)
                        else:
                            shots.append(path.name)
                    job["screenshots"] = shots[:80]
                    subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=15)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.log(job, f"Cleanup: {exc}")
                    job["limitations"].append(f"پاک‌سازی خودکار کامل نشد؛ کانتینر {container} را در Docker بررسی کنید.")
            shutil.rmtree(work, ignore_errors=True)
            with self.lock:
                job["finished_at"] = now()
                job["elapsed_s"] = round(time.monotonic() - begin, 2)
                job["phase"] = job["status"]
                job["progress"] = 100 if job["status"] == "completed" else job["progress"]
                write_reports(directory, job)
                self.save(job)
                self.active = None
                self.health_at = 0
                self.deadlines.pop(job_id, None)

