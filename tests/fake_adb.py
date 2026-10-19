"""Fake adb executable selected by LDPC_ADB, controlled by LDPC_FAKE_ADB_STATE."""
import json
import os
import sys
from pathlib import Path


def run(args, state):
    """Shared command behavior for the executable and in-process unit fixture."""
    state.setdefault("calls", []).append(args)
    serial = None
    if args[:1] == ["-s"]:
        serial, args = args[1], args[2:]
    output, code = "", 0
    if args == ["devices"]:
        output = "List of devices attached\n" + "\n".join(f"{s}\t{v}" for s, v in state.get("devices", {}).items())
    elif args == ["forward", "--list"]:
        output = "\n".join(" ".join(row) for row in state.get("forwards", []))
    elif args[:1] == ["forward"]:
        rows = state.setdefault("forwards", [])
        if args[1] == "--remove":
            state["forwards"] = [r for r in rows if not (r[0] == serial and r[1] == args[2])]
        else:
            local, remote = args[-2:]
            if any(r[1] == local for r in rows) and "--no-rebind" in args:
                code = 1
            else:
                rows.append([serial, local, remote])
    elif args[:3] == ["shell", "test", "-d"]:
        code = 0 if args[3] in state.get("directories", []) else 1
    elif args == ["shell", "date", "+%s"]:
        times = state.get("phone_times")
        output = str(times.pop(0) if times else state.get("phone_time", ""))
    elif args == ["shell", "date", "+%z"]:
        output = state.get("phone_zone", "+0000")
    elif args == ["shell", "am", "start", "-a", "android.intent.action.MAIN", "-c",
                  "android.intent.category.LAUNCHER", "-n", "io.github.xororz.localdream/.MainActivity"]:
        output = state.get("foreground_output", "Starting: Intent")
        code = state.get("foreground_code", 0)
    elif args[:2] == ["shell", "getprop"]:
        output = state.get("props", {}).get(serial, {}).get(args[2], "")
    elif args == ["shell", "dumpsys", "activity", "exit-info", "io.github.xororz.localdream"]:
        output = state.get("exit_info", "")
    elif args == ["shell", "dumpsys", "meminfo"]:
        output = state.get("memory_apps", "")
    elif args[:2] == ["shell", "dumpsys"]:
        output = state.get(args[2], "User \"Example\" (current): deviceLocked=0, deviceSecure=0")
    elif args[:2] == ["shell", "ip -4 -o addr show wlan0"]:
        ip = state.get("wifi", {}).get(serial)
        output = f"30: wlan0    inet {ip}/24 brd 192.0.2.255 scope global wlan0" if ip else ""
    elif args == ["shell", "am", "start", "-n", "io.github.xororz.localdream/.remote.HostModeCommandActivity",
                  "-a", "io.github.xororz.localdream.action.HOST_MODE_START"]:
        output = state.get("host_command", "Error type 3: Activity does not exist")
        code = state.get("host_command_code", 0)
    elif args == ["shell", "cat", "/proc/meminfo"]:
        output = state.get("meminfo", "")
    elif args[:2] == ["shell", "cat"]:
        output = '<hierarchy><node text="More" bounds="[0,0][20,20]"/></hierarchy>'
    elif args[:2] == ["shell", "uiautomator"]:
        output = "UI hierarchy dumped"
    elif args[:1] == ["pair"]:
        output = state.get("pair_output", "Successfully paired")
    elif args[:1] == ["connect"]:
        target = args[1]
        responses = state.get("connect_results", {}).get(target)
        output = responses.pop(0) if responses else f"connected to {target}"
        if "connected" in output.lower() and "failed" not in output.lower():
            state.setdefault("devices", {})[target] = "device"
            state.setdefault("props", {})[target] = {"ro.serialno": "example-hardware", "ro.product.model": "Example phone"}
    elif args == ["mdns", "services"]:
        output = state.get("mdns", "")
    elif args[:1] != ["shell"]:
        code = 1
    return output, code


def main():
    path = Path(os.environ["LDPC_FAKE_ADB_STATE"])
    state = json.loads(path.read_text(encoding="utf-8"))
    output, code = run(sys.argv[1:], state)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
