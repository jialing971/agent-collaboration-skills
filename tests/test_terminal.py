import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/project-relay/scripts/relay.py"
SCRIPTS = ROOT / "skills/project-relay/scripts"
sys.path.insert(0, str(SCRIPTS))
import terminal as terminal_view  # noqa: E402
import relay as relay_cli  # noqa: E402
WINDOWS = unittest.skipUnless(sys.platform == "win32", "接力终端仅 Windows 支持")


@WINDOWS
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="terminal-test-")
        self.addCleanup(self.tmp.cleanup)
        self.project = (Path(self.tmp.name) / "中文 项目").resolve()
        self.project.mkdir()
        self.plan = Path(self.tmp.name) / "计划.md"
        self.plan.write_text(
            "实现接力终端看板。\n\n验收：终端显示目标、实时操作和审核记录。",
            encoding="utf-8")
        self.cli("init")
        self.task = self.cli("new", "--title", "终端任务",
                             "--plan-file", str(self.plan))["task"]["id"]
        self.processes = []
        self.addCleanup(self.stop_processes)

    def run_cli(self, *args, config=None, env=None, timeout=60):
        command = [sys.executable, "-X", "utf8", str(SCRIPT),
                   "--project", str(self.project)]
        if config:
            command += ["--config", str(config)]
        command += list(map(str, args))
        environment = dict(os.environ)
        environment.update(env or {})
        return subprocess.run(command, capture_output=True, encoding="utf-8",
                              errors="replace", env=environment, timeout=timeout)

    def cli(self, *args, ok=True, config=None, env=None):
        result = self.run_cli(*args, config=config, env=env)
        self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
        return json.loads(result.stdout)

    def monitor(self, *args, ok=True, env=None, timeout=60):
        result = self.run_cli("monitor", "--task", self.task, *args, env=env, timeout=timeout)
        self.assertEqual(result.returncode == 0, ok,
                         (result.stdout or "") + (result.stderr or ""))
        return result

    def console_cli(self, *args, config=None, fail_window=False):
        """Exercise the CLI while replacing only the native console boundary."""
        real_popen = subprocess.Popen

        def create_process(command, **kwargs):
            if str(terminal_view.__file__) in command:
                if fail_window:
                    raise OSError("模拟操作系统开窗失败")
                kwargs.update(creationflags=subprocess.CREATE_NO_WINDOW,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
            process = real_popen(command, **kwargs)
            self.processes.append(process)
            return process

        argv = [str(SCRIPT), "--project", str(self.project)]
        if config:
            argv.extend(["--config", str(config)])
        argv.extend(map(str, args))
        output = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            with patch.object(subprocess, "Popen", side_effect=create_process):
                code = relay_cli.main()
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        self.processes = []

    def kill_pid(self, pid):
        if not pid:
            return
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)

    def executor_config(self, body, terminal=False, dashboard=False):
        folder = Path(self.tmp.name)
        fake = folder / "fake_claude.py"
        fake.write_text(body, encoding="utf-8")
        settings = folder / "desktop.json"
        settings.write_text(json.dumps({
            "inferenceGatewayBaseUrl": "http://127.0.0.1:15721/claude-desktop",
            "inferenceGatewayApiKey": "TEST-SECRET-123456",
            "inferenceModels": [{"name": "claude-sonnet-5",
                                 "labelOverride": "deepseek-flash"}]}), encoding="utf-8")
        state = folder / "live-state.json"
        state.write_text(json.dumps({"apps": {"claude-desktop": {
            "mode": "proxy", "attached": True, "proxy_route": "deepseek-id"}}}),
            encoding="utf-8")
        config = {"command": [sys.executable, "-X", "utf8", str(fake)],
                  "desktop_config": str(settings), "cc_switch_state": str(state),
                  "expected_provider_id": "deepseek-id", "model": "claude-sonnet-5",
                  "expected_upstream_model": "deepseek-flash"}
        if terminal:
            config["terminal"] = True
        if dashboard:
            config["dashboard"] = True
        path = folder / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def state_path(self):
        return self.project / "协作记录" / "state.sqlite3"

    def set_status(self, status):
        db = sqlite3.connect(self.state_path())
        try:
            db.execute("UPDATE tasks SET status=? WHERE id=?", (status, self.task))
            db.commit()
        finally:
            db.close()

    def live_file(self, name, lines, tail=""):
        folder = self.project / "协作记录" / self.task
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"live-{name}.jsonl"
        text = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines) + tail
        path.write_text(text, encoding="utf-8")
        return path

    def event(self, attempt, kind, payload, at="2026-11-13T10:00:00+00:00"):
        return {"at": at, "attempt_id": attempt, "kind": kind, "payload": payload}

    def start_follow(self, interval="0.2"):
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(self.project),
                   "monitor", "--task", self.task, "--interval", interval]
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   bufsize=1, env=environment)
        self.processes.append(process)
        self.output = ""
        self.lines = queue.Queue()
        self.errors = []

        def pump_lines():
            try:
                for line in process.stdout:
                    self.lines.put(line)
            finally:
                self.lines.put(None)

        def pump_errors():
            try:
                for line in process.stderr:
                    self.errors.append(line)
            except (OSError, ValueError):
                pass

        threading.Thread(target=pump_lines, daemon=True).start()
        threading.Thread(target=pump_errors, daemon=True).start()
        return process

    def read_until(self, marker, timeout=10):
        deadline = time.monotonic() + timeout
        while marker not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"等待输出 {marker!r} 超时；当前输出：\n{self.output}\n"
                          f"错误：{''.join(self.errors)}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                self.fail(f"等待输出 {marker!r} 超时；当前输出：\n{self.output}\n"
                          f"错误：{''.join(self.errors)}")
            if line is None:
                self.fail(f"monitor 在出现 {marker!r} 前退出；输出：\n{self.output}\n"
                          f"错误：{''.join(self.errors)}")
            self.output += line
        return self.output

    def append_live(self, name, lines):
        path = self.project / "协作记录" / self.task / f"live-{name}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                handle.flush()

    def test_monitor_once_shows_plan_reviews_and_old_task_without_http(self):
        checkpoint = Path(self.tmp.name) / "检查点.md"
        checkpoint.write_text("阶段检查点：已完成只读视图。", encoding="utf-8")
        self.cli("takeover", "--task", self.task, "--agent", "codex")
        self.cli("checkpoint", "--task", self.task, "--agent", "codex",
                 "--file", str(checkpoint))
        passed = Path(self.tmp.name) / "通过.md"
        passed.write_text("阶段复核通过：显示内容完整。", encoding="utf-8")
        self.cli("review", "--task", self.task, "--agent", "codex",
                 "--outcome", "passed", "--file", str(passed))
        rework = Path(self.tmp.name) / "返工.md"
        rework.write_text("要求返工：补充循环刷新。", encoding="utf-8")
        self.cli("review", "--task", self.task, "--agent", "codex",
                 "--outcome", "changes_requested", "--file", str(rework))

        before = self.state_path().read_bytes()
        result = self.monitor("--once")
        after = self.state_path().read_bytes()

        text = result.stdout
        self.assertFalse(text.lstrip().startswith("{"), "直接 monitor 应输出文本而不是 JSON")
        self.assertIn("终端任务", text)
        self.assertIn("实现接力终端看板", text)
        self.assertIn("验收", text)
        self.assertIn("阶段检查点", text)
        self.assertIn("已完成只读视图", text)
        self.assertIn("阶段复核通过", text)
        self.assertIn("要求返工", text)
        self.assertIn("补充循环刷新", text)
        self.assertIn("尚无执行批次", text)
        self.assertEqual(before, after, "只读视图不得改写 SQLite")
        self.assertFalse((self.project / "协作记录" / "看板").exists(),
                         "终端视图不得启动 HTTP 看板")

    def test_monitor_once_shows_live_tools_results_deltas_and_noise(self):
        attempt = "aaaa11112222"
        events = [
            self.event(attempt, "started", {"attempt_number": 1, "model": "测试模型"}),
            self.event(attempt, "system", {"type": "system", "subtype": "init",
                                          "session_id": "session-visible"}),
            self.event(attempt, "system", {"type": "system", "subtype": "thinking_tokens",
                                          "session_id": "NOISE-SESSION"}),
            self.event(attempt, "thinking", {"text": "THINKING-MARKER"}),
            self.event(attempt, "thinking_delta", {"text": "THINKING-DELTA-MARKER"}),
            self.event(attempt, "assistant_delta", {"text": "连"}),
            self.event(attempt, "assistant_delta", {"text": "续"}),
            self.event(attempt, "system", {"type": "system", "subtype": "thinking_tokens",
                                          "session_id": "NOISE-SESSION-2"}),
            self.event(attempt, "assistant_delta", {"text": "输"}),
            self.event(attempt, "assistant_delta", {"text": "出"}),
            self.event(attempt, "assistant_delta", {"text": " MARKER"}),
            self.event(attempt, "tool_use", {"name": "Bash",
                                             "input": {"command": "echo 你好"}}),
            self.event(attempt, "tool_result", {"content": [{"type": "text",
                                                            "text": "你好\n世界"}],
                                                "is_error": False}),
            self.event(attempt, "tool_use", {"name": "Read",
                                             "input": {"file_path": str(self.project / "sum.py")}}),
            self.event(attempt, "tool_result", {"content": "权限不足", "is_error": True}),
            self.event(attempt, "assistant", {"text": "公开回复 MARKER"}),
            self.event(attempt, "stderr", {"text": "错误输出 MARKER"}),
            self.event(attempt, "result", {"result": "完成", "subtype": "success",
                                           "is_error": False}),
            self.event(attempt, "finished", {"ok": False, "error": "测试失败 MARKER"}),
        ]
        self.live_file(attempt, events, tail='{not-json\n{"at": "2026-11-13T10:00:09+00:00", "attempt_id": "aaaa11112222"')
        self.set_status("pending_review")

        text = self.monitor("--once").stdout
        self.assertIn("第 1 次执行", text)
        self.assertIn("工具：Bash：[echo] echo 你好", text)
        self.assertIn("工具结果：你好 世界", text)
        self.assertIn("工具：Read：sum.py", text)
        self.assertIn("工具失败：权限不足", text)
        self.assertIn("连续输出 MARKER", text)
        self.assertNotIn("连\n续\n输\n出", text)
        self.assertNotIn("THINKING-MARKER", text)
        self.assertNotIn("THINKING-DELTA-MARKER", text)
        self.assertNotIn("NOISE-SESSION", text)
        self.assertNotIn("thinking_tokens", text)
        self.assertIn("1 行无法解析", text)
        self.assertIn("最后一行尚未写完整", text)
        self.assertIn("执行结束（失败）：测试失败 MARKER", text)
        self.assertIn("等待 Codex 复核", text)
        self.assertIn("原始日志", text)

    def test_monitor_once_handles_event_window_offset(self):
        attempt = "window000001"
        events = [self.event(attempt, "tool_use", {
            "name": "Bash", "input": {"command": "echo EARLY-DROPPED"}})]
        events.extend(self.event(attempt, "tool_use", {
            "name": "Bash", "input": {"command": f"echo {index}"}})
            for index in range(1, 4999))
        events.append(self.event(attempt, "tool_use", {
            "name": "Bash", "input": {"command": "echo MARKER-LAST"}}))
        events.append(self.event(attempt, "finished", {"ok": True, "error": None}))
        self.live_file(attempt, events)

        text = self.monitor("--once").stdout
        self.assertIn("仅显示最近 5000 条", text)
        self.assertIn("共 5001 条", text)
        self.assertIn("窗口偏移 1", text)
        self.assertIn("MARKER-LAST", text)
        self.assertNotIn("EARLY-DROPPED", text)
        self.assertIn("原始日志", text)

    def test_follow_appends_only_new_events_and_switches_batches(self):
        first = "bbbb11112222"
        self.live_file(first, [
            self.event(first, "started", {"attempt_number": 1, "model": "测试模型"}),
            self.event(first, "tool_use", {"name": "Bash",
                                           "input": {"command": "echo FIRST-MARKER"}}),
        ])
        self.start_follow()
        output = self.read_until("FIRST-MARKER")
        self.assertIn("任务目标与验收", output)
        self.assertIn("第 1 次执行", output)

        self.append_live(first, [
            self.event(first, "tool_use", {"name": "Bash",
                                           "input": {"command": "echo SECOND-MARKER"}}),
        ])
        output = self.read_until("SECOND-MARKER")
        self.assertEqual(output.count("实现接力终端看板"), 1, "计划不应在轮询中重复")
        self.assertEqual(output.count("FIRST-MARKER"), 1, "旧事件不应重复追加")

        time.sleep(0.05)
        second = "cccc33334444"
        self.live_file(second, [
            self.event(second, "started", {"attempt_number": 2, "model": "测试模型"}),
            self.event(second, "tool_use", {"name": "Bash",
                                            "input": {"command": "echo BATCH2-MARKER"}}),
        ])
        output = self.read_until("BATCH2-MARKER")
        self.assertIn("第 2 次执行", output)
        self.assertEqual(output.count("FIRST-MARKER"), 1)

    def test_follow_continues_after_pending_review_and_shows_new_review(self):
        attempt = "dddd11112222"
        self.live_file(attempt, [
            self.event(attempt, "started", {"attempt_number": 1, "model": "测试模型"}),
            self.event(attempt, "finished", {"ok": True, "error": None}),
        ])
        self.set_status("pending_review")
        self.start_follow()
        output = self.read_until("等待 Codex 复核")
        self.assertIn("待复核", output)

        rework = Path(self.tmp.name) / "返工要求.md"
        rework.write_text("返工要求 MARKER：补充循环刷新。", encoding="utf-8")
        self.cli("review", "--task", self.task, "--agent", "codex",
                 "--outcome", "changes_requested", "--file", str(rework))
        output = self.read_until("返工要求 MARKER")
        self.assertIn("要求返工", output)
        self.assertIn("【状态更新】", output)
        self.assertIn("需返工", output)

        passed = Path(self.tmp.name) / "复核通过.md"
        passed.write_text("复核通过 MARKER：可以进入最终验收。", encoding="utf-8")
        self.cli("review", "--task", self.task, "--agent", "codex",
                 "--outcome", "passed", "--file", str(passed))
        output = self.read_until("复核通过 MARKER")
        self.assertIn("阶段复核通过（待最终验收）", output)
        self.assertIn("已结束", output)

    def test_follow_reports_controller_exit_without_database_status_change(self):
        controller = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.processes.append(controller)
        with sqlite3.connect(self.state_path()) as db:
            db.execute("UPDATE tasks SET status='running',run_pid=? WHERE id=?",
                       (controller.pid, self.task))
        db.close()
        self.start_follow()
        self.read_until("执行进程运行中")
        controller.kill()
        controller.wait(timeout=5)
        self.assertIn("【状态更新】", self.read_until("执行进程已退出"))

    def test_monitor_displays_plan_and_logs_without_terminal_control_sequences(self):
        self.plan.write_text("目标\x1b[2J仍可见\x07\x08", encoding="utf-8")
        self.task = self.cli("new", "--title", "标题\x1b]0;伪造标题\x07",
                             "--plan-file", str(self.plan))["task"]["id"]
        attempt = "safe11112222"
        self.live_file(attempt, [self.event(attempt, "tool_result", {
            "content": "结果\x1b[31m红字\x1b[0m\x00\x08", "is_error": False})])
        text = self.monitor("--once").stdout
        self.assertIn("目标仍可见", text)
        self.assertIn("结果红字", text)
        self.assertNotIn("伪造标题", text)
        for control in ("\x1b", "\x07", "\x08", "\x00"):
            self.assertNotIn(control, text)

    def test_follow_merges_streaming_text_across_polls_and_complete_reply(self):
        attempt = "text11112222"
        self.live_file(attempt, [])
        self.start_follow(interval="0.05")
        self.read_until("原始日志")
        self.append_live(attempt, [self.event(attempt, "assistant_delta", {"text": "甲"})])
        time.sleep(0.3)
        self.append_live(attempt, [self.event(attempt, "assistant_delta", {"text": "乙"})])
        time.sleep(0.3)
        self.append_live(attempt, [
            self.event(attempt, "assistant", {"text": "甲乙丙"}),
            self.event(attempt, "assistant_delta", {"text": "丙"}),
            self.event(attempt, "tool_use", {"name": "Read", "input": {"file_path": "END-MARKER"}}),
        ])
        output = self.read_until("END-MARKER")
        self.assertEqual(output.count("甲乙丙"), 1)
        self.assertNotIn("甲\n", output)
        self.assertNotIn("乙\n", output)
        self.assertNotIn("输出：丙", output)

    def test_monitor_merges_delayed_deltas_after_multiple_complete_messages(self):
        attempt = "late11112222"
        self.live_file(attempt, [
            self.event(attempt, "assistant", {"text": "甲乙"}),
            self.event(attempt, "tool_use", {"name": "Read", "input": {"file_path": "目标"}}),
            self.event(attempt, "assistant", {"text": "丙丁"}),
            self.event(attempt, "assistant_delta", {"text": "甲乙丙"}),
            self.event(attempt, "assistant_delta", {"text": "丁戊"}),
            self.event(attempt, "assistant", {"text": "戊己"}),
            self.event(attempt, "result", {"result": "戊己", "subtype": "success", "is_error": False}),
            self.event(attempt, "assistant_delta", {"text": "己"}),
            self.event(attempt, "finished", {"ok": True}),
        ])
        text = self.monitor("--once").stdout
        for message in ("甲乙", "丙丁", "戊己"):
            self.assertEqual(text.count(message), 1, text)
        self.assertIn("最终结果", text)

    def test_follow_preserves_new_text_when_old_deltas_leave_the_event_window(self):
        attempt = "move11112222"
        self.live_file(attempt, [self.event(attempt, "assistant", {"text": "PREFIX"})])
        self.start_follow(interval="1")
        self.read_until("回复：PREFIX")
        events = [self.event(attempt, "assistant_delta", {"text": "PREFIX"})]
        events.extend(self.event(attempt, "tool_use", {"name": "Read", "input": {"file_path": "旧事件"}})
                      for _ in range(5000))
        events.extend([
            self.event(attempt, "assistant_delta", {"text": "PREFIX NEW-TAIL"}),
            self.event(attempt, "finished", {"ok": False, "error": "模拟中断"}),
        ])
        self.append_live(attempt, events)
        output = self.read_until("模拟中断")
        self.assertIn("输出：PREFIX NEW-TAIL", output[-1200:])

    def test_open_uses_argument_array_and_clean_environment(self):
        class FakeProcess:
            pid = 43210

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            ready = Path(command[command.index("--ready-file") + 1])
            captured["ready"] = ready
            ready.write_text(json.dumps({"ok": True, "pid": 43210, "task": self.task}),
                             encoding="utf-8")
            return FakeProcess()

        environment = {"ANTHROPIC_AUTH_TOKEN": "TEST-SECRET-INTERNAL"}
        with patch.dict(os.environ, environment):
            with patch.object(terminal_view.subprocess, "Popen", side_effect=fake_popen):
                result = terminal_view.open_window(self.project, self.task, 1.0)

        self.assertEqual(result["pid"], 43210)
        self.assertTrue(result["ready"])
        self.assertIsInstance(captured["command"], list)
        self.assertEqual(captured["command"][captured["command"].index("--project") + 1],
                         str(self.project.resolve()))
        self.assertEqual(captured["command"][captured["command"].index("--task") + 1],
                         self.task)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", captured["kwargs"]["env"])
        self.assertNotIn("TEST-SECRET-INTERNAL",
                         json.dumps(captured["kwargs"]["env"], ensure_ascii=False))
        self.assertEqual(captured["kwargs"]["creationflags"], subprocess.CREATE_NEW_CONSOLE)
        self.assertEqual(captured["kwargs"]["cwd"], str(self.project.resolve()))
        self.assertFalse(captured["ready"].exists())

    def test_open_returns_ready_pid_and_hides_token(self):
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "TEST-SECRET-OUTPUT"}):
            payload = self.console_cli("monitor", "--task", self.task, "--open")
        self.assertTrue(payload["opened"])
        self.assertTrue(payload["ready"])
        self.assertIsInstance(payload["pid"], int)
        self.addCleanup(self.kill_pid, payload["pid"])
        self.assertNotIn("TEST-SECRET-OUTPUT", json.dumps(payload))

    def test_run_terminal_config_opens_viewer_and_reports_fields(self):
        config = self.executor_config(
            "import json,sys\nfrom pathlib import Path\n"
            "Path('executed.txt').write_text('ok', encoding='utf-8')\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'result':'完成'}), flush=True)\n",
            terminal=True)
        result = self.console_cli("run", "--task", self.task, config=config)
        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["status"], "pending_review")
        self.assertIsInstance(result["terminal_pid"], int)
        self.assertIsNone(result["terminal_error"])
        self.assertIsNone(result["dashboard_url"])
        self.addCleanup(self.kill_pid, result["terminal_pid"])
        self.assertTrue((self.project / "executed.txt").exists())
        evidence = json.loads(Path(result["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["terminal_pid"], result["terminal_pid"])
        self.assertFalse((self.project / "协作记录" / "看板" / "server.json").exists())

    def test_run_terminal_failure_keeps_dispatch_and_reports_error(self):
        config = self.executor_config(
            "import json,sys\nfrom pathlib import Path\n"
            "Path('executed.txt').write_text('ok', encoding='utf-8')\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'result':'完成'}), flush=True)\n",
            terminal=True)
        result = self.console_cli("run", "--task", self.task, config=config, fail_window=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["status"], "pending_review")
        self.assertIsNone(result["terminal_pid"])
        self.assertIn("终端看板启动失败", result["terminal_error"])
        self.assertTrue((self.project / "executed.txt").exists())
        evidence = json.loads(Path(result["evidence"]).read_text(encoding="utf-8"))
        self.assertIn("终端看板启动失败", evidence["terminal_error"])

    def test_run_terminal_takes_priority_over_dashboard(self):
        config = self.executor_config(
            "import json,sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'result':'完成'}), flush=True)\n",
            terminal=True, dashboard=True)
        result = self.console_cli("run", "--task", self.task, config=config)
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["terminal_pid"], int)
        self.assertIsNone(result["dashboard_url"])
        self.addCleanup(self.kill_pid, result["terminal_pid"])
        self.assertFalse((self.project / "协作记录" / "看板" / "server.json").exists())

    def test_monitor_rejects_invalid_task_uninitialized_project_and_bad_interval(self):
        invalid = self.run_cli("monitor", "--task", "bad", "--once")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("任务 ID", invalid.stderr)

        missing = self.run_cli("monitor", "--task", "0" * 12, "--once")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("任务不存在", missing.stderr)

        for interval in ("0", "-1", "nan", "inf"):
            with self.subTest(interval=interval):
                result = self.run_cli("monitor", "--task", self.task, "--once",
                                      "--interval", interval)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("正数", result.stderr)

        conflict = self.run_cli("monitor", "--task", self.task, "--open", "--once")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("同时使用", conflict.stdout)

        other = Path(self.tmp.name) / "未初始化 项目"
        other.mkdir()
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(other),
             "monitor", "--task", "0" * 12, "--once"],
            capture_output=True, encoding="utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("尚未初始化", result.stderr)
        self.assertFalse((other / "协作记录" / "state.sqlite3").exists())

    def test_monitor_separates_reviewed_and_approved(self):
        project = Path(self.tmp.name) / "Git 项目"
        project.mkdir()
        (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
        git = ["git", "-C", str(project)]
        subprocess.run(git + ["init", "-q"], check=True, capture_output=True)
        subprocess.run(git + ["add", "app.py"], check=True, capture_output=True)
        subprocess.run(git + ["-c", "user.name=Terminal Test", "-c", "user.email=t@localhost",
                              "commit", "-qm", "baseline"], check=True, capture_output=True)
        plan = Path(self.tmp.name) / "git计划.md"
        plan.write_text("实现功能。验收：测试通过。", encoding="utf-8")

        def run(*args):
            result = subprocess.run(
                [sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(project), *args],
                capture_output=True, encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        run("init")
        task = run("new", "--title", "Git 任务", "--plan-file", str(plan))["task"]["id"]
        passed = Path(self.tmp.name) / "阶段通过.md"
        passed.write_text("阶段复核通过 MARKER。", encoding="utf-8")
        run("review", "--task", task, "--agent", "codex", "--outcome", "passed",
            "--file", str(passed))
        report = Path(self.tmp.name) / "最终审核.md"
        report.write_text("最终验收通过 MARKER。", encoding="utf-8")
        head = subprocess.run(git + ["rev-parse", "HEAD"], check=True,
                              capture_output=True, encoding="utf-8").stdout.strip()
        run("approve", "--task", task, "--agent", "codex", "--revision", head,
            "--report", str(report))

        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(project),
             "monitor", "--task", task, "--once"],
            capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = result.stdout
        self.assertIn("阶段复核通过（待最终验收）", text)
        self.assertIn("最终验收通过", text)
        self.assertIn("阶段复核通过 MARKER", text)
        self.assertIn("最终验收通过 MARKER", text)
        self.assertIn("最终验收基线", text)

    def test_stopping_view_does_not_change_task_state(self):
        attempt = "eeee11112222"
        self.live_file(attempt, [
            self.event(attempt, "started", {"attempt_number": 1, "model": "测试模型"}),
            self.event(attempt, "finished", {"ok": True, "error": None}),
        ])
        self.set_status("pending_review")
        before = self.cli("handoff", "--task", self.task)["task"]
        process = self.start_follow()
        self.read_until("等待 Codex 复核")
        process.kill()
        process.wait(timeout=5)
        after = self.cli("handoff", "--task", self.task)["task"]
        self.assertEqual(before, after)
        self.assertEqual(after["status"], "pending_review")
