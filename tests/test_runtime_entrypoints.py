import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "skills/project-relay/scripts/relay.ps1"
MEMORY = ROOT / "skills/shared-project-memory/scripts/memory.ps1"
POWERSHELL = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell 5.1 入口")
class RuntimeEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="entry-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "用户配置"
        self.profile.mkdir()
        self.project = self.root / "临时 项目" / "中文 路径"
        self.project.mkdir(parents=True)

    def run_ps(self, script, *args, **overrides):
        environment = dict(os.environ)
        environment.pop("PROJECT_RELAY_PYTHON", None)
        environment.pop("SHARED_PROJECT_MEMORY_PYTHON", None)
        environment["USERPROFILE"] = str(self.profile)
        environment.update({key: str(value) for key, value in overrides.items()})
        return subprocess.run([str(POWERSHELL), "-NoProfile", "-File", str(script),
                               *map(str, args)],
                              env=environment, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def test_relay_entry_runs_with_configured_python_and_chinese_space_path(self):
        result = self.run_ps(RELAY, "-Project", self.project, "init",
                             PROJECT_RELAY_PYTHON=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["project"], str(self.project.resolve()))
        status = self.run_ps(RELAY, "-Project", self.project, "status",
                             PROJECT_RELAY_PYTHON=sys.executable)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["tasks"], [])
        self.assertTrue((self.project / "协作记录").is_dir())

    def test_memory_entry_runs_with_configured_python_and_chinese_space_path(self):
        store = self.root / "共享 记忆.sqlite3"
        result = self.run_ps(MEMORY, "--project", self.project, "--store", store, "context",
                             SHARED_PROJECT_MEMORY_PYTHON=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["project_root"], str(self.project.resolve()))

    def test_relay_entry_rejects_invalid_configured_python_without_fallback(self):
        missing = self.root / "missing-python.exe"
        result = self.run_ps(RELAY, "-Project", self.project, "init",
                             PROJECT_RELAY_PYTHON=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PROJECT_RELAY_PYTHON", result.stderr)
        self.assertIn(str(missing), result.stderr)
        self.assertFalse((self.project / "协作记录").exists())

    def test_memory_entry_rejects_invalid_configured_python_without_fallback(self):
        missing = self.root / "missing-python.exe"
        store = self.root / "隔离.sqlite3"
        result = self.run_ps(MEMORY, "--project", self.project, "--store", store, "context",
                             SHARED_PROJECT_MEMORY_PYTHON=missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHARED_PROJECT_MEMORY_PYTHON", result.stderr)
        self.assertIn(str(missing), result.stderr)
        self.assertFalse(store.exists())

    def test_relay_entry_reports_missing_default_runtime(self):
        result = self.run_ps(RELAY, "-Project", self.project, "init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Codex 自带 Python 不存在", result.stderr)
        self.assertFalse((self.project / "协作记录").exists())


if __name__ == "__main__":
    unittest.main()
