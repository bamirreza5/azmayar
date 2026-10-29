"""Escaped, standalone reports; untrusted target output is always text."""
import csv
import html
import io
import json


def resource_summary(samples):
    result = {}
    for scope in ("application", "container", "host"):
        rows = [x for x in samples if x.get("scope") == scope]
        cpus = [x["cpu_percent"] for x in rows if isinstance(x.get("cpu_percent"), (int, float))]
        mems = [x["memory_mb"] for x in rows if isinstance(x.get("memory_mb"), (int, float))]
        result[scope] = {
            "samples": len(rows), "cpu_avg": round(sum(cpus) / len(cpus), 2) if cpus else None,
            "cpu_peak": max(cpus) if cpus else None,
            "memory_peak_mb": max(mems) if mems else None,
        }
    return result


def resource_csv(samples):
    buffer = io.StringIO(newline="")
    keys = ["timestamp", "elapsed_s", "scope", "phase", "cpu_percent", "memory_mb",
            "memory_percent", "processes", "read_mb", "write_mb", "network_rx_mb", "network_tx_mb"]
    writer = csv.DictWriter(buffer, keys, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(samples)
    return "\ufeff" + buffer.getvalue()


def standalone(job):
    esc = lambda v: html.escape(str(v if v is not None else "—"), quote=True)
    labels = {"pass": "موفق", "fail": "ناموفق", "warn": "هشدار", "skip": "اجرا نشد"}
    status = {"completed": "تکمیل شد", "failed": "اجرای ناقص", "cancelled": "متوقف شد", "interrupted": "قطع‌شده"}.get(job["status"], job["status"])
    rows = "".join(
        f'<tr><td>{esc(c.get("name"))}</td><td class="{esc(c.get("status"))}">{esc(labels.get(c.get("status"), c.get("status")))}</td><td><pre>{esc(c.get("details", ""))}</pre></td></tr>'
        for c in job.get("checks", [])
    )
    metrics = resource_summary(job.get("resources", []))
    resource_rows = "".join(f'<tr><td>{label}</td><td>{m["samples"]}</td><td>{esc(m["cpu_avg"])}</td><td>{esc(m["cpu_peak"])}</td><td>{esc(m["memory_peak_mb"])}</td></tr>'
        for scope, label in [("application", "فرایندهای برنامه"), ("container", "کل کانتینر"), ("host", "کل سیستم میزبان")]
        for m in [metrics[scope]])
    load = job.get("load") or {}
    load_text = " | ".join(f'{k}: {esc(load[k])}' for k in ["requests", "errors", "error_percent", "rps", "p50_ms", "p95_ms", "p99_ms", "elapsed_s"] if k in load) or "اجرا نشده"
    limits = "<li>" + "</li><li>".join(esc(x) for x in job.get("limitations", [])) + "</li>"
    return f'''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>گزارش آزمایار</title>
<style>body{{font:15px/1.9 Tahoma,Arial,sans-serif;max-width:1100px;margin:40px auto;padding:0 24px;color:#19263b}}h1{{color:#1845c2}}table{{border-collapse:collapse;width:100%;margin:20px 0}}td,th{{border:1px solid #dce3ee;padding:12px;text-align:right}}th{{background:#eff4ff}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;direction:ltr;text-align:left;font:12px/1.6 monospace}}.pass{{color:#137f54}}.fail{{color:#bb2849}}.warn{{color:#986200}}.skip{{color:#63708a}}.note{{padding:16px;background:#f3f6fb;border-radius:12px}}@media print{{body{{font-size:11px;margin:10px}}tr{{break-inside:avoid}}}}</style>
<h1>آزمایار · گزارش آزمون نرم‌افزار</h1><p>{esc(status)} · {esc(job.get("created_at"))}</p>
<p dir="ltr">{esc(job.get("config", {}).get("repo_url"))}<br>Commit: {esc(job.get("commit"))}</p>
<p class="note">قبولی آزمون‌ها به معنای نبودن تمام خطاهای نرم‌افزار نیست. نتیجه فقط به آزمون‌های اجراشده و نسخهٔ ثبت‌شده مربوط است. این گزارش از محیط آزمایشی تولید شده است.</p>
<h2>نتایج بررسی‌ها</h2><table><thead><tr><th>بررسی</th><th>وضعیت</th><th>جزئیات و شواهد</th></tr></thead><tbody>{rows}</tbody></table>
<h2>مصرف منابع</h2><table><tr><th>دامنه</th><th>تعداد نمونه</th><th>میانگین CPU ٪</th><th>بیشینه CPU ٪</th><th>بیشینه RAM (MiB)</th></tr>{resource_rows}</table>
<p>CPU برنامه و کانتینر: ۱۰۰٪ برابر یک هسته است. CPU میزبان: درصد کل ظرفیت سیستم. حافظه برنامه مجموع RSS فرایندهای سرور است و ممکن است صفحات اشتراکی را دوباره بشمارد. منابع کانتینر شامل نصب، تست و مرورگر نیز هست؛ منابع میزبان شامل سایر برنامه‌هاست. نمونهٔ ثبت‌نشده با خط تیره نمایش داده می‌شود.</p>
<h2>آزمون فشار HTTP</h2><p dir="ltr">{load_text}</p><p>این آزمون درخواست HTTP می‌فرستد؛ کاربران هم‌زمانِ مرورگر کامل را شبیه‌سازی نمی‌کند. زمان پاسخ با ساعت یکنواخت اندازه‌گیری می‌شود.</p>
<h2>حدود بررسی</h2><ul>{limits}</ul><h2>خروجی اجرا</h2><pre>{esc(chr(10).join(job.get("logs", [])))}</pre></html>'''


def write_reports(folder, job):
    (folder / "report.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "report.html").write_text(standalone(job), encoding="utf-8")
    (folder / "resources.csv").write_text(resource_csv(job.get("resources", [])), encoding="utf-8")
