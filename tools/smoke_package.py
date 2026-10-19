"""Exercise the actual frozen GUI, HTTP resources, quit, and console CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paths import APP_ID, __version__

def smoke(package: Path) -> None:
    suffix = ".exe" if sys.platform == "win32" else ""
    gui, cli = (package / (name + suffix) for name in ("Local Dream Desktop", "ldd"))
    for executable in (gui, cli):
        if not executable.is_file():
            raise RuntimeError(f"Packaged executable missing: {executable.name}")
    with socket.socket() as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Isolate the smoke from any desktop instance already in use.
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    scratch = ROOT / ".test-tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="package-smoke-", dir=scratch) as name:
        tmp = Path(name)
        config = tmp / "smoke.json"
        config.write_text(json.dumps({"out": str(tmp / "output")}), encoding="utf-8")
        env = os.environ.copy()
        env.update(LDPC_CONFIG=str(config), LDPC_NO_ADB="1",
                   LDPC_NO_BROWSER="1", LDPC_NO_DIALOG="1", LDPC_PORT=str(port))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        process = subprocess.Popen([str(gui), "--no-browser"], env=env, cwd=tmp)
        ready = False
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Packaged GUI exited before readiness: {process.returncode}")
                try:
                    with opener.open(url + "/api/status", timeout=1) as response:
                        status = json.load(response)
                    if status.get("app") != APP_ID or status.get("version") != __version__:
                        raise RuntimeError("Another service answered on the application port")
                    assert status["capabilities"]["quit"] is True
                    ready = True
                    break
                except (OSError, ValueError):
                    time.sleep(0.1)
            if not ready:
                raise RuntimeError("Packaged app did not become ready within 45 seconds")
            for path, marker in (("/", b"<!doctype html"), ("/vendor/qrcode.js", b"QRCode")):
                with opener.open(url + path, timeout=5) as response:
                    assert response.status == 200 and marker.lower() in response.read().lower(), path
            # Relaunch must reuse the live instance without a second listener.
            again = subprocess.run([str(gui), "--no-browser"], cwd=tmp, env=env, timeout=15)
            assert again.returncode == 0 and process.poll() is None
            request = urllib.request.Request(url + "/api/quit", data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
            with opener.open(request, timeout=5) as response:
                assert response.status == 202 and json.load(response)["quitting"]
            assert process.wait(timeout=15) == 0
            result = subprocess.run([str(cli), "--cli", "devices", "--json"], cwd=tmp,
                                    env=env, capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, "Packaged CLI failed"
            payload = json.loads(result.stdout)
            assert payload["ok"] is True and isinstance(payload["phones"], list)
            failed = subprocess.run([str(cli), "--cli", "unknown-command", "--json"], cwd=tmp,
                                    env=env, capture_output=True, text=True, timeout=20)
            assert failed.returncode == 1 and json.loads(failed.stdout)["ok"] is False
            print("Packaged smoke PASS: status, page, vendor, reopen, quit/exit, CLI JSON and exit codes")
        finally:
            if process.poll() is None:
                # Only terminate the exact child created above, never another listener.
                process.terminate()
                process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    smoke(args.package.resolve())


if __name__ == "__main__":
    main()
