"""Strict validation for user input; repository commands never run on the host."""
import re
from urllib.parse import urlsplit, urlunsplit

PROVIDERS = {"github.com", "gitlab.com", "bitbucket.org"}
PROFILES = {"auto", "django", "node", "python"}


def repo_url(value):
    value = str(value).strip()
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in PROVIDERS
            or parsed.username or parsed.password or parsed.port
            or parsed.query or parsed.fragment):
        raise ValueError("آدرس HTTPS مخزن عمومی GitHub، GitLab یا Bitbucket را وارد کنید.")
    path = parsed.path.rstrip("/").removesuffix(".git")
    if not re.fullmatch(r"/[\w.-]+(?:/[\w.-]+)+", path, flags=re.ASCII):
        raise ValueError("آدرس باید مستقیماً به مخزن اشاره کند؛ مثل github.com/user/repo")
    if any(p in {".", "..", "tree", "blob"} for p in path.split("/")[1:]):
        raise ValueError("آدرس صفحهٔ اصلی مخزن را وارد کنید؛ شاخه را در کادر جدا بنویسید.")
    if parsed.hostname in {"github.com", "bitbucket.org"} and len(path.split("/")) != 3:
        raise ValueError("آدرس صفحهٔ اصلی مخزن را وارد کنید.")
    return urlunsplit(("https", parsed.hostname, path + ".git", "", ""))


def integer(data, key, default, minimum, maximum):
    value = data.get(key, default)
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"مقدار {key} باید عدد صحیح باشد.")
    value = int(value)
    if not minimum <= value <= maximum:
        raise ValueError(f"مقدار {key} باید بین {minimum} و {maximum} باشد.")
    return value


def safe_path(value):
    value = str(value).strip()
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or "#" in value or any(ord(c) < 32 for c in value)
            or any(part == ".." for part in value.split("/"))):
        raise ValueError("مسیر صفحه باید محلی و با / شروع شود.")
    return value


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("تنظیمات باید یک شیء JSON باشد.")
    result = {"repo_url": repo_url(data.get("repo_url", ""))}
    branch = str(data.get("branch", "")).strip()
    if branch and (len(branch) > 120 or not re.fullmatch(r"[\w][\w./-]*", branch, re.ASCII)
                   or ".." in branch or "@{" in branch or branch.endswith(("/", "."))):
        raise ValueError("نام شاخه نامعتبر است.")
    result["branch"] = branch
    subdir = str(data.get("subdir", "")).strip().replace("\\", "/")
    if subdir and (subdir.startswith("/") or ":" in subdir
                   or any(x in {"..", ".", ""} for x in subdir.split("/"))):
        raise ValueError("زیرپوشه باید مسیر نسبی داخل مخزن باشد.")
    result["subdir"] = subdir
    profile = data.get("profile", "auto")
    if profile not in PROFILES:
        raise ValueError("نوع پروژه معتبر نیست.")
    result["profile"] = profile
    for key, default, low, high in [
        ("users", 5, 1, 50), ("duration", 15, 5, 120),
        ("max_pages", 12, 1, 30), ("memory_mb", 2048, 512, 8192),
        ("cpus", 2, 1, 8), ("timeout_minutes", 25, 3, 60),
        ("slow_ms", 1500, 100, 30000),
    ]:
        result[key] = integer(data, key, default, low, high)
    for key in ["run_browser", "run_load", "run_tests", "run_deploy_check"]:
        value = data.get(key, True)
        if not isinstance(value, bool):
            raise ValueError(f"{key} باید true یا false باشد.")
        result[key] = value
    paths = data.get("paths", ["/"])
    if isinstance(paths, str):
        paths = [x.strip() for x in paths.splitlines() if x.strip()]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 30:
        raise ValueError("بین ۱ تا ۳۰ مسیر صفحه وارد کنید.")
    result["paths"] = list(dict.fromkeys(safe_path(x) for x in paths))
    for key in ["setup_command", "test_command", "start_command", "django_settings"]:
        value = str(data.get(key, "")).strip()
        if len(value) > 3000 or "\x00" in value:
            raise ValueError("طول فرمان بیش از حد مجاز است.")
        result[key] = value
    if result["django_settings"] and not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", result["django_settings"]):
        raise ValueError("نام ماژول تنظیمات Django معتبر نیست.")
    scenarios = data.get("scenarios", [])
    if not isinstance(scenarios, list) or len(scenarios) > 30:
        raise ValueError("حداکثر ۳۰ سناریو مجاز است.")
    actions = {"goto", "fill", "click", "expect_text", "expect_url", "select", "check"}
    for scenario in scenarios:
        if (not isinstance(scenario, dict) or not isinstance(scenario.get("name"), str)
                or not scenario["name"].strip() or len(scenario["name"]) > 200
                or not isinstance(scenario.get("steps"), list) or not 1 <= len(scenario["steps"]) <= 80):
            raise ValueError("هر سناریو به name و ۱ تا ۸۰ steps نیاز دارد.")
        for step in scenario["steps"]:
            if not isinstance(step, dict) or step.get("action") not in actions:
                raise ValueError("عملیات سناریو معتبر نیست.")
            for key in ("selector", "value"):
                if key in step and (not isinstance(step[key], str) or len(step[key]) > 5000):
                    raise ValueError("مقدار یا انتخابگر سناریو معتبر نیست.")
            if step["action"] == "goto":
                safe_path(step.get("value", ""))
            elif step["action"] in {"fill", "click", "select", "check"} and not step.get("selector"):
                raise ValueError("برای این عملیات selector لازم است.")
    result["scenarios"] = scenarios
    return result
