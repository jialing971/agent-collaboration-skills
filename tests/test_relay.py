import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import time

SCRIPT = Path(__file__).resolve().parents[1] / "skills/project-relay/scripts/relay.py"
WINDOWS_EXECUTOR = unittest.skipUnless(sys.platform == "win32", "执行器进程管理仅 Windows 支持")


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="relay-test-")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "中文项目"
        self.project.mkdir()
        self.plan = Path(self.tmp.name) / "计划.md"
        self.plan.write_text("实现整数相加。验证：2 + 3 = 5。", encoding="utf-8")

    def cli(self, *args, ok=True, config=None):
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(self.project)]
        if config:
            command += ["--config", str(config)]
        result = subprocess.run(command + list(args), capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def new(self):
        self.cli("init")
        return self.cli("new", "--title", "整数相加", "--plan-file", str(self.plan))["task"]["id"]

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.project), *args], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def git_project(self):
        self.git("init", "-q")
        (self.project / "sum.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        self.git("add", "sum.py")
        self.git("-c", "user.name=Relay Test", "-c", "user.email=relay@localhost", "commit", "-qm", "baseline")

    def commit(self, content, message):
        (self.project / "sum.py").write_text(content, encoding="utf-8")
        self.git("add", "sum.py")
        self.git("-c", "user.name=Relay Test", "-c", "user.email=relay@localhost", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def reviewed_baseline(self):
        db = sqlite3.connect(self.project / "协作记录" / "state.sqlite3")
        try:
            row = db.execute("SELECT sha FROM reviews").fetchone()
            return row[0] if row else None
        finally:
            db.close()

    def executor_config(self, body):
        folder = Path(self.tmp.name)
        fake = folder / "fake_claude.py"
        fake.write_text(body, encoding="utf-8")
        settings = folder / "desktop.json"
        settings.write_text(json.dumps({"inferenceGatewayBaseUrl": "http://127.0.0.1:15721/claude-desktop",
            "inferenceGatewayApiKey": "TEST-SECRET-123456", "inferenceModels": [
                {"name": "claude-sonnet-5", "labelOverride": "deepseek-flash"}]}), encoding="utf-8")
        state = folder / "live-state.json"
        state.write_text(json.dumps({"apps": {"claude-desktop": {
            "mode": "proxy", "attached": True, "proxy_route": "deepseek-id"}}}), encoding="utf-8")
        config = folder / "config.json"
        config.write_text(json.dumps({"command": [sys.executable, "-X", "utf8", str(fake)],
            "desktop_config": str(settings), "cc_switch_state": str(state),
            "expected_provider_id": "deepseek-id", "model": "claude-sonnet-5",
            "expected_upstream_model": "deepseek-flash"}), encoding="utf-8")
        return config

    def test_claude_can_take_over_and_codex_recovers_all_checkpoints(self):
        task = self.new()
        checkpoint = Path(self.tmp.name) / "交接.md"
        checkpoint.write_text("已完成函数；测试通过；下一步边界测试。", encoding="utf-8")
        self.cli("takeover", "--task", task, "--agent", "claude")
        self.cli("checkpoint", "--task", task, "--agent", "claude", "--file", str(checkpoint))
        self.cli("takeover", "--task", task, "--agent", "codex")
        handoff = self.cli("handoff", "--task", task)
        self.assertEqual(handoff["task"]["owner"], "codex")
        self.assertIn("下一步边界测试", handoff["checkpoints"][0]["body"])
        self.assertEqual(handoff["task"]["status"], "pending_review")
        self.assertIsNone(handoff["task"]["reviewed_sha"])

    @WINDOWS_EXECUTOR
    def test_brief_handoff_matches_prompt_and_excludes_bulk_history(self):
        task = self.new()
        old_checkpoint = Path(self.tmp.name) / "旧检查点.md"
        old_checkpoint.write_text("OLD-CHECKPOINT-MARK " + "旧" * 6000, encoding="utf-8")
        self.cli("checkpoint", "--task", task, "--agent", "codex", "--file", str(old_checkpoint))
        latest_checkpoint = Path(self.tmp.name) / "最新检查点.md"
        latest_checkpoint.write_text("NEWEST-CHECKPOINT 已完成第一阶段。", encoding="utf-8")
        self.cli("checkpoint", "--task", task, "--agent", "codex",
                 "--file", str(latest_checkpoint))
        review = Path(self.tmp.name) / "审核.md"
        review.write_text("LATEST-REVIEW 请先补测试。" + "审" * 4000, encoding="utf-8")
        self.cli("review", "--task", task, "--agent", "codex",
                 "--outcome", "changes_requested", "--file", str(review))
        bulk = self.executor_config(
            'import json,sys\n'
            'sys.stdin.read()\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"OLD-STDOUT-MARK" + "输" * 40000}), flush=True)\n')
        self.cli("run", "--task", task, config=bulk)
        self.cli("takeover", "--task", task, "--agent", "codex")
        brief = self.cli("handoff", "--task", task, "--brief")
        captured = Path(self.tmp.name) / "prompt.txt"
        config = self.executor_config(
            'import json,sys\nfrom pathlib import Path\n'
            f'Path({json.dumps(str(captured))}).write_text(sys.stdin.read(), encoding="utf-8")\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"完成"}), flush=True)\n')
        self.cli("run", "--task", task, config=config)
        prompt = captured.read_text(encoding="utf-8")
        context = brief["brief"]
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertIn("实现整数相加", prompt)
        self.assertIn("NEWEST-CHECKPOINT", prompt)
        self.assertIn("LATEST-REVIEW", prompt)
        self.assertNotIn("OLD-CHECKPOINT-MARK", prompt)
        self.assertNotIn("OLD-STDOUT-MARK", prompt)
        self.assertIn(serialized, prompt)
        self.assertEqual(brief["context_chars"], len(serialized))
        self.assertEqual(brief["context_limit"], 20000)
        self.assertLessEqual(len(serialized), 20000)
        self.assertLess(len(prompt), 20500)
        self.assertNotIn("events", context)
        self.assertNotIn("stdout", serialized)
        self.assertEqual(context["last_checkpoint"]["body"], "NEWEST-CHECKPOINT 已完成第一阶段。")
        self.assertTrue(context["last_review"]["body"].startswith("LATEST-REVIEW"))
        self.assertTrue(context["last_review"]["truncated"])
        self.assertTrue(context["last_execution"]["ok"])
        self.assertTrue(Path(context["last_execution"]["evidence"]).is_file())
        full = self.cli("handoff", "--task", task)
        self.assertTrue(any("OLD-STDOUT-MARK" in event["body"] for event in full["events"]))
        self.assertTrue(any("OLD-CHECKPOINT-MARK" in item["body"]
                            for item in full["checkpoints"]))

    def test_brief_rejects_oversized_plan_before_executor_or_live_file(self):
        self.cli("init")
        big_plan = Path(self.tmp.name) / "超长计划.md"
        big_plan.write_text("目标：实现。" + "验收条件" * 3000, encoding="utf-8")
        task = self.cli("new", "--title", "超长计划",
                        "--plan-file", str(big_plan))["task"]["id"]
        config = self.executor_config(
            'from pathlib import Path\nPath("SHOULD_NOT_START").touch()\n')
        result = self.cli("run", "--task", task, config=config, ok=False)
        self.assertIn("12000", result["error"])
        self.assertFalse((self.project / "SHOULD_NOT_START").exists())
        self.assertEqual(self.live_files(task), [])
        handoff = self.cli("handoff", "--task", task)
        self.assertEqual(handoff["task"]["owner"], "codex")
        self.assertEqual(handoff["task"]["status"], "planned")
        self.assertIsNone(handoff["task"]["run_pid"])
        brief = self.cli("handoff", "--task", task, "--brief", ok=False)
        self.assertIn("12000", brief["error"])

    def test_codex_can_record_reviewed_commit_with_report(self):
        self.git_project()
        task = self.new()
        report = Path(self.tmp.name) / "审核.md"
        report.write_text("已检查函数并独立验证 2+3=5。", encoding="utf-8")
        revision = self.git("rev-parse", "HEAD")
        result = self.cli("approve", "--task", task, "--agent", "codex", "--revision", revision,
                          "--report", str(report))
        self.assertEqual(result["task"]["reviewed_sha"], revision)
        self.assertEqual(result["task"]["status"], "approved")

    @WINDOWS_EXECUTOR
    def test_external_executor_returns_pending_review_and_redacts_secrets(self):
        task = self.new()
        config = self.executor_config('import json,os,sys\nfrom pathlib import Path\n'
            'prompt=sys.stdin.read()\nassert "实现整数相加" in prompt\n'
            'Path("executed.txt").write_text("ok")\n'
            'print(json.dumps({"is_error":False,"subtype":"success","result":os.environ["ANTHROPIC_AUTH_TOKEN"]}))\n')
        result = self.cli("run", "--task", task, config=config)
        self.assertTrue((self.project / "executed.txt").exists())
        self.assertEqual(result["task"]["status"], "pending_review")
        self.assertNotIn("TEST-SECRET", json.dumps(result))
        logs = list((self.project / "协作记录" / task).glob("*.json"))
        self.assertTrue(logs)
        self.assertTrue(all("TEST-SECRET" not in p.read_text(encoding="utf-8") for p in logs))
        self.assertIsNone(result["task"]["reviewed_sha"])

    @WINDOWS_EXECUTOR
    def test_failed_executor_can_be_taken_over_without_codex_service(self):
        task = self.new()
        config = self.executor_config('import json\nprint(json.dumps({"is_error":True,"subtype":"error","result":"失败"}))\n')
        self.cli("run", "--task", task, config=config, ok=False)
        result = self.cli("takeover", "--task", task, "--agent", "claude")
        self.assertEqual(result["task"]["status"], "interrupted")
        self.assertEqual(result["task"]["owner"], "claude")

    @WINDOWS_EXECUTOR
    def test_executor_uses_current_gateway_token_instead_of_stale_desktop_token(self):
        task = self.new()
        config = self.executor_config('import json,os\n'
            'valid=os.environ["ANTHROPIC_AUTH_TOKEN"] == "CURRENT-GATEWAY-SECRET"\n'
            'print(json.dumps({"is_error":not valid,"subtype":"success" if valid else "error",'
            '"result":os.environ["ANTHROPIC_AUTH_TOKEN"]}))\n')
        gateway = Path(self.tmp.name) / "gateway.sqlite3"
        with sqlite3.connect(gateway) as db:
            db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO settings VALUES (?,?)",
                       ("claude_desktop_gateway_token", "CURRENT-GATEWAY-SECRET"))
        db.close()
        settings = json.loads(config.read_text(encoding="utf-8"))
        settings["cc_switch_db"] = str(gateway)
        config.write_text(json.dumps(settings), encoding="utf-8")
        before = gateway.read_bytes()
        result = self.cli("run", "--task", task, config=config)
        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["status"], "pending_review")
        self.assertEqual(gateway.read_bytes(), before)
        self.assertNotIn("CURRENT-GATEWAY-SECRET", Path(result["evidence"]).read_text(encoding="utf-8"))
        with sqlite3.connect(gateway) as db:
            db.execute("UPDATE settings SET value=?", ("ROTATED-GATEWAY-SECRET",))
        db.close()
        fake = Path(self.tmp.name) / "fake_claude.py"
        fake.write_text(fake.read_text(encoding="utf-8").replace(
            "CURRENT-GATEWAY-SECRET", "ROTATED-GATEWAY-SECRET"), encoding="utf-8")
        rotated = self.cli("run", "--task", task, config=config)
        self.assertTrue(rotated["ok"])
        self.assertNotIn("ROTATED-GATEWAY-SECRET", Path(rotated["evidence"]).read_text(encoding="utf-8"))

    def test_missing_gateway_database_is_rejected_without_creating_it(self):
        task = self.new()
        config = self.executor_config('from pathlib import Path\nPath("SHOULD_NOT_EXIST").touch()\n')
        gateway = Path(self.tmp.name) / "missing.sqlite3"
        settings = json.loads(config.read_text(encoding="utf-8"))
        settings["cc_switch_db"] = str(gateway)
        config.write_text(json.dumps(settings), encoding="utf-8")
        result = self.cli("run", "--task", task, config=config, ok=False)
        self.assertIn("网关凭据", result["error"])
        self.assertFalse(gateway.exists())
        self.assertFalse((self.project / "SHOULD_NOT_EXIST").exists())
        self.assertEqual(self.cli("handoff", "--task", task)["task"]["status"], "planned")
        rejected = next((self.project / "协作记录" / task).glob("执行拒绝-*.json"))
        self.assertFalse(json.loads(rejected.read_text(encoding="utf-8"))["executor_started"])

    def test_missing_gateway_token_never_falls_back_to_desktop_credentials(self):
        task = self.new()
        config = self.executor_config('from pathlib import Path\nPath("SHOULD_NOT_EXIST").touch()\n')
        gateway = Path(self.tmp.name) / "gateway.sqlite3"
        with sqlite3.connect(gateway) as db:
            db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        db.close()
        settings = json.loads(config.read_text(encoding="utf-8"))
        settings["cc_switch_db"] = str(gateway)
        config.write_text(json.dumps(settings), encoding="utf-8")
        result = self.cli("run", "--task", task, config=config, ok=False)
        self.assertIn("不回退", result["error"])
        self.assertFalse((self.project / "SHOULD_NOT_EXIST").exists())
        self.assertEqual(self.cli("handoff", "--task", task)["task"]["status"], "planned")

    @WINDOWS_EXECUTOR
    def test_timeout_preserves_evidence_and_releases_execution(self):
        task = self.new()
        config = self.executor_config('import time\ntime.sleep(30)\n')
        result = self.cli("run", "--task", task, "--timeout", "0.2", config=config, ok=False)
        self.assertIn("超时", result["error"])
        self.assertTrue(Path(result["evidence"]).is_file())
        self.assertIsNone(self.cli("takeover", "--task", task, "--agent", "codex")["task"]["run_pid"])

    def test_route_change_is_rejected_before_starting_executor(self):
        task = self.new()
        config = self.executor_config('from pathlib import Path\nPath("SHOULD_NOT_EXIST").touch()\n')
        state = Path(self.tmp.name) / "live-state.json"
        state.write_text('{"apps":{}}', encoding="utf-8")
        result = self.cli("run", "--task", task, config=config, ok=False)
        self.assertIn("路由", result["error"])
        self.assertFalse((self.project / "SHOULD_NOT_EXIST").exists())
        rejected = list((self.project / "协作记录" / task).glob("执行拒绝-*.json"))
        self.assertEqual(len(rejected), 1)
        payload = json.loads(rejected[0].read_text(encoding="utf-8"))
        self.assertFalse(payload["ok"])
        self.assertIn("路由", payload["error"])
        self.assertNotIn("TEST-SECRET", rejected[0].read_text(encoding="utf-8"))
        handoff = self.cli("handoff", "--task", task)
        self.assertEqual(handoff["task"]["owner"], "codex")
        self.assertEqual(handoff["task"]["status"], "planned")
        self.assertIsNone(handoff["task"]["run_pid"])
        self.assertEqual([e["kind"] for e in handoff["events"]], ["created", "execution_rejected"])
        self.assertIn("路由", handoff["events"][-1]["body"])

    def test_handoff_requires_task_branch(self):
        self.git_project()
        task = self.new()
        branch = self.git("branch", "--show-current")
        self.git("checkout", "-qb", "other")
        result = self.cli("handoff", "--task", task, ok=False)
        self.assertIn("分支", result["error"])
        self.git("checkout", "-q", branch)
        self.assertIsNone(self.cli("handoff", "--task", task)["task"]["reviewed_sha"])

    def test_wrong_agent_dirty_code_revision_and_branch_cannot_be_approved(self):
        self.git_project()
        task = self.new()
        report = Path(self.tmp.name) / "review.md"
        report.write_text("实际审核意见", encoding="utf-8")
        head = self.git("rev-parse", "HEAD")
        args = ("approve", "--task", task, "--report", str(report))
        self.cli(*args, "--agent", "claude", "--revision", head, ok=False)
        self.cli(*args, "--agent", "codex", "--revision", "0" * 40, ok=False)
        (self.project / "sum.py").write_text("broken\n", encoding="utf-8")
        self.cli(*args, "--agent", "codex", "--revision", head, ok=False)
        self.git("checkout", "--", "sum.py")
        branch = self.git("branch", "--show-current")
        self.git("checkout", "-qb", "other")
        self.cli(*args, "--agent", "codex", "--revision", head, ok=False)
        self.cli("handoff", "--task", task, ok=False)
        self.git("checkout", "-q", branch)
        self.assertIsNone(self.cli("handoff", "--task", task)["task"]["reviewed_sha"])

    @WINDOWS_EXECUTOR
    def test_parallel_dispatch_and_takeover_are_rejected_while_worker_runs(self):
        task = self.new()
        config = self.executor_config('import time,json\nfrom pathlib import Path\n'
            'Path("started").touch()\ntime.sleep(2)\n'
            'print(json.dumps({"is_error":False,"subtype":"success","result":"done"}))\n')
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(self.project),
                   "--config", str(config), "run", "--task", task]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.project / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.project / "started").exists())
            checkpoint = Path(self.tmp.name) / "运行中交接.md"
            checkpoint.write_text("执行中阶段结果。", encoding="utf-8")
            self.cli("checkpoint", "--task", task, "--agent", "claude", "--file", str(checkpoint))
            self.assertEqual(self.cli("handoff", "--task", task)["task"]["status"], "running")
            self.cli("run", "--task", task, config=config, ok=False)
            self.cli("takeover", "--task", task, "--agent", "codex", ok=False)
        finally:
            output, errors = process.communicate(timeout=6)
        self.assertEqual(process.returncode, 0, output + errors)

    def test_approve_rejects_commit_before_task_start_even_if_it_is_head(self):
        self.git_project()
        baseline = self.git("rev-parse", "HEAD")
        first = self.new()
        report = Path(self.tmp.name) / "review.md"
        report.write_text("已审核初始提交。", encoding="utf-8")
        self.cli("approve", "--task", first, "--agent", "codex", "--revision", baseline,
                 "--report", str(report))
        earlier = self.commit("def add(a, b): return b + a\n", "change A")
        self.commit("def add(a, b): return a + b + 0\n", "change B")
        task = self.cli("new", "--title", "回退任务", "--plan-file", str(self.plan))["task"]["id"]
        self.git("reset", "--hard", earlier)
        result = self.cli("approve", "--task", task, "--agent", "codex", "--revision", earlier,
                          "--report", str(report), ok=False)
        self.assertIn("起点", result["error"])
        handoff = self.cli("handoff", "--task", task)
        self.assertIsNone(handoff["task"]["reviewed_sha"])
        self.assertEqual(handoff["last_reviewed_sha"], baseline)
        self.assertFalse([e for e in handoff["events"] if e["kind"] == "approved"])
        self.assertEqual(self.reviewed_baseline(), baseline)

    def test_approve_rejects_review_baseline_missing_from_rewritten_history(self):
        self.git_project()
        baseline = self.git("rev-parse", "HEAD")
        first = self.new()
        report = Path(self.tmp.name) / "review.md"
        report.write_text("已审核初始提交。", encoding="utf-8")
        self.cli("approve", "--task", first, "--agent", "codex", "--revision", baseline,
                 "--report", str(report))
        branch = self.git("branch", "--show-current")
        self.git("checkout", "-q", "--orphan", "rewritten")
        self.git("-c", "user.name=Relay Test", "-c", "user.email=relay@localhost", "commit", "-qm", "rewritten root")
        rewritten = self.git("rev-parse", "HEAD")
        self.git("branch", "-M", branch)
        task = self.cli("new", "--title", "重写历史任务", "--plan-file", str(self.plan))["task"]["id"]
        self.cli("handoff", "--task", task, ok=False)
        result = self.cli("approve", "--task", task, "--agent", "codex", "--revision", rewritten,
                          "--report", str(report), ok=False)
        self.assertIn("起点", result["error"])
        self.assertEqual(self.cli("status")["tasks"][-1]["reviewed_sha"], None)
        self.assertEqual(self.reviewed_baseline(), baseline)

    @WINDOWS_EXECUTOR
    def test_unclean_controller_exit_blocks_takeover_until_worker_exits(self):
        task = self.new()
        config = self.executor_config('import time,json\nfrom pathlib import Path\n'
            'Path("started").touch()\ntime.sleep(1)\nPath("finished").touch()\n'
            'print(json.dumps({"is_error":False,"subtype":"success","result":"done"}))\n')
        process = subprocess.Popen([sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(self.project),
            "--config", str(config), "run", "--task", task], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.project / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.project / "started").exists())
            process.kill()
            process.wait(timeout=3)
            time.sleep(1.1)
            self.assertFalse((self.project / "finished").exists(), "控制进程退出后，其执行进程树应被终止")
            result = self.cli("takeover", "--task", task, "--agent", "claude")
            self.assertEqual(result["task"]["status"], "interrupted")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def live_files(self, task):
        return sorted((self.project / "协作记录" / task).glob("live-*.jsonl"))

    def wait_until(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    @WINDOWS_EXECUTOR
    def test_stream_events_are_readable_before_executor_finishes(self):
        task = self.new()
        config = self.executor_config(
            'import json,sys,time\nfrom pathlib import Path\n'
            'Path("argv.json").write_text(json.dumps(sys.argv), encoding="utf-8")\n'
            'sys.stdin.read()\n'
            'print(json.dumps({"type":"system","subtype":"init","session_id":"session-live"}), flush=True)\n'
            'print(json.dumps({"type":"assistant","message":{"content":['
            '{"type":"tool_use","id":"tool-1","name":"Bash","input":{"command":"echo live"}}]}}), flush=True)\n'
            'time.sleep(1.5)\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"完成","session_id":"session-live"}), flush=True)\n')
        process = subprocess.Popen([sys.executable, "-X", "utf8", str(SCRIPT),
            "--project", str(self.project), "--config", str(config), "run", "--task", task],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8")
        try:
            def tool_visible():
                return any("tool_use" in p.read_text(encoding="utf-8") for p in self.live_files(task))
            self.assertTrue(self.wait_until(tool_visible), "执行尚未结束时，应能读到工具事件")
            self.assertIsNone(process.poll(), "工具事件出现时执行器应仍在运行")
        finally:
            output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)
        result = json.loads(output)
        self.assertTrue(result["ok"])
        self.assertEqual(result["task"]["status"], "pending_review")
        argv = json.loads((self.project / "argv.json").read_text(encoding="utf-8"))
        self.assertIn("stream-json", argv)
        self.assertIn("--verbose", argv)
        live = self.live_files(task)
        self.assertEqual(len(live), 1)
        entries = [json.loads(line) for line in
                   live[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(all({"at", "attempt_id", "kind", "payload"} <= set(e) for e in entries))
        tool = next(e for e in entries if e["kind"] == "tool_use")
        self.assertEqual(tool["payload"]["name"], "Bash")
        self.assertEqual(tool["session_id"], "session-live")
        self.assertEqual(entries[0]["attempt_id"], entries[-1]["attempt_id"])
        self.assertTrue(any(e["kind"] == "result" for e in entries))
        self.assertEqual(entries[-1]["kind"], "finished")
        self.assertTrue(entries[-1]["payload"]["ok"])

    @WINDOWS_EXECUTOR
    def test_timeout_keeps_events_already_streamed(self):
        task = self.new()
        config = self.executor_config(
            'import json,sys,time\n'
            'sys.stdin.read()\n'
            'print(json.dumps({"type":"system","subtype":"init","session_id":"session-timeout"}), flush=True)\n'
            'print(json.dumps({"type":"assistant","message":{"content":['
            '{"type":"tool_use","id":"tool-1","name":"Read","input":{"file_path":"sum.py"}}]}}), flush=True)\n'
            'time.sleep(30)\n')
        result = self.cli("run", "--task", task, "--timeout", "0.6", config=config, ok=False)
        self.assertIn("超时", result["error"])
        self.assertEqual(result["task"]["status"], "interrupted")
        live = self.live_files(task)
        self.assertEqual(len(live), 1)
        text = live[0].read_text(encoding="utf-8")
        self.assertIn('"tool_use"', text)
        self.assertIn('"session-timeout"', text)
        self.assertIsNone(self.cli("takeover", "--task", task, "--agent", "codex")["task"]["run_pid"])

    @WINDOWS_EXECUTOR
    def test_malformed_or_error_result_is_not_success_and_retry_batch_is_independent(self):
        task = self.new()
        config = self.executor_config(
            'import json,sys\nfrom pathlib import Path\n'
            'sys.stdin.read()\n'
            'count = Path("attempts.txt")\n'
            'n = int(count.read_text()) if count.exists() else 0\n'
            'count.write_text(str(n + 1))\n'
            'print(json.dumps({"type":"system","subtype":"init","session_id":"session-" + str(n)}), flush=True)\n'
            'if n == 0:\n'
            '    print("{not-json", flush=True)\n'
            'else:\n'
            '    print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"重试完成","session_id":"session-1"}), flush=True)\n')
        failed = self.cli("run", "--task", task, config=config, ok=False)
        self.assertEqual(failed["task"]["status"], "interrupted")
        self.assertIn("JSON", failed["error"])
        self.assertEqual(len(self.live_files(task)), 1)
        retried = self.cli("run", "--task", task, config=config)
        self.assertTrue(retried["ok"])
        self.assertEqual(retried["task"]["status"], "pending_review")
        live = self.live_files(task)
        self.assertEqual(len(live), 2)
        first = [json.loads(line) for line in live[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        second = [json.loads(line) for line in live[1].read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertNotEqual(first[0]["attempt_id"], second[0]["attempt_id"])

    @WINDOWS_EXECUTOR
    def test_route_change_during_execution_keeps_failure_over_success_result(self):
        task = self.new()
        state = Path(self.tmp.name) / "live-state.json"
        config = self.executor_config(
            'import json,sys\nfrom pathlib import Path\n'
            'sys.stdin.read()\n'
            f'Path({json.dumps(str(state))}).write_text(\'{{"apps": {{}}}}\', encoding="utf-8")\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"完成","session_id":"session-route"}), flush=True)\n')
        result = self.cli("run", "--task", task, config=config, ok=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["task"]["status"], "interrupted")
        self.assertIn("路由", result["error"])
        evidence_payload = json.loads(Path(result["evidence"]).read_text(encoding="utf-8"))
        self.assertIn("路由", evidence_payload["error"])
        self.assertIn('"subtype": "success"', evidence_payload["stdout"])
        live = self.live_files(task)
        self.assertEqual(len(live), 1)
        self.assertIn('"result"', live[0].read_text(encoding="utf-8"))

    @WINDOWS_EXECUTOR
    def test_malformed_line_is_not_success_even_when_success_result_follows(self):
        task = self.new()
        config = self.executor_config(
            'import json,sys\n'
            'sys.stdin.read()\n'
            'print(json.dumps({"type":"system","subtype":"init","session_id":"session-broken"}), flush=True)\n'
            'print("BROKEN JSON", flush=True)\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"完成","session_id":"session-broken"}), flush=True)\n')
        result = self.cli("run", "--task", task, config=config, ok=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["task"]["status"], "interrupted")
        self.assertIn("JSON", result["error"])
        evidence = json.loads(Path(result["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["parse_errors"], 1)
        self.assertIn("BROKEN JSON", evidence["stdout"])
        live = self.live_files(task)
        self.assertEqual(len(live), 1)
        entries = [json.loads(line) for line in live[0].read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        self.assertTrue(any(item["kind"] == "parse_error" for item in entries))
        self.assertTrue(any(item["kind"] == "result" for item in entries))

    @WINDOWS_EXECUTOR
    def test_rejected_second_run_does_not_create_live_attempt(self):
        task = self.new()
        config = self.executor_config(
            'import json,sys,time\nfrom pathlib import Path\n'
            'Path("started").touch()\n'
            'sys.stdin.read()\n'
            'time.sleep(2)\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"完成"}), flush=True)\n')
        command = [sys.executable, "-X", "utf8", str(SCRIPT), "--project", str(self.project),
                   "--config", str(config), "run", "--task", task]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.project / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.project / "started").exists())
            rejected = self.cli("run", "--task", task, config=config, ok=False)
            self.assertIn("进程", rejected["error"])
            live = self.live_files(task)
            self.assertEqual(len(live), 1, "被拒绝的第二次派活不应创建 live 批次")
            entries = [json.loads(line) for line in live[0].read_text(encoding="utf-8").splitlines()
                       if line.strip()]
            started = next(item for item in entries if item["kind"] == "started")
            self.assertEqual(started["payload"]["attempt_number"], 1)
        finally:
            output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, (output or b"").decode("utf-8", "replace")
                         + (errors or b"").decode("utf-8", "replace"))
        result = json.loads(output)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.live_files(task)), 1)

    @WINDOWS_EXECUTOR
    def test_error_result_payload_is_not_success(self):
        task = self.new()
        config = self.executor_config(
            'import json,sys\n'
            'sys.stdin.read()\n'
            'print(json.dumps({"type":"result","subtype":"error","is_error":True,"result":"失败"}), flush=True)\n')
        result = self.cli("run", "--task", task, config=config, ok=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["task"]["status"], "interrupted")
        self.assertTrue(self.live_files(task))

    @WINDOWS_EXECUTOR
    def test_token_is_redacted_across_delta_events_and_raw_output(self):
        task = self.new()
        token = "TEST-SECRET-123456"
        config = self.executor_config(
            'import json,sys,time\n'
            'sys.stdin.read()\n'
            'stream = json.dumps({"type":"stream_event","event":{"type":"content_block_delta",'
            '"delta":{"type":"text_delta","text":"' + token + '"}}})\n'
            + 'for piece in (stream[:25], stream[25:41], stream[41:]):\n'
            '    sys.stdout.write(piece)\n'
            '    sys.stdout.flush()\n'
            '    time.sleep(0.05)\n'
            'sys.stdout.write("\\n")\n'
            'sys.stdout.flush()\n'
            'for part in ("TEST-SEC", "RET-123", "456"):\n'
            '    print(json.dumps({"type":"stream_event","event":{"type":"content_block_delta",'
            '"delta":{"type":"text_delta","text":part}}}), flush=True)\n'
            'print(json.dumps({"type":"stream_event","event":{"type":"content_block_delta",'
            '"delta":{"type":"thinking_delta","thinking":"THINKING-MARKER"}}}), flush=True)\n'
            'print(json.dumps({"type":"assistant","message":{"content":['
            '{"type":"thinking","thinking":"THINKING-MARKER"},'
            '{"type":"text","text":"结果 ' + token + '"}]}}), flush=True)\n'
            + 'sys.stderr.write("stderr token ' + token + '\\n")\n'
            + 'sys.stderr.flush()\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"' + token + '","session_id":"session-token"}), flush=True)\n')
        result = self.cli("run", "--task", task, config=config)
        self.assertTrue(result["ok"])
        evidence = Path(result["evidence"]).read_text(encoding="utf-8")
        live = self.live_files(task)
        self.assertEqual(len(live), 1)
        live_text = live[0].read_text(encoding="utf-8")
        for text in (evidence, live_text, json.dumps(result)):
            self.assertNotIn(token, text)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN=", text)
        evidence_payload = json.loads(evidence)
        reconstructed = []
        for line in evidence_payload["stdout"].splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") in ("assistant_delta", "assistant"):
                reconstructed.append(item.get("text") or json.dumps(item, ensure_ascii=False))
        self.assertNotIn(token, "".join(reconstructed))
        self.assertIn("[REDACTED]", live_text)
        self.assertNotIn("THINKING-MARKER", live_text)
        entries = [json.loads(line) for line in live_text.splitlines() if line.strip()]
        deltas = [e["payload"]["text"] for e in entries if e["kind"] == "assistant_delta"]
        self.assertIn("[REDACTED]", "".join(deltas))

    @WINDOWS_EXECUTOR
    def test_thinking_token_stats_are_not_operation_events(self):
        task = self.new()
        config = self.executor_config(
            'import json,sys\n'
            'sys.stdin.read()\n'
            'def emit(payload):\n'
            '    print(json.dumps(payload), flush=True)\n'
            'emit({"type":"system","subtype":"init","session_id":"session-noise"})\n'
            'for _ in range(200):\n'
            '    emit({"type":"system","subtype":"thinking_tokens","session_id":"session-noise"})\n'
            'emit({"type":"system","subtype":"status","session_id":"session-noise","status":"requesting"})\n'
            'emit({"type":"system","subtype":"task_summary","session_id":"session-noise","summary":"摘要"})\n'
            'emit({"type":"assistant","message":{"content":[{"type":"text","text":"公开回复"}]}})\n'
            'emit({"type":"result","subtype":"success","is_error":False,'
            '"result":"完成","session_id":"session-noise"})\n')
        result = self.cli("run", "--task", task, config=config)
        self.assertTrue(result["ok"])
        live = self.live_files(task)
        self.assertEqual(len(live), 1)
        live_text = live[0].read_text(encoding="utf-8")
        self.assertNotIn("thinking_tokens", live_text)
        evidence = json.loads(Path(result["evidence"]).read_text(encoding="utf-8"))
        self.assertNotIn("thinking_tokens", evidence["stdout"])
        self.assertEqual(evidence["parse_errors"], 0)
        entries = [json.loads(line) for line in live_text.splitlines() if line.strip()]
        subtypes = [event["payload"].get("subtype") for event in entries
                    if event["kind"] == "system"]
        self.assertEqual(subtypes, ["init", "status", "task_summary"])
        self.assertIn("公开回复", live_text)

    @WINDOWS_EXECUTOR
    def test_permission_denials_cannot_be_reported_as_success(self):
        task = self.new()
        config = self.executor_config(
            'import json,sys\n'
            'sys.stdin.read()\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"result":"完成","permission_denials":[{"tool_name":"Bash"}]}), flush=True)\n')
        result = self.cli("run", "--task", task, config=config, ok=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["task"]["status"], "interrupted")

    def test_review_outcome_changes_stage_without_fixing_baseline(self):
        self.git_project()
        task = self.new()
        self.cli("takeover", "--task", task, "--agent", "codex")
        report = Path(self.tmp.name) / "阶段审核.md"
        report.write_text("独立检查了改动与测试，阶段通过。", encoding="utf-8")
        passed = self.cli("review", "--task", task, "--agent", "codex",
                          "--outcome", "passed", "--file", str(report))
        self.assertEqual(passed["task"]["status"], "reviewed")
        self.assertIsNone(passed["task"]["reviewed_sha"])
        self.assertIsNone(self.reviewed_baseline())
        handoff = self.cli("handoff", "--task", task)
        self.assertEqual(handoff["events"][-1]["kind"], "review_passed")
        self.assertIn("独立检查", handoff["events"][-1]["body"])

        changes = Path(self.tmp.name) / "返工.md"
        changes.write_text("验收条件未满足，退回返工。", encoding="utf-8")
        requested = self.cli("review", "--task", task, "--agent", "codex",
                             "--outcome", "changes_requested", "--file", str(changes))
        self.assertEqual(requested["task"]["status"], "changes_requested")
        self.assertIsNone(requested["task"]["reviewed_sha"])
        self.assertEqual(self.cli("handoff", "--task", task)["events"][-1]["kind"],
                         "review_changes_requested")

        revision = self.git("rev-parse", "HEAD")
        approved = self.cli("approve", "--task", task, "--agent", "codex",
                            "--revision", revision, "--report", str(report))
        self.assertEqual(approved["task"]["status"], "approved")
        self.assertEqual(approved["task"]["reviewed_sha"], revision)
        self.assertEqual(self.reviewed_baseline(), revision)

    def test_review_requires_codex_owner_and_idle_execution(self):
        task = self.new()
        report = Path(self.tmp.name) / "审核.md"
        report.write_text("意见", encoding="utf-8")
        self.cli("review", "--task", task, "--agent", "claude", "--outcome", "passed",
                 "--file", str(report), ok=False)
        self.cli("takeover", "--task", task, "--agent", "claude")
        self.cli("review", "--task", task, "--agent", "codex", "--outcome", "passed",
                 "--file", str(report), ok=False)
        db = sqlite3.connect(self.project / "协作记录" / "state.sqlite3")
        try:
            db.execute("UPDATE tasks SET owner='codex',status='running',run_pid=? WHERE id=?",
                       (os.getpid(), task))
            db.commit()
        finally:
            db.close()
        result = self.cli("review", "--task", task, "--agent", "codex", "--outcome", "passed",
                          "--file", str(report), ok=False)
        self.assertIn("进程", result["error"])

    def test_new_task_does_not_skip_changes_since_last_project_review(self):
        self.git_project()
        first = self.new()
        report = Path(self.tmp.name) / "review.md"
        report.write_text("已审核初始提交。", encoding="utf-8")
        approved = self.git("rev-parse", "HEAD")
        self.cli("approve", "--task", first, "--agent", "codex", "--revision", approved,
                 "--report", str(report))
        (self.project / "sum.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
        self.git("add", "sum.py")
        self.git("-c", "user.name=Relay Test", "-c", "user.email=relay@localhost", "commit", "-qm", "claude change")
        second = self.cli("new", "--title", "继续修改", "--plan-file", str(self.plan))["task"]["id"]
        handoff = self.cli("handoff", "--task", second)
        self.assertEqual(handoff["review_from"], approved)
        self.assertTrue(handoff["needs_review"])


if __name__ == "__main__":
    unittest.main()
