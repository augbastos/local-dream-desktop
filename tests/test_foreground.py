"""Preflight controls use fake adb only; warnings are advisory on every build."""
import unittest
from unittest.mock import patch

import sdgen
from tests.test_backend import BackendCase
from tests.fakeld import model, result


class ForegroundTests(BackendCase):
    def phone(self, **values):
        # The standalone preflight needs a phone snapshot, not a registry write.
        return {"id": "example", "host": "192.0.2.50", **values}

    def test_app_warning_fields_are_optional_and_strict(self):
        for state in (None, {}, {"foreground": True, "locked": False}, {"foreground": 0, "locked": 1}, "invalid"):
            self.assertEqual(sdgen.app_warnings(state), [])
        self.assertEqual(sdgen.app_warnings({"foreground": False}), ["recommend_foreground"])
        self.assertEqual(sdgen.app_warnings({"locked": True}), ["recommend_unlock"])
        self.assertEqual(sdgen.app_warnings({"foreground": False, "locked": True}),
                         ["recommend_foreground", "recommend_unlock"])

    def test_without_matching_attachment_no_phone_commands(self):
        for devices in ({}, {"192.0.2.51:12345": "device"}, {"192.0.2.50:12345": "offline"},
                        {"192.0.2.50:12345": "unauthorized"}):
            self.set_adb(devices=devices)
            phone = self.phone()
            original = {"foreground": False, "locked": False}
            self.assertEqual(sdgen.foreground_before_work(phone, original), original)
            self.assertEqual(self.adb_state()["calls"], [["devices"]])

    def test_locked_and_unknown_keyguard_never_launch_or_unlock(self):
        phone = self.phone()
        for fields in ({"window": "mShowingLockscreen=true"}, {"trust": "User (current): deviceLocked=1"},
                       {"trust": "User (current): deviceLocked=true"}, {"window": "", "trust": ""}):
            self.set_adb(devices={"192.0.2.50:12345": "device"}, **fields)
            state = sdgen.foreground_before_work(phone, {"foreground": False})
            if fields.get("trust") != "":
                self.assertTrue(state["locked"])
            self.assertFalse(any("input" in call or "am" in call or "wm" in call
                                 for call in self.adb_state()["calls"]))
        self.set_adb(devices={"192.0.2.50:12345": "device"})
        self.assertTrue(sdgen.foreground_before_work(phone, {"locked": True})["locked"])
        self.assertEqual(self.adb_state()["calls"], [])

    def test_foreground_with_attached_usb_lan_and_mdns_only(self):
        phone = self.phone(serialno="example-hardware")
        for serial in ("example-usb", "192.0.2.50:12345", "example._adb-tls-connect._tcp"):
            with self.subTest(serial=serial):
                self.set_adb(devices={serial: "device"}, props={serial: {"ro.serialno": "example-hardware"}},
                             wifi={serial: "192.0.2.50"})
                original = {"foreground": False, "locked": False}
                state = sdgen.foreground_before_work(phone, original)
                self.assertEqual(state, {"foreground": True, "locked": False})
                self.assertFalse(original["foreground"])
                calls = self.adb_state()["calls"]
                wake = ["-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"]
                launch = ["-s", serial, "shell", "am", "start", "-a", "android.intent.action.MAIN",
                          "-c", "android.intent.category.LAUNCHER", "-n", sdgen.PKG + "/.MainActivity"]
                self.assertLess(calls.index(wake), calls.index(launch))
                self.assertEqual(calls[-1], launch)
                self.assertFalse(any(word in call for call in calls
                                     for word in ("connect", "pair", "forward", "force-stop", "dismiss-keyguard")))

    def test_keyguard_appearing_after_wake_skips_launch(self):
        phone = self.phone()
        self.set_adb(devices={"192.0.2.50:12345": "device"})
        raw = sdgen._adb_raw

        def command(*args, **kwargs):
            output = raw(*args, **kwargs)
            if "KEYCODE_WAKEUP" in args:
                self._adb_state["window"] = "mKeyguardShowing=true"
            return output

        with patch.object(sdgen, "_adb_raw", side_effect=command):
            self.assertTrue(sdgen.foreground_before_work(phone, {})["locked"])
        self.assertFalse(any("am" in call for call in self.adb_state()["calls"]))

    def test_failed_launch_or_adb_keeps_advisory(self):
        phone = self.phone()
        for fields in ({"foreground_output": "Error type 3"}, {"foreground_code": 1}):
            self.set_adb(devices={"192.0.2.50:12345": "device"}, **fields)
            self.assertFalse(sdgen.foreground_before_work(phone, {"foreground": False})["foreground"])
        with patch.object(sdgen, "_adb_raw", side_effect=sdgen.PhoneError("timeout")):
            self.assertFalse(sdgen.foreground_before_work(phone, {"foreground": False})["foreground"])

    def test_foreground_precedes_select_and_removes_cli_warning(self):
        fake = self.fake()
        self.record()
        fake.app_state = {"foreground": False, "locked": False}
        self.set_adb(devices={"127.0.0.1:12345": "device"})
        raw, select = sdgen._adb_raw, sdgen.Api.select

        def command(*args, **kwargs):
            output = raw(*args, **kwargs)
            if ".MainActivity" in " ".join(args):
                fake.app_state = {"foreground": True, "locked": False}
            return output

        def selecting(client, *args):
            self.assertTrue(any(".MainActivity" in " ".join(c) for c in self.adb_state()["calls"]))
            return select(client, *args)

        with patch.object(sdgen, "_adb_raw", side_effect=command), patch.object(sdgen.Api, "select", selecting):
            status, data = self.cli("gen", "garden")
        self.assertEqual(status, 0)
        self.assertEqual(data["warnings"], [])
        self.assertEqual(len(fake.posts("/generate")), 1)

    def test_cli_advisories_on_success_and_failure_never_block(self):
        fake = self.fake()
        self.record()
        fake.app_state = {"foreground": False, "locked": True}
        for mode, expected in (("complete", 0), ("error", 1)):
            fake.mode = mode
            status, data = self.cli("gen", "garden")
            self.assertEqual(status, expected)
            self.assertEqual(data["warnings"], list(sdgen.APP_WARNINGS.values()))
        self.assertEqual(len(fake.posts("/generate")), 2)
        self.assertFalse(any("shell" in call for call in self.adb_state()["calls"]))

    def test_official_build_without_adb_has_no_state_warning(self):
        self.fake()
        self.record()
        status, data = self.cli("gen", "garden")
        self.assertEqual(status, 0)
        self.assertEqual(data["warnings"], [])

    def test_reference_memory_warning_in_cli(self):
        fake = self.fake()
        fake.models = [model("dit")]
        self.record(models=fake.models)
        with patch.object(sdgen, "phone_memory", return_value=5000):
            _, meta = sdgen.generate("garden", model_id="dit", image_b64=result(fmt="png")["image"],
                                     warning=lambda _: None)
        self.assertEqual(len(meta["warnings"]), 1)
        self.assertIn("5.7 GB", meta["warnings"][0])


if __name__ == "__main__":
    unittest.main()
