"""Desktop-link contract tests: local fixtures only, no phone or multicast network."""
import io
import re
import socket
import struct
import threading
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from unittest.mock import patch

import sdgen
import studio
from tests import test_backend
from tests.fakeld import model


# Compressed Android NSD-style answer: PTR, SRV and A in one datagram.
SERVICE = b"\x0b_localdream\x04_tcp\x05local\0"
PACKET = (struct.pack("!6H", 0, 0x8400, 0, 3, 0, 0) + SERVICE
          + struct.pack("!HHIH", 12, 1, 120, 10) + b"\x07Example\xc0\x0c"
          + b"\xc0\x2e" + struct.pack("!HHIH", 33, 0x8001, 120, 19)
          + b"\0\0\0\0\x22\x68\x05phone\x05local\0"
          + b"\xc0\x4a" + struct.pack("!HHIH", 1, 0x8001, 120, 4) + b"\xc0\0\x02\x32")


class DesktopLinkTests(test_backend.BackendCase):
    def test_token_persists_atomic_and_concurrent(self):
        with patch.object(sdgen.socket, "gethostname", return_value="Example computer"):
            with ThreadPoolExecutor(max_workers=4) as pool:
                identities = list(pool.map(lambda _: sdgen.desktop_link(), range(8)))
        self.assertEqual(len({item["token"] for item in identities}), 1)
        self.assertTrue(bool(re.fullmatch(r"[0-9a-f]{32}", identities[0]["token"])))
        self.assertTrue(sdgen.desktop_link() == identities[0])
        self.assertEqual(identities[0]["name"], "Example computer")
        self.assertEqual(list(self.out.glob(".ldpc-*.tmp")), [])
        path = self.out / "desktop-link.json"
        path.write_text("broken", encoding="utf-8")
        self.code("bad_param", sdgen.desktop_link)
        self.assertEqual(path.read_text(), "broken")

    def test_token_on_every_control_request_and_not_generation(self):
        fake = self.fake()
        self.record()
        token = sdgen.desktop_link()["token"]  # existing pairing still works on the first request
        client = sdgen.Api(*fake.bases)
        client.info(); client.models(); client.select("sd15", 512, 512); client.status(); client.stop("sd15")
        client.health(); client.tokenize("example"); client.generate({})
        for path, header in fake.headers:
            with self.subTest(path=path):
                if path in ("/info", "/models", "/select", "/status", "/stop"):
                    self.assertTrue(header == token)
                else:
                    self.assertIsNone(header)

    def test_control_401_and_info_auth_are_not_paired(self):
        fake = self.fake()
        phone = self.record()
        fake.control_unauthorized = True
        client = sdgen.api(phone)
        for action in (client.models, client.status, lambda: client.select("sd15", 512, 512), lambda: client.stop("sd15")):
            self.code("not_paired", action)
        fake.desktop_link = {"enabled": True, "auth_required": True, "token_ok": False}
        self.code("not_paired", sdgen.ensure_host, phone)
        self.code("not_paired", sdgen.add_lan, "127.0.0.1")
        self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "not_paired")
        # /info itself can also return 401: ensure_host must preserve the stable code.
        with patch.object(sdgen.Api, "info", side_effect=sdgen.PhoneError("not_paired")):
            self.code("not_paired", sdgen.ensure_host, phone)

    def test_control_redirect_does_not_forward_token(self):
        fake = self.fake()
        self.record()
        sdgen.desktop_link()
        fake.control_redirect = fake.bases[1] + "/health"
        self.code("stream_broken", sdgen.Api(*fake.bases).info)
        self.assertEqual([path for path, _ in fake.headers], ["/info"])

    def test_official_info_is_unpaired_without_auth(self):
        self.fake()
        phone = self.record()
        sdgen.ensure_host(phone)
        self.assertEqual(sdgen.link_status(sdgen.api(phone).info()),
                         {"enabled": False, "auth_required": False, "token_ok": False})
        self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "ready")
        self.assertFalse((self.out / "desktop-link.json").exists())

    def test_official_operations_never_create_pairing_identity(self):
        fake = self.fake()
        phone = sdgen.add_lan("127.0.0.1")
        sdgen.list_models(phone)
        sdgen.phone_status()
        sdgen.generate("A quiet garden", phone_id=phone["id"])
        self.assertFalse((self.out / "desktop-link.json").exists())
        self.assertTrue(all(header is None for _, header in fake.headers))

    def test_identity_created_only_after_extension_report_or_pairing_start(self):
        fake = self.fake()
        self.record()
        client = sdgen.Api(*fake.bases)
        client.info()
        self.assertFalse((self.out / "desktop-link.json").exists())
        fake.desktop_link = {"enabled": True, "auth_required": True, "token_ok": False}
        client.info()
        self.assertTrue((self.out / "desktop-link.json").is_file())
        self.assertEqual([header is None for _, header in fake.headers], [True, True, False])
        identity = sdgen.desktop_link()
        self.assertEqual(client._desktop_token, identity["token"])
        self.assertEqual(sdgen.desktop_link(), identity)

    def test_pairing_start_creates_identity_without_a_phone(self):
        self.assertFalse((self.out / "desktop-link.json").exists())
        payload = sdgen.pairing_payload()
        self.assertTrue((self.out / "desktop-link.json").is_file())
        self.assertTrue(payload["url"].startswith("localdream://pair?"))

    def test_mdns_query_and_compressed_fixture(self):
        self.assertEqual(sdgen.mdns_query(), struct.pack("!6H", 0, 0, 1, 0, 0, 0) + SERVICE + b"\0\x0c\x80\x01")
        rows = sdgen.parse_mdns_packet(PACKET)
        self.assertEqual(rows, [
            ("_localdream._tcp.local", 12, "Example._localdream._tcp.local"),
            ("example._localdream._tcp.local", 33, ("phone.local", 8808)),
            ("phone.local", 1, "192.0.2.50")])
        for bad in (b"", PACKET[:30], PACKET[:-1],
                    struct.pack("!6H", 0, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c"):
            self.assertEqual(sdgen.parse_mdns_packet(bad), [])

    def test_mdns_socket_timeout_split_replies_and_no_results(self):
        # Split the same fixture across PTR/SRV/A packets, preserving compression offsets.
        with patch.object(sdgen.socket, "socket") as factory:
            sock = factory.return_value.__enter__.return_value
            sock.recvfrom.side_effect = [(PACKET, ("192.0.2.50", 5353)), socket.timeout()]
            self.assertEqual(sdgen.mdns_discover(), [{"name": "Example", "host": "192.0.2.50", "port": 8808}])
            sock.setsockopt.assert_any_call(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            self.assertTrue(all(call.args[0] <= 2 for call in sock.settimeout.call_args_list))
            sock.recvfrom.side_effect = [socket.timeout()]
            self.assertEqual(sdgen.mdns_discover(), [])
        records = sdgen.parse_mdns_packet(PACKET)
        with patch.object(sdgen, "parse_mdns_packet", side_effect=[[records[0]], [records[1]], [records[2]]]):
            with patch.object(sdgen.socket, "socket") as factory:
                sock = factory.return_value.__enter__.return_value
                sock.recvfrom.side_effect = [(b"", ("192.0.2.50", 5353))] * 3 + [socket.timeout()]
                self.assertEqual(len(sdgen.mdns_discover()), 1)
                self.assertEqual(sock.sendto.call_count, 3)  # PTR, missing SRV, missing A

    def test_discovery_merges_known_hosts_and_adopts_only_paired(self):
        fake = self.fake()
        phone = self.record(name="Old example")
        discovered = [{"name": "NSD example", "host": "127.0.0.1", "port": fake.ports[0]}]
        with patch.object(sdgen, "mdns_discover", return_value=discovered):
            before = sdgen.PHONES.read_bytes()
            found = sdgen.discover(adopt=True)
            self.assertEqual(len(found), 1)
            self.assertFalse(found[0]["desktop_link"]["token_ok"])
            self.assertEqual(sdgen.PHONES.read_bytes(), before)
            fake.desktop_link = {"enabled": True, "auth_required": True, "token_ok": True}
            fake.proof_token = sdgen.desktop_link()["token"]
            found = sdgen.discover()
            self.assertEqual(sdgen.PHONES.read_bytes(), before)  # GET is read-only
            found = sdgen.discover(adopt=True)
            self.assertEqual(found[0]["phone_id"], phone["id"])
            self.assertEqual(len(sdgen.phones()), 1)
            self.assertEqual(sdgen.get_phone()["name"], "Old example")   # a name the owner already has is kept
            sdgen.save_phones([])
            found = sdgen.discover(adopt=True)
            self.assertEqual(sdgen.get_phone()["transport"], "lan")
            self.assertEqual(sdgen.get_phone()["host"], "127.0.0.1")
            self.assertEqual(found[0]["phone_id"], sdgen.get_phone()["id"])
        with patch.object(sdgen, "mdns_discover", return_value=[]):
            self.assertEqual(len(sdgen.discover()), 1)  # known hosts still probed

    def test_unverified_host_never_receives_token(self):
        fake = self.fake()
        client = sdgen.Api(*fake.bases)
        client.info(); client.models(); client.select("sd15", 512, 512); client.status(); client.stop("sd15")
        self.assertTrue(all(header is None for _, header in fake.headers))

    def test_discovery_rejects_wrong_proof_even_if_peer_claims_token_ok(self):
        fake = self.fake()
        fake.desktop_link = {"enabled": True, "auth_required": True, "token_ok": True}
        fake.proof_override = ["0" * 64, "A" * 64, None]
        peer = {"name": "Example", "host": "127.0.0.1", "port": fake.ports[0]}
        with patch.object(sdgen, "mdns_discover", return_value=[peer]):
            found = sdgen.discover(adopt=True)
        self.assertFalse(found[0]["desktop_link"]["token_ok"])
        self.assertEqual(found[0]["state"], "not_paired")
        self.assertEqual(sdgen.phones(), [])
        self.assertTrue(all(header is None for _, header in fake.headers))
        self.assertTrue(bool(re.fullmatch(r"[0-9a-f]{32}", fake.challenges[0])))

    def test_right_proof_adopted_before_first_token_header(self):
        fake = self.fake()
        fake.desktop_link = {"enabled": True, "auth_required": True, "token_ok": False}
        fake.proof_token = sdgen.desktop_link()["token"]
        peer = {"name": "Example", "host": "127.0.0.1", "port": fake.ports[0]}
        with patch.object(sdgen, "mdns_discover", return_value=[peer]):
            found = sdgen.discover(adopt=True)
        self.assertTrue(found[0]["desktop_link"]["token_ok"])
        self.assertEqual(len(sdgen.phones()), 1)
        self.assertEqual(fake.headers[0], ("/info", None))
        self.assertTrue(all(header == fake.proof_token for path, header in fake.headers if path != "/info"))
        self.assertIn(("GET", "/models", None), fake.requests)

    def test_extension_command_first_without_ui(self):
        phone = self.record(transport="usb", serialno="example-hardware")
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}},
                     host_command="Starting: Intent")
        with patch.object(sdgen, "host_up", return_value=True):
            sdgen.open_host_mode(phone)
        calls = self.adb_state()["calls"]
        command = [c for c in calls if "am" in c]
        self.assertEqual(len(command), 1)
        self.assertIn("io.github.xororz.localdream.action.HOST_MODE_START", command[0])
        self.assertFalse(any("monkey" in c or "uiautomator" in c or "keyevent" in c for c in calls))

    def test_command_permission_or_absent_activity_falls_back(self):
        phone = self.record(transport="usb", serialno="example-hardware")
        for message, code in (("Activity does not exist", 0), ("Permission Denial", 0), ("Permission Denial", 1)):
            with self.subTest(message=message, code=code):
                self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}},
                             host_command=message, host_command_code=code)
                with patch.object(sdgen.time, "sleep"), patch.object(sdgen, "host_up", return_value=True):
                    sdgen.open_host_mode(phone)
                calls = self.adb_state()["calls"]
                self.assertLess(next(i for i, c in enumerate(calls) if "am" in c),
                                next(i for i, c in enumerate(calls) if "monkey" in c))

    def test_memory_attached_transports_and_never_connects(self):
        phone = self.record(serialno="example-hardware", host="192.0.2.50")
        for serial in ("example-usb", "192.0.2.50:5555", "192.0.2.50:37123", "adb-example._adb-tls-connect._tcp"):
            with self.subTest(serial=serial):
                self.set_adb(devices={serial: "device"}, props={serial: {"ro.serialno": "example-hardware"}},
                             wifi={serial: "192.0.2.50"}, meminfo="MemTotal: 8192000 kB\nMemAvailable: 2048000 kB\n")
                self.assertEqual(sdgen.phone_memory(phone), 2000)
                self.assertFalse(any(c[0] in ("connect", "pair", "mdns", "forward", "tcpip") for c in self.adb_state()["calls"]))
        for devices, meminfo in (({}, ""), ({"192.0.2.51:5555": "device"}, "MemAvailable: 2048000 kB"),
                                 ({"192.0.2.50:5555": "unauthorized"}, ""),
                                 ({"192.0.2.50:5555": "device"}, "MemFree: 2048000 kB")):
            self.set_adb(devices=devices, meminfo=meminfo)
            self.assertIsNone(sdgen.phone_memory(phone))
        with patch.object(sdgen, "_adb_raw", side_effect=sdgen.PhoneError("no_adb")):
            self.assertIsNone(sdgen.phone_memory(phone))

    def test_memory_thresholds_unknown_equal_and_low(self):
        for family, needed in (("dit", 4200), ("sdxl", 4000), ("anima", 4000), ("sd15", 1500), ("sd15cpu", 1500)):
            for available in (None, needed - 1, needed, needed + 1):
                with patch.object(sdgen, "phone_memory", return_value=available):
                    self.assertEqual(sdgen.memory_status({}, model(family)),
                                     {"available_mb": available, "needed_mb": needed,
                                      "reference_extra_mb": 1500 if family in ("dit", "sdxl", "anima") else 0,
                                      "low": available is not None and available < needed})

    def test_cli_json_warning_and_plain_stderr(self):
        self.fake(); self.record()
        with patch.object(sdgen, "phone_memory", return_value=1000):
            status, data = self.cli("gen", "example")
            self.assertEqual(status, 0)
            self.assertEqual(len(data["warnings"]), 1)
            self.assertIn("1.0 GB free", data["warnings"][0])
            self.assertIn("1.5 GB", data["warnings"][0])
            errors = io.StringIO()
            with redirect_stderr(errors), patch("builtins.print") as output:
                self.assertEqual(sdgen.main(["gen", "example"]), 0)
            # Mocked print avoids dumping paths; inspect that a warning was sent to stderr.
            self.assertTrue(any("GB free" in str(c.args[0]) and c.kwargs.get("file") is errors for c in output.call_args_list))


class DesktopLinkStudioTests(test_backend.BackendCase):
    def setUp(self):
        super().setUp()
        self.server = studio.Server(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_address[1]

    close_server = test_backend.StudioTests.close_server
    request = test_backend.StudioTests.request

    def test_guarded_pairing_qr_and_discovery_routes(self):
        status, data = self.request("GET", "/api/desktop-link")
        self.assertEqual(status, 200)
        parsed = urllib.parse.urlsplit(data["url"])
        self.assertEqual((parsed.scheme, parsed.netloc), ("localdream", "pair"))
        query = urllib.parse.parse_qs(parsed.query)
        self.assertTrue(query["t"][0] == sdgen.desktop_link()["token"])
        self.assertEqual(self.request("GET", "/vendor/qrcode.js")[0], 200)
        for route in ("/api/desktop-link", "/vendor/qrcode.js", "/api/discover", "/api/memory"):
            for headers in ({"Host": "foreign.example"}, {"Origin": "https://foreign.example"}):
                self.assertEqual(self.request("GET", route, headers=headers)[0], 403)
        with patch.object(sdgen, "_devices", return_value={}), patch.object(sdgen, "discover", return_value=[]) as discover:
            self.assertEqual(self.request("GET", "/api/discover")[0], 404)
            discover.assert_not_called()
            self.assertEqual(self.request("POST", "/api/discover", {}), (200, []))
            discover.assert_called_once_with(adopt=True)
        page = studio.HTML.read_text(encoding="utf-8")
        self.assertLess(page.index('${t("c_scan")}'), page.index('${t("c_usb")}'))
        self.assertIn('qrcode(0, "M")', page)
        self.assertIn('fetch("/api/discover", { method: "POST"', page)

    def test_memory_endpoint_model_and_unknown_memory(self):
        self.fake()
        phone = self.record()
        route = "/api/memory?phone_id=example&model_id=sd15"
        with patch.object(sdgen, "phone_memory", return_value=1000):
            self.assertEqual(self.request("GET", route),
                             (200, {"available_mb": 1000, "needed_mb": 1500, "reference_extra_mb": 0, "low": True}))
        with patch.object(sdgen, "phone_memory", return_value=None):
            self.assertEqual(self.request("GET", route),
                             (200, {"available_mb": None, "needed_mb": 1500, "reference_extra_mb": 0, "low": False}))
        status, data = self.request("GET", "/api/memory?phone_id=example&model_id=missing")
        self.assertEqual((status, data["error"]), (400, "model_missing"))


    def test_app_state_advisory_and_failed_job_snapshot(self):
        fake = self.fake()
        self.record()
        route = "/api/memory?phone_id=example&model_id=sd15"
        fake.app_state = {"foreground": False, "locked": True}
        status, data = self.request("GET", route)
        self.assertEqual(status, 200)
        self.assertEqual(data["app_state"], fake.app_state)
        self.assertEqual(fake.posts("/select"), [])  # advisory is read-only
        fake.mode = "error"
        status, _ = self.request("POST", "/api/generate", {"prompt": "garden"})
        self.assertEqual(status, 202)                # warnings never block generation
        self.wait_idle()
        # Later phone state cannot rewrite the state recorded at job start.
        fake.app_state = {"foreground": True, "locked": False}
        _, data = self.request("GET", "/api/status")
        self.assertEqual(data["diagnosis"]["cause"], "app_not_foreground")
        self.assertIn("recommend_foreground", data["diagnosis"]["actions"])
        self.assertIn("recommend_unlock", data["diagnosis"]["actions"])
        self.assertEqual(self.adb_state()["calls"], [["devices"], ["devices"], ["devices"]])

    def test_foreground_state_in_running_job_and_no_stale_warning(self):
        fake = self.fake()
        self.record()
        fake.app_state = {"foreground": False, "locked": False}
        self.set_adb(devices={"127.0.0.1:12345": "device"})
        raw = sdgen._adb_raw

        def command(*args, **kwargs):
            out = raw(*args, **kwargs)
            if ".MainActivity" in " ".join(args):
                fake.app_state = {"foreground": True, "locked": False}
            return out

        fake.mode = "block"
        with patch.object(sdgen, "_adb_raw", side_effect=command):
            status, _ = self.request("POST", "/api/generate", {"prompt": "garden"})
            self.assertEqual(status, 202)
            self.assertTrue(fake.started.wait(10))
            try:
                _, data = self.request("GET", "/api/status")
                self.assertEqual(data["job"]["app_state"], {"foreground": True, "locked": False})
                self.assertEqual(data["job"]["model_id"], "sd15")
                self.assertIsNone(data["diagnosis"])
            finally:
                fake.release.set()
                self.wait_idle()


if __name__ == "__main__":
    unittest.main()
