"""Recognize the public status contract without launching browsers or probing ports."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import local_dream_desktop as entry
import paths
import sdgen
import studio
from tools import check_env


class StartupTests(unittest.TestCase):
    def payload(self):
        return {"app": paths.APP_ID, "version": paths.__version__,
                "busy": None, "job": None, "error": None, "last": None,
                "now": 0, "phones": {"phones": []}}

    def response(self, data):
        return io.BytesIO(json.dumps(data).encode())

    def test_preflight_recognizes_running_studio(self):
        with patch.object(check_env.socket, "socket") as socket, \
                patch.object(check_env.urllib.request, "build_opener") as opener:
            socket.return_value.__enter__.return_value.bind.side_effect = OSError("occupied")
            opener.return_value.open.return_value = self.response(self.payload())
            self.assertEqual(check_env.port_status(), "studio")
            opener.return_value.open.return_value = self.response({"unrelated": True})
            self.assertEqual(check_env.port_status(), "unavailable")

    def test_relaunch_opens_only_a_recognized_studio(self):
        legacy = {k: v for k, v in self.payload().items() if k not in ("app", "version")}
        for data, expected in ((self.payload(), True), ({"unrelated": True}, False),
                               (legacy, False), ({**self.payload(), "version": "other"}, False)):
            with self.subTest(recognized=expected), \
                    patch.object(studio, "Server", side_effect=OSError("occupied")), \
                    patch.object(studio.urllib.request, "ProxyHandler") as proxy, \
                    patch.object(studio.urllib.request, "build_opener") as opener, \
                    patch.object(studio.webbrowser, "open") as browser:
                opener.return_value.open.return_value = self.response(data)
                if expected:
                    self.assertEqual(studio.main(), 0)
                else:
                    with self.assertRaisesRegex(studio.StartupError, "port 47865"):
                        studio.main()
                proxy.assert_called_once_with({})
                opener.assert_called_once_with(proxy.return_value)
                opener.return_value.open.assert_called_once_with(
                    f"http://{studio.HOST}:{studio.PORT}/api/status", timeout=3)
                self.assertEqual(browser.called, expected)
                if expected:
                    browser.assert_called_once_with(studio.URL)

    def test_resources_and_config_source_and_frozen(self):
        root = Path(paths.__file__).resolve().parent
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "frozen", False, create=True):
            self.assertEqual(paths.resource_dir(), root)
            self.assertEqual(paths.config_path(), root / "local.json")
            paths.prepare_config()
            self.assertNotIn("LDPC_CONFIG", os.environ)
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", str(root / "bundle"), create=True), \
                patch.object(sys, "executable", str(root / "release" / "ldd.exe")):
            self.assertEqual(paths.resource_dir(), root / "bundle")
            self.assertEqual(paths.config_path(), root / "release" / "local.json")
            paths.prepare_config()
            self.assertEqual(Path(os.environ["LDPC_CONFIG"]), root / "release" / "local.json")
        with patch.dict(os.environ, {"LDPC_CONFIG": "chosen.json"}):
            self.assertEqual(paths.config_path(), Path("chosen.json"))
            paths.prepare_config()
            self.assertEqual(os.environ["LDPC_CONFIG"], "chosen.json")

    def test_cli_forwards_arguments_and_exit_code_without_gui(self):
        with patch.object(sdgen, "main", return_value=7) as cli, patch.object(studio, "main") as gui:
            self.assertEqual(entry.main(["--cli", "devices", "--json"]), 7)
            cli.assert_called_once_with(["devices", "--json"])
            gui.assert_not_called()
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(Path.cwd() / "ldd.exe")), \
                patch.dict(os.environ, {}, clear=True), patch.object(sdgen, "main", return_value=3) as cli:
            self.assertEqual(entry.main(["devices", "--json"]), 3)
            cli.assert_called_once_with(["devices", "--json"])
            self.assertEqual(Path(os.environ["LDPC_CONFIG"]), Path.cwd() / "local.json")

    def test_visible_error_has_log_path_and_no_private_exception_text(self):
        scratch = Path(__file__).resolve().parents[1] / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp, \
                patch.object(entry, "output_dir", return_value=Path(tmp)), \
                patch.object(sdgen, "OUT", Path(tmp)), \
                patch.object(studio, "main", side_effect=studio.StartupError("Port 47865 is unavailable")), \
                patch.object(sys, "stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(entry.main([]), 1)
            self.assertIn("47865", stderr.getvalue())
            self.assertIn(str(Path(tmp) / "studio.log"), stderr.getvalue())
            self.assertTrue((Path(tmp) / "studio.log").is_file())
        with patch.object(studio, "main", side_effect=RuntimeError("private-value")), \
                patch.object(entry, "show_error") as show:
            self.assertEqual(entry.main([]), 1)
            self.assertNotIn("private-value", show.call_args.args[0])

    def test_headless_does_not_open_browser(self):
        with patch.object(studio, "main", return_value=0) as main:
            self.assertEqual(entry.main(["--no-browser"]), 0)
            main.assert_called_once_with(open_browser=False)

    def test_native_error_dialog_and_headless_suppression(self):
        scratch = Path(__file__).resolve().parents[1] / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp, \
                patch.object(sys, "platform", "win32"), patch.object(sys, "stderr", None), \
                patch.dict(os.environ, {"LDPC_NO_DIALOG": "0"}), \
                patch("ctypes.windll", create=True) as native:
            entry.show_error("Port unavailable", Path(tmp) / "studio.log")
            native.user32.MessageBoxW.assert_called_once()
            self.assertIn("Log file:", native.user32.MessageBoxW.call_args.args[1])
            native.reset_mock()
            with patch.dict(os.environ, {"LDPC_NO_DIALOG": "1"}):
                entry.show_error("Port unavailable", Path(tmp) / "studio.log")
            native.user32.MessageBoxW.assert_not_called()

    def test_output_directory_and_port_overrides(self):
        scratch = Path(__file__).resolve().parents[1] / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            config = Path(tmp) / "settings.json"
            config.write_text(json.dumps({"out": str(Path(tmp) / "images")}), encoding="utf-8")
            with patch.dict(os.environ, {"LDPC_CONFIG": str(config), "LDPC_PORT": "51234"}):
                self.assertEqual(paths.output_dir(), Path(tmp) / "images")
                self.assertEqual(paths.server_port(), 51234)
            with patch.dict(os.environ, {"LDPC_CONFIG": str(Path(tmp) / "missing.json"), "LDPC_PORT": "0"}):
                self.assertEqual(paths.output_dir(), Path.home() / "LocalDreamDesktop")
                with self.assertRaises(ValueError):
                    paths.server_port()
