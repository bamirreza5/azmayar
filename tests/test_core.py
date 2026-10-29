import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qa.config import validate, repo_url, safe_path
from qa.engine import Engine, Cancelled, snapshot, parse_size
from qa.report import standalone, resource_summary, resource_csv
from app import make_server

spec = importlib.util.spec_from_file_location("worker_runner", ROOT / "worker" / "runner.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class ValidationTests(unittest.TestCase):
    def test_valid_github_default(self):
        cfg = validate({"repo_url": "https://github.com/bamirreza5/Dideban/"})
        self.assertEqual(cfg["repo_url"], "https://github.com/bamirreza5/Dideban.git")
        self.assertEqual(cfg["duration"], 15)

    def test_gitlab_subgroups(self):
        self.assertEqual(repo_url("https://gitlab.com/team/sub/project"), "https://gitlab.com/team/sub/project.git")

    def test_reject_unsafe_repositories(self):
        for address in ["file:///tmp/code", "https://127.0.0.1/project", "git@github.com:a/b", "https://github.com.evil.test/a/b",
                        "https://secret@github.com/a/b", "https://github.com/a/b/tree/main", "https://github.com/a/../b", "https://github.com/a/b?x=y"]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                repo_url(address)

    def test_reject_bounds_and_booleans(self):
        for key, value in [("users", 0), ("duration", 999), ("max_pages", True), ("cpus", 1.5), ("memory_mb", 100), ("run_load", "false")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({"repo_url": "https://github.com/a/b", key: value})

    def test_reject_path_escape(self):
        for value in ["../backend", "/tmp", "a/../../tmp", "C:\\data", "a//b"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({"repo_url":"https://github.com/a/b", "subdir":value})

    def test_reject_branch_option_injection(self):
        for branch in ["--upload-pack=evil", "main; rm -rf", "a..b", "main@{x}"]:
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                validate({"repo_url":"https://github.com/a/b", "branch":branch})

    def test_scenarios_require_steps_and_local_navigation(self):
        for scenario in [{"name":"bad", "steps":[]}, {"name":"bad", "steps":[{"action":"goto","value":"https://example.com"}]},
                         {"name":"bad", "steps":[{"action":"click"}]}, {"name":"bad", "steps":[{"action":"exec","value":"evil"}]}]:
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                validate({"repo_url":"https://github.com/a/b", "scenarios":[scenario]})

    def test_valid_scenario_and_unique_paths(self):
        cfg=validate({"repo_url":"https://github.com/a/b", "paths":["/", "/", "/a?q=1"], "scenarios":[{"name":"search","steps":[{"action":"goto","value":"/"},{"action":"expect_text","value":"ready"}]}]})
        self.assertEqual(cfg["paths"], ["/", "/a?q=1"])

    def test_paths_reject_cross_origin(self):
        for path in ["//evil.test", "https://evil.test", "/a/../b", "/\\evil", "/a\nb"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_path(path)


class EvidenceTests(unittest.TestCase):
    def test_report_escapes_target_output(self):
        job={"status":"completed","checks":[{"name":"<script>alert(1)</script>","status":"fail","details":"<img onerror=evil>"}],"logs":["</pre><script>evil</script>"],"config":{},"limitations":[]}
        report=standalone(job)
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("&lt;img", report)

    def test_zero_and_missing_are_different(self):
        empty=resource_summary([])
        zero=resource_summary([{"scope":"application","cpu_percent":0,"memory_mb":5}])
        self.assertIsNone(empty["application"]["cpu_peak"])
        self.assertEqual(zero["application"]["cpu_peak"], 0)

    def test_resource_scopes_remain_separate(self):
        result=resource_summary([{"scope":"application","cpu_percent":10,"memory_mb":30},{"scope":"application","cpu_percent":30,"memory_mb":45},{"scope":"host","cpu_percent":99,"memory_mb":9000}])
        self.assertEqual(result["application"]["cpu_avg"],20)
        self.assertEqual(result["application"]["memory_peak_mb"],45)
        self.assertEqual(result["host"]["memory_peak_mb"],9000)

    def test_csv_has_scope_and_bom(self):
        value=resource_csv([{"scope":"host","cpu_percent":5,"memory_mb":10}])
        rows=list(csv.DictReader(io.StringIO(value.removeprefix("\ufeff"))))
        self.assertTrue(value.startswith("\ufeff"))
        self.assertEqual(rows[0]["scope"],"host")

    def test_docker_memory_units(self):
        self.assertEqual(parse_size("1GiB"),1024)
        self.assertEqual(parse_size("512MiB"),512)
        self.assertEqual(parse_size("1048576B"),1)
        self.assertIsNone(parse_size("bad"))

    def test_snapshot_excludes_database_env_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/"source";dest=root/"dest";source.mkdir()
            for name in ["manage.py", ".env", "db.sqlite3", "project.db"]:(source/name).write_text("test")
            (source/".git").mkdir();(source/".git"/"config").write_text("not copied")
            if hasattr(os,"symlink"):
                try:(source/"escape").symlink_to(root, target_is_directory=True)
                except OSError:pass
            snapshot(source,dest)
            self.assertEqual({p.name for p in dest.iterdir()}, {"manage.py"})

    def test_django_test_result_counts_failures(self):
        result=worker.parse_django_tests("Ran 23 tests in 0.3s\nFAILED (failures=2, errors=1, skipped=3)")
        self.assertEqual(result,{"total":23,"failures":2,"errors":1,"skipped":3,"passed":17})

    def test_no_tests_not_invented(self):
        self.assertIsNone(worker.parse_django_tests("ImportError: missing package"))
        self.assertEqual(worker.parse_django_tests("Ran 0 tests in 0.0s\nOK")["total"],0)

    def test_latency_percentile_includes_slow_tail(self):
        result=worker.summarize_latency([1,2,3,4,100],1,2)
        self.assertEqual(result["p95_ms"],100)
        self.assertEqual(result["rps"],2.5)
        self.assertEqual(result["error_percent"],20)
        self.assertIsNone(worker.summarize_latency([],0,1)["p95_ms"])

    def test_crawler_blocks_destructive_and_external_urls(self):
        for url in ["https://evil.test", "http://127.0.0.1:8888/", worker.BASE_URL+"/accounts/logout/", worker.BASE_URL+"/customers/3/delete/", worker.BASE_URL+"/followups/1/toggle/"]:
            with self.subTest(url=url):self.assertIsNone(worker.local_url(url))
        self.assertEqual(worker.local_url("/customers/"),worker.BASE_URL+"/customers/")


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.engine=Engine(ROOT,self.root)

    def tearDown(self):self.temp.cleanup()

    def test_refuses_start_without_docker(self):
        with patch.object(self.engine,"health",return_value={"ready":False,"detail":"Docker unavailable"}), self.assertRaisesRegex(ValueError,"Docker unavailable"):
            self.engine.start({"repo_url":"https://github.com/a/b"})
        self.assertIsNone(self.engine.active)

    def test_only_one_active_run(self):
        with patch.object(self.engine,"health",return_value={"ready":True}),patch.object(self.engine,"_execute"):
            first=self.engine.start({"repo_url":"https://github.com/a/b"})
            with self.assertRaises(ValueError):self.engine.start({"repo_url":"https://github.com/c/d"})
            self.engine.cancel(first["id"])
            self.assertTrue(self.engine.cancel_events[first["id"]].is_set())

    def test_recovery_marks_previous_incomplete(self):
        job_id="a"*16;directory=self.root/job_id;directory.mkdir()
        (directory/"state.json").write_text(json.dumps({"id":job_id,"status":"running","phase":"tests","checks":[],"created_at":"2026-10-29T00:00:00Z","config":{}}))
        recovered=Engine(ROOT,self.root)
        self.assertEqual(recovered.view(job_id)["status"],"interrupted")
        self.assertTrue((directory/"report.html").exists())

    def test_quiet_process_can_be_cancelled(self):
        job={"id":"x","logs":[]};event=threading.Event();self.engine.cancel_events["x"]=event
        timer=threading.Timer(.25,event.set);timer.start();started=time.monotonic()
        try:
            with self.assertRaises(Cancelled):self.engine.command(job,[sys.executable,"-c","import time; time.sleep(20)"],30)
            self.assertLess(time.monotonic()-started,3)
        finally:timer.cancel()

    def test_total_deadline_prevents_starting_more_commands(self):
        self.engine.deadlines["expired"]=time.monotonic()-1
        with patch("qa.engine.subprocess.Popen") as popen,self.assertRaises(TimeoutError):
            self.engine.command({"id":"expired","logs":[]},["never-run"],60)
        popen.assert_not_called()

    def test_worker_event_not_reparsed_from_target_log(self):
        job={"checks":[],"logs":[]}
        self.engine.event(job,'QA_EVENT '+json.dumps({"type":"log","line":'QA_EVENT {"type":"check","check":{"status":"pass"}}'}))
        self.assertEqual(job["checks"],[])


class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.runner=worker.Runner({"duration":.05,"users":1,"run_browser":False,"paths":["/"]},source=self.temp.name,work=self.temp.name,output=self.temp.name)
        self.events=patch.object(worker,"emit");self.events.start()

    def tearDown(self):
        self.events.stop();self.temp.cleanup()

    def test_process_monitor_measures_cpu_and_rss(self):
        monitor=worker.AppMonitor(SimpleNamespace(pid=123),lambda:"load")
        class Stop:
            calls=0
            def is_set(self):return self.calls>=2
            def wait(self,seconds):self.calls+=1
        monitor.stop_event=Stop()
        fake=SimpleNamespace(pid=123,create_time=lambda:1,children=lambda recursive:[],
            memory_info=lambda:SimpleNamespace(rss=64*1024**2),io_counters=lambda:SimpleNamespace(read_bytes=1024**2,write_bytes=0))
        calls=iter([SimpleNamespace(user=1,system=0),SimpleNamespace(user=1.5,system=0)])
        fake.cpu_times=lambda:next(calls)
        with patch.object(worker.psutil,"Process",return_value=fake),patch.object(worker.time,"monotonic",side_effect=[0,1,2]):monitor.loop()
        self.assertEqual(len(monitor.samples),1)
        self.assertEqual(monitor.samples[0]["cpu_percent"],50)
        self.assertEqual(monitor.samples[0]["memory_mb"],64)

    def test_missing_process_does_not_become_zero_usage(self):
        monitor=worker.AppMonitor(SimpleNamespace(pid=999),lambda:"load")
        with patch.object(worker.psutil,"Process",side_effect=worker.psutil.NoSuchProcess(999)):monitor.loop()
        self.assertEqual(monitor.samples,[])
        self.assertIn("999",monitor.error)

    @unittest.skipIf(os.name=="nt", "POSIX shell test belongs to the Linux Docker worker")
    def test_custom_command_preserves_target_virtualenv_path(self):
        self.runner.root.mkdir(exist_ok=True)
        binaries=Path(self.temp.name)/"bin";binaries.mkdir()
        executable=binaries/"python";executable.write_text("#!/bin/sh\nprintf target-virtualenv")
        executable.chmod(0o755)
        self.runner.env["PATH"]=str(binaries)+os.pathsep+os.environ.get("PATH","")
        code,output=self.runner.custom("python -c ignored",timeout=5)
        self.assertEqual(code,0)
        self.assertEqual(output,"target-virtualenv")

    def serve(self):
        class Target(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=="/broken/":
                    self.send_response(500);self.end_headers();self.wfile.write(b"failed");return
                if self.path=="/" and "sessionid=ok" not in self.headers.get("Cookie", ""):
                    self.send_response(302);self.send_header("Location","/accounts/login/");self.end_headers();return
                self.send_response(200);self.end_headers()
                self.wfile.write(b'<input type="password"><input name="csrfmiddlewaretoken" value="csrf">' if self.path=="/accounts/login/" else b"dashboard")
            def do_POST(self):
                data=self.rfile.read(int(self.headers["Content-Length"]))
                if b"username=qa" in data and b"csrfmiddlewaretoken=csrf" in data:
                    self.send_response(302);self.send_header("Set-Cookie","sessionid=ok; Path=/; HttpOnly");self.send_header("Location","/");self.end_headers()
                else:self.send_response(403);self.end_headers()
        server=ThreadingHTTPServer(("127.0.0.1",0),Target)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(2)))
        patcher=patch.object(worker,"BASE_URL",f"http://127.0.0.1:{server.server_address[1]}")
        patcher.start();self.addCleanup(patcher.stop)

    def test_http_login_carries_authenticated_session(self):
        self.serve();self.runner.profile="django";self.runner.username="qa"
        cookie,authenticated=self.runner.http_session()
        self.assertTrue(authenticated)
        self.assertIn("sessionid=ok",cookie)
        self.runner.load_test(cookie,[worker.BASE_URL],authenticated)
        self.assertEqual(self.runner.load["errors"],0)

    def test_login_redirect_is_error_even_with_http_200(self):
        self.serve();self.runner.load_test("",[worker.BASE_URL],False)
        self.assertGreater(self.runner.load["requests"],0)
        self.assertEqual(self.runner.load["errors"],self.runner.load["requests"])
        self.assertEqual(self.runner.checks[-1]["status"],"fail")

    def test_http_500_is_reported_with_evidence(self):
        self.serve();self.runner.load_test("",[worker.BASE_URL+"/broken/"],False)
        self.assertGreater(self.runner.load["errors"],0)
        self.assertEqual(self.runner.load["status_codes"],{"500":1})



class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server,self.engine=make_server(0,self.temp.name)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.temp.cleanup()

    def get(self,path):
        with urlopen(self.base+path,timeout=10) as response:return response.read(),response.headers

    def test_ui_served_with_content_security_policy(self):
        data,headers=self.get("/")
        self.assertIn("آزمایار".encode(),data)
        self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])

    def test_reject_post_without_session_token(self):
        request=Request(self.base+"/api/jobs",data=b"{}",headers={"Content-Type":"application/json"})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,403)

    def test_reject_cross_origin_even_with_token(self):
        data,_=self.get("/api/health");token=json.loads(data)["session_token"]
        request=Request(self.base+"/api/jobs",data=b"{}",headers={"Content-Type":"application/json","X-QA-Token":token,"Origin":"https://evil.test"})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,403)

    def test_invalid_job_and_path_traversal_return_404(self):
        for path in ["/api/jobs/"+"0"*16,"/../app.py","/api/jobs/"+"0"*16+"/files/../../app.py"]:
            with self.subTest(path=path),self.assertRaises(HTTPError) as error:urlopen(self.base+path)
            self.assertEqual(error.exception.code,404)

    def test_invalid_json_returns_actionable_error(self):
        data,_=self.get("/api/health");token=json.loads(data)["session_token"]
        request=Request(self.base+"/api/jobs",data=b"{bad",headers={"Content-Type":"application/json","X-QA-Token":token})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,400)

    def test_preview_cannot_run_jobs(self):
        token=self.server.RequestHandlerClass.keywords["token"]
        self.server.RequestHandlerClass.keywords["preview"]=True
        request=Request(self.base+"/api/jobs",data=b"{}",headers={"Content-Type":"application/json","X-QA-Token":token})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,403)
        self.assertIn("خواندنی",json.loads(error.exception.read())["error"])


if __name__=="__main__":unittest.main()
