import json
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
