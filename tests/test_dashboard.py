import http.client
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

SCRIPT = Path(__file__).resolve().parents[1] / "skills/project-relay/scripts/relay.py"
sys.path.insert(0, str(SCRIPT.parent))
import relay as relay_cli  # noqa: E402
DASHBOARD_ASSETS = Path(__file__).resolve().parents[1] / "skills/project-relay/dashboard"
WINDOWS_EXECUTOR = unittest.skipUnless(sys.platform == "win32", "看板服务仅 Windows 支持")


@WINDOWS_EXECUTOR
class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dashboard-test-")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "中文 项目"
        self.project.mkdir()
        self.plan = Path(self.tmp.name) / "计划.md"
        self.plan.write_text("实现看板。验收：浏览器同页显示目标、日志和审核记录。", encoding="utf-8")
        self.cli("init")
        self.task = self.cli("new", "--title", "看板任务",
                             "--plan-file", str(self.plan))["task"]["id"]
        self.server = None
        self.addCleanup(self.stop)

    def cli(self, *args, ok=True, config=None, env=None):
        command = [sys.executable, "-X", "utf8", str(SCRIPT),
                   "--project", str(self.project)]
        if config:
            command += ["--config", str(config)]
        command += list(map(str, args))
        environment = dict(os.environ)
        environment.update(env or {})
        result = subprocess.run(command, capture_output=True, encoding="utf-8",
                                errors="replace", env=environment)
        self.assertEqual(result.returncode == 0, ok,
                         (result.stdout or "") + (result.stderr or ""))
        return json.loads(result.stdout)

    def start(self, *args, ok=True):
        result = self.cli("dashboard", "--no-open", "--port", "0", *args, ok=ok)
        if result.get("pid"):
            self.server = result
        return result

    def stop(self):
        info = self.project / "协作记录" / "看板" / "server.json"
        if self.server or info.exists():
            try:
                self.cli("dashboard", "--stop")
            except (AssertionError, ValueError):
                pass
        self.server = None

    def setUpServer(self):
        self.addCleanup(self.stop)
        return self.start("--task", self.task)

    def http(self, method, path, token=None, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server["port"], timeout=5)
        outgoing = dict(headers or {})
        if token:
            outgoing["X-Dashboard-Token"] = token
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        data = response.read().decode("utf-8", "replace")
        result = (response.status, {k.lower(): v for k, v in response.getheaders()}, data)
        connection.close()
        return result

    def get_json(self, path, token=None, headers=None, expect=200):
        status, response_headers, data = self.http("GET", path, token=token, headers=headers)
        self.assertEqual(status, expect, data)
        return json.loads(data), response_headers

    def state(self, task=None, token=None):
        path = "/api/state"
        if task:
            path += "?task=" + quote(task)
        return self.get_json(path, token=token or self.server["token"])

    def live_file(self, name, lines, tail=""):
        folder = self.project / "协作记录" / self.task
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"live-{name}.jsonl"
        text = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines) + tail
        path.write_text(text, encoding="utf-8")
        return path

    def executor_config(self, body, dashboard=False):
        fake = Path(self.tmp.name) / "fake_claude.py"
        fake.write_text(body, encoding="utf-8")
        settings = Path(self.tmp.name) / "desktop.json"
        settings.write_text(json.dumps({
            "inferenceGatewayBaseUrl": "http://127.0.0.1:15721/claude-desktop",
            "inferenceGatewayApiKey": "TEST-SECRET-123456",
            "inferenceModels": [{"name": "claude-sonnet-5", "labelOverride": "deepseek-flash"}]}),
            encoding="utf-8")
        state = Path(self.tmp.name) / "live-state.json"
        state.write_text(json.dumps({"apps": {"claude-desktop": {
            "mode": "proxy", "attached": True, "proxy_route": "deepseek-id"}}}), encoding="utf-8")
        config = {"command": [sys.executable, "-X", "utf8", str(fake)],
            "desktop_config": str(settings), "cc_switch_state": str(state),
            "expected_provider_id": "deepseek-id", "model": "claude-sonnet-5",
            "expected_upstream_model": "deepseek-flash"}
        if dashboard:
            config["dashboard"] = True
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_state_serves_task_plan_history_and_static_assets(self):
        server = self.setUpServer()
        self.assertTrue(server["url"].startswith("http://127.0.0.1:"))
        data, headers = self.state(self.task)
        self.assertEqual(data["task"]["id"], self.task)
        self.assertIn("实现看板", data["task"]["plan"])
        self.assertIn("验收", data["task"]["plan"])
        self.assertEqual(data["attempts"], [])
        self.assertEqual(data["live"], [])
        self.assertIn("events", data)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertNotIn("access-control-allow-origin", headers)

        status, static_headers, html = self.http("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", static_headers["content-type"])
        self.assertIn("/app.js", html)
        self.assertIn("/styles.css", html)
        self.assertIn("content-security-policy", static_headers)
        status, _, javascript = self.http("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertNotIn("innerHTML", javascript)
        self.assertIn("textContent", javascript)
        status, _, styles = self.http("GET", "/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("#1b6c80", styles)

        before = (self.project / "协作记录" / "state.sqlite3").read_bytes()
        self.state(self.task)
        self.http("GET", "/")
        after = (self.project / "协作记录" / "state.sqlite3").read_bytes()
        self.assertEqual(before, after)

    def test_api_enforces_token_task_ids_and_local_host_origin(self):
        self.setUpServer()
        status, _, data = self.http("GET", "/api/state?task=" + quote(self.task))
        self.assertEqual(status, 401)
        self.assertIn("token", json.loads(data)["error"].lower())
        self.get_json("/api/state?task=" + quote("0" * 12),
                      token=self.server["token"], expect=404)
        self.get_json("/api/state?task=" + quote("../state"),
                      token=self.server["token"], expect=400)
        self.get_json("/README.md", token=self.server["token"], expect=404)
        status, headers, data = self.http("GET", "/api/state?task=" + quote(self.task),
            token=self.server["token"], headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403, data)
        self.assertNotIn("access-control-allow-origin", headers)
        status, _, data = self.http("GET", "/api/state?task=" + quote(self.task),
            token=self.server["token"], headers={"Host": "evil.example"})
        self.assertEqual(status, 403, data)
        status, _, data = self.http("POST", "/api/state?task=" + quote(self.task),
            token=self.server["token"], body=b"{}")
        self.assertEqual(status, 405, data)
        self.assertIn("error", json.loads(data))

    def test_live_attempts_incomplete_tail_and_rework_are_visible(self):
        self.setUpServer()
        first = self.live_file("aaaa11112222", [
            {"at": "2026-11-06T10:00:00+00:00", "attempt_id": "aaaa11112222",
             "kind": "started", "payload": {"attempt_number": 1}},
            {"at": "2026-11-06T10:00:01+00:00", "attempt_id": "aaaa11112222",
             "kind": "tool_use", "payload": {"name": "Bash", "input": {"command": "echo hi"}}},
        ], tail='{"at": "2026-11-06T10:00:02+00:00", "attempt_id": "aaaa11112222"')
        time.sleep(0.02)
        self.live_file("bbbb33334444", [
            {"at": "2026-11-06T10:01:00+00:00", "attempt_id": "bbbb33334444",
             "kind": "started", "payload": {"attempt_number": 2}},
            {"at": "2026-11-06T10:01:01+00:00", "attempt_id": "bbbb33334444",
             "kind": "assistant_delta", "payload": {"text": "返工执行中"}},
            {"at": "2026-11-06T10:01:02+00:00", "attempt_id": "bbbb33334444",
             "kind": "finished", "payload": {"ok": False, "error": "测试中断"}},
        ])
        db = sqlite3.connect(self.project / "协作记录" / "state.sqlite3")
        try:
            db.execute("UPDATE tasks SET status='pending_review' WHERE id=?", (self.task,))
            db.commit()
        finally:
            db.close()
        data, _ = self.state(self.task)
        self.assertEqual(data["attempt_count"], 2)
        self.assertTrue(data["rework"])
        self.assertTrue(data["attempts"][0]["incomplete_tail"])
        self.assertEqual(len(data["attempts"][0]["events"]), 2)
        self.assertEqual(len(data["attempts"][1]["events"]), 3)
        self.assertEqual(data["live"][-1]["kind"], "finished")
        self.assertEqual(data["live"][-1]["payload"]["error"], "测试中断")
        self.assertIsNotNone(data["last_event_at"])
        self.assertIn("返工", data["status_label"])

    def test_controller_exit_is_reported(self):
        self.setUpServer()
        db = sqlite3.connect(self.project / "协作记录" / "state.sqlite3")
        try:
            db.execute("UPDATE tasks SET status='running',run_pid=999999,child_pid=NULL WHERE id=?",
                       (self.task,))
            db.commit()
        finally:
            db.close()
        data, _ = self.state(self.task)
        self.assertFalse(data["controller_alive"])
        self.assertFalse(data["execution_alive"])
        self.assertIn("退出", data["status_detail"])

    def test_server_reuses_process_and_stop_removes_it(self):
        first = self.start("--task", self.task)
        second = self.start("--task", self.task)
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual(first["port"], second["port"])
        self.assertTrue(second["reused"])
        logs = list((self.project / "协作记录" / "看板").glob("server-*.log"))
        self.assertTrue(logs)
        self.assertNotIn(first["token"], logs[0].read_text(encoding="utf-8", errors="replace"))
        health, _ = self.get_json("/api/health", token=first["token"])
        self.assertTrue(health["ok"])
        stopped = self.cli("dashboard", "--stop")
        self.assertTrue(stopped["stopped"])
        self.assertFalse((self.project / "协作记录" / "看板" / "server.json").exists())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.http("GET", "/api/health?token=" + first["token"])
            except (ConnectionError, OSError):
                break
            time.sleep(0.05)
        else:
            self.fail("stop 后看板端口仍可连接")
        self.server = None
        self.assertFalse(self.cli("dashboard", "--stop")["stopped"])

    def test_start_failure_is_explicit_and_does_not_return_url(self):
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            result = self.cli("dashboard", "--no-open", "--port", port, ok=False)
        finally:
            holder.close()
        self.assertIn("启动失败", result["error"])
        self.assertNotIn("url", result)
        self.assertFalse((self.project / "协作记录" / "看板" / "server.json").exists())

    def test_empty_project_and_review_history_are_served(self):
        review = Path(self.tmp.name) / "审核.md"
        review.write_text("阶段检查通过。", encoding="utf-8")
        self.cli("takeover", "--task", self.task, "--agent", "codex")
        self.cli("review", "--task", self.task, "--agent", "codex",
                 "--outcome", "passed", "--file", str(review))
        self.cli("review", "--task", self.task, "--agent", "codex",
                 "--outcome", "changes_requested", "--file", str(review))
        self.setUpServer()
        data, _ = self.state(self.task)
        self.assertEqual(data["task"]["status"], "changes_requested")
        self.assertEqual([item["kind"] for item in data["reviews"]],
                         ["review_passed", "review_changes_requested"])
        self.assertEqual(data["reviews"][-1]["body"], "阶段检查通过。")
        self.assertIn("changes_requested", data["stage"])

        other = Path(self.tmp.name) / "空项目"
        other.mkdir()
        subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(other), "init"],
                       check=True, capture_output=True)
        empty = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(other),
                                "dashboard", "--no-open", "--port", "0"],
                               capture_output=True, encoding="utf-8", check=True)
        info = json.loads(empty.stdout)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=5)
            connection.request("GET", "/api/state",
                               headers={"X-Dashboard-Token": info["token"]})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["tasks"], [])
        finally:
            subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(other),
                            "dashboard", "--stop"], capture_output=True)

    def test_rejected_second_run_does_not_show_phantom_attempt(self):
        config = self.executor_config(
            "import json,sys,time\nfrom pathlib import Path\n"
            "Path('started').touch()\n"
            "sys.stdin.read()\n"
            "time.sleep(2)\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'result':'完成'}), flush=True)\n")
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(self.project),
             "--config", str(config), "run", "--task", self.task],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.project / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.project / "started").exists())
            self.cli("run", "--task", self.task, config=config, ok=False)
            self.start("--task", self.task)
            data, _ = self.state(self.task)
            self.assertEqual(data["attempt_count"], 1)
            self.assertFalse(data["rework"])
            self.assertEqual(data["status_label"], "执行中")
        finally:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def test_event_window_uses_offset_so_new_events_after_limit_are_visible(self):
        self.setUpServer()
        attempt = "window000001"
        events = [
            {"at": f"2026-11-06T10:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
             "attempt_id": attempt, "kind": "tool_use",
             "payload": {"name": "Bash", "input": {"command": f"echo {index}"}}}
            for index in range(5000)
        ]
        events.append({"at": "2026-11-06T11:00:00+00:00", "attempt_id": attempt,
                       "kind": "tool_use",
                       "payload": {"name": "Bash", "input": {"command": "MARKER-5001"}}})
        events.append({"at": "2026-11-06T11:00:01+00:00", "attempt_id": attempt,
                       "kind": "finished", "payload": {"ok": True, "error": None}})
        self.live_file(attempt, events)
        data, _ = self.state(self.task)
        window = data["attempts"][0]
        self.assertTrue(window["truncated"])
        self.assertEqual(window["event_offset"], 2)
        self.assertEqual(window["event_total"], 5002)
        self.assertEqual(len(window["events"]), 5000)
        self.assertEqual(window["events"][-2]["payload"]["input"]["command"], "MARKER-5001")
        self.assertEqual(window["events"][-1]["kind"], "finished")
        self.assertEqual(data["live"][-1]["kind"], "finished")

    def test_live_thinking_token_noise_is_ignored_before_event_window(self):
        self.setUpServer()
        attempt = "noise0000001"
        events = [
            {"at": "2026-11-06T10:00:00+00:00", "attempt_id": attempt, "kind": "system",
             "payload": {"type": "system", "subtype": "thinking_tokens", "session_id": "noise"}}
            for _ in range(5200)
        ]
        events.extend([
            {"at": "2026-11-06T10:01:00+00:00", "attempt_id": attempt, "kind": "system",
             "payload": {"type": "system", "subtype": "init", "session_id": "noise"}},
            {"at": "2026-11-06T10:01:01+00:00", "attempt_id": attempt, "kind": "system",
             "payload": {"type": "system", "subtype": "status", "session_id": "noise",
                         "status": "requesting"}},
            {"at": "2026-11-06T10:01:02+00:00", "attempt_id": attempt, "kind": "system",
             "payload": {"type": "system", "subtype": "task_summary", "session_id": "noise"}},
            {"at": "2026-11-06T10:01:03+00:00", "attempt_id": attempt, "kind": "tool_use",
             "payload": {"name": "Bash", "input": {"command": "echo visible"}}},
            {"at": "2026-11-06T10:01:04+00:00", "attempt_id": attempt, "kind": "finished",
             "payload": {"ok": True, "error": None}},
        ])
        path = self.live_file(attempt, events)
        before = path.read_bytes()
        data, _ = self.state(self.task)
        self.assertEqual(path.read_bytes(), before, "不得改写历史证据原件")
        self.assertEqual(len(data["attempts"]), 1)
        window = data["attempts"][0]
        self.assertEqual(window["ignored_events"], 5200)
        self.assertEqual(window["event_total"], 5)
        self.assertFalse(window["truncated"])
        self.assertEqual(window["event_offset"], 0)
        subtypes = [event["payload"].get("subtype") for event in window["events"]
                    if event["kind"] == "system"]
        self.assertEqual(subtypes, ["init", "status", "task_summary"])
        self.assertEqual(window["events"][-1]["kind"], "finished")

    def test_run_without_dashboard_config_does_not_start_server(self):
        config = self.executor_config(
            "import json,sys\nsys.stdin.read()\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'result':'完成'}), flush=True)\n")
        result = self.cli("run", "--task", self.task, config=config)
        self.assertTrue(result["ok"])
        self.assertIsNone(result.get("dashboard_url"))
        self.assertFalse((self.project / "协作记录" / "看板" / "server.json").exists())

    def test_run_with_dashboard_config_starts_server_and_opens_task(self):
        # 在浏览器边界校验 URL，同时拦截原生启动入口。
        self.addCleanup(self.stop)
        config = self.executor_config(
            "import json,sys\nsys.stdin.read()\n"
            "print(json.dumps({'type':'system','subtype':'init','session_id':'session-dash'}), flush=True)\n"
            "print(json.dumps({'type':'assistant','message':{'content':["
            "{'type':'tool_use','id':'tool-1','name':'Bash','input':{'command':'echo hi'}}]}}), flush=True)\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'result':'完成','session_id':'session-dash'}), flush=True)\n",
            dashboard=True)
        output = io.StringIO()
        command = [str(SCRIPT), "--project", str(self.project), "--config", str(config),
                   "run", "--task", self.task]
        processes = []
        real_popen = subprocess.Popen

        def start_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        def close_processes():
            self.stop()
            for process in processes:
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

        self.addCleanup(close_processes)
        with patch("webbrowser.open", return_value=True) as browser:
            with patch.object(sys, "argv", command), redirect_stdout(output):
                with patch("os.startfile") as native_browser, patch.object(
                        subprocess, "Popen", side_effect=start_process):
                    code = relay_cli.main()
        self.assertEqual(code, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertIn(self.task, result["dashboard_url"])
        browser.assert_called_once_with(result["dashboard_url"])
        native_browser.assert_not_called()
        parts = urlsplit(result["dashboard_url"])
        token = parse_qs(parts.query)["token"][0]
        self.server = {"port": parts.port, "token": token}
        data, _ = self.state(self.task)
        self.assertEqual(data["task"]["id"], self.task)
        self.assertGreaterEqual(data["attempt_count"], 1)
        self.assertTrue(any(event["kind"] == "tool_use" for event in data["live"]))
        evidence = Path(result["evidence"]).read_text(encoding="utf-8")
        self.assertNotIn(token, evidence)


if __name__ == "__main__":
    unittest.main()
