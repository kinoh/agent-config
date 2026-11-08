import importlib.util
from pathlib import Path
import tomllib
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
import importlib.machinery
spec = importlib.util.spec_from_file_location("herdr_launch", ROOT / "scripts/codex-herdr", loader=importlib.machinery.SourceFileLoader("herdr_launch", str(ROOT / "scripts/codex-herdr")))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class HerdrLaunchTests(unittest.TestCase):
    def environment(self, pane):
        return {"HERDR_ENV": "1", "HERDR_PANE_ID": pane, "HERDR_TAB_ID": "w1:t1",
                "HERDR_WORKSPACE_ID": "w1", "HERDR_SOCKET_PATH": "/home/test/herdr.sock"}

    def test_each_launch_passes_its_own_environment_and_preserves_arguments(self):
        args = ["--dangerously-bypass-approvals-and-sandbox", "resume", "session"]
        for pane in ["w1:p1", "w1:p2"]:
            env = self.environment(pane)
            result = launcher.launch_args(args, env)
            self.assertEqual(result[0], "--no-daemon")
            self.assertEqual(result[-len(args):], args)
            values = {}
            for flag, override in zip(result[1:-len(args):2], result[2:-len(args):2]):
                self.assertEqual(flag, "-c")
                values.update(tomllib.loads(override)["shell_environment_policy"]["set"])
            self.assertEqual(values, env)

    def test_environment_values_are_literal_toml_strings(self):
        env = self.environment("w1:p1")
        env["HERDR_SOCKET_PATH"] = '/path/"quote"/改行\n/$(private)`value`'
        result = launcher.launch_args([], env)
        values = {}
        for override in result[2::2]:
            values.update(tomllib.loads(override)["shell_environment_policy"]["set"])
        self.assertEqual(values, env)

    def test_incomplete_herdr_context_is_rejected(self):
        env = self.environment("w1:p1")
        del env["HERDR_SOCKET_PATH"]
        with self.assertRaisesRegex(ValueError, "HERDR_SOCKET_PATH"):
            launcher.launch_args([], env)

    def test_outside_herdr_preserves_the_original_launch(self):
        self.assertEqual(launcher.launch_args(["--help"], {}), ["--help"])

    def test_codex_is_resolved_from_path(self):
        with mock.patch.object(launcher.shutil, "which", return_value="/opt/bin/codex"), \
                mock.patch.object(launcher.os, "execv") as execv, \
                mock.patch.object(launcher.sys, "argv", ["codex-herdr", "--help"]), \
                mock.patch.dict(launcher.os.environ, {}, clear=True):
            launcher.main()
        execv.assert_called_once_with("/opt/bin/codex", ["/opt/bin/codex", "--help"])

    def test_missing_codex_fails_without_launching(self):
        with mock.patch.object(launcher.shutil, "which", return_value=None), \
                mock.patch.object(launcher.os, "execv") as execv, \
                mock.patch.object(launcher.sys, "argv", ["codex-herdr"]), \
                mock.patch.dict(launcher.os.environ, {}, clear=True):
            self.assertEqual(launcher.main(), 2)
        execv.assert_not_called()
