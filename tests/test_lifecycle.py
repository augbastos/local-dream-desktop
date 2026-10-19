"""Real loopback lifecycle checks with no browser, adb, or phone interaction."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import paths
import sdgen
import studio


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        studio.st.update(busy=None, job=None, error=None, diagnosis=None, last=None,
                         phones={"phones": [], "usb_new": False, "usb_waiting": False})
        self.server = studio.Server((studio.HOST, 0))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(studio.HOST, self.server.server_port, timeout=5)
        try:
            conn.request(method, path, None if body is None else json.dumps(body), headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_status_identity_and_quit_capability(self):
        code, payload = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertEqual((payload["app"], payload["version"]), (paths.APP_ID, paths.__version__))
        self.assertTrue(payload["capabilities"]["quit"])

    def test_page_quit_control_accepts_real_status_response(self):
        code, payload = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for the server/page contract test")
        result = subprocess.run(
            [node, str(Path(__file__).with_name("check_page.js")), "--status-contract"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_quit_rejects_foreign_requests_and_malformed_body(self):
        for headers in ({"Host": "foreign.example"}, {"Origin": "https://foreign.example"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            self.assertEqual(self.request("POST", "/api/quit", {}, headers)[0], 403)
            self.assertFalse(self.server.quitting)
        for body in ([], {"after_job": "true"}, {"after_job": 1}):
            self.assertEqual(self.request("POST", "/api/quit", body)[0], 400)
            self.assertFalse(self.server.quitting)

    def test_idle_quit_exits_server(self):
        status, body = self.request("POST", "/api/quit", {}, {"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 202)
        self.assertEqual(body, {"quitting": True, "after_job": False})
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def test_running_job_requires_explicit_after_job_and_waits_for_save(self):
        release, saved = threading.Event(), threading.Event()
        scratch = Path(__file__).resolve().parents[1] / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            target = Path(tmp) / "saved.png"

            def generate():
                release.wait(5)
                target.write_bytes(b"saved image")
                saved.set()

            self.assertIsNotNone(studio.run("generating", generate, server=self.server))
            try:
                code, body = self.request("POST", "/api/quit", {})
                self.assertEqual((code, body["error"]), (409, "job_running"))
                self.assertFalse(self.server.quitting)
                code, body = self.request("POST", "/api/quit", {"after_job": True}, {"Sec-Fetch-Site": "none"})
                self.assertEqual((code, body["after_job"]), (202, True))
                self.assertTrue(self.thread.is_alive())
                self.assertFalse(target.exists())
                code, body = self.request("POST", "/api/connect", {})
                self.assertEqual((code, body["error"]), (409, "shutting_down"))
                self.assertIsNone(studio.run("connecting", lambda: None, server=self.server))
            finally:
                release.set()
                self.assertTrue(saved.wait(5))
                self.thread.join(5)
            self.assertFalse(self.thread.is_alive())
            self.assertEqual(target.read_bytes(), b"saved image")

    def test_failed_job_also_completes_scheduled_shutdown(self):
        release = threading.Event()

        def fail():
            release.wait(5)
            raise sdgen.PhoneError("timeout")

        with patch.object(studio, "log"):
            studio.run("generating", fail, server=self.server)
            try:
                self.assertEqual(self.request("POST", "/api/quit", {"after_job": True})[0], 202)
            finally:
                release.set()
                self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(studio.st["error"]["code"], "timeout")

    def test_after_job_also_waits_for_catalog_write_lock(self):
        studio.lock.acquire()
        try:
            code, body = self.request("POST", "/api/quit", {"after_job": True})
            self.assertEqual((code, body["after_job"]), (202, True))
            self.assertTrue(self.thread.is_alive())
        finally:
            studio.lock.release()
            self.thread.join(5)
        self.assertFalse(self.thread.is_alive())


class ReadinessTests(unittest.TestCase):
    def test_startup_confirms_identity_before_browser_and_exits_cleanly(self):
        seen = []
        real_status = studio.status_at

        def status(port, timeout):
            payload = real_status(port, timeout)
            seen.append(studio.is_our_status(payload))
            return payload

        def browser(url):
            self.assertEqual(url, "http://127.0.0.1:47865/")
            self.assertEqual(seen, [True])
            studio._active_server.begin_shutdown()

        # Server binding is assigned an ephemeral port; all readiness probes follow it.
        real_server = studio.Server

        def server(address):
            instance = real_server((address[0], 0))
            studio.PORT = instance.server_port
            return instance

        with patch.object(studio, "PORT", 0), patch.object(studio, "Server", side_effect=server), \
                patch.object(studio, "watch"), patch.object(studio, "log"), \
                patch.object(studio, "status_at", side_effect=status), \
                patch.object(studio.webbrowser, "open", side_effect=browser):
            self.assertEqual(studio.main(), 0)
        self.assertIsNone(studio._active_server)

    def test_readiness_timeout_never_opens_browser(self):
        real_server = studio.Server
        with patch.object(studio, "Server", side_effect=lambda _: real_server((studio.HOST, 0))), \
                patch.object(studio, "status_at", return_value={"app": "foreign"}), \
                patch.object(studio, "log"), patch.object(studio.webbrowser, "open") as browser:
            with self.assertRaisesRegex(studio.StartupError, "did not become ready"):
                studio.main(readiness_timeout=0)
            browser.assert_not_called()
        self.assertIsNone(studio._active_server)

    def test_foreign_service_is_not_reopened(self):
        class Foreign(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"app":"some-other-program","version":"0.1.0"}')

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer((studio.HOST, 0), Foreign) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(studio, "PORT", server.server_port), patch.object(studio.webbrowser, "open") as browser:
                    with self.assertRaisesRegex(studio.StartupError, "Another program"):
                        studio.main()
                    browser.assert_not_called()
            finally:
                server.shutdown()
                thread.join(2)
