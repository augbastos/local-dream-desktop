"""Official control contract (647dc6f), with loopback HTTP and fake adb.

RemoteProtocol.kt defines info/catalog/status. RemoteHostServer.kt defines
routes, ignored headers and error envelopes. RemoteHostService.kt defines
selection, stop and service lifetime. The shared native generation fixture
exercises the desktop client; these Kotlin files do not define its SSE payloads.
"""
import threading
import unittest
from unittest.mock import patch

from PIL import Image

import sdgen
import studio
from tests import test_backend
from tests.fakeld import result


class OfficialAppTests(test_backend.BackendCase):
    def test_ports_and_exact_control_shapes(self):
        self.assertEqual((sdgen.CONTROL_PORT, sdgen.GEN_PORT), (8808, 8081))
        self.fake()
        phone = self.record()
        # Official headers are ignored, even if they would fail extension validation.
        client = sdgen.Api(*sdgen.endpoints(phone), trusted=False, challenge="not-a-valid-challenge")
        client.trusted, client._desktop_token = True, "not-an-official-requirement"
        info = client.info()
        self.assertEqual(set(info), {"app", "protocol", "version", "device"})
        self.assertEqual((info["app"], info["protocol"]), ("localdream", 1))
        self.assertEqual(client._json(client.ctl_base, "/info?ignored=1", {}), info)
        catalog = client.models()
        self.assertEqual(set(catalog), {"use_img2img", "models", "upscalers"})
        self.assertEqual(set(catalog["models"][0]), {
            "id", "name", "description", "run_on_cpu", "is_sdxl", "is_anima", "dit_kind",
            "is_custom", "generation_size", "defaults", "resolutions"})
        self.assertEqual(set(catalog["models"][0]["defaults"]), {
            "prompt", "negative_prompt", "steps", "cfg", "scheduler"})
        self.assertEqual(client.status(), {"state": "idle", "serving_model_id": None,
                                           "width": None, "height": None, "message": None,
                                           "error_model_id": None})
        self.code("model_missing", client._json, client.ctl_base, "/select")  # GET gets HTTP 404
        self.code("stream_broken", client._json, client.ctl_base, "/unknown")
        self.code("stream_broken", client._json, client.gen_base, "/info")
        self.code("stream_broken", client._json, client.ctl_base, "/generate", {})

    def test_official_select_error_bodies_and_no_generation_retry(self):
        fake = self.fake()
        phone = self.record()
        client = sdgen.api(phone)
        for status, code, detail in (
                (400, "bad_param", "DiT resolution must be 512..2048 in 64-pixel steps"),
                (404, "model_missing", "model not found"),
                (500, "load_failed", "backend start rejected")):
            with self.subTest(status=status):
                fake.select_http_error = status
                with self.assertRaises(sdgen.PhoneError) as caught:
                    client.select("sd15", 512, 512)
                self.assertEqual((caught.exception.code, caught.exception.detail), (code, detail))
        self.code("load_failed", sdgen.generate, "A quiet garden", phone_id=phone["id"])
        self.assertEqual(fake.posts("/generate"), [])

    def test_select_ignores_previous_model_error_while_starting(self):
        fake = self.fake()
        phone = self.record()
        old_error = {**fake.state, "state": "error", "message": "previous model failed",
                     "error_model_id": "previous-model"}
        fake.select_statuses = [dict(fake.state), old_error,
                                {**fake.state, "state": "starting"}]
        with patch.object(sdgen.time, "sleep"):
            sdgen.select(phone, "sd15", 512, 512)
        self.assertEqual(len(fake.posts("/select")), 1)
        self.assertEqual(sdgen.api(phone).status()["state"], "running")

    def test_stop_is_model_scoped_and_does_not_exit_host_mode(self):
        self.fake()
        phone = self.record()
        client = sdgen.api(phone)
        self.assertEqual(client.select("sd15", 512, 512), {"ok": True})
        self.assertEqual(client.stop("previous-model"), {"ok": True, "ignored": True})
        self.assertEqual(client.status()["serving_model_id"], "sd15")
        self.assertEqual(client.stop("sd15"), {"ok": True})
        self.assertEqual(client.status()["state"], "idle")
        self.assertEqual(client.info()["app"], "localdream")
        # The host accepts legacy ID-less stops, but the desktop never emits one.
        client.select("sd15", 512, 512)
        self.assertEqual(client._json(client.ctl_base, "/stop", {}), {"ok": True})

    def test_host_mode_off_closes_both_endpoints(self):
        fake = self.fake()
        phone = self.record()
        self.assertEqual(sdgen.api(phone).info()["app"], "localdream")
        fake.__exit__()  # RemoteHostService.onDestroy closes control and stops the backend
        # A closed LAN port can be refused or time out (for example under a
        # firewall). Status must not invent pairing; generation requests host mode.
        state = sdgen.phone_status()["phones"][0]["state"]
        self.assertIn(state, ("connection_refused", "timeout", "unreachable"))
        self.assertFalse(sdgen.api(phone).health())
        with self.assertRaises(sdgen.PhoneError) as caught:
            sdgen.generate("A quiet garden", phone_id=phone["id"])
        self.assertIn(caught.exception.code, ("connection_refused", "timeout", "unreachable"))
        self.assertEqual(fake.posts("/select"), [])
        self.assertEqual(fake.posts("/generate"), [])

    def test_official_host_start_falls_back_to_manual_screen_navigation(self):
        phone = self.record(transport="usb", serialno="example-hardware")
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {
            "ro.serialno": "example-hardware"}})
        with patch.object(sdgen.time, "sleep"), patch.object(
                sdgen, "host_up", side_effect=[False, False, False, True]), patch.object(
                sdgen, "screen", side_effect=[[("More", 10, 20)], [("Device link", 30, 40)],
                                              [("Enter host mode", 50, 60)]]):
            sdgen.open_host_mode(phone)
        calls = self.adb_state()["calls"]
        taps = [call[-2:] for call in calls if "tap" in call]
        self.assertEqual(taps, [["10", "20"], ["30", "40"], ["50", "60"]])
        self.assertFalse(any("force-stop" in call or "start-foreground-service" in call for call in calls))

    def test_features_depend_on_fields_not_version_or_pairing_state(self):
        fake = self.fake()
        client = sdgen.Api(*fake.bases, trusted=False)
        official = client.info()
        for field in ("desktop_link", "app_state"):
            self.assertNotIn(field, official)
        self.assertEqual(sdgen.app_features(official), {"desktop_link": False, "app_state": False})
        # Hosts with different optional capabilities may report the same version.
        fake.desktop_link = {"enabled": False, "auth_required": False, "token_ok": False}
        fake.app_state = {"foreground": False, "locked": True}
        extended = client.info()
        self.assertEqual(extended["version"], official["version"])
        self.assertEqual(sdgen.app_features(extended), {"desktop_link": True, "app_state": True})

    def test_manual_lan_and_saved_discovery_without_nsd_or_proof(self):
        fake = self.fake()
        with patch.object(sdgen, "mdns_discover", return_value=[]), patch.object(
                sdgen, "desktop_link", side_effect=AssertionError("official discovery needs no identity")):
            self.assertEqual(sdgen.discover(adopt=True), [])  # no LAN scan or invented NSD
            phone = sdgen.add_lan("127.0.0.1")
            found = sdgen.discover(adopt=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["phone_id"], phone["id"])
        self.assertEqual(found[0]["state"], "ready")
        self.assertFalse(found[0]["features"]["desktop_link"])
        self.assertFalse(found[0]["desktop_link"]["auth_required"])
        self.assertTrue(all(header is None for _, header in fake.headers))
        self.assertFalse((self.out / "desktop-link.json").exists())
        sdgen.ensure_host(phone)
        self.assertEqual(sdgen.phone_status()["phones"][0]["state"], "ready")

    def test_usb_discovery_connection_and_catalog_with_wifi_saved(self):
        fake = self.fake()
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {
            "ro.serialno": "example-hardware", "ro.product.model": "Example phone"}},
            wifi={"example-usb": "192.0.2.50"})
        self.assertTrue(sdgen.phone_status()["usb_new"])
        forward = sdgen._usb_endpoints

        def fixture_ports(phone, serial):
            # Fake adb records forwards; the HTTP fixture already listens on its
            # ephemeral loopback pair instead of an actual Android connection.
            phone["usb_ports"] = fake.ports
            sdgen._update(phone["id"], usb_ports=fake.ports)
            return forward(phone, serial)

        with patch.object(sdgen, "_usb_endpoints", side_effect=fixture_ports), patch.object(
                sdgen, "_bindable", return_value=True):
            ids = sdgen.connect_usb()
        self.assertEqual(len(ids), 1)
        phone = sdgen.get_phone(ids[0])
        self.assertEqual(phone["transport"], "usb")
        self.assertEqual(sdgen.endpoints(phone), fake.bases)
        with patch.object(sdgen, "mdns_discover", return_value=[]):
            found = sdgen.discover(adopt=True)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["transport"], found[0]["state"]), ("usb", "ready"))
        self.assertEqual(found[0]["port"], fake.ports[0])  # cached Wi-Fi must not win
        self.assertEqual(sdgen.list_models(phone), fake.models)
        self.assertEqual(sdgen.phone_status()["phones"][0]["features"],
                         {"desktop_link": False, "app_state": False})
        calls = self.adb_state()["calls"]
        self.assertTrue(any("forward" in call and f"tcp:{sdgen.CONTROL_PORT}" in call for call in calls))
        self.assertTrue(any("forward" in call and f"tcp:{sdgen.GEN_PORT}" in call for call in calls))
        self.assertFalse(any(call[0] in ("pair", "connect", "tcpip") for call in calls))
        path, meta = sdgen.generate("A quiet garden", phone_id=phone["id"], seed=7)
        self.assertTrue(path.is_file())
        self.assertEqual(meta["transport"], "usb")
        sdgen.stop(phone)
        self.assertEqual(fake.posts("/stop"), [{"model_id": "sd15"}])

    def test_lan_catalog_text_generation_and_stop_without_adb(self):
        fake = self.fake()
        phone = sdgen.add_lan("127.0.0.1")
        with patch.object(sdgen, "adb_path", side_effect=sdgen.PhoneError("no_adb")):
            self.assertEqual(sdgen.list_models(phone), fake.models)
            context, warnings, progress = {}, [], []
            path, meta = sdgen.generate("A quiet garden", phone_id=phone["id"], seed=7,
                                        context=context, warning=warnings.append,
                                        progress=lambda step, total: progress.append((step, total)))
            sdgen.stop(phone)
        with Image.open(path) as image:
            self.assertEqual(image.size, (fake.output["width"], fake.output["height"]))
        self.assertEqual(meta["seed"], 7)
        self.assertEqual(meta["warnings"], [])
        self.assertEqual(warnings, [])
        self.assertEqual(context["app_state"], {})
        self.assertEqual(progress, [(1, 4)])
        self.assertEqual(len(fake.posts("/generate")), 1)
        self.assertNotIn("image", fake.posts("/generate")[0])
        self.assertEqual(fake.posts("/stop"), [{"model_id": "sd15"}])

    def test_reference_generation_uses_catalog_support(self):
        fake = self.fake()
        phone = sdgen.add_lan("127.0.0.1")
        sdgen.list_models(phone)
        reference = result(fmt="png")["image"]
        path, meta = sdgen.generate("A quiet garden", phone_id=phone["id"],
                                    image_b64=reference, strength=.6)
        self.assertTrue(path.is_file())
        self.assertTrue(meta["img2img"])
        self.assertEqual(fake.posts("/generate")[0]["image"], reference)
        self.assertEqual(fake.posts("/generate")[0]["denoise_strength"], .6)
        fake.use_img2img = False
        phone = sdgen.get_phone(phone["id"])
        sdgen.list_models(phone)
        self.code("img2img_disabled", sdgen.generate, "A quiet garden", phone_id=phone["id"],
                  image_b64=reference)
        self.assertEqual(len(fake.posts("/generate")), 1)

    def test_locked_adb_does_not_invent_missing_app_state(self):
        self.fake()
        phone = self.record(serialno="example-hardware")
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {
            "ro.serialno": "example-hardware"}}, trust="User (current): deviceLocked=1")
        context, warnings = {}, []
        _, meta = sdgen.generate("A quiet garden", phone_id=phone["id"],
                                 context=context, warning=warnings.append)
        self.assertEqual(context["app_state"], {})
        self.assertEqual(meta["warnings"], [])
        self.assertEqual(warnings, [])
        self.assertFalse(any("input" in call or "am" in call for call in self.adb_state()["calls"]))


class OfficialAppStudioTests(test_backend.BackendCase):
    def setUp(self):
        super().setUp()
        self.server = studio.Server(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_address[1]

    close_server = test_backend.StudioTests.close_server
    request = test_backend.StudioTests.request

    def test_page_payload_without_pairing_or_app_state_requirements(self):
        self.fake()
        phone = self.record()
        studio.refresh_phones()
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["phones"]["phones"][0]["state"], "ready")
        expected = {"desktop_link": False, "app_state": False}
        self.assertEqual(payload["phones"]["phones"][0]["features"], expected)
        status, models = self.request("GET", "/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(models[0]["features"], expected)
        self.assertEqual(models[0]["models"][0]["id"], phone["models"][0]["id"])
        status, memory = self.request("GET", "/api/memory?phone_id=example&model_id=sd15")
        self.assertEqual(status, 200)
        self.assertNotIn("app_state", memory)

    def test_failed_load_diagnosis_without_app_state(self):
        fake = self.fake()
        self.record(serialno="example-hardware")
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {
            "ro.serialno": "example-hardware"}}, trust="User (current): deviceLocked=1")
        fake.select_error = "model failed to load"
        status, _ = self.request("POST", "/api/generate", {"prompt": "A quiet garden", "phone_id": "example"})
        self.assertEqual(status, 202)
        self.wait_idle()
        _, payload = self.request("GET", "/api/status")
        self.assertEqual(payload["error"]["code"], "load_failed")
        self.assertEqual(payload["diagnosis"]["cause"], "load_failed")
        self.assertNotIn("app_not_foreground", payload["diagnosis"]["causes"])
        for action in ("recommend_foreground", "recommend_unlock"):
            self.assertNotIn(action, payload["diagnosis"]["actions"])
        self.assertEqual(fake.posts("/generate"), [])

    def test_backend_start_rejected_is_load_diagnosis(self):
        fake = self.fake()
        self.record()
        fake.select_http_error = 500
        status, _ = self.request("POST", "/api/generate", {"prompt": "A quiet garden", "phone_id": "example"})
        self.assertEqual(status, 202)
        self.wait_idle()
        _, payload = self.request("GET", "/api/status")
        self.assertEqual(payload["error"], {"code": "load_failed", "detail": "backend start rejected"})
        self.assertEqual(payload["diagnosis"]["cause"], "load_failed")
        self.assertEqual(fake.posts("/generate"), [])


if __name__ == "__main__":
    unittest.main()
