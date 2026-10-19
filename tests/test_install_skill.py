"""Render and install skills without touching the user's real home directory."""
import argparse
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tools import install_skill


ROOT = Path(__file__).resolve().parents[1]


class InstallSkillTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Portable folder with spaces"
        template = self.root / ".claude" / "skills" / "local-dream" / "SKILL.md"
        template.parent.mkdir(parents=True)
        shutil.copy2(ROOT / ".claude" / "skills" / "local-dream" / "SKILL.md", template)

    def assert_commands(self, content, command):
        self.assertNotIn("{{LDPC_", content)
        self.assertIn(f"{command} devices --json", content)
        self.assertIn(f"{command} models --phone DEVICE_ID --json", content)
        self.assertIn(f'{command} gen "PROMPT" --phone DEVICE_ID --model MODEL_ID --json', content)
        self.assertTrue(content.endswith(install_skill.MARKER + "\n"))

    def test_source_template_uses_its_own_sdgen(self):
        (self.root / "sdgen.py").touch()
        content = install_skill.render_skill(self.root)
        self.assert_commands(content, f'python "{self.root.as_posix()}/sdgen.py"')
        self.assertIn(f"Installation: `{self.root.as_posix()}`", content)

    def test_packaged_template_uses_console_cli_even_if_source_is_present(self):
        (self.root / "sdgen.py").touch()
        for executable in ("ldd.exe", "ldd"):
            with self.subTest(executable=executable):
                path = self.root / executable
                path.touch()
                try:
                    content = install_skill.render_skill(self.root)
                    prefix = "& " if os.name == "nt" else ""
                    self.assert_commands(content, f'{prefix}"{path.as_posix()}" --cli')
                    self.assertNotIn(f'python "{self.root.as_posix()}/sdgen.py"', content)
                    self.assertNotIn(f'"{ROOT.as_posix()}/sdgen.py"', content)
                    self.assertNotIn(f'"{ROOT.as_posix()}/{executable}"', content)
                finally:
                    path.unlink()

    def test_incomplete_installation_fails_instead_of_using_a_developer_path(self):
        with self.assertRaisesRegex(ValueError, "complete source tree or extracted package"):
            install_skill.render_skill(self.root)

    def test_install_from_package_is_idempotent_for_both_agent_targets(self):
        (self.root / "ldd.exe").touch()
        home = Path(self.tmp.name) / "isolated home"
        with patch.object(install_skill, "ROOT", self.root), patch.object(Path, "home", return_value=home):
            for codex in (False, True):
                args = argparse.Namespace(codex=codex, uninstall=False, force=False, dry_run=False)
                target = home / (".codex" if codex else ".claude") / "skills" / "local-dream" / "SKILL.md"
                with redirect_stdout(io.StringIO()):
                    install_skill.install(args)
                self.assertEqual(target.read_text(encoding="utf-8"), install_skill.render_skill(self.root))
                with redirect_stdout(io.StringIO()) as output:
                    install_skill.install(args)
                self.assertIn("already up to date", output.getvalue())
                args.uninstall = True
                with redirect_stdout(io.StringIO()):
                    install_skill.install(args)
                self.assertFalse(target.exists())
