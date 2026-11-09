"""Offline contract tests for local HyperFrames opt-in wrapper."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

FILE = Path(__file__).resolve().parents[1] / "scripts" / "hyperframes_local_render.py"
spec = importlib.util.spec_from_file_location("hyperframes_local_render", FILE)
unit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unit)


class HyperFramesSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "video"
        self.project.mkdir()
        (self.project / "package.json").write_text('{"name":"owned-video"}')

    def test_plan_does_not_run_or_publish(self):
        result = unit.prepare(self.project)
        self.assertEqual(result["command"], ["hyperframes", "render"])
        self.assertFalse(result["auto_publish"])
        self.assertFalse(result["rights_confirmed"])

    def test_missing_package_denied(self):
        (self.project / "package.json").unlink()
        with self.assertRaises(ValueError):
            unit.prepare(self.project)

    def test_symlink_denied(self):
        link = self.project.parent / "link"
        link.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(ValueError):
            unit.prepare(link)

    def test_successful_local_only_render_call(self):
        called = {}
        def fake_runner(command, **kwargs):
            called.update(kwargs)
            return SimpleNamespace(returncode=0)
        with patch.object(unit.shutil, "which", return_value="/usr/local/bin/hyperframes"):
            result = unit.render(self.project, runner=fake_runner)
        self.assertEqual(result["status"], "RENDER_COMMAND_COMPLETED")
        self.assertFalse(result["media_rights_attested"])
        self.assertNotIn("OPENAI_API_KEY", called["env"])
        self.assertFalse(result["runtime_verified_output"])

    def test_missing_binary_refused(self):
        with patch.object(unit.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                unit.render(self.project)

    def test_failure_exit_code_refused(self):
        def failure(*args, **kwargs):
            return SimpleNamespace(returncode=127)
        with patch.object(unit.shutil, "which", return_value="/usr/bin/hyperframes"):
            with self.assertRaises(RuntimeError):
                unit.render(self.project, runner=failure)


if __name__ == "__main__":
    unittest.main()
