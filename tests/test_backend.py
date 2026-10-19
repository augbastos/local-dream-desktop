import base64
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import sdgen
import studio
from tests.fakeld import FakeLD, model, result
from tests import fake_adb

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / ".test-tmp"
CHILD_TIMEOUT = 45  # child startup/file I/O allowance; adb's own timeout remains tested separately


class BackendCase(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.config = self.out / "test-config.json"
        self.state = self.out / "adb-state.json"
        self.set_adb()
        settings = {"OUT": self.out, "PHONES": self.out / "phones.json", "CFG": {},
                    "_serials": {}, "_hardware_serials": {}}
        self.patcher = patch.multiple(sdgen, **settings)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        refresh_clock = patch.object(studio, "_catalog_refresh_after", {})
        refresh_clock.start()
        self.addCleanup(refresh_clock.stop)
        # Test atomic replacement/cleanup on real files, without measuring physical
        # disk flush latency. Production still fsyncs every completed write.
        flush = patch.object(sdgen.os, "fsync", return_value=None)
        flush.start()
        self.addCleanup(flush.stop)
        env = patch.dict(os.environ, {"LDPC_ADB": str(ROOT / "tests" / "fake_adb.py"),
                                      "LDPC_FAKE_ADB_STATE": str(self.state), "LDPC_CONFIG": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        # Unit transport tests reuse the executable fixture without interpreter startup.
        # CLI/locking children still run as real subprocesses, and _adb_raw still handles
        # return codes and timeouts (the bounded-call test overrides this adapter).
        process_run, adb_mutex = subprocess.run, threading.Lock()

        def fixture_run(command, **kwargs):
            if len(command) > 1 and command[1] == str(ROOT / "tests" / "fake_adb.py"):
                with adb_mutex:
                    stdout, code = fake_adb.run(command[2:], self._adb_state)
                return subprocess.CompletedProcess(command, code, stdout + "\n", "")
            if Path(command[0]).stem.lower() == "adb":
                raise AssertionError("tests must never execute real adb")
            # Only actual child processes need serialized configuration/adb state.
            if not self.config.exists():
                self.config.write_text(json.dumps({"out": str(self.out)}), encoding="utf-8")
            self.state.write_text(json.dumps(self._adb_state), encoding="utf-8")
            return process_run(command, **kwargs)

        adapter = patch.object(subprocess, "run", side_effect=fixture_run)
        adapter.start()
        self.addCleanup(adapter.stop)
        studio.st.update(busy=None, job=None, error=None, diagnosis=None, last=None, last_poll=0,
                         phones={"phones": [], "usb_new": False, "usb_waiting": False})

    def set_adb(self, **values):
        data = {"devices": {}, "props": {}, "forwards": [], "calls": []}
        data.update(values)
        self._adb_state = json.loads(json.dumps(data))  # match the executable's isolated JSON state

    def adb_state(self):
        return json.loads(json.dumps(self._adb_state))

    def record(self, **values):
        p = {"id": "example", "name": "Example phone", "transport": "lan", "host": "127.0.0.1",
             "models": [model()], "use_img2img": True}
        p.update(values)
        sdgen.save_phones([p])
        return p

    def fake(self):
        fake = FakeLD()
        fake.__enter__()
        self.addCleanup(fake.__exit__)
        ports = patch.multiple(sdgen, CONTROL_PORT=fake.ports[0], GEN_PORT=fake.ports[1])
        ports.start()
        self.addCleanup(ports.stop)
        return fake

    def code(self, expected, fn, *args, **kwargs):
        with self.assertRaises(sdgen.PhoneError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, expected)

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            status = sdgen.main([*args, "--json"])
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, output.getvalue())
        return status, json.loads(lines[0])

    def wait_idle(self):
        end = time.monotonic() + 90  # bounded allowance for first-use output writes on Windows
        while studio.st["busy"] and time.monotonic() < end:
            time.sleep(0.01)
        stages = (studio.st["job"] or {}).get("stages", [])
        self.assertIsNone(studio.st["busy"], f"unfinished stages: {stages}")

    def assert_no_phone_bookkeeping(self, payload):
        if isinstance(payload, dict):
            for key, value in payload.items():
                self.assertNotIn(key, ("record_id", "catalog_revision", "serialno", "usb_ports",
                                       "usb_forward_serial", "control_port", "future_bookkeeping"))
                self.assert_no_phone_bookkeeping(value)
        elif isinstance(payload, list):
            for value in payload:
                self.assert_no_phone_bookkeeping(value)


class ConfigAndTransportTests(BackendCase):
    def test_public_phone_excludes_bookkeeping_without_mutating_registry_record(self):
        phone = self.record(serialno="example-hardware", usb_ports=[47810, 47811],
                            usb_forward_serial="example-usb", control_port=8808,
                            future_bookkeeping={"pending": True})
        original = phone.copy()
        public = sdgen.public_phone(phone)
        self.assert_no_phone_bookkeeping(public)
        self.assertEqual(public["id"], phone["id"])
        self.assertEqual(public["name"], phone["name"])
        self.assertEqual(public["models"], phone["models"])
        self.assertEqual(phone, original)
        self.assertEqual(sdgen.get_phone(), original)

    def test_cli_phone_outputs_exclude_registry_bookkeeping(self):
        self.fake()
        phone = self.record(serialno="example-hardware", usb_ports=[47810, 47811],
                            usb_forward_serial="example-usb", future_bookkeeping={"pending": True})
        commands = [("add-lan", "127.0.0.1"),
                    ("pair", "127.0.0.1:37000", "123456", "--connect", "127.0.0.1:37001"),
                    ("devices",), ("models",)]
        for command in commands:
            with self.subTest(command=command[0]):
                status, data = self.cli(*command)
                self.assertEqual(status, 0)
                self.assert_no_phone_bookkeeping(data)
                public = data["phone"] if "phone" in data else data["phones"][0]
                self.assertEqual(public["id"], phone["id"])
                self.assertEqual(public["name"], phone["name"])
        self.assertEqual(sdgen.get_phone()["record_id"], phone["record_id"])
        self.assertIn("catalog_revision", sdgen.get_phone())

    def test_missing_and_example_config(self):
        self.assertEqual(sdgen.load_config(self.out / "missing.json"), {})
        cfg = sdgen.load_config(ROOT / "local.example.json")
        self.assertEqual(cfg["usb_port_base"], 47810)
        self.assertEqual(sdgen.phones(), [])
        sdgen.stop()

    def test_child_config_override_and_default_output(self):
        script = 'import sdgen,json; print(json.dumps({"out":str(sdgen.OUT),"config":sdgen.CFG}))'
        for config, expected in (({"out": str(self.out)}, self.out), ({}, Path.home() / "LocalDreamDesktop")):
            self.config.write_text(json.dumps(config), encoding="utf-8")
            child = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True,
                                   text=True, timeout=CHILD_TIMEOUT, creationflags=sdgen.NOWIN)
            self.assertEqual(child.returncode, 0, child.stderr)
            loaded = json.loads(child.stdout)
            self.assertEqual(Path(loaded["out"]), expected)
            self.assertEqual(loaded["config"], config)

    def test_migration_preserves_every_record_and_image(self):
        old = [{"id": "a", "name": "Example A", "host": "192.0.2.1", "adb_port": 47860, "models": [model()]},
               {"id": "b", "name": "Example B", "host": "192.0.2.2", "adb_port": 47880},
               {"id": "c", "name": "Example C", "serialno": "example-c", "adb_port": 47900}]
        image = self.out / "existing.png"
        image.write_bytes(b"preserve")
        sdgen.PHONES.write_text(json.dumps({"phones": old, "extra": "keep"}), encoding="utf-8")
        migrated = sdgen.phones()
        self.assertEqual([p["id"] for p in migrated], ["a", "b", "c"])
        self.assertEqual([p["transport"] for p in migrated], ["lan", "lan", "usb"])
        self.assertTrue(all("adb_port" not in p for p in migrated))
        self.assertEqual(json.loads(sdgen.PHONES.read_text())["extra"], "keep")
        self.assertEqual(image.read_bytes(), b"preserve")
        self.assertEqual(sdgen.phones(), migrated)
        self.assertTrue(all(p["record_id"] and p["catalog_revision"] == 0 for p in migrated))
        self.assertEqual(list(self.out.glob(".ldpc-*")), [])

    def test_corrupt_registry_is_not_overwritten(self):
        sdgen.PHONES.write_text("broken", encoding="utf-8")
        self.code("bad_param", sdgen.phones)
        self.code("bad_param", sdgen.save_phones, [])
        self.assertEqual(sdgen.PHONES.read_text(), "broken")

    def test_lan_generation_only_checks_for_attached_adb(self):
        fake = self.fake()
        phone = self.record()
        self.assertEqual(sdgen.endpoints(phone), fake.bases)
        path, meta = sdgen.generate("A garden")
        sdgen.stop(phone)
        self.assertEqual(self.adb_state()["calls"], [["devices"]])
        self.assertTrue(path.is_file())
        self.assertEqual(meta["transport"], "lan")
        self.assertEqual(fake.posts("/stop"), [{"model_id": "sd15"}])

    def test_no_adb_disables_discovery_and_usb_but_allows_lan_generation(self):
        self.fake()
        phone = self.record()
        self.set_adb(devices={"example-usb": "device"},
                     props={"example-usb": {"ro.serialno": "example-hardware"}})
        with patch.dict(os.environ, {"LDPC_NO_ADB": "1"}), \
                patch.object(sdgen.shutil, "which", side_effect=AssertionError("adb discovery")) as discover, \
                patch.object(subprocess, "Popen", side_effect=AssertionError("adb subprocess")) as popen, \
                patch.object(subprocess, "run", side_effect=AssertionError("adb subprocess")) as run:
            self.code("no_adb", sdgen.adb_path)
            self.code("no_adb", sdgen._adb_raw, "devices")
            status = sdgen.phone_status()
            self.assertFalse(status["adb_available"])
            self.assertFalse(status["usb_new"])
            self.assertFalse(status["usb_waiting"])
            self.assertEqual(status["phones"][0]["state"], "ready")
            self.assertEqual(sdgen.connect_usb(), [])
            self.assertEqual(sdgen.connect_usb(include_ignored=True), [])
            studio.refresh_phones()
            path, meta = sdgen.generate("A garden")
            sdgen.stop(phone)
            self.assertTrue(path.is_file())
            self.assertEqual(meta["transport"], "lan")
            self.assertEqual([p["id"] for p in sdgen.phones()], [phone["id"]])
            discover.assert_not_called()
            run.assert_not_called()
            popen.assert_not_called()
        self.assertEqual(self.adb_state()["calls"], [])

    def test_lan_control_down_does_not_try_adb(self):
        phone = self.record()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with patch.object(sdgen, "CONTROL_PORT", port), patch.object(sdgen, "_adb_raw", side_effect=AssertionError("adb")):
            self.code("connection_refused", sdgen.ensure_host, phone)

    def test_usb_unplugged_and_unauthorized_never_use_host(self):
        phone = self.record(transport="usb", serialno="example-hardware", host="192.0.2.3")
        with patch.object(sdgen.Api, "info", side_effect=AssertionError("fell back to LAN")):
            self.code("usb_unplugged", sdgen.generate, "garden")
            self.set_adb(devices={"example-usb": "unauthorized"})
            self.code("usb_unauthorized", sdgen.endpoints, phone)

    def test_port_allocation_skips_foreign_forward_and_bound_pair(self):
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}},
                     forwards=[["other-usb", "tcp:47810", "tcp:8808"]])
        phone = self.record(transport="usb", serialno="example-hardware")
        with patch.object(sdgen, "_bindable", side_effect=lambda n: n not in (47812, 47813)):
            self.assertEqual(sdgen.endpoints(phone), ("http://127.0.0.1:47814", "http://127.0.0.1:47815"))
        self.assertEqual(sdgen.get_phone()["usb_ports"], [47814, 47815])
        first = self.adb_state()
        with patch.object(sdgen, "_bindable", return_value=False):
            sdgen.endpoints(phone)
        calls = self.adb_state()["calls"]
        forward_adds = [c for c in calls if "--no-rebind" in c]
        self.assertEqual(len(forward_adds), 2)
        self.assertEqual(first["forwards"], self.adb_state()["forwards"])
        self.assertFalse(any("tcpip" in c or "--remove-all" in c for c in calls))

    def test_remove_owned_forwards_only_and_keep_images(self):
        phone = self.record(transport="usb", serialno="example-hardware", usb_ports=[47810, 47811])
        self.set_adb(props={"example-usb": {"ro.serialno": "example-hardware"}}, forwards=[
            ["example-usb", "tcp:47810", "tcp:8808"], ["example-usb", "tcp:47811", "tcp:8081"],
            ["example-usb", "tcp:49000", "tcp:9000"], ["other-usb", "tcp:49001", "tcp:8081"]])
        image = self.out / "keep.png"
        image.write_bytes(b"keep")
        sdgen.remove_phone(phone["id"])
        self.assertEqual(self.adb_state()["forwards"], [["example-usb", "tcp:49000", "tcp:9000"], ["other-usb", "tcp:49001", "tcp:8081"]])
        self.assertTrue(image.exists())
        self.assertEqual(sdgen.phones(), [])

    def test_remove_unplugged_forward_ownership_survives_restart(self):
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}})
        phone = self.record(transport="usb", serialno="example-hardware")
        with patch.object(sdgen, "_bindable", return_value=True):
            sdgen.endpoints(phone)
        persisted = sdgen.get_phone()
        self.assertEqual(persisted["usb_forward_serial"], "example-usb")
        state = self.adb_state()
        state.update(devices={}, props={})
        self.set_adb(**state)
        sdgen._serials.clear()
        sdgen.remove_phone(phone["id"])
        self.assertEqual(self.adb_state()["forwards"], [])

    def test_foreign_persisted_ports_are_never_rebound(self):
        phone = self.record(transport="usb", serialno="example-hardware", usb_ports=[47810, 47811])
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}},
                     forwards=[["other-usb", "tcp:47810", "tcp:8808"]])
        self.code("busy", sdgen.endpoints, phone)
        self.assertFalse(any("--no-rebind" in c for c in self.adb_state()["calls"]))
        self.assertEqual(self.adb_state()["forwards"], [["other-usb", "tcp:47810", "tcp:8808"]])

    def test_existing_same_device_forward_is_not_adopted_or_removed(self):
        existing = [["example-usb", "tcp:47810", "tcp:8808"], ["example-usb", "tcp:47811", "tcp:8081"]]
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}},
                     forwards=existing)
        phone = self.record(transport="usb", serialno="example-hardware")
        with patch.object(sdgen, "_bindable", return_value=True):
            sdgen.endpoints(phone)
        self.assertEqual(sdgen.get_phone()["usb_ports"], [47812, 47813])
        sdgen.remove_phone(phone["id"])
        self.assertEqual(self.adb_state()["forwards"], existing)

    def test_two_usb_phones_allocate_distinct_pairs(self):
        a = self.record(transport="usb", serialno="example-a")
        b = {"id": "second", "name": "Example B", "transport": "usb", "serialno": "example-b"}
        sdgen.save_phones([a, b])
        self.set_adb(devices={"usb-a": "device", "usb-b": "device"}, props={
            "usb-a": {"ro.serialno": "example-a"}, "usb-b": {"ro.serialno": "example-b"}})
        with patch.object(sdgen, "_bindable", return_value=True):
            sdgen.endpoints(a)
            sdgen.endpoints(b)
        self.assertEqual(sdgen.get_phone("example")["usb_ports"], [47810, 47811])
        self.assertEqual(sdgen.get_phone("second")["usb_ports"], [47812, 47813])

    def test_add_lan_requires_local_dream_and_valid_host(self):
        fake = self.fake()
        self.code("bad_host", sdgen.add_lan, "http://192.0.2.1/")
        fake.app = "other"
        self.code("no_host_mode", sdgen.add_lan, "127.0.0.1")
        self.assertEqual(sdgen.phones(), [])
        fake.app = "localdream"
        status, payload = self.cli("add-lan", "127.0.0.1", "--name", "Example LAN")
        self.assertEqual(status, 0)
        self.assertEqual(payload["phone"]["name"], "Example LAN")
        sdgen.add_lan("127.0.0.1")
        self.assertEqual(len(sdgen.phones()), 1)

    def test_usb_full_generation_on_forwarded_loopback_endpoints(self):
        fake = self.fake()
        # The fake servers stand in for already-owned forwards; remote phone ports remain standard.
        phone = self.record(transport="usb", host="192.0.2.4", serialno="example-hardware", usb_ports=fake.ports)
        with patch.multiple(sdgen, CONTROL_PORT=8808, GEN_PORT=8081):
            self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}},
                         forwards=[["example-usb", f"tcp:{p}", f"tcp:{r}"] for p, r in zip(fake.ports, (8808, 8081))])
            path, meta = sdgen.generate("garden", phone_id=phone["id"])
        self.assertTrue(path.exists())
        self.assertEqual(meta["transport"], "usb")
        self.assertEqual(len(fake.posts("/generate")), 1)

    def test_adopt_by_hardware_serial_and_keep_selected_transport(self):
        self.record(serialno="example-hardware")
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {
            "ro.serialno": "example-hardware", "ro.product.marketname": "Example phone"}})
        self.assertEqual(sdgen.connect_usb(), ["example"])
        self.assertEqual(len(sdgen.phones()), 1)
        self.assertEqual(sdgen.get_phone()["transport"], "lan")
        with patch.object(sdgen, "_bindable", return_value=True):
            adopted = sdgen.add_usb("example-usb")
        self.assertEqual(adopted["transport"], "usb")

    def test_status_one_devices_call_and_new_waiting_flags(self):
        fake = self.fake()
        self.record()
        self.set_adb(devices={"new-example": "device", "waiting-example": "unauthorized"},
                     props={"new-example": {"ro.serialno": "new-hardware"}})
        status = sdgen.phone_status()
        self.assertEqual(status["phones"][0]["state"], "ready")
        self.assertTrue(status["usb_new"])
        self.assertTrue(status["usb_waiting"])
        self.assertEqual(self.adb_state()["calls"].count(["devices"]), 1)
        self.assertEqual(fake.requests[-1][1], "/info")

    def test_status_usb_unplugged_unauthorized_and_no_host(self):
        self.record(transport="usb", serialno="example-hardware")
        self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "usb_unplugged")
        self.set_adb(devices={"example-usb": "unauthorized"})
        self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "usb_unauthorized")
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}})
        with patch.object(sdgen.Api, "info", side_effect=sdgen.PhoneError("unreachable")):
            self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "unreachable")
        self.assertEqual(self.adb_state()["calls"].count(["devices"]), 1)

    def test_status_usb_is_read_only_and_caches_hardware_lookup(self):
        fake = self.fake()
        sdgen.desktop_link()  # first-use identity creation is separate from registry polling
        self.record(transport="usb", serialno="example-hardware", usb_ports=fake.ports)
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}})
        before, timestamp = sdgen.PHONES.read_bytes(), sdgen.PHONES.stat().st_mtime_ns
        with patch.object(sdgen, "_atomic_json", side_effect=AssertionError("poll wrote registry")):
            for _ in range(2):
                self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "ready")
        calls = self.adb_state()["calls"]
        self.assertEqual(calls.count(["devices"]), 2)
        self.assertEqual(calls.count(["-s", "example-usb", "shell", "getprop", "ro.serialno"]), 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sdgen.PHONES.read_bytes(), before)
        self.assertEqual(sdgen.PHONES.stat().st_mtime_ns, timestamp)

    def test_status_defers_usb_forward_allocation_and_repair_to_operations(self):
        phone = self.record(transport="usb", serialno="example-hardware")
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}})
        self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "unreachable")
        self.assertFalse(any("forward" in c for c in self.adb_state()["calls"]))
        self.assertNotIn("usb_ports", sdgen.get_phone())
        with patch.object(sdgen, "_bindable", return_value=True):
            sdgen.endpoints(phone)
            state = self.adb_state()
            self.assertEqual(len(state["forwards"]), 2)
            state["forwards"], state["calls"] = [], []
            self.set_adb(**state)
            with patch.object(sdgen.Api, "info", side_effect=sdgen.PhoneError("no_host_mode")):
                self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "no_host_mode")
            self.assertEqual(self.adb_state()["forwards"], [])
            self.assertEqual(self.adb_state()["calls"], [["devices"]])
            sdgen.endpoints(phone)
        self.assertEqual(len(self.adb_state()["forwards"]), 2)

    def test_status_does_not_persist_legacy_migration(self):
        old = {"phones": [{"id": "example", "name": "Example", "serialno": "example-hardware", "adb_port": 47860}]}
        sdgen.PHONES.write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "usb_unplugged")
        self.assertEqual(json.loads(sdgen.PHONES.read_text()), old)

    def test_pair_mdns_explicit_and_missing_connect_port(self):
        self.set_adb(mdns="example _adb-tls-connect._tcp. 192.0.2.1:37001")
        phone = sdgen.pair("192.0.2.1:37000", "123456")
        self.assertEqual(phone["transport"], "lan")
        self.assertIn(["connect", "192.0.2.1:37001"], self.adb_state()["calls"])
        self.assertFalse(any("tcpip" in c for c in self.adb_state()["calls"]))
        self.set_adb()
        self.code("pair_connect_port", sdgen.pair, "192.0.2.2:37000", "123456")
        self.assertEqual(len(self.adb_state()["calls"]), 2)
        self.set_adb()
        sdgen.pair("192.0.2.2:37000", "123456", "192.0.2.2:38000")
        self.assertFalse(any(c[:1] == ["mdns"] for c in self.adb_state()["calls"]))

    def test_pair_input_and_failure(self):
        for address, code in (("bad", "123456"), ("192.0.2.1:99999", "123456"), ("192.0.2.1:37000", "a")):
            self.code("pair_input", sdgen.pair, address, code)
        self.assertEqual(self.adb_state()["calls"], [])
        self.set_adb(pair_output="failed")
        self.code("pair_failed", sdgen.pair, "192.0.2.1:37000", "123456")

    def test_adb_discovery_and_bounded_calls(self):
        self.assertEqual(sdgen.adb_path(), str(ROOT / "tests" / "fake_adb.py"))
        with patch.dict(os.environ, {}, clear=True), patch.object(sdgen.shutil, "which", return_value="example-adb"):
            self.assertEqual(sdgen.adb_path(), "example-adb")
        with patch.object(sdgen.subprocess, "run", side_effect=subprocess.TimeoutExpired("adb", 15)) as run:
            self.code("timeout", sdgen._adb_raw, "devices")
            self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_invalid_adb_override_falls_back_or_reports_unavailable(self):
        for override in (self.out / "missing-adb.exe", self.out):
            with self.subTest(override=override), patch.dict(os.environ, {"LDPC_ADB": str(override)}):
                with patch.object(sdgen.shutil, "which", return_value="fallback-adb"):
                    self.assertEqual(sdgen.adb_path(), "fallback-adb")
                with patch.object(sdgen.shutil, "which", return_value=None), \
                        patch.object(sdgen.Path, "home", return_value=self.out), \
                        patch.dict(os.environ, {"ANDROID_HOME": str(self.out), "ANDROID_SDK_ROOT": str(self.out),
                                                "LOCALAPPDATA": str(self.out)}):
                    self.code("no_adb", sdgen.adb_path)
                    self.assertFalse(sdgen.phone_status()["adb_available"])
                    sdk_adb = self.out / "platform-tools" / ("adb.exe" if sys.platform == "win32" else "adb")
                    sdk_adb.parent.mkdir(exist_ok=True)
                    sdk_adb.touch()
                    self.assertEqual(sdgen.adb_path(), str(sdk_adb))
                    sdk_adb.unlink()

    def test_existing_python_adb_override_remains_usable(self):
        self.assertEqual(sdgen._adb_raw("devices").strip(), "List of devices attached")
        self.assertTrue(sdgen.phone_status()["adb_available"])


class ModelAndGenerationTests(BackendCase):
    def test_catalog_resolutions_drop_non_integer_pairs_and_unsafe_bounds(self):
        invalid = [None, "512x512", {}, [512], [512, 512, 512], [True, 512],
                   [512.0, 512], ["<img src=x onerror=alert(1)>", 512], [0, 512],
                   [127, 512], [512, 2049]]
        c = sdgen.capabilities(model(resolutions=invalid + [[128, 128], [512, 768], [2048, 2048]]), True)
        self.assertEqual(c["sizes"]["list"], [[128, 128], [512, 768], [2048, 2048]])
        for resolutions in (invalid, None, "not a list", {"size": [512, 512]}):
            with self.subTest(resolutions=resolutions):
                c = sdgen.capabilities(model(resolutions=resolutions), True)
                self.assertEqual(c["sizes"]["list"], [[512, 512]])

    def test_progress_ignores_malformed_counts_without_losing_completion(self):
        counts = [("<img src=x onerror=alert(1)>", 4), (1, "4"), (True, 4),
                  (1.0, 4), (1, None), (-1, 4), (1, 0), (5, 4), (1, 4)]
        stream = b"".join(b"event: progress\ndata: " + json.dumps(
            {"step": step, "total_steps": total}).encode() + b"\n\n" for step, total in counts)
        stream += b'event: progress\ndata: {}\n\n'
        stream += b'event: complete\ndata: {"type":"complete"}\n\n'
        client, seen = sdgen.Api("http://127.0.0.1", "http://127.0.0.1", trusted=False), []
        with patch.object(client, "_open", return_value=io.BytesIO(stream)):
            completed = client.generate({}, lambda step, total: seen.append((step, total)))
        self.assertEqual(seen, [(1, 4)])
        self.assertEqual(completed, {"type": "complete"})

    def test_reference_capability_is_limited_to_enabled_native_edit_models(self):
        for m in [model(f) for f in ("sd15", "sd15cpu", "sdxl", "anima")] + [
                model("dit", dit_kind=kind) for kind in ("zimage", "klein", "qwen21", "future")]:
            for enabled in (False, True):
                with self.subTest(kind=m["dit_kind"], family=m["id"], enabled=enabled):
                    caps = sdgen.capabilities(m, enabled)
                    if enabled and m["dit_kind"] in ("klein", "qwen21"):
                        self.assertTrue(caps["reference_edit"])
                        self.assertEqual(caps["max_references"], 3)
                    else:
                        self.assertNotIn("reference_edit", caps)
                        self.assertNotIn("max_references", caps)

    def test_reference_encoding_order_prompt_and_native_defaults(self):
        images = [result(fmt, width=2 + i, height=3 + i)["image"] for i, fmt in enumerate(("png", "jpeg", "webp"))]
        normalized = sdgen.reference_data(images)
        for kind, steps in (("klein", 4), ("qwen21", 20)):
            m = model("dit", dit_kind=kind, defaults={"steps": steps, "cfg": 1, "scheduler": "euler"})
            phone = self.record(models=[m])
            for count in (1, sdgen.MAX_REFERENCES):
                refs = [("data:image/" + fmt + ";base64," if i != 1 else "") + data
                        for i, (fmt, data) in enumerate(zip(("png", "jpeg", "webp"), images[:count]))]
                prompt = "Change image 1 using the colors of image 2"
                _, sent = sdgen.prepare(phone, prompt, reference_images=refs)
                self.assertEqual(sent["reference_images"], normalized[:count])
                for i, encoded in enumerate(sent["reference_images"]):
                    with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as decoded:
                        self.assertEqual((decoded.format, decoded.mode, decoded.size), ("JPEG", "RGB", (2 + i, 3 + i)))
                self.assertEqual(sent["prompt"], prompt)
                self.assertEqual((sent["steps"], sent["cfg"]), (steps, 1))
                self.assertTrue({"image", "mask", "strength", "denoise_strength"}.isdisjoint(sent))
                _, plain = sdgen.prepare(phone, prompt)
                self.assertEqual(plain, {k: v for k, v in sent.items() if k != "reference_images"})

    def test_webp_reference_becomes_rgb_jpeg_within_one_megapixel(self):
        buffer = io.BytesIO()
        with Image.new("RGBA", (1800, 1200), (20, 60, 200, 128)) as source:
            source.save(buffer, format="WEBP")
        encoded = sdgen.reference_data(["data:image/webp;base64," + base64.b64encode(buffer.getvalue()).decode()])[0]
        self.assertFalse(encoded.startswith("data:"))
        with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as decoded:
            self.assertEqual((decoded.format, decoded.mode), ("JPEG", "RGB"))
            self.assertLessEqual(decoded.width * decoded.height, 1_048_576)
            self.assertGreater(decoded.width * decoded.height, 1_040_000)
            self.assertAlmostEqual(decoded.width / decoded.height, 1800 / 1200, delta=.005)
            # Standard JPEG luminance table at quality 95 starts with 2.
            self.assertEqual(decoded.quantization[0][0], 2)

    def test_twelve_megapixel_jpeg_reference_is_downscaled_without_cropping(self):
        buffer = io.BytesIO()
        with Image.new("RGB", (4000, 3000), (20, 60, 200)) as source:
            source.save(buffer, format="JPEG", quality=95)
        encoded = sdgen.reference_data([base64.b64encode(buffer.getvalue()).decode()])[0]
        with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as decoded:
            self.assertEqual((decoded.format, decoded.mode), ("JPEG", "RGB"))
            self.assertLessEqual(decoded.width * decoded.height, 1_048_576)
            self.assertGreater(decoded.width * decoded.height, 1_040_000)
            self.assertAlmostEqual(decoded.width / decoded.height, 4000 / 3000, delta=.005)

    def test_mpo_reference_accepts_the_primary_jpeg_picture(self):
        buffer = io.BytesIO()
        with Image.new("RGB", (64, 32), "red") as primary, Image.new("RGB", (64, 32), "green") as secondary:
            primary.save(buffer, format="MPO", save_all=True, append_images=[secondary])
        with Image.open(io.BytesIO(buffer.getvalue())) as source:
            self.assertEqual(source.format, "MPO")
            self.assertEqual(source.n_frames, 2)
        encoded = sdgen.reference_data(["data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()])[0]
        with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as decoded:
            self.assertEqual((decoded.format, decoded.mode, decoded.size), ("JPEG", "RGB", (64, 32)))
            red, green, blue = decoded.getpixel((32, 16))
            self.assertGreater(red, 240)
            self.assertLess(max(green, blue), 10)

    def test_reference_applies_exif_orientation_without_upscaling_or_metadata(self):
        buffer = io.BytesIO()
        with Image.new("RGB", (80, 40), "red") as source:
            source.paste("blue", (40, 0, 80, 40))
            exif = Image.Exif()
            exif[274], exif[315] = 6, "synthetic fixture"
            source.save(buffer, format="JPEG", exif=exif)
        encoded = sdgen.reference_data([base64.b64encode(buffer.getvalue()).decode()])[0]
        with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as decoded:
            self.assertEqual(decoded.size, (40, 80))
            self.assertGreater(decoded.getpixel((20, 10))[0], 240)
            self.assertGreater(decoded.getpixel((20, 70))[2], 240)
            self.assertFalse(decoded.getexif())

    def test_reference_type_count_size_and_mode_validation_before_phone_io(self):
        phone = self.record(models=[model("dit", dit_kind="klein")])
        image = result("png")["image"]
        for refs, error in (([], "bad_param"), ({}, "bad_param"), (image, "bad_param"),
                            ([image] * 4, "bad_param"), ([None], "bad_image"), ([3], "bad_image"),
                            ([""], "bad_image"), (["invalid!"], "bad_image"), (["é"], "bad_image"),
                            (["data:text/plain;base64," + image], "bad_image"),
                            ([base64.b64encode(b"not an image").decode()], "bad_image"),
                            ([result("gif")["image"]], "bad_image")):
            with self.subTest(refs_type=type(refs).__name__, error=error), patch.object(
                    sdgen, "api", side_effect=AssertionError("phone contacted")):
                self.code(error, sdgen.generate, "edit", reference_images=refs)
        for extra in ({"strength": .5}, {"image_b64": image}, {"mask_b64": image}):
            self.code("bad_param", sdgen.prepare, phone, "edit", reference_images=[image], **extra)
        with patch.object(sdgen, "MAX_REFERENCE_BYTES", len(base64.b64decode(image)) - 1):
            self.code("too_large", sdgen.prepare, phone, "edit", reference_images=[image])
        with patch.object(sdgen, "MAX_REFERENCE_PIXELS", 5):
            self.code("too_large", sdgen.prepare, phone, "edit", reference_images=[image])
        self.assertEqual(self.adb_state()["calls"], [])

    def test_reference_gating_before_any_phone_request(self):
        image = result("png")["image"]
        models = [model(f) for f in ("sd15", "sd15cpu", "sdxl", "anima")] + [
            model("dit", dit_kind=kind) for kind in ("zimage", "future")]
        for m, enabled in [(m, True) for m in models] + [
                (model("dit", dit_kind=kind), False) for kind in ("klein", "qwen21")]:
            self.record(models=[m], use_img2img=enabled)
            with patch.object(sdgen, "api", side_effect=AssertionError("phone contacted")):
                self.code("bad_param", sdgen.generate, "edit", reference_images=[image])
        self.record(models=[])
        with patch.object(sdgen, "api", side_effect=AssertionError("phone contacted")):
            self.code("no_models", sdgen.generate, "edit", reference_images=[image])

    def test_reference_ram_is_per_image_and_metadata_excludes_bytes(self):
        fake = self.fake()
        fake.models = [model("dit", dit_kind="qwen21")]
        self.record(models=fake.models)
        images = [result(fmt)["image"] for fmt in ("png", "jpeg", "webp")]
        warnings = []
        with patch.object(sdgen, "phone_memory", return_value=7000):
            path, meta = sdgen.generate("edit image 1", reference_images=images, warning=warnings.append)
        normalized = sdgen.reference_data(images)
        self.assertEqual(fake.posts("/generate")[0]["reference_images"], normalized)
        self.assertTrue(any("8.7 GB" in warning for warning in warnings))
        self.assertEqual(meta["reference_count"], 3)
        self.assertTrue(meta["reference_edit"])
        self.assertFalse(meta["img2img"] or meta["inpaint"])
        self.assertNotIn("reference_images", meta)
        saved = path.with_suffix(".json").read_text(encoding="utf-8")
        for image in [*images, *normalized]:
            self.assertNotIn(image, saved)

    def test_capabilities_all_families_and_defaults(self):
        for family, kind, size in (("sd15", "list", 512), ("sd15cpu", "square", 256),
                                   ("dit", "dit", 1024), ("sdxl", "fixed", 1024), ("anima", "fixed", 1024)):
            with self.subTest(family=family):
                m = model(family)
                c = sdgen.capabilities(m, True)
                self.assertEqual(c["family"], family)
                self.assertEqual(c["sizes"]["kind"], kind)
                self.assertEqual(c["defaults"]["width"], size)
                self.assertEqual(c["defaults"]["steps"], 4)
                self.assertEqual(c["defaults"]["negative_prompt"], "model negative")
                self.assertEqual(c["defaults"]["scheduler"], "euler")
                self.assertTrue(c["inpaint"])
                self.assertFalse(sdgen.capabilities(m, False)["inpaint"])
                p = {"models": [m], "use_img2img": True}
                _, body = sdgen.prepare(p, "garden")
                self.assertEqual(body["width"], size)
                self.assertEqual(body["scheduler"], "euler")
        old = model()
        del old["dit_kind"]
        self.assertEqual(sdgen.capabilities(old, False)["family"], "sd15")
        self.assertEqual(sdgen.capabilities(model("sdxl"), False)["sizes"]["aspects"], ["1:1"])
        self.assertEqual(sdgen.capabilities(model("dit"), True)["sizes"]["presets"], sdgen.DIT_SIZES)

    def test_valid_and_invalid_sizes_by_family(self):
        cases = {"sd15": ([(512, 512), (512, 768)], [(768, 512), (256, 256)]),
                 "sd15cpu": ([(128, 128), (512, 512)], [(128, 192), (576, 576), (129, 129)]),
                 "dit": ([(512, 2048), (896, 1152)], [(448, 512), (513, 512), (2112, 1024)]),
                 "sdxl": ([(1024, 1024)], [(768, 1024)]), "anima": ([(1024, 1024)], [(512, 512)])}
        for family, (good, bad) in cases.items():
            for w, h in good:
                sdgen.validate(model(family), w, h, "1:1", 4, 5, 0.6, 0)
            for w, h in bad:
                self.code("bad_size", sdgen.validate, model(family), w, h, "1:1", 4, 5, 0.6, 0)
        sdgen.validate(model("sdxl"), 1024, 1024, "3:4", 4, 5, 0.6, None)
        self.code("bad_size", sdgen.validate, model(), 512, 512, "3:4", 4, 5, 0.6, None)

    def test_numeric_ranges_types_nan_and_infinity(self):
        p = self.record()
        for fields in ({"steps": 0}, {"steps": 51}, {"steps": 4.5}, {"steps": True}, {"steps": "4"},
                       {"cfg": float("nan")}, {"cfg": float("inf")}, {"cfg": 0}, {"cfg": 31},
                       {"strength": -0.1}, {"strength": 1.1}, {"seed": -1}, {"seed": 4294967296}, {"seed": True},
                       {"scheduler": ""}, {"scheduler": 7}, {"neg": 3}):
            with self.subTest(fields=fields):
                self.code("bad_param", sdgen.prepare, p, "garden", **fields)
        for fields in ({"width": "512"}, {"height": True}, {"width": 512.0}):
            self.code("bad_size", sdgen.prepare, p, "garden", **fields)

    def test_img2img_mask_aspect_and_reference_validation(self):
        p = self.record(models=[model("sdxl")], use_img2img=False)
        self.code("img2img_disabled", sdgen.prepare, p, "garden", aspect_ratio="3:4")
        self.code("img2img_disabled", sdgen.prepare, p, "garden", image_b64="anything")
        p["use_img2img"] = True
        self.code("bad_param", sdgen.prepare, p, "garden", mask_b64="anything")
        self.code("bad_image", sdgen.prepare, p, "garden", image_b64="invalid")
        image = result("png")["image"]
        _, body = sdgen.prepare(p, "garden", image_b64="data:image/png;base64," + image, mask_b64=image, strength=0)
        self.assertEqual(body["image"], image)
        self.assertEqual(body["mask"], image)
        self.assertEqual(body["denoise_strength"], 0)

    def test_bad_input_is_rejected_before_http_or_adb(self):
        self.record()
        with patch.object(sdgen, "api", side_effect=AssertionError("request before validation")):
            self.code("bad_size", sdgen.generate, "garden", width=768, height=512)
            self.code("model_missing", sdgen.generate, "garden", model_id="missing")
            self.code("empty_prompt", sdgen.generate, "  ")

    def test_inpaint_known_families_and_unknown_dit_rejected(self):
        image = result("png")["image"]
        for m in [model(f) for f in ("sd15", "sd15cpu", "sdxl", "anima")] + [
                model("dit", dit_kind=kind) for kind in ("zimage", "klein", "qwen21")]:
            with self.subTest(model=m["id"], kind=m["dit_kind"]):
                phone = {"models": [m], "use_img2img": True}
                _, body = sdgen.prepare(phone, "garden", image_b64=image, mask_b64=image, strength=.35)
                self.assertEqual(body["mask"], image)
                self.assertEqual(body["denoise_strength"], .35)
                phone["use_img2img"] = False
                self.code("img2img_disabled", sdgen.prepare, phone, "garden", image_b64=image, mask_b64=image)
        unknown = model("dit", dit_kind="future-architecture")
        self.assertFalse(sdgen.capabilities(unknown, True)["inpaint"])
        self.record(models=[unknown])
        with patch.object(sdgen, "api", side_effect=AssertionError("request before validation")):
            self.code("bad_param", sdgen.generate, "garden", image_b64=image, mask_b64=image)
        # Existing unmasked img2img remains available, including future DiT kinds.
        _, body = sdgen.prepare(sdgen.get_phone(), "garden", image_b64=image)
        self.assertNotIn("mask", body)

    def test_request_defaults_overrides_and_metadata(self):
        fake = self.fake()
        self.record()
        stages, progress = [], []
        path, meta = sdgen.generate("A quiet garden", stage=stages.append, progress=lambda s, t: progress.append((s, t)))
        sent = fake.posts("/generate")[0]
        self.assertEqual(sent["negative_prompt"], "model negative")
        self.assertEqual(sent["scheduler"], "euler")
        self.assertEqual(sent["steps"], 4)
        self.assertEqual(sent["cfg"], 5)
        self.assertEqual(sent["output_format"], "raw")
        self.assertFalse(sent["use_opencl"])
        self.assertEqual(progress, [(1, 4)])
        self.assertEqual(stages, ["connect", "load", "generate", "save"])
        self.assertEqual(meta["width"], 2)
        self.assertEqual(meta["height"], 3)
        self.assertEqual(meta["app_version"], "3.example")
        self.assertEqual(meta["prompt_token_cap"], 0)
        self.assertEqual(meta["prompt_tokens"], 3)
        self.assertEqual(json.loads(path.with_suffix(".json").read_text()), meta)
        with Image.open(path) as image:
            self.assertEqual(image.size, (2, 3))
        sdgen.generate("garden", neg="", steps=1, cfg=1, scheduler="dpm", seed=0)
        override = fake.posts("/generate")[-1]
        self.assertEqual((override["negative_prompt"], override["scheduler"], override["seed"]), ("", "dpm", 0))

    def test_img2img_and_inpaint_body_meta_exclude_payloads(self):
        fake = self.fake()
        fake.models = [model("sdxl")]
        self.record(models=fake.models)
        image = result("png")["image"]
        _, meta = sdgen.generate("garden", image_b64=image, mask_b64=image, strength=0,
                                  aspect_ratio="3:4", reference="example-folder\\reference.png")
        body = fake.posts("/generate")[0]
        self.assertEqual((body["width"], body["height"]), (1024, 1024))
        self.assertEqual(body["aspect_ratio"], "3:4")
        self.assertEqual(body["denoise_strength"], 0)
        self.assertEqual(body["mask"], image)
        self.assertNotIn("image", meta)
        self.assertNotIn("mask", meta)
        self.assertEqual(meta["reference"], "reference.png")
        self.assertTrue(meta["img2img"] and meta["inpaint"])
        self.assertEqual(meta["family"], "sdxl")

    def test_raw_rgb_rgba_jpeg_png_and_bad_length(self):
        for fmt, channels in (("raw", 3), ("raw", 4), ("jpeg", 3), ("png", 4)):
            with self.subTest(fmt=fmt, channels=channels):
                image = sdgen.decode_image(result(fmt, channels))
                self.assertEqual(image.size, (2, 3))
                self.assertEqual(image.mode, "RGB" if channels == 3 else "RGBA")
        for d in (result(image="YQ=="), result(channels=2), result(width=0), result(image="invalid"), result(seed="bad"),
                  result(fmt="gif"), result("png", width=2, height=3) | {"width": 3}):
            self.code("bad_image", sdgen.decode_image, d)

    def test_unique_atomic_outputs_and_cleanup_on_save_failure(self):
        self.fake()
        self.record()
        fixed = datetime(2026, 1, 2, 3, 4, 5)
        with patch.object(sdgen, "datetime") as clock:
            clock.now.return_value = fixed
            paths = [sdgen.generate("garden")[0] for _ in range(3)]
        self.assertEqual([p.name for p in paths], ["030405_7.png", "030405_7_2.png", "030405_7_3.png"])
        self.assertTrue(all(p.with_suffix(".json").exists() for p in paths))
        self.assertEqual(list(self.out.rglob("*.tmp")), [])
        self.assertEqual(list(self.out.rglob("*.reserve")), [])
        with patch.object(sdgen.os, "replace", side_effect=OSError("test disk error")):
            self.code("save_failed", sdgen._save_image, Image.new("RGB", (2, 3)), {"seed": 8})
        self.assertEqual(list(self.out.rglob("*.tmp")), [])
        self.assertEqual(list(self.out.rglob("*.reserve")), [])
        self.assertTrue(all(p.exists() for p in paths))

    def test_select_waits_for_health_and_skips_matching_worker(self):
        fake = self.fake()
        phone = self.record()
        fake.health_pending = 2
        with patch.object(sdgen.time, "sleep", return_value=None):
            sdgen.select(phone, "sd15", 512, 512)
        self.assertGreaterEqual(sum(route == "/health" for _, route, _ in fake.requests), 3)
        sdgen.select(phone, "sd15", 512, 512)
        self.assertEqual(len(fake.posts("/select")), 1)
        fake.select_error = "example load failure"
        with self.assertRaises(sdgen.PhoneError) as caught:
            sdgen.select(phone, "sd15", 512, 768)
        self.assertEqual(caught.exception.code, "load_failed")
        self.assertEqual(caught.exception.detail, "example load failure")

    def test_select_404_is_still_model_missing(self):
        fake = self.fake()
        phone = self.record()
        fake.select_http_error = 404
        self.code("model_missing", sdgen.select, phone, "sd15", 512, 512)

    def test_tokenizer_failure_does_not_abort_generation(self):
        fake = self.fake()
        self.record()
        for error in (500, "invalid", "truncated"):
            with self.subTest(error=error):
                fake.tokenize_error = error
                before = len(fake.posts("/generate"))
                path, meta = sdgen.generate("garden")
                self.assertTrue(path.exists())
                self.assertIsNone(meta["prompt_tokens"])
                self.assertIsNone(meta["prompt_token_cap"])
                self.assertEqual(json.loads(path.with_suffix(".json").read_text()), meta)
                self.assertEqual(len(fake.posts("/generate")), before + 1)

    def test_select_timeout(self):
        fake = self.fake()
        phone = self.record()
        fake.health_pending = 100
        with patch.object(sdgen, "LOAD_TIMEOUT", 0.02), patch.object(sdgen.time, "sleep", return_value=None):
            self.code("timeout", sdgen.select, phone, "sd15", 512, 512)

    def test_stream_errors_premature_end_timeout_and_no_retry(self):
        fake = self.fake()
        self.record()
        for mode, expected in (("error", "stream_broken"), ("premature", "stream_broken"),
                               ("invalid", "stream_broken"), ("timeout", "timeout")):
            fake.mode = mode
            before = len(fake.posts("/generate"))
            with patch.object(sdgen, "STREAM_TIMEOUT", 0.05):
                self.code(expected, sdgen.generate, "garden")
            self.assertEqual(len(fake.posts("/generate")), before + 1)
        self.assertEqual(list(self.out.glob("*/*.png")), [])

    def test_broken_stream_preserves_transport_or_worker_evidence(self):
        fake = self.fake()
        self.record()
        fake.mode = "premature"
        original_info = sdgen.Api.info
        def info(client, *args, **kwargs):
            if fake.posts("/generate"):
                raise sdgen.PhoneError("connection_refused")
            return original_info(client, *args, **kwargs)
        with patch.object(sdgen.Api, "info", info):
            self.code("connection_refused", sdgen.generate, "garden")
        fake.requests.clear()
        original_status = sdgen.Api.status
        with patch.object(sdgen.Api, "status", lambda client: {"state": "idle"} if fake.posts("/generate") else original_status(client)):
            self.code("worker_stopped", sdgen.generate, "garden")
        self.assertEqual(len(fake.posts("/generate")), 1)

    def test_http_generate_error_and_truncated_image(self):
        fake = self.fake()
        self.record()
        fake.http_error = 400
        self.code("bad_param", sdgen.generate, "garden")
        self.assertEqual(len(fake.posts("/generate")), 1)
        fake.http_error = None
        fake.output["image"] = "YQ=="
        self.code("bad_image", sdgen.generate, "garden")
        self.assertEqual(len(fake.posts("/generate")), 2)

    def test_os_lock_blocks_second_process_generate_and_releases(self):
        fake = self.fake()
        phone = self.record()
        script = 'import sdgen,sys; sys.exit(sdgen.main(["gen","garden","--json"]))'
        with sdgen.phone_lock(phone):
            self.code("busy", sdgen.generate, "garden")
            child = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                   capture_output=True, text=True, timeout=CHILD_TIMEOUT, creationflags=sdgen.NOWIN)
            self.assertEqual(child.returncode, 1, child.stderr)
            self.assertEqual(json.loads(child.stdout)["error"], "busy")
            self.assertEqual(fake.posts("/generate"), [])
        self.assertTrue(sdgen.generate("garden")[0].exists())

    def test_stop_never_sends_idless(self):
        fake = self.fake()
        phone = self.record()
        sdgen.stop(phone)
        self.assertEqual(fake.posts("/stop"), [])
        sdgen.generate("garden")
        sdgen.stop(phone)
        self.assertEqual(fake.posts("/stop"), [{"model_id": "sd15"}])
        self.code("bad_param", sdgen.Api(*fake.bases).stop, None)

    def test_stop_reports_unavailable_host(self):
        phone = self.record()
        with patch.object(sdgen, "api", side_effect=sdgen.PhoneError("no_host_mode")):
            self.code("no_host_mode", sdgen.stop, phone)

    def test_cli_commands_json_and_errors(self):
        fake = self.fake()
        self.record()
        status, data = self.cli("gen", "garden", "--phone", "example", "--size", "512x512")
        self.assertEqual(status, 0)
        self.assertTrue(Path(data["path"]).is_absolute())
        self.assertEqual(data["meta"]["scheduler"], "euler")
        status, data = self.cli("models", "--phone", "example")
        self.assertEqual(data["phones"][0]["models"][0]["family"], "sd15")
        self.assertEqual(status, 0)
        for args in (("gen", "garden", "--steps", "nan"), ("gen", "garden", "--size", "bad"),
                     ("host-mode",), ("pair", "bad", "123456"), ("unknown",)):
            status, data = self.cli(*args)
            self.assertEqual(status, 1)
            self.assertFalse(data["ok"])
            self.assertIn("detail", data)
        for args in (("devices",), ("stop", "--phone", "example")):
            status, data = self.cli(*args)
            self.assertEqual(status, 0)
            self.assertTrue(data["ok"])
        with patch.object(sdgen, "open_host_mode") as host:
            self.assertEqual(self.cli("host-mode", "--phone", "example")[0], 0)
            host.assert_called_once()
        self.assertTrue(fake.requests)

    def test_cli_subprocess_one_json_and_exit_status(self):
        fake = self.fake()
        self.record()
        script = ('import sdgen,sys; sdgen.CONTROL_PORT=int(sys.argv[1]); '
                  'sdgen.GEN_PORT=int(sys.argv[2]); sdgen._devices=lambda: {}; '
                  'sdgen.os.fsync=lambda fd: None; '
                  'sys.exit(sdgen.main(sys.argv[3:]))')
        child = subprocess.run([sys.executable, "-c", script, *map(str, fake.ports),
                                "gen", "garden", "--json"], cwd=ROOT, capture_output=True, text=True,
                               timeout=CHILD_TIMEOUT, creationflags=sdgen.NOWIN)  # no attached adb in this LAN fixture
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(len(child.stdout.splitlines()), 1)
        self.assertTrue(json.loads(child.stdout)["ok"])


class StudioTests(BackendCase):
    def setUp(self):
        super().setUp()
        self.server = studio.Server(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_address[1]

    def close_server(self):
        self.wait_idle()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            h = {"Host": f"127.0.0.1:{self.port}"}
            h.update(headers or {})
            conn.request(method, path, json.dumps(body) if body is not None else None, h)
            response = conn.getresponse()
            data = response.read()
            return response.status, json.loads(data) if response.getheader("Content-Type") == "application/json" else data
        finally:
            conn.close()

    def test_host_origin_allowlist_and_exclusive_bind(self):
        for headers in ({"Host": "foreign.example"}, {"Origin": "https://foreign.example"},
                        {"Origin": f"http://127.0.0.1:{self.port}/"}, {"Origin": "null"}, {"Origin": "http://["}):
            status, data = self.request("POST", "/api/stop", {}, headers)
            self.assertEqual(status, 403)
            self.assertEqual(data["error"], "bad_host")
        status, _ = self.request("GET", "/api/status", headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 200)
        with self.assertRaises(OSError):
            studio.Server(("127.0.0.1", self.port))
        with self.assertRaises(ValueError):
            studio.Server(("0.0.0.0", 0))

    def test_fetch_metadata_blocks_all_api_routes_without_origin(self):
        routes = ["/api/status", "/api/models", "/api/gallery", "/api/desktop-link",
                  "/api/memory?phone_id=example&model_id=sd15", "/api/discover",
                  "/api/generate", "/api/stop", "/api/connect", "/api/pair",
                  "/api/tokenize", "/api/host_mode", "/api/delete", "/api/phones/add_lan",
                  "/api/phones/transport", "/api/phones/remove"]
        with patch.object(sdgen, "discover") as discover, patch.object(sdgen, "memory_status") as memory, \
                patch.object(sdgen, "pairing_payload") as pairing, patch.object(studio, "run") as run:
            for method in ("GET", "POST"):
                for route in routes:
                    for site in ("cross-site", "same-site", "unknown", ""):
                        with self.subTest(method=method, route=route, site=site):
                            status, data = self.request(method, route, headers={"Sec-Fetch-Site": site})
                            self.assertEqual((status, data["error"]), (403, "bad_host"))
            for fn in (discover, memory, pairing, run):
                fn.assert_not_called()
        for site in ("same-origin", "none"):
            status, _ = self.request("GET", "/api/status", headers={"Sec-Fetch-Site": site})
            self.assertEqual(status, 200)
            status, _ = self.request("POST", "/api/connect", {}, {"Sec-Fetch-Site": site})
            self.assertEqual(status, 202)
            self.wait_idle()
        with patch.object(sdgen, "discover") as discover:
            status, _ = self.request("GET", "/api/discover")
            self.assertEqual(status, 404)
            discover.assert_not_called()

    def test_security_headers_on_page_api_and_errors(self):
        for route, extra in (("/", {}), ("/api/status", {}),
                             ("/api/status", {"Sec-Fetch-Site": "cross-site"})):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("GET", route, headers=extra)
                response = conn.getresponse()
                response.read()
                csp = response.getheader("Content-Security-Policy")
                for directive in ("default-src 'self'", "img-src 'self' data: blob:",
                                  "script-src 'self' 'unsafe-inline'", "frame-ancestors 'none'",
                                  "object-src 'none'", "base-uri 'none'"):
                    self.assertIn(directive, csp)
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
            finally:
                conn.close()

    def test_malformed_catalog_returns_json_error_on_listing_and_generation(self):
        invalid = [model(defaults={"steps": value}) for value in ("bad", None, [], {}, True, 4.5)]
        invalid += [model(defaults="bad"), model("dit", generation_size="bad")]
        for m in invalid:
            self.record(models=[m])
            for method, route, body in (("GET", "/api/models", None),
                                        ("POST", "/api/generate", {"prompt": "garden"})):
                with self.subTest(model=m, route=route):
                    status, data = self.request(method, route, body)
                    self.assertEqual((status, data["error"]), (400, "bad_param"))
                    self.assertIsInstance(data["detail"], str)
                    self.assertIsNone(studio.st["busy"])

    def test_watch_refreshes_ready_empty_catalog_and_page_can_read_it(self):
        fake = self.fake()
        self.record(models=[])
        _, before = self.request("GET", "/api/models")
        self.assertEqual(before[0]["models"], [])
        # Exercise one actual watcher iteration, without leaving a daemon running.
        with patch.object(studio.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                studio.watch()
        self.assertIn(("GET", "/models", None), fake.requests)
        self.assertEqual(sdgen.get_phone()["models"], fake.models)
        _, after = self.request("GET", "/api/models")
        self.assertEqual(after[0]["state"], "ready")
        self.assertEqual(after[0]["models"][0]["id"], fake.models[0]["id"])

    def test_empty_catalog_refresh_waits_for_ready_idle_phone(self):
        self.record(models=[])
        for phone_state, busy in (("unreachable", False), ("ready", True)):
            status = {"phones": [{"id": "example", "state": phone_state}], "usb_new": False}
            if busy:
                studio.lock.acquire()
            try:
                with patch.object(sdgen, "phone_status", return_value=status), \
                        patch.object(sdgen, "list_models") as listing:
                    studio.refresh_phones()
                    listing.assert_not_called()
            finally:
                if busy:
                    studio.lock.release()

    def test_slow_background_catalog_does_not_block_generation(self):
        self.fake()
        self.record(models=[])
        status = {"phones": [{"id": "example", "state": "ready"}], "usb_new": False}
        fetching, release_catalog = threading.Event(), threading.Event()
        generating, release_job = threading.Event(), threading.Event()
        errors = []
        list_models = sdgen.list_models

        def blocked_catalog(phone, *, persist=True):
            fetching.set()
            if not release_catalog.wait(10):
                raise TimeoutError("catalog test was not released")
            return list_models(phone, persist=persist)

        def refresh():
            try:
                studio.refresh_phones()
            except Exception as error:
                errors.append(error)

        def generate():
            generating.set()
            if not release_job.wait(10):
                raise TimeoutError("generation test was not released")

        with patch.object(sdgen, "phone_status", return_value=status), \
                patch.object(sdgen, "list_models", side_effect=blocked_catalog):
            watcher = threading.Thread(target=refresh, daemon=True)
            watcher.start()
            try:
                self.assertTrue(fetching.wait(10))
                self.assertIsNone(studio.st["busy"])
                self.assertIsNotNone(studio.run("generating", generate))
                self.assertTrue(generating.wait(10))
                _, payload = self.request("GET", "/api/status")
                self.assertEqual(payload["busy"], "generating")
                # A job that overtakes the request must also prevent cache writes.
                release_catalog.set()
                watcher.join(10)
                self.assertFalse(watcher.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(sdgen.get_phone()["models"], [])
                self.assertEqual(studio.st["busy"], "generating")
            finally:
                release_catalog.set()
                release_job.set()
                watcher.join(10)
                self.wait_idle()

    def test_empty_catalog_refresh_backs_off_per_phone_including_failures(self):
        first = self.record(models=[])
        sdgen.save_phones([first, {**first, "id": "second"}])
        status = {"phones": [{"id": pid, "state": "ready"} for pid in ("example", "second")],
                  "usb_new": False}
        for fail in (False, True):
            attempts = []

            def listing(phone, *, persist=True):
                self.assertFalse(persist)
                attempts.append(phone["id"])
                if fail:
                    raise sdgen.PhoneError("timeout")
                phone.update(models=[], use_img2img=True)
                return []

            with self.subTest(fail=fail), patch.object(studio, "_catalog_refresh_after", {}), \
                    patch.object(studio.time, "monotonic", return_value=100) as clock, \
                    patch.object(sdgen, "phone_status", return_value=status), \
                    patch.object(sdgen, "list_models", side_effect=listing):
                studio.refresh_phones()
                self.assertEqual(attempts, ["example", "second"])
                studio.refresh_phones()
                clock.return_value = 159
                studio.refresh_phones()
                self.assertEqual(attempts, ["example", "second"])
                clock.return_value = 160
                studio.refresh_phones()
                self.assertEqual(attempts, ["example", "second"] * 2)

    def assert_delayed_catalog_yields_to(self, change_catalog):
        fake = self.fake()
        initial = self.record(models=[], use_img2img=True)
        fetched, release = threading.Event(), threading.Event()
        errors = []
        list_models = sdgen.list_models

        def delayed_catalog(phone, client=None, *, persist=True):
            models = list_models(phone, client, persist=persist)
            if not persist:
                # Freeze the already-fetched response, not the fixture's current catalog.
                if not models or not phone["use_img2img"]:
                    raise AssertionError("expected a stale non-empty catalog with image-to-image")
                fetched.set()
                if not release.wait(10):
                    raise TimeoutError("catalog test was not released")
            return models

        def refresh():
            try:
                studio.refresh_phones()
            except Exception as error:
                errors.append(error)

        with patch.object(sdgen, "list_models", side_effect=delayed_catalog):
            watcher = threading.Thread(target=refresh, daemon=True)
            watcher.start()
            try:
                self.assertTrue(fetched.wait(10))
                change_catalog(fake, initial)
                winner = sdgen.get_phone(initial["id"])
            finally:
                release.set()
                watcher.join(10)
            self.assertFalse(watcher.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(sdgen.get_phone(initial["id"]), winner)
        return initial, winner

    def test_manual_empty_refresh_wins_over_delayed_automatic_catalog(self):
        def manual_refresh(fake, initial):
            fake.models, fake.use_img2img = [], False
            status, _ = self.request("POST", "/api/connect", {})
            self.assertEqual(status, 202)
            self.wait_idle()
            self.assertIsNone(studio.st["error"])

        initial, current = self.assert_delayed_catalog_yields_to(manual_refresh)
        self.assertEqual(current["models"], [])
        self.assertFalse(current["use_img2img"])
        self.assertEqual(current["record_id"], initial["record_id"])
        self.assertEqual(current["catalog_revision"], initial["catalog_revision"] + 1)

    def test_readded_phone_does_not_receive_delayed_automatic_catalog(self):
        def replace_phone(fake, initial):
            sdgen.remove_phone(initial["id"])
            fake.models, fake.use_img2img = [], False
            replacement = sdgen.add_lan("127.0.0.1", name="example")
            self.assertEqual(replacement["id"], initial["id"])
            # Matching revisions and endpoint make record identity the deciding guard.
            sdgen.list_models(replacement)
            self.assertEqual(replacement["catalog_revision"], initial["catalog_revision"])

        initial, current = self.assert_delayed_catalog_yields_to(replace_phone)
        self.assertNotEqual(current["record_id"], initial["record_id"])
        self.assertEqual(current["models"], [])
        self.assertFalse(current["use_img2img"])

    def test_all_catalog_writes_increment_revision_even_for_unchanged_values(self):
        fake = self.fake()
        fake.models = []
        phone = self.record(models=[])
        identity, revision = phone["record_id"], phone["catalog_revision"]
        for _ in range(2):
            sdgen.list_models(phone)
            revision += 1
            self.assertEqual(phone["catalog_revision"], revision)
            self.assertEqual(sdgen.get_phone()["catalog_revision"], revision)
            self.assertEqual(phone["record_id"], identity)
        sdgen._update(phone["id"], use_img2img=True)
        self.assertEqual(sdgen.get_phone()["catalog_revision"], revision + 1)
        sdgen.save_phones(sdgen.phones())
        current = sdgen.get_phone()
        self.assertEqual(current["catalog_revision"], revision + 2)
        self.assertEqual(current["record_id"], identity)

    def test_manual_refresh_replaces_stale_nonempty_catalog(self):
        fake = self.fake()
        self.record(models=[model("sdxl")])
        status, _ = self.request("POST", "/api/connect", {})
        self.assertEqual(status, 202)
        self.wait_idle()
        self.assertIsNone(studio.st["error"])
        self.assertIn(("GET", "/models", None), fake.requests)
        self.assertEqual(sdgen.get_phone()["models"], fake.models)
        _, data = self.request("GET", "/api/models")
        self.assertEqual(data[0]["models"][0]["id"], "sd15")

    def test_mask_png_reaches_native_endpoint_and_payloads_stay_private(self):
        fake = self.fake()
        self.record()
        source = result("png")["image"]
        mask = Image.new("RGB", (2, 3), "black")
        mask.putpixel((1, 1), (255, 255, 255))
        buffer = io.BytesIO()
        mask.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        status, _ = self.request("POST", "/api/generate", {"prompt": "garden", "image": source,
                                "mask": "data:image/png;base64," + encoded, "strength": .45})
        self.assertEqual(status, 202)
        self.wait_idle()
        sent = fake.posts("/generate")[0]
        self.assertEqual(sent["image"], source)
        self.assertEqual(sent["mask"], encoded)
        self.assertEqual(sent["denoise_strength"], .45)
        with Image.open(io.BytesIO(base64.b64decode(sent["mask"], validate=True))) as decoded:
            self.assertEqual((decoded.format, decoded.mode, decoded.size), ("PNG", "RGB", (2, 3)))
            self.assertEqual(decoded.getpixel((0, 0)), (0, 0, 0))
            self.assertEqual(decoded.getpixel((1, 1)), (255, 255, 255))
        _, state = self.request("GET", "/api/status")
        self.assertTrue(state["last"]["meta"]["inpaint"])
        self.assertNotIn(encoded, json.dumps(state))
        self.assertNotIn(source, json.dumps(state))

    def test_unsupported_mask_rejected_before_admission(self):
        image = result("png")["image"]
        for m, enabled, error in ((model("dit", dit_kind="future-architecture"), True, "bad_param"),
                                  (model("sdxl"), False, "img2img_disabled")):
            self.record(models=[m], use_img2img=enabled)
            with patch.object(sdgen, "api", side_effect=AssertionError("request before validation")):
                status, data = self.request("POST", "/api/generate", {"prompt": "garden", "image": image, "mask": image})
            self.assertEqual((status, data["error"]), (400, error))
            self.assertIsNone(studio.st["busy"])

    def test_reference_endpoint_keeps_running_completed_and_failed_status_private(self):
        fake = self.fake()
        fake.models = [model("dit", dit_kind="klein")]
        self.record(models=fake.models)
        images = [result(fmt)["image"] for fmt in ("webp", "jpeg")]
        normalized = sdgen.reference_data(images)
        body = {"prompt": "Change image 1 using image 2", "reference_images": [
            "data:image/webp;base64," + images[0], images[1]]}
        fake.mode = "block"
        try:
            status, _ = self.request("POST", "/api/generate", body)
            self.assertEqual(status, 202)
            self.assertTrue(fake.started.wait(10))
            _, state = self.request("GET", "/api/status")
            self.assertEqual(state["busy"], "generating")
            for image in [*images, *normalized]:
                self.assertNotIn(image, json.dumps(state))
        finally:
            fake.release.set()
        self.wait_idle()
        sent = fake.posts("/generate")[0]
        self.assertEqual(sent["reference_images"], normalized)
        self.assertTrue({"image", "mask", "strength", "denoise_strength"}.isdisjoint(sent))
        _, state = self.request("GET", "/api/status")
        self.assertIsNone(state["error"])
        self.assertEqual(state["last"]["meta"]["reference_count"], 2)
        for image in [*images, *normalized]:
            self.assertNotIn(image, json.dumps(state))
        fake.mode = "error"
        status, _ = self.request("POST", "/api/generate", body)
        self.assertEqual(status, 202)
        self.wait_idle()
        _, state = self.request("GET", "/api/status")
        self.assertIsNotNone(state["error"])
        self.assertIn("retry_without_reference", state["diagnosis"]["actions"])
        for image in [*images, *normalized]:
            self.assertNotIn(image, json.dumps(state))
        _, catalog = self.request("GET", "/api/models")
        self.assertTrue(catalog[0]["models"][0]["reference_edit"])
        self.assertEqual(catalog[0]["models"][0]["max_references"], 3)

    def test_reference_endpoint_rejects_invalid_input_before_admission(self):
        image = result("png")["image"]
        for m, enabled in ((model(), True), (model("dit"), True), (model("sdxl"), True),
                           (model("anima"), True), (model("dit", dit_kind="qwen21"), False)):
            self.record(models=[m], use_img2img=enabled)
            with patch.object(sdgen, "api", side_effect=AssertionError("phone contacted")):
                status, data = self.request("POST", "/api/generate", {"prompt": "edit", "reference_images": [image]})
            self.assertEqual((status, data["error"]), (400, "bad_param"))
        self.record(models=[model("dit", dit_kind="klein")])
        for refs, error in ((None, "bad_param"), ([], "bad_param"), ([image] * 4, "bad_param"),
                            ([42], "bad_image"), (["invalid!"], "bad_image")):
            with patch.object(sdgen, "api", side_effect=AssertionError("phone contacted")):
                status, data = self.request("POST", "/api/generate", {"prompt": "edit", "reference_images": refs})
            self.assertEqual((status, data["error"]), (400, error))
            self.assertIsNone(studio.st["busy"])

    def test_body_cap_and_invalid_json_types_lengths(self):
        status, data = self.request("POST", "/api/stop", headers={"Content-Length": str(studio.BODY_CAP + 1)})
        self.assertEqual((status, data["error"]), (413, "too_large"))
        for body in ([], "string", 7, float("nan")):
            status, data = self.request("POST", "/api/generate", body)
            self.assertEqual((status, data["error"]), (400, "bad_param"))
        status, data = self.request("POST", "/api/generate", headers={"Content-Length": "-1"})
        self.assertEqual((status, data["error"]), (400, "bad_param"))

    def test_validate_before_202_and_cli_lock_returns_409(self):
        self.record()
        for body, code in (({"prompt": ""}, "empty_prompt"), ({"prompt": "garden", "width": 768, "height": 512}, "bad_size"),
                           ({"prompt": "garden", "steps": 0}, "bad_param"), ({"prompt": "garden", "cfg": "bad"}, "bad_param"),
                           ({"prompt": "garden", "model_id": "missing"}, "model_missing")):
            with patch.object(sdgen, "api", side_effect=AssertionError("request")):
                status, data = self.request("POST", "/api/generate", body)
            self.assertEqual((status, data["error"]), (400, code))
            self.assertIsNone(studio.st["busy"])
        with sdgen.phone_lock(sdgen.get_phone()):
            status, data = self.request("POST", "/api/generate", {"prompt": "garden"})
        self.assertEqual((status, data["error"]), (409, "busy"))

    def test_202_busy_job_immediately_and_increasing_ids(self):
        fake = self.fake()
        self.record()
        fake.mode = "block"
        status, accepted = self.request("POST", "/api/generate", {"prompt": "garden", "phone_id": "example", "width": 512, "height": 512})
        self.assertEqual(status, 202)
        _, state = self.request("GET", "/api/status")
        self.assertEqual(state["busy"], "generating")
        self.assertEqual(state["job"]["id"], accepted["job_id"])
        self.assertEqual(state["job"]["phone_id"], "example")
        self.assertIn("started", state["job"])
        status, data = self.request("POST", "/api/generate", {"prompt": "garden"})
        self.assertEqual((status, data["error"]), (409, "busy"))
        fake.release.set()
        self.wait_idle()
        status, accepted2 = self.request("POST", "/api/stop", {"phone_id": "example"})
        self.assertEqual(status, 202)
        self.assertGreater(accepted2["job_id"], accepted["job_id"])
        self.wait_idle()
        self.assertEqual(fake.posts("/stop"), [{"model_id": "sd15"}])

    def test_status_and_stop_without_phones(self):
        status, state = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertLessEqual({"busy", "error", "diagnosis", "last", "now", "phones", "job",
                              "app", "version", "capabilities", "quitting"}, set(state))
        self.assertIsInstance(state["app"], str)
        self.assertIsInstance(state["version"], str)
        self.assertEqual((state["app"], state["version"]), (studio.APP_ID, studio.__version__))
        self.assertIsInstance(state["capabilities"], dict)
        self.assertIs(state["capabilities"]["quit"], True)
        self.assertIs(state["quitting"], False)
        self.assertIsInstance(state["now"], (int, float))
        self.assertIsInstance(state["phones"], dict)
        self.assertEqual(state["phones"]["phones"], [])
        for field in ("busy", "error", "diagnosis", "last", "job"):
            self.assertIsNone(state[field])
        status, _ = self.request("POST", "/api/stop", {})
        self.assertEqual(status, 202)
        self.wait_idle()
        self.assertIsNone(studio.st["error"])

    def test_models_cached_payload_and_tokenize(self):
        fake = self.fake()
        self.record(models=[model(f) for f in ("sd15", "sd15cpu", "sdxl", "anima", "dit")])
        with patch.object(sdgen, "api", side_effect=AssertionError("cached models made a request")):
            status, payload = self.request("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["family"] for m in payload[0]["models"]], ["sd15", "sd15cpu", "sdxl", "anima", "dit"])
        self.assertEqual(payload[0]["transport"], "lan")
        status, tokens = self.request("POST", "/api/tokenize", {"prompt": "a garden"})
        self.assertEqual((status, tokens), (200, {"count": 2, "max": 0}))
        self.assertTrue(fake.requests)

    def test_gallery_serving_delete_and_estimate(self):
        self.fake()
        self.record()
        png, meta = sdgen.generate("garden")
        url = f"/out/{png.parent.name}/{png.name}"
        status, data = self.request("GET", url)
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(studio.gallery()[0]["meta"], meta)
        self.assertEqual(studio.estimate(meta["model"], 2, 3, 4), 0.0)
        status, result = self.request("POST", "/api/delete", {"url": url})
        self.assertEqual((status, result), (200, {"deleted": True}))
        self.assertFalse(png.exists())
        self.assertFalse(png.with_suffix(".json").exists())

    def test_path_traversal_rejected_without_deleting_other_files(self):
        keep = self.out / "keep.json"
        keep.write_text("keep", encoding="utf-8")
        for url in ("/out/../keep.png", "/out/2026-01-01/../../keep.png", "/out/%2e%2e/keep.png",
                    "/out/2026-01-01/keep.json", str(keep)):
            status, _ = self.request("POST", "/api/delete", {"url": url})
            self.assertEqual(status, 400)
            status, _ = self.request("GET", url if url.startswith("/") else "/out/../keep.png")
            self.assertEqual(status, 404)
        self.assertEqual(keep.read_text(), "keep")
        self.code("bad_param", sdgen.file_lock("../bad").__enter__)

    def test_symlink_escape_image_and_sidecar(self):
        external = self.out / "external"
        external.mkdir()
        outside = external / "keep.png"
        outside.write_bytes(b"keep")
        output = self.out / "output"
        output.mkdir()
        with patch.multiple(sdgen, OUT=output, PHONES=output / "phones.json"):
            day = output / "2026-01-01"
            day.mkdir()
            link = day / "escape.png"
            try:
                link.symlink_to(outside)
            except OSError:
                # A sandbox without symlink privilege still exercises the resolved-target guard.
                original = Path.resolve

                def resolve(p, *args, **kwargs):
                    return outside if p == link else original(p, *args, **kwargs)

                with patch.object(Path, "resolve", resolve):
                    self.assertEqual(self.request("GET", "/out/2026-01-01/escape.png")[0], 404)
                    self.assertEqual(self.request("POST", "/api/delete", {"url": "/out/2026-01-01/escape.png"})[0], 400)
            else:
                self.assertEqual(self.request("GET", "/out/2026-01-01/escape.png")[0], 404)
                self.assertEqual(self.request("POST", "/api/delete", {"url": "/out/2026-01-01/escape.png"})[0], 400)
            self.assertEqual(outside.read_bytes(), b"keep")

    def test_sidecar_escape_is_rejected_before_image_deletion(self):
        day = self.out / "2026-01-01"
        day.mkdir()
        image = day / "example.png"
        image.write_bytes(b"keep image")
        sidecar = image.with_suffix(".json")
        # Treat a separate subtree as the output root so the sibling is outside it.
        outside = self.out / "keep.json"
        outside.write_text("keep metadata", encoding="utf-8")
        root = day.parent
        original = Path.resolve

        def resolve(p, *args, **kwargs):
            return root.parent / "outside-example.json" if p == sidecar else original(p, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            status, data = self.request("POST", "/api/delete", {"url": "/out/2026-01-01/example.png"})
        self.assertEqual((status, data["error"]), (400, "bad_param"))
        self.assertTrue(image.exists())
        self.assertEqual(outside.read_text(), "keep metadata")

    def test_background_error_has_stable_code(self):
        fake = self.fake()
        self.record()
        fake.mode = "error"
        status, _ = self.request("POST", "/api/generate", {"prompt": "garden"})
        self.assertEqual(status, 202)
        self.wait_idle()
        _, state = self.request("GET", "/api/status")
        self.assertEqual(state["error"]["code"], "stream_broken")
        self.assertIn("detail", state["error"])
        self.assertEqual(state["diagnosis"]["cause"], "unknown")
        self.assertIn("adb_unavailable", state["diagnosis"]["evidence"])
        self.assertEqual(state["diagnosis"]["actions"], ["use_other_model", "retry"])

    def test_model_load_failure_has_diagnosis_and_clears_next_job(self):
        fake = self.fake()
        self.record()
        fake.select_error = "example load failure"
        status, _ = self.request("POST", "/api/generate", {"prompt": "garden"})
        self.assertEqual(status, 202)
        self.wait_idle()
        _, state = self.request("GET", "/api/status")
        self.assertEqual(state["error"]["code"], "load_failed")
        self.assertEqual(state["diagnosis"]["cause"], "load_failed")
        fake.select_error = None
        status, _ = self.request("POST", "/api/generate", {"prompt": "garden"})
        self.assertEqual(status, 202)
        self.wait_idle()
        _, state = self.request("GET", "/api/status")
        self.assertIsNone(state["error"])
        self.assertIsNone(state["diagnosis"])

    def test_failed_job_uses_recent_android_exit_and_request(self):
        fake = self.fake()
        phone = self.record(models=[model("dit")])
        fake.models = phone["models"]
        fake.mode = "error"
        job_time = time.time()
        self.set_adb(devices={"127.0.0.1:12345": "device"},
                     phone_times=[int(job_time), int(job_time + 1)],
                     exit_info=f"ApplicationExitInfo #0: timestamp={int((job_time + 1) * 1000)} reason=3 (LOW_MEMORY)",
                     meminfo="MemAvailable: 6000000 kB")
        reference = result(fmt="png")["image"]
        with patch.object(sdgen.time, "time", return_value=job_time):
            status, _ = self.request("POST", "/api/generate", {"prompt": "garden", "model_id": "dit", "image": reference})
            self.assertEqual(status, 202)
            self.wait_idle()
            _, state = self.request("GET", "/api/status")
        diagnosis = state["diagnosis"]
        self.assertEqual(diagnosis["cause"], "out_of_memory")
        self.assertIn("reference_uses_more_memory", diagnosis["evidence"])
        self.assertIn("retry_without_reference", diagnosis["actions"])
        self.assertEqual(diagnosis["smaller"], {"width": 768, "height": 768})
        self.assertNotIn(reference, json.dumps(state))

    def test_http_phone_responses_exclude_registry_bookkeeping(self):
        self.fake()
        phone = self.record(serialno="example-hardware", usb_ports=[47810, 47811],
                            usb_forward_serial="example-usb", future_bookkeeping={"pending": True})
        # Keep a full internal snapshot in status to test the HTTP boundary itself,
        # independently of phone_status's current projection.
        snapshot = {"phones": [{**phone, "state": "ready", "features": {"desktop_link": False}}],
                    "usb_new": False, "usb_waiting": False}
        with patch.object(sdgen, "phone_status", return_value=snapshot):
            status, data = self.request("POST", "/api/phones/add_lan", {"host": "127.0.0.1"})
            self.assertEqual(status, 202)
            self.assert_no_phone_bookkeeping(data)
            self.wait_idle()
            self.assertIsNone(studio.st["error"])
            status, data = self.request("POST", "/api/phones/transport",
                                        {"phone_id": phone["id"], "transport": "lan"})
            self.assertEqual(status, 200)
            self.assert_no_phone_bookkeeping(data)
            self.assertEqual(data["phone"]["id"], phone["id"])
            for route in ("/api/models", "/api/status"):
                with self.subTest(route=route):
                    status, data = self.request("GET", route)
                    self.assertEqual(status, 200)
                    self.assert_no_phone_bookkeeping(data)
                    public = data["phones"]["phones"][0] if route == "/api/status" else data[0]
                    self.assertEqual(public["id"], phone["id"])
                    self.assertEqual(public["name"], phone["name"])
            with patch.object(sdgen, "discover", return_value=[{**phone, "phone_id": phone["id"], "port": 8808}]):
                status, data = self.request("POST", "/api/discover", {})
                self.assertEqual(status, 200)
                self.assert_no_phone_bookkeeping(data)
                self.assertEqual(data[0]["phone_id"], phone["id"])
                self.assertEqual(data[0]["port"], 8808)
        self.assertEqual(sdgen.get_phone()["record_id"], phone["record_id"])
        self.assertIn("catalog_revision", sdgen.get_phone())

    def test_lan_add_transport_remove_and_connect_routes(self):
        self.fake()
        status, _ = self.request("POST", "/api/phones/add_lan", {"host": "127.0.0.1", "name": "Example phone"})
        self.assertEqual(status, 202)
        self.wait_idle()
        p = sdgen.get_phone()
        self.assertEqual(p["name"], "Example phone")
        self.assertTrue(p["models"])
        status, data = self.request("POST", "/api/phones/transport", {"phone_id": p["id"], "transport": "lan"})
        self.assertEqual(status, 200)
        self.assertEqual(data["phone"]["transport"], "lan")
        self.assertEqual(self.request("POST", "/api/connect", {})[0], 202)
        self.wait_idle()
        self.assertEqual(self.request("POST", "/api/phones/remove", {"phone_id": p["id"]})[0], 200)
        self.assertEqual(sdgen.phones(), [])

    def test_find_it_restores_removed_usb_but_watcher_does_not(self):
        self.record(transport="usb", serialno="example-hardware")
        sdgen.remove_phone("example")
        self.set_adb(devices={"example-usb": "device", "other-usb": "device"}, props={
            "example-usb": {"ro.serialno": "example-hardware"},
            "other-usb": {"ro.serialno": "other-hardware"}})
        with patch.object(sdgen, "endpoints", return_value=None), \
                patch.object(sdgen, "host_up", return_value=False):
            # A different newly plugged-in phone triggers the automatic reconnect.
            studio.refresh_phones()
            self.wait_idle()
            self.assertIsNone(studio.st["error"])
            self.assertEqual([p["serialno"] for p in sdgen.phones()], ["other-hardware"])
            self.assertEqual(sdgen._read_registry()["ignored_serials"], ["example-hardware"])
            status, _ = self.request("POST", "/api/connect", {})
            self.assertEqual(status, 202)
            self.wait_idle()
        self.assertIsNone(studio.st["error"])
        self.assertEqual({p["serialno"] for p in sdgen.phones()}, {"example-hardware", "other-hardware"})
        self.assertEqual(sdgen._read_registry()["ignored_serials"], [])


class UsbAdoptionTests(BackendCase):
    def test_plugging_in_a_wifi_phone_reuses_its_record(self):
        self.record(id="example", host="192.0.2.10")            # added over Wi-Fi: no serial on record
        self.set_adb(devices={"example-usb": "device"}, wifi={"example-usb": "192.0.2.10"},
                     props={"example-usb": {"ro.serialno": "example-hardware", "ro.product.model": "Example phone"}})
        with patch.object(sdgen, "endpoints", return_value=None):
            sdgen.connect_usb()
        phones = sdgen.phones()
        self.assertEqual([p["id"] for p in phones], ["example"])
        self.assertEqual(phones[0]["serialno"], "example-hardware")
        self.assertEqual(phones[0]["transport"], "lan")          # the user's choice is kept


class ConnectionUxTests(BackendCase):
    def test_removed_usb_stays_ignored_and_preserves_images(self):
        self.record(transport="usb", serialno="example-hardware")
        image = self.out / "kept.png"
        history = self.out / "kept.json"
        image.write_bytes(b"image fixture")
        history.write_text('{"prompt":"example"}', encoding="utf-8")
        self.set_adb(devices={"example-usb": "device", "other-usb": "device"}, props={
            "example-usb": {"ro.serialno": "example-hardware"},
            "other-usb": {"ro.serialno": "other-hardware"}})
        sdgen.remove_phone("example")
        self.assertEqual(sdgen._read_registry()["ignored_serials"], ["example-hardware"])
        with patch.object(sdgen, "endpoints", return_value=None):
            sdgen.connect_usb()  # another new USB phone must not re-adopt the removed one
        self.assertEqual([p["serialno"] for p in sdgen.phones()], ["other-hardware"])
        self.assertFalse(sdgen.phone_status()["usb_new"])
        with patch.object(sdgen, "endpoints", return_value=None):
            sdgen.connect_usb(include_ignored=True)  # explicit Find it contract
        self.assertEqual({p["serialno"] for p in sdgen.phones()}, {"example-hardware", "other-hardware"})
        self.assertEqual(sdgen._read_registry()["ignored_serials"], [])
        self.assertEqual(image.read_bytes(), b"image fixture")
        self.assertEqual(json.loads(history.read_text()), {"prompt": "example"})
        removed = next(p for p in sdgen.phones() if p["serialno"] == "example-hardware")
        sdgen.remove_phone(removed["id"])
        with patch.object(sdgen, "endpoints", return_value=None):
            sdgen.add_usb("example-usb")  # choosing a specific cable also restores it
        self.assertNotIn("example-hardware", sdgen._read_registry()["ignored_serials"])

    def test_lan_without_adb_has_manual_recovery(self):
        phone = self.record()
        with patch.object(sdgen, "adb_path", side_effect=sdgen.PhoneError("no_adb")), \
                patch.object(sdgen, "adb") as adb:
            self.code("host_mode_manual", sdgen.open_host_mode, phone)
            status = sdgen.phone_status()
        adb.assert_not_called()
        self.assertFalse(status["adb_available"])
        self.assertFalse(status["phones"][0]["has_adb"])

    def test_transport_prerequisites_and_attached_adb_are_public(self):
        self.fake()
        phone = self.record()
        self.code("usb_unplugged", sdgen.set_transport, phone["id"], "usb")
        self.assertFalse(sdgen.public_phone(phone)["has_usb"])
        self.assertTrue(sdgen.public_phone(phone)["has_lan"])
        self.set_adb(devices={"127.0.0.1:12345": "device"})
        status = sdgen.phone_status()
        self.assertTrue(status["adb_available"])
        self.assertTrue(status["phones"][0]["has_adb"])
        phone = self.record(transport="usb", serialno="example-hardware", host=None)
        self.code("bad_host", sdgen.set_transport, phone["id"], "lan")
        self.assertTrue(sdgen.public_phone(phone)["has_usb"])
        self.assertFalse(sdgen.public_phone(phone)["has_lan"])

    def test_refused_timeout_and_non_host_are_distinct(self):
        import urllib.error
        import errno
        client = sdgen.Api("http://127.0.0.1:9", "http://127.0.0.1:9", trusted=False)
        phone = self.record()
        for reason, code in ((ConnectionRefusedError(errno.ECONNREFUSED, "refused"), "connection_refused"),
                             (TimeoutError(), "timeout"), (OSError(errno.ENETUNREACH, "unreachable"), "unreachable")):
            with self.subTest(code=code), patch.object(client.opener, "open", side_effect=urllib.error.URLError(reason)):
                self.code(code, sdgen.ensure_host, phone, client=client)
            with patch.object(sdgen.Api, "info", side_effect=sdgen.PhoneError(code)):
                self.assertEqual(sdgen.phone_status()["phones"][0]["state"], code)
        with patch.object(client, "info", return_value={"app": "another-app"}):
            self.code("no_host_mode", sdgen.ensure_host, phone, client=client)

    def test_bad_param_identifies_invalid_field(self):
        for field, value, expected in (("steps", 0, "steps: 1..50"), ("cfg", 31, "cfg: 1..30"),
                                        ("strength", 2, "strength: 0..1"), ("seed", -1, "seed: 0..4294967295")):
            params = dict(steps=20, cfg=7, strength=.6, seed=1)
            params[field] = value
            with self.subTest(field=field), self.assertRaises(sdgen.PhoneError) as caught:
                sdgen.validate(model(), 512, 512, "1:1", **params)
            self.assertEqual(caught.exception.code, "bad_param")
            self.assertEqual(caught.exception.detail, expected)

    def test_img2img_size_type_and_pixel_guards_before_phone_io(self):
        phone = self.record()
        source = io.BytesIO()
        Image.new("RGB", (4001, 4000)).save(source, format="PNG")
        large_pixels = base64.b64encode(source.getvalue()).decode()
        gif = io.BytesIO()
        Image.new("RGB", (2, 2)).save(gif, format="GIF")
        values = (large_pixels, "A" * (4 * ((sdgen.MAX_REFERENCE_BYTES + 2) // 3) + 4),
                  base64.b64encode(gif.getvalue()).decode())
        with patch.object(sdgen, "api", side_effect=AssertionError("must reject before phone I/O")):
            for value in values:
                self.code("bad_image", sdgen.prepare, phone, "example", image_b64=value)
            self.code("bad_image", sdgen.prepare, phone, "example", image_b64=result(fmt="png")["image"], mask_b64=large_pixels)


class HostModeTests(BackendCase):
    def test_locked_phone_is_not_launched_or_force_stopped(self):
        phone = self.record(transport="usb", serialno="example-hardware")
        for value in ("1", "true"):
            with self.subTest(value=value):
                self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}},
                             trust=f'User "Example" (current): deviceLocked={value}, deviceSecure=1')
                with patch.object(sdgen.time, "sleep", return_value=None):
                    self.code("phone_locked", sdgen.open_host_mode, phone)
                self.assertFalse(any("monkey" in c or "force-stop" in c for c in self.adb_state()["calls"]))

    def test_unlocked_current_user_proceeds_after_keyguard_dismiss(self):
        phone = self.record(transport="usb", serialno="example-hardware")
        for trust in ('User "Example" (current): deviceLocked=0, deviceSecure=0',
                      'User "Other": deviceLocked=1, deviceSecure=1\nUser "Example" (current): deviceLocked=0, deviceSecure=0',
                      'User "Example" (current): deviceLocked=false, deviceSecure=false\nUser "Other": deviceLocked=true'):
            with self.subTest(trust=trust):
                self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}}, trust=trust)
                with patch.object(sdgen.time, "sleep", return_value=None), patch.object(sdgen, "host_up", return_value=True):
                    sdgen.open_host_mode(phone)
                calls = self.adb_state()["calls"]
                dismiss = next(i for i, c in enumerate(calls) if "dismiss-keyguard" in c)
                launch = next(i for i, c in enumerate(calls) if "monkey" in c)
                self.assertLess(dismiss, launch)
                self.assertFalse(any("force-stop" in c for c in calls))

    def test_all_app_languages_and_dump_failure(self):
        phone = self.record()
        languages = (("More", "Device link", "Enter host mode"), ("その他", "デバイス連携", "ホストモードを開始"),
                     ("더보기", "기기 연결", "호스트 모드 시작"), ("更多", "设备互联", "进入受控模式"))
        for labels in languages:
            screens = [[(text, 10, 20)] for text in labels]
            def response(*args, **kwargs):
                return "Activity does not exist" if "am" in args else ""
            with patch.object(sdgen, "connect", return_value="example-usb"), patch.object(sdgen, "adb", side_effect=response) as adb:
                with patch.object(sdgen.time, "sleep", return_value=None), patch.object(sdgen, "host_up", side_effect=[False, False, False, True]), patch.object(sdgen, "screen", side_effect=screens):
                    sdgen.open_host_mode(phone)
                self.assertEqual(sum("tap" in call.args for call in adb.call_args_list), 3)
                self.assertFalse(any("force-stop" in call.args for call in adb.call_args_list))
        with patch.object(sdgen, "adb", return_value="invalid XML"):
            self.code("host_mode_manual", sdgen.screen, phone)


if __name__ == "__main__":
    unittest.main()
