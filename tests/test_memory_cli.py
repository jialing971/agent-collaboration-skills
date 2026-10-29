import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills/shared-project-memory/scripts/memory.py"


class MemoryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "中文项目"
        self.project.mkdir()
        self.store = self.root / "shared.sqlite3"

    def command(self, *args, project=None):
        return [sys.executable, "-X", "utf8", str(SCRIPT), "--store", str(self.store),
                "--project", str(project or self.project), *args]

    def run_cli(self, *args, project=None):
        result = subprocess.run(self.command(*args, project=project), capture_output=True,
                                text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def save(self, body, agent="codex", project=None):
        source = self.root / f"{agent}.md"
        source.write_text(body, encoding="utf-8")
        return self.run_cli("save", "--agent", agent, "--title", "阶段交接",
                            "--file", str(source), project=project)

    def test_codex_and_claude_can_read_each_others_chinese_checkpoints(self):
        self.save("目标：修复相机超时。已改配置，现场尚未验证。", "codex")
        self.save("已读取 Codex 记录。下一步：运行离线测试。", "claude")
        records = self.run_cli("recall")["records"]
        self.assertEqual([r["agent"] for r in records], ["claude", "codex"])
        self.assertEqual(records[1]["body"], "目标：修复相机超时。已改配置，现场尚未验证。")

    def git(self, *args):
        result = subprocess.run([shutil.which("git"), "-C", str(self.project), *args],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_git_worktree_shares_project_but_isolates_branch(self):
        self.git("init", "-b", "main")
        self.git("-c", "user.name=Memory Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-m", "initial")
        self.save("主分支：已验证离线启动。")
        self.git("switch", "-c", "feature/camera")
        self.save("相机分支：正在排障，尚未验证。", "claude")
        worktree = self.root / "主分支工作树"
        self.git("worktree", "add", str(worktree), "main")
        main_records = self.run_cli("recall", project=worktree)["records"]
        self.assertEqual([r["body"] for r in main_records], ["主分支：已验证离线启动。"])
        feature_records = self.run_cli("recall")["records"]
        self.assertEqual([r["body"] for r in feature_records], ["相机分支：正在排障，尚未验证。"])

    def test_keyword_search_reads_both_agents_without_crossing_projects(self):
        self.save("相机连接超时，先核对网卡。", "codex")
        self.save("PLC 模拟器启动通过。", "claude")
        other = self.root / "另一个项目"
        other.mkdir()
        self.save("另一个客户的相机超时。", "claude", project=other)
        records = self.run_cli("search", "--query", "相机 超时")["records"]
        self.assertEqual([r["body"] for r in records], ["相机连接超时，先核对网卡。"])

    def test_context_and_empty_recall_leave_unstarted_store_absent(self):
        context = self.run_cli("context")
        self.assertEqual(context["branch"], "(无 Git)")
        self.assertEqual(context["project_root"], str(self.project.resolve()))
        self.assertEqual(self.run_cli("recall")["records"], [])
        self.assertFalse(self.store.exists())

    def test_concurrent_writers_keep_old_and_new_checkpoints(self):
        self.save("最早的交接记录。")
        processes = []
        for index in range(8):
            source = self.root / f"parallel-{index}.md"
            source.write_text(f"并发记录 {index}", encoding="utf-8")
            command = self.command("save", "--agent", "codex" if index % 2 else "claude",
                                   "--title", f"阶段 {index}", "--file", str(source))
            processes.append(subprocess.Popen(command, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True, encoding="utf-8"))
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertTrue(json.loads(stdout)["saved"])
        records = self.run_cli("recall", "--limit", "50")["records"]
        self.assertEqual(len(records), 9)
        self.assertEqual({r["body"] for r in records},
                         {"最早的交接记录。", *(f"并发记录 {i}" for i in range(8))})

    def test_invalid_read_limit_is_rejected(self):
        self.save("有效记录。")
        result = subprocess.run(self.command("recall", "--limit", "-1"),
                                capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.run_cli("recall")["records"]), 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell 5.1 接口")
    def test_powershell51_entrypoint_preserves_chinese_project_path(self):
        shell = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        environment = dict(os.environ, SHARED_PROJECT_MEMORY_PYTHON=sys.executable)
        result = subprocess.run([str(shell), "-NoProfile", "-File",
                                 str(SCRIPT.with_suffix(".ps1")), "--project", str(self.project),
                                 "--store", str(self.store), "context"],
                                env=environment, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["project_root"], str(self.project.resolve()))

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell 5.1 接口")
    def test_entrypoint_fails_clearly_when_verified_runtime_is_missing(self):
        environment = dict(os.environ, USERPROFILE=str(self.root))
        environment.pop("SHARED_PROJECT_MEMORY_PYTHON", None)
        result = subprocess.run(["C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                                 "-NoProfile", "-File", str(SCRIPT.with_suffix(".ps1")),
                                 "--project", ".", "context"],
                                env=environment, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Codex 自带 Python 不存在", result.stderr)


if __name__ == "__main__":
    unittest.main()
