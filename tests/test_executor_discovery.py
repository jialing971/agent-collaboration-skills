import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/project-relay/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("relay_discovery", SCRIPTS / "relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class ExecutorDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / "claude-code"
        self.settings = self.folder / "desktop.json"
        self.settings.write_text(json.dumps({
            "inferenceGatewayBaseUrl": "http://127.0.0.1:15721/claude-desktop",
            "inferenceGatewayApiKey": "TEST-SECRET",
            "inferenceModels": [{"name": "alias", "labelOverride": "upstream"}],
        }), encoding="utf-8")
        self.state = self.folder / "state.json"
        self.state.write_text(json.dumps({"apps": {"claude-desktop": {
            "mode": "proxy", "attached": True, "proxy_route": "provider",
        }}}), encoding="utf-8")
        self.config = self.folder / "config.json"
        self.data = {
            "command": [str(self.root / "2.1.317" / "old" / "claude.exe"), "--extra"],
            "claude_code_root": str(self.root),
            "desktop_config": str(self.settings), "cc_switch_state": str(self.state),
            "expected_provider_id": "provider", "model": "alias",
            "expected_upstream_model": "upstream",
        }

    def candidate(self, version, build="build"):
        executable = self.root / version / build / "claude.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"test executable")
        return executable

    def load(self):
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        return relay.load_executor(self.config)

    @patch.object(relay.subprocess, "run")
    def test_doctor_discovers_upgrade_and_keeps_arguments_and_config(self, run):
        self.candidate("2.1.9")
        current = self.candidate("2.1.321")
        run.return_value = subprocess.CompletedProcess([], 0, "2.1.321 (Claude Code)\n", "")
        config, environment, metadata = self.load()
        self.assertEqual(config["command"], [str(current), "--extra"])
        self.assertEqual(metadata["command"], [str(current), "--extra"])
        self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "TEST-SECRET")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), self.data)
        self.assertEqual(run.call_args.args[0], [str(current), "--version"])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", run.call_args.kwargs["env"])

    @patch.object(relay.subprocess, "run")
    def test_next_load_discovers_new_version_while_old_binary_still_exists(self, run):
        old = self.candidate("2.1.9")
        self.data["command"] = [str(old)]
        run.return_value = subprocess.CompletedProcess([], 0, "2.1.9 (Claude Code)", "")
        self.assertEqual(self.load()[2]["command"], [str(old)])
        current = self.candidate("2.1.10")
        run.return_value = subprocess.CompletedProcess([], 0, "2.1.10 (Claude Code)", "")
        self.assertEqual(self.load()[2]["command"], [str(current)])

    @patch.object(relay.subprocess, "run")
    def test_same_version_prefers_most_recent_build(self, run):
        old = self.candidate("2.1.321", "old")
        current = self.candidate("2.1.321", "new")
        os.utime(old, (100, 100))
        os.utime(current, (200, 200))
        run.return_value = subprocess.CompletedProcess([], 0, "2.1.321 (Claude Code)", "")
        self.assertEqual(self.load()[2]["command"][0], str(current))

    @patch.object(relay.subprocess, "run")
    def test_unusable_upgrade_uses_valid_installed_version(self, run):
        old = self.candidate("2.1.320")
        self.candidate("2.1.321")
        for failure in (
            OSError("incomplete update"),
            subprocess.TimeoutExpired("claude", 10),
            subprocess.CompletedProcess([], 1, "", "error"),
            subprocess.CompletedProcess([], 0, "2.1.999 (Claude Code)", ""),
        ):
            with self.subTest(failure=failure):
                run.side_effect = [failure, subprocess.CompletedProcess([], 0, "2.1.320 (Claude Code)", "")]
                self.assertEqual(self.load()[2]["command"][0], str(old))

    @patch.object(relay.subprocess, "run")
    def test_empty_and_unversioned_downloads_are_ignored(self, run):
        self.candidate("2.1.322").write_bytes(b"")
        self.candidate("partial-download")
        current = self.candidate("2.1.321")
        run.return_value = subprocess.CompletedProcess([], 0, "2.1.321 (Claude Code)", "")
        self.assertEqual(self.load()[2]["command"][0], str(current))
        self.assertEqual(run.call_count, 1)

    @patch.object(relay.subprocess, "run")
    def test_no_valid_binary_fails_with_actionable_error(self, run):
        self.candidate("2.1.321")
        run.return_value = subprocess.CompletedProcess([], 0, "Python 3.12", "")
        with self.assertRaisesRegex(ValueError, "没有通过 --version 校验"):
            self.load()

    @patch.object(relay.subprocess, "run")
    def test_route_rejection_happens_before_any_binary_probe(self, run):
        self.candidate("2.1.321")
        self.state.write_text('{"apps":{}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "路由"):
            self.load()
        run.assert_not_called()

    @patch.object(relay.subprocess, "run")
    def test_fixed_command_is_preserved_when_discovery_is_unconfigured(self, run):
        del self.data["claude_code_root"]
        self.data["command"] = [sys.executable, "-X", "utf8"]
        self.assertEqual(self.load()[2]["command"], [sys.executable, "-X", "utf8"])
        run.assert_not_called()

    @patch.object(relay.subprocess, "run")
    def test_missing_or_relative_root_is_rejected(self, run):
        with self.assertRaisesRegex(ValueError, "没有通过 --version 校验"):
            self.load()
        self.data["claude_code_root"] = "relative/claude-code"
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            self.load()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
