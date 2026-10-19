"""Failure diagnosis uses only local fixtures and already attached adb transports."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import sdgen
from tests.test_backend import BackendCase
from tests.fakeld import model


START = datetime(2010, 1, 2, 19, 40, tzinfo=timezone.utc).timestamp()
APPS = """Total PSS by process:
    900,000K: io.github.xororz.localdream (pid 100)
    800,000K: system_server (pid 101)
    700,000K: com.android.systemui (pid 102)
    600,000K: android.example (pid 103)
    512,000K: com.example.browser (pid 104)
    256,000K: com.example.video (pid 105)
    128,000K: com.example.chat:service (pid 106)
    128,000K: com.example.chat (pid 107)
    100,000K: com.example.four (pid 108)
    90,000K: com.example.five (pid 109)
    80,000K: com.example.six (pid 110)
    70,000K: surfaceflinger (pid 111)

Total PSS by OOM adjustment:
    999,000K: com.example.wrong (pid 112)
"""


def exit_info(reason="LOW_MEMORY", at="2010-01-02 19:44:00.123"):
    # Android exit records include both numeric reasons and human-readable labels.
    number, label = {"SIGNALED": (2, "SIGNALED"), "LOW_MEMORY": (3, "LOW_MEMORY"), "CRASH": (4, "APP CRASH(EXCEPTION)"),
                     "CRASH_NATIVE": (5, "APP CRASH(NATIVE)"), "ANR": (6, "ANR"),
                     "USER_REQUESTED": (10, "USER REQUESTED")}[reason]
    return (f"ApplicationExitInfo #0:\n  timestamp={at} pid=1 realUid=1 user=0\n"
            f"  process=x reason={number} ({label}) subreason=0 (UNKNOWN) status=0\n  importance=125\n")


class DiagnosisTests(BackendCase):
    def record(self, **values):
        # Diagnosis consumes this snapshot only; unrelated registry disk writes
        # would slow every rule/fixture case without exercising more behavior.
        return {"id": "example", "name": "Example phone", "transport": "lan", "host": "127.0.0.1",
                "models": [model()], "use_img2img": True, **values}

    def attached(self, **fields):
        self.set_adb(devices={"192.0.2.50:12345": "device"}, exit_info=fields.get("exit_info", exit_info()),
                     phone_time=START + 300,
                     meminfo=fields.get("meminfo", "MemAvailable: 1024000 kB\n"), memory_apps=APPS)
        return self.record(host="192.0.2.50", models=[model("dit")])

    def diagnose(self, phone, code="app_closed", **fields):
        request = {"started": START, "phone_started": START, "model_id": "dit", "width": 1024, "height": 1024, "steps": 9}
        request.update(fields)
        return sdgen.diagnose_failure(phone, request, sdgen.PhoneError(code))

    def test_low_memory_reference_and_read_only_probes(self):
        phone = self.attached()
        result = self.diagnose(phone, image="example-reference", mask="example-mask")
        self.assertEqual(result["cause"], "out_of_memory")
        self.assertIn("reference_uses_more_memory", result["evidence"])
        self.assertTrue({"retry", "retry_without_reference", "retry_smaller", "close_apps", "reopen_host_mode", "use_other_model"} <= set(result["actions"]))
        self.assertEqual(result["smaller"], {"width": 768, "height": 768})
        self.assertEqual(result["apps"][0], {"package": "com.example.browser", "mb": 500})
        self.assertEqual(len(result["apps"]), 5)
        self.assertEqual(result["apps"][1]["mb"], 250)
        self.assertTrue(result["evidence"][-1]["mask"])
        for call in self.adb_state()["calls"]:
            self.assertTrue(call == ["devices"] or call[:3] == ["-s", "192.0.2.50:12345", "shell"], call)
        without = self.diagnose(phone)
        self.assertNotIn("retry_without_reference", without["actions"])

    def test_each_cause(self):
        for reason, error, expected in (("CRASH_NATIVE", "app_closed", "native_crash"),
                                        ("CRASH", "stream_broken", "app_closed"),
                                        ("ANR", "stream_broken", "app_closed"),
                                        ("USER_REQUESTED", "stream_broken", "app_closed"),
                                        ("SIGNALED", "stream_broken", "app_closed"),
                                        (None, "no_host_mode", "host_mode_off"),
                                        (None, "app_closed", "host_mode_off"),
                                        (None, "unreachable", "network"),
                                        (None, "connection_refused", "network"),
                                        (None, "timeout", "network"),
                                        (None, "load_failed", "load_failed"),
                                        (None, "worker_stopped", "worker_stopped"),
                                        (None, "stream_broken", "unknown")):
            with self.subTest(reason=reason, error=error):
                phone = self.attached(exit_info=exit_info(reason) if reason else "", meminfo="MemAvailable: 6000000 kB")
                result = self.diagnose(phone, error)
                self.assertEqual(result["cause"], expected)
                if expected == "network":
                    self.assertEqual(result["actions"], ["check_wifi", "retry"])
                if expected == "host_mode_off":
                    self.assertIn("reopen_host_mode", result["actions"])

    def test_closed_and_low_memory_without_exit_record(self):
        self.assertEqual(self.diagnose(self.attached(exit_info=""))["cause"], "out_of_memory")
        phone = self.attached(exit_info="", meminfo="MemAvailable: 5120000 kB")
        self.assertEqual(self.diagnose(phone, image="ref")["cause"], "out_of_memory")
        self.assertEqual(self.diagnose(phone)["cause"], "host_mode_off")

    def test_simultaneous_causes_union_and_stable_action_order(self):
        phone = self.attached()
        result = self.diagnose(phone, image="ref", app_state={"foreground": False, "locked": True})
        self.assertEqual(result["cause"], "out_of_memory")
        self.assertEqual(result["causes"], ["out_of_memory", "app_not_foreground"])
        self.assertEqual(result["actions"], ["retry_without_reference", "retry_smaller", "recommend_foreground",
                                           "recommend_unlock", "close_apps", "reopen_host_mode", "use_other_model", "retry"])
        phone = self.attached(exit_info=exit_info("CRASH_NATIVE"))
        result = self.diagnose(phone)
        self.assertEqual(result["cause"], "native_crash")
        self.assertEqual(set(result["causes"]), {"out_of_memory", "native_crash"})

    def test_usb_public_storage_does_not_hide_network_actions(self):
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "hardware"}},
                     directories=["/sdcard/Download/LocalDream/models/dit"])
        phone = self.record(transport="usb", serialno="hardware", models=[model("dit")])
        result = self.diagnose(phone, "unreachable")
        self.assertEqual(result["causes"], ["models_in_public_storage", "network"])
        self.assertEqual(result["actions"], ["move_to_app_storage", "check_wifi", "retry"])

    def test_phone_clock_window_ignores_pc_clock_and_timezone(self):
        phone = self.attached()
        # Android's formatted exit is in its own timezone, independent of the PC.
        self._adb_state.update(phone_zone="+0900", exit_info=exit_info(at="2010-01-03 04:44:00.123"))
        for pc_clock in (START - 86400, START + 86400):
            with patch.object(sdgen.time, "time", return_value=pc_clock):
                self.assertEqual(self.diagnose(phone, "stream_broken")["cause"], "out_of_memory")
        for stamp in (START - 10, START + 600):
            self._adb_state["exit_info"] = f"ApplicationExitInfo timestamp={int(stamp * 1000)} reason=3 (LOW_MEMORY)"
            self.assertEqual(self.diagnose(phone, "stream_broken")["cause"], "unknown")

    def test_missing_phone_clock_uses_only_anchored_duration_window(self):
        phone = self.attached()
        self._adb_state["exit_info"] = f"ApplicationExitInfo timestamp={int((START + 240) * 1000)} reason=3 (LOW_MEMORY)"
        with patch.object(sdgen.time, "monotonic", return_value=1000):
            # Missing start reading: bound the window from phone now and job duration.
            result = self.diagnose(phone, "stream_broken", phone_started=None, started_monotonic=700)
            self.assertEqual(result["cause"], "out_of_memory")
            # Missing end reading: use phone start and job duration.
            self._adb_state["phone_time"] = ""
            result = self.diagnose(phone, "stream_broken", started_monotonic=700)
            self.assertEqual(result["cause"], "out_of_memory")
            # No phone clock anchor: no attribution to a historical exit.
            result = self.diagnose(phone, "stream_broken", phone_started=None, started_monotonic=700)
            self.assertEqual(result["cause"], "unknown")
            # A duration too short cannot include the exit.
            self._adb_state["phone_time"] = START + 300
            result = self.diagnose(phone, "stream_broken", phone_started=None, started_monotonic=990)
            self.assertEqual(result["cause"], "unknown")

    def test_start_clock_uses_matching_attachment_and_shared_budget(self):
        phone = self.attached()
        self.assertEqual(sdgen.phone_job_clock(phone), START + 300)
        self.assertEqual(self.adb_state()["calls"], [["devices"], ["-s", "192.0.2.50:12345", "shell", "date", "+%s"]])
        self.set_adb(devices={"192.0.2.51:12345": "device"})
        self.assertIsNone(sdgen.phone_job_clock(phone))
        self.assertEqual(self.adb_state()["calls"], [["devices"]])
        clock = [0]

        def probe(*args, timeout):
            self.assertEqual(timeout, 5)
            clock[0] += 5
            return "192.0.2.50:12345 device"

        with patch.object(sdgen.time, "monotonic", side_effect=lambda: clock[0]), patch.object(sdgen, "_adb_raw", side_effect=probe):
            self.assertIsNone(sdgen.phone_job_clock(phone))

    def test_newest_record_and_stale_record(self):
        # Text order is not relied upon; only the newest timestamp may be used.
        text = exit_info("LOW_MEMORY", "2010-01-02 19:20:00") + exit_info("CRASH_NATIVE")
        result = self.diagnose(self.attached(exit_info=text), "stream_broken")
        self.assertEqual(result["cause"], "native_crash")
        self.assertGreater(result["evidence"][0]["timestamp"], START)
        for at in ("2010-01-02 19:20:00", "2010-01-02 19:40:00"):
            result = self.diagnose(self.attached(exit_info=exit_info(at=at)), "stream_broken")
            self.assertEqual(result["cause"], "unknown")
            self.assertFalse(any(isinstance(e, dict) and e["code"] == "exit_reason" for e in result["evidence"]))
        self.assertIsNone(sdgen._latest_exit("ApplicationExitInfo timestamp=invalid reason=3 (LOW_MEMORY)", START))

    def test_no_attachment_or_wrong_phone_never_connects(self):
        for state in ("offline", "unauthorized", "device"):
            self.set_adb(devices={"192.0.2.51:12345": state})
            result = self.diagnose(self.record(host="192.0.2.50", models=[model("dit")]), "load_failed", image="ref")
            self.assertEqual(result["cause"], "load_failed")
            self.assertIn("adb_unavailable", result["evidence"])
            self.assertIn("retry_without_reference", result["actions"])
            self.assertEqual(self.adb_state()["calls"], [["devices"]])
        with patch.object(sdgen, "adb_path", side_effect=sdgen.PhoneError("no_adb")):
            result = self.diagnose(self.record(), "timeout")
            self.assertEqual(result["cause"], "network")
            self.assertIn("adb_unavailable", result["evidence"])

    def test_attached_usb_and_mdns_identity(self):
        self.set_adb(devices={"example-usb": "device"}, props={"example-usb": {"ro.serialno": "example-hardware"}}, exit_info=exit_info(), phone_time=START + 300)
        result = self.diagnose(self.record(transport="usb", serialno="example-hardware"))
        self.assertEqual(result["cause"], "out_of_memory")
        self.set_adb(devices={"example._adb-tls-connect._tcp": "device"}, wifi={"example._adb-tls-connect._tcp": "192.0.2.50"}, exit_info=exit_info(), phone_time=START + 300)
        result = self.diagnose(self.record(host="192.0.2.50"))
        self.assertEqual(result["cause"], "out_of_memory")
        self.assertFalse(any("connect" in call or "forward" in call or "pair" in call for call in self.adb_state()["calls"]))

    def test_apps_rss_and_sections(self):
        apps = sdgen._memory_apps(APPS.replace("Total PSS", "Total RSS"))
        self.assertEqual(len(apps), 5)
        self.assertEqual(apps[1]["package"], "com.example.video")
        self.assertEqual(apps[2], {"package": "com.example.chat", "mb": 250})
        self.assertEqual(sdgen._memory_apps("no supported memory section"), [])

    def test_smaller_per_family_and_boundaries(self):
        cases = [("sd15", 512, 768, "1:1", {"width": 512, "height": 512}),
                 ("sd15cpu", 512, 512, "1:1", {"width": 384, "height": 384}),
                 ("dit", 896, 1152, "1:1", {"width": 640, "height": 832}),
                 ("dit", 512, 1024, "1:1", {"width": 512, "height": 768}),
                 ("sdxl", 1024, 1024, "3:4", {"width": 1024, "height": 1024, "aspect_ratio": "1:1"}),
                 ("anima", 1024, 1024, "4:3", {"width": 1024, "height": 1024, "aspect_ratio": "1:1"}),
                 ("sd15", 512, 512, "1:1", None), ("sd15cpu", 128, 128, "1:1", None),
                 ("dit", 512, 512, "1:1", None), ("sdxl", 1024, 1024, "1:1", None)]
        for family, w, h, aspect, expected in cases:
            with self.subTest(family=family, size=(w, h)):
                caps = sdgen.capabilities(model(family), True)
                request = {"width": w, "height": h, "aspect_ratio": aspect}
                actual = sdgen.smaller_request(request, caps)
                self.assertEqual(actual, expected)
                if actual:
                    sdgen.validate(model(family), actual["width"], actual["height"], actual.get("aspect_ratio"), 4, 5, .7, None)
                result = self.diagnose(self.record(models=[model(family)]), "load_failed", model_id=family, **request)
                self.assertEqual("retry_smaller" in result["actions"], expected is not None)

    def test_budget_shared_by_all_commands(self):
        clock = [10.0]
        timeouts = []

        def command(*args, timeout):
            timeouts.append(timeout)
            if args == ("devices",):
                clock[0] += 1
                return "192.0.2.50:12345 device"
            if "test" in args:
                raise sdgen.PhoneError("no_adb")
            if "+%s" in args:
                return str(START + 300)
            if "+%z" in args:
                return "+0000"
            if "exit-info" in args:
                clock[0] += 1
                return exit_info()
            clock[0] += timeout
            raise sdgen.PhoneError("timeout")

        with patch.object(sdgen.time, "monotonic", side_effect=lambda: clock[0]), patch.object(sdgen, "_adb_raw", side_effect=command):
            result = self.diagnose(self.record(host="192.0.2.50", models=[model("dit")]))
        self.assertEqual(result["cause"], "out_of_memory")
        self.assertEqual(timeouts, [5, 4, 4, 4, 4, 3])
        self.assertEqual(clock[0], 15)


    def test_background_at_start_and_unlock_actions(self):
        phone = self.record(models=[model("dit")])
        result = self.diagnose(phone, "stream_broken", app_state={"foreground": False, "locked": True})
        self.assertEqual(result["cause"], "app_not_foreground")
        self.assertIn("app_not_foreground", result["evidence"])
        self.assertIn("phone_locked", result["evidence"])
        self.assertEqual(result["actions"], ["recommend_foreground", "recommend_unlock", "retry"])
        for state in ({}, {"foreground": True}, None):
            self.assertEqual(self.diagnose(phone, "load_failed", app_state=state)["cause"], "load_failed")

    def test_public_storage_for_large_models_and_safe_path(self):
        for family in ("dit", "sdxl", "anima", "sd15"):
            with self.subTest(family=family):
                path = "/sdcard/Download/LocalDream/models/" + family
                self.set_adb(devices={"192.0.2.50:12345": "device"}, directories=[path],
                             exit_info=exit_info(), phone_time=START + 300, meminfo="MemAvailable: 6144000 kB")
                phone = self.record(host="192.0.2.50", models=[model(family)])
                result = self.diagnose(phone, model_id=family, app_state={"foreground": False})
                large = family in ("dit", "sdxl")
                self.assertEqual(result["cause"], "models_in_public_storage" if large else "out_of_memory")
                self.assertEqual("move_to_app_storage" in result["actions"], large)
                self.assertIn("recommend_foreground", result["actions"])
                self.assertEqual("models_in_public_storage" in result["evidence"], large)
                self.assertTrue(any(isinstance(e, dict) and e["code"] == "exit_reason" for e in result["evidence"]))
        for model_id in ("../escape", "bad;command", "bad name", ".", ".."):
            self.set_adb(devices={"192.0.2.50:12345": "device"})
            result = self.diagnose(self.record(host="192.0.2.50"), model_id=model_id,
                                   capabilities=sdgen.capabilities(model("dit"), True))
            self.assertNotEqual(result["cause"], "models_in_public_storage")
            self.assertFalse(any("test" in call for call in self.adb_state()["calls"]))


if __name__ == "__main__":
    unittest.main()
