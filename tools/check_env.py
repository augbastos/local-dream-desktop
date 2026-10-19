"""Check Local Dream Desktop requirements; stdlib only, no phone connections."""
import argparse
import importlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paths import APP_ID, __version__, output_dir, server_port


def find_adb():
    """Use the same discovery order as sdgen's CLI contract."""
    executable = "adb.exe" if os.name == "nt" else "adb"
    candidates = []
    override = os.environ.get("LDPC_ADB")
    if override:
        candidates.append(override)
    on_path = shutil.which("adb")
    if on_path:
        candidates.append(on_path)
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(variable)
        if root:
            candidates.append(str(Path(root) / "platform-tools" / executable))
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    defaults = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) if local else home / "AppData" / "Local" if home else None
        if root:
            defaults = [root / "Android" / "Sdk"]
    elif home and sys.platform == "darwin":
        defaults = [home / "Library" / "Android" / "sdk"]
    elif home:
        defaults = [home / "Android" / "Sdk", home / "Android" / "sdk"]
    candidates.extend(str(root / "platform-tools" / executable) for root in defaults)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def port_status():
    port = server_port()
    try:
        with socket.socket() as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", port))
        return "free"
    except OSError:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
                data = json.loads(response.read(65537))
            if isinstance(data, dict):
                contract = data.get("app") == APP_ID and data.get("version") == __version__
                if contract:
                    return "studio"
        except (OSError, ValueError, http.client.HTTPException):
            pass
        return "unavailable"


def check():
    checks = []
    supported = sys.version_info >= (3, 10)
    checks.append({"name": "python", "ok": supported,
                   "detail": "Python {} (requires 3.10+)".format(sys.version.split()[0])})
    try:
        importlib.import_module("PIL.Image")
        pillow = True
    except Exception:
        pillow = False
    checks.append({"name": "pillow", "ok": pillow,
                   "detail": "Pillow available" if pillow else "Pillow missing or broken. Run: " + (
                       "py -m pip install -r requirements.txt" if os.name == "nt" else
                       "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")})
    adb = find_adb()
    checks.append({"name": "adb", "ok": True,
                   "detail": "adb found" if adb else "adb not found (needed only for USB and for opening host mode from the desktop)"})
    port = port_status()
    checks.append({"name": "port", "ok": port != "unavailable",
                   "detail": {"free": f"Port {server_port()} free", "studio": f"Port {server_port()} already serves Local Dream Desktop",
                              "unavailable": f"Port {server_port()} unavailable or occupied by another application. Log file: {output_dir() / 'studio.log'}"}[port]})
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def launch():
    """Select the checked interpreter's windowless sibling where available."""
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if not windowless.is_file():
        windowless = executable
    subprocess.Popen([str(windowless), str(Path(__file__).resolve().parents[1] / "local_dream_desktop.py")],
                     cwd=str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print one JSON report")
    parser.add_argument("--report-file", type=Path, help="Save the report instead of printing it (for pythonw launchers)")
    parser.add_argument("--launch", action="store_true", help="Launch Local Dream Desktop with this Python after checking")
    args = parser.parse_args()
    report = check()
    output = json.dumps(report) if args.json else "\n".join(
        ("OK: " if item["ok"] else "ERROR: ") + item["detail"] for item in report["checks"])
    if args.report_file:
        args.report_file.write_text(output + "\n", encoding="utf-8")
    if sys.stdout is not None and not args.report_file and (not args.launch or not report["ok"]):
        print(output)
    if not report["ok"]:
        return 1
    if args.launch:
        try:
            launch()
        except OSError:
            if sys.stdout is not None:
                print("ERROR: Could not launch Local Dream Desktop (local_dream_desktop.py)")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
