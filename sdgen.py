"""Local Dream Desktop: generate images on your phone over LAN or USB (stdlib + Pillow).

    python sdgen.py devices --json
    python sdgen.py gen "A quiet garden" --phone example --seed 7
    python sdgen.py stop --phone example

Settings live in local.json (or LDPC_CONFIG).
"""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

PKG = "io.github.xororz.localdream"
GEN_PORT, CONTROL_PORT = 8081, 8808


def load_config(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("configuration must be an object")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A bad local config must not prevent stop or produce a traceback on import.
        return {}


CFG = load_config(Path(os.environ.get("LDPC_CONFIG") or Path(__file__).with_name("local.json")))
OUT = Path(os.path.expandvars(os.path.expanduser(CFG.get("out") or "~/LocalDreamDesktop")))
PHONES = OUT / "phones.json"
NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
DIT_SIZES = [[1024, 1024], [896, 1152], [1152, 896], [768, 1344], [1344, 768], [1536, 1536]]
CONTROL_TIMEOUT, HEALTH_TIMEOUT, STREAM_TIMEOUT, LOAD_TIMEOUT = 8, 3, 900, 240
_serials: dict[str, str] = {}
_hardware_serials: dict[str, str] = {}
_registry_mutex = threading.RLock()


class PhoneError(Exception):
    """Stable code and readable detail shared by the CLI and browser."""
    def __init__(self, code: str, detail: str | None = None):
        self.code, self.detail = code, detail or code
        super().__init__(self.detail)


def _noop(*_):
    pass


# --- files and phones on record ------------------------------------------------------------------
def inside_out(path: Path) -> Path:
    root, target = OUT.resolve(), path.resolve()
    if root not in target.parents:
        raise PhoneError("bad_param", "path outside output directory")
    return target


@contextmanager
def file_lock(name: str, wait: float = 0):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise PhoneError("bad_param", "invalid phone id")
    path = inside_out(OUT / "locks" / f"{name}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    f, acquired = path.open("a+b"), False
    try:
        if f.seek(0, 2) == 0:
            f.write(b"\0")
            f.flush()
        end = time.monotonic() + wait
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= end:
                    raise PhoneError("busy") from None
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def phone_lock(phone: dict):
    return file_lock(phone["id"])


def _atomic_json(path: Path, value) -> None:
    path = inside_out(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".ldpc-", suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            json.dump(value, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def desktop_link(*, create=True) -> dict | None:
    """Read the shared computer identity, creating it only when requested."""
    if not create and not inside_out(OUT / "desktop-link.json").is_file():
        return None
    with _registry_mutex, file_lock("_desktop_link", wait=8):
        path = inside_out(OUT / "desktop-link.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or not isinstance(data.get("token"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", data["token"])
                    or not isinstance(data.get("name"), str)):
                raise ValueError
        except FileNotFoundError:
            if not create:
                return None
            data = {"token": secrets.token_hex(16), "name": socket.gethostname()}
            _atomic_json(path, data)
        except (OSError, ValueError):
            raise PhoneError("bad_param", "cannot read desktop link identity") from None
        return data


def pairing_payload() -> dict:
    data = desktop_link()
    return {"url": "localdream://pair?" + urllib.parse.urlencode({"t": data["token"], "n": data["name"]})}


def link_status(info: dict) -> dict:
    link = info.get("desktop_link") or {}
    return {k: link.get(k) is True for k in ("enabled", "auth_required", "token_ok")}


def app_features(info: dict) -> dict:
    """Optional extensions are reported by /info, never inferred from a version."""
    return {"desktop_link": isinstance(info.get("desktop_link"), dict),
            "app_state": isinstance(info.get("app_state"), dict)}


def pairing_proved(info: dict, nonce: str) -> bool:
    link = info.get("desktop_link")
    proofs = link.get("proofs", []) if isinstance(link, dict) else []
    if not isinstance(proofs, list) or not proofs:
        return False
    expected = hmac.new(desktop_link()["token"].encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return any(isinstance(proof, str) and re.fullmatch(r"[0-9a-f]{64}", proof)
               and hmac.compare_digest(expected, proof) for proof in proofs)


def registered_control(base: str) -> bool:
    """A service announcement alone must never grant access to the computer identity."""
    for phone in _read_registry()["phones"]:
        if phone.get("transport", "lan") == "lan" and phone.get("host"):
            if base == _host_url(phone["host"], phone.get("control_port", CONTROL_PORT)):
                return True
        elif phone.get("usb_ports") and base == f"http://127.0.0.1:{phone['usb_ports'][0]}":
            return True
    return False


def _read_registry() -> dict:
    try:
        data = json.loads(inside_out(PHONES).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("phones"), list):
            raise ValueError("invalid phone registry")
        return data
    except FileNotFoundError:
        return {"phones": []}
    except (OSError, ValueError) as e:
        raise PhoneError("bad_param", "cannot read phone registry") from e


def _migrate(lst: list[dict]) -> bool:
    changed = False
    for p in lst:
        if "transport" not in p:
            p["transport"] = "lan" if p.get("host") else "usb"
            changed = True
        if "adb_port" in p:
            del p["adb_port"]
            changed = True
        if "record_id" not in p:
            p["record_id"] = secrets.token_hex(16)
            changed = True
        if "catalog_revision" not in p:
            p["catalog_revision"] = 0
            changed = True
    return changed


@contextmanager
def _registry():
    with _registry_mutex, file_lock("_registry", wait=8):
        data = _read_registry()
        _migrate(data["phones"])
        yield data
        _atomic_json(PHONES, data)


def phones() -> list[dict]:
    with _registry_mutex:
        data = _read_registry()
        if _migrate(data["phones"]):
            with _registry() as current:
                return current["phones"]
        return data["phones"]


def save_phones(lst: list[dict]) -> None:
    with _registry() as data:
        _migrate(lst)
        previous = {p["id"]: p for p in data["phones"]}
        for p in lst:
            old = previous.get(p["id"])
            # A replacement invalidates snapshots even if its catalog is unchanged.
            p["record_id"] = old["record_id"] if old else secrets.token_hex(16)
            p["catalog_revision"] = old["catalog_revision"] + 1 if old else 1
        data["phones"] = lst


def get_phone(pid: str | None = None) -> dict:
    for p in phones():
        if pid is None or p["id"] == pid:
            return p
    raise PhoneError("unreachable", "phone not found; add a LAN or USB phone first")


def public_phone(phone: dict) -> dict:
    """Expose only phone fields used by the page and CLI, never registry bookkeeping."""
    return {k: phone[k] for k in ("id", "name", "transport", "host", "state", "features",
                                  "models", "use_img2img", "phone_id", "port", "desktop_link", "has_adb") if k in phone} | {
        "has_usb": bool(phone.get("serialno") or phone.get("has_usb")),
        "has_lan": bool(phone.get("host") or phone.get("has_lan"))}


def _update(pid: str, *, expected_catalog: tuple[str, int] | None = None, **fields) -> dict | None:
    with _registry() as data:
        for p in data["phones"]:
            if p["id"] == pid:
                if expected_catalog is not None and (p["record_id"], p["catalog_revision"]) != expected_catalog:
                    return None
                revision = p["catalog_revision"]
                p.update(fields)
                if "models" in fields or "use_img2img" in fields:
                    p["catalog_revision"] = revision + 1
                return p.copy()
    if expected_catalog is not None:
        return None
    raise PhoneError("unreachable", "phone not found")


def _new_id(name: str, lst: list[dict]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "phone"
    pid, n = stem, 2
    while any(p["id"] == pid for p in lst):
        pid, n = f"{stem}-{n}", n + 1
    return pid


def valid_host(host: str) -> str:
    if not isinstance(host, str) or not host or len(host) > 253:
        raise PhoneError("bad_host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", x) for x in host.split(".")):
            raise PhoneError("bad_host") from None
    return host


def _host_url(host: str, port: int) -> str:
    host = valid_host(host)
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


# --- shared HTTP client --------------------------------------------------------------------------
class _ControlRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise copies custom headers to redirected hosts. Keep the identity
        # on the requested control endpoint, while preserving generation redirects.
        if req.has_header("X-desktop-token") or req.has_header("X-desktop-challenge"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Api:
    def __init__(self, ctl_base: str, gen_base: str, *, trusted=None, challenge=None):
        self.ctl_base, self.gen_base = ctl_base.rstrip("/"), gen_base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _ControlRedirect())
        self._desktop_token = None
        self.trusted = registered_control(self.ctl_base) if trusted is None else trusted
        self.challenge = challenge

    def _open(self, base, path, body=None, timeout=CONTROL_TIMEOUT):
        headers = {"Content-Type": "application/json"}
        if base == self.ctl_base and self.trusted:
            if self._desktop_token is None:
                identity = desktop_link(create=False)
                self._desktop_token = identity["token"] if identity else None
            if self._desktop_token is not None:
                headers["X-Desktop-Token"] = self._desktop_token
        if base == self.ctl_base and path == "/info" and self.challenge:
            headers["X-Desktop-Challenge"] = self.challenge
        req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                     headers)
        try:
            return self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                error = json.load(e).get("error", e.reason)
                detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            except (ValueError, OSError):
                detail = str(e.reason)
            finally:
                e.close()
            code = ("not_paired" if e.code == 401 else "model_missing" if path == "/select" and e.code == 404
                    else "load_failed" if path == "/select" and e.code == 500
                    else "bad_param" if e.code == 400 else "stream_broken")
            if code == "not_paired":
                detail = "pair this computer with the phone"
            raise PhoneError(code, detail) from None
        except (TimeoutError, socket.timeout):
            raise PhoneError("timeout") from None
        except urllib.error.URLError as e:
            code = "timeout" if isinstance(e.reason, TimeoutError) else "unreachable"
            if getattr(e.reason, "errno", None) in (errno.ECONNREFUSED, 10061):
                code = "connection_refused"
            raise PhoneError(code) from None
        except OSError:
            raise PhoneError("unreachable") from None

    def _json(self, base, path, body=None, timeout=CONTROL_TIMEOUT):
        try:
            with self._open(base, path, body, timeout) as r:
                data = json.load(r)
                if not isinstance(data, dict):
                    raise ValueError("JSON object required")
                return data
        except (TimeoutError, socket.timeout):
            raise PhoneError("timeout") from None
        except (ValueError, OSError, http.client.HTTPException) as e:
            raise PhoneError("stream_broken", "invalid HTTP response") from e

    def info(self, timeout=CONTROL_TIMEOUT):
        info = self._json(self.ctl_base, "/info", timeout=timeout)
        if (self.trusted and self._desktop_token is None and info.get("app") == "localdream"
                and app_features(info)["desktop_link"]):
            self._desktop_token = desktop_link()["token"]
            return self._json(self.ctl_base, "/info", timeout=timeout)
        return info

    def models(self):
        return self._json(self.ctl_base, "/models")

    def select(self, model_id, width, height):
        return self._json(self.ctl_base, "/select", {"model_id": model_id, "width": width, "height": height})

    def status(self):
        return self._json(self.ctl_base, "/status")

    def stop(self, model_id):
        if not model_id:
            raise PhoneError("bad_param", "stop requires a model id")
        return self._json(self.ctl_base, "/stop", {"model_id": model_id})

    def health(self):
        try:
            with self._open(self.gen_base, "/health", timeout=HEALTH_TIMEOUT) as r:
                return r.status == 200
        except PhoneError:
            return False

    def tokenize(self, prompt):
        d = self._json(self.gen_base, "/tokenize", {"prompt": prompt})
        if not _number(d.get("count"), 0, 2147483647, True) or not _number(d.get("max_length"), 0, 2147483647, True):
            raise PhoneError("stream_broken", "invalid tokenizer response")
        return d["count"], d["max_length"]

    def generate(self, body: dict, progress=_noop):
        """A single POST: never replay a disconnected stream."""
        try:
            with self._open(self.gen_base, "/generate", body, STREAM_TIMEOUT) as r:
                event, lines = None, []

                def dispatch():
                    if not lines:
                        return None
                    d = json.loads("\n".join(lines))
                    kind = event or d.get("type")
                    if kind == "progress":
                        step, total = d.get("step"), d.get("total_steps")
                        if type(step) is int and type(total) is int and 0 <= step <= total and total > 0:
                            progress(step, total)
                    elif kind == "complete":
                        return d
                    elif kind == "error":
                        raise PhoneError("stream_broken", str(d.get("message", "generation failed")))
                    return None

                for raw in r:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if not line:
                        result = dispatch()
                        if result is not None:
                            return result
                        event, lines = None, []
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                result = dispatch()
                if result is not None:
                    return result
        except (TimeoutError, socket.timeout):
            raise PhoneError("timeout") from None
        except (OSError, ValueError, KeyError, UnicodeError, http.client.IncompleteRead) as e:
            raise PhoneError("stream_broken", "generation stream is incomplete or invalid") from e
        raise PhoneError("stream_broken", "stream ended without a result")


# --- adb and transports --------------------------------------------------------------------------
def adb_path() -> str:
    if os.environ.get("LDPC_NO_ADB") == "1":
        raise PhoneError("no_adb")
    override = os.environ.get("LDPC_ADB")
    if override and Path(override).is_file():
        return override
    found = shutil.which("adb")
    if found:
        return found
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    if sys.platform == "win32":
        roots.append(str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Android" / "Sdk"))
    elif sys.platform == "darwin":
        roots.append(str(Path.home() / "Library" / "Android" / "sdk"))
    else:
        roots.append(str(Path.home() / "Android" / "Sdk"))
    for root in filter(None, roots):
        p = Path(root) / "platform-tools" / ("adb.exe" if sys.platform == "win32" else "adb")
        if p.is_file():
            return str(p)
    raise PhoneError("no_adb")


def _adb_raw(*args: str, timeout=15) -> str:
    path = adb_path()
    command = [sys.executable, path] if path.endswith(".py") else [path]
    try:
        r = subprocess.run([*command, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NOWIN, timeout=timeout)
    except FileNotFoundError:
        raise PhoneError("no_adb") from None
    except subprocess.TimeoutExpired:
        raise PhoneError("timeout", "adb timed out") from None
    if r.returncode:
        if (any("/.remote.HostModeCommandActivity" in arg for arg in args)
                and re.search(r"does not exist|Permission Denial|Error type 3", r.stdout + r.stderr, re.I)):
            raise PhoneError("host_command_unavailable")
        raise PhoneError("no_adb", "adb command failed")
    return r.stdout


def _devices() -> dict[str, str]:
    try:
        return {p[0]: p[1] for line in _adb_raw("devices").splitlines()
                if len(p := line.split()) >= 2 and p[1] in ("device", "unauthorized", "offline")}
    except PhoneError as e:
        if e.code == "no_adb":
            return {}
        raise


def _is_usb(serial: str) -> bool:
    return ":" not in serial and "._adb" not in serial


def _prop(serial: str, key: str) -> str:
    return _adb_raw("-s", serial, "shell", "getprop", key).strip()


def _wifi_ip(serial: str) -> str | None:
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", _adb_raw("-s", serial, "shell", "ip -4 -o addr show wlan0"))
    return m.group(1) if m else None


def _serialno(serial: str) -> str:
    if serial not in _hardware_serials:
        value = _prop(serial, "ro.serialno")
        if value:
            _hardware_serials[serial] = value
        return value
    return _hardware_serials[serial]


def _usb_serial(phone: dict, devices: dict | None = None) -> str:
    devs = _devices() if devices is None else devices
    for serial, state in devs.items():
        if _is_usb(serial) and state == "device" and _serialno(serial) == phone.get("serialno"):
            _serials[phone["id"]] = serial
            return serial
    if any(_is_usb(s) and state == "unauthorized" for s, state in devs.items()):
        raise PhoneError("usb_unauthorized")
    raise PhoneError("usb_unplugged")


def _forwards() -> list[tuple[str, str, str]]:
    return [tuple(p) for line in _adb_raw("forward", "--list").splitlines() if len(p := line.split()) == 3]


def _bindable(port: int) -> bool:
    try:
        with socket.socket() as s:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _usb_endpoints(phone: dict, serial: str) -> tuple[str, str]:
    # Allocate under the registry lock: UI and CLI cannot reserve the same ports.
    with _registry() as data:
        p = next((x for x in data["phones"] if x["id"] == phone["id"]), phone)
        forwards = _forwards()
        used = {n for x in data["phones"] if x["id"] != p["id"] for n in x.get("usb_ports", [])}

        def free(port, remote, owned=True):
            rows = [row for row in forwards if row[1] == f"tcp:{port}"]
            return (not rows and _bindable(port)) or (owned and rows == [(serial, f"tcp:{port}", f"tcp:{remote}")])

        ports = p.get("usb_ports")
        if ports is None:
            base = CFG.get("usb_port_base", 47810)
            if type(base) is not int or not 1024 <= base <= 65534:
                raise PhoneError("bad_param", "invalid usb_port_base")
            ports = next(([n, n + 1] for n in range(base, 65535, 2)
                          if n not in used and n + 1 not in used and free(n, CONTROL_PORT, False) and free(n + 1, GEN_PORT, False)), None)
            if ports is None:
                raise PhoneError("busy", "no free USB forwarding ports")
            p["usb_ports"] = ports
        if (not isinstance(ports, list) or len(ports) != 2 or
                any(type(n) is not int or not 1024 <= n <= 65535 for n in ports) or ports[0] == ports[1]):
            raise PhoneError("bad_param", "invalid usb_ports")
        if any(n in used for n in ports) or not all(free(n, r) for n, r in zip(ports, (CONTROL_PORT, GEN_PORT))):
            raise PhoneError("busy", "USB forwarding port already in use")
        p["usb_forward_serial"] = serial
        # Record ownership before adb: even a partly failed pair can be cleaned up later.
        _atomic_json(PHONES, data)
        for n, remote in zip(ports, (CONTROL_PORT, GEN_PORT)):
            if (serial, f"tcp:{n}", f"tcp:{remote}") not in forwards:
                _adb_raw("-s", serial, "forward", "--no-rebind", f"tcp:{n}", f"tcp:{remote}")
        phone.update(usb_ports=ports, usb_forward_serial=serial)
    return tuple(f"http://127.0.0.1:{n}" for n in ports)


def endpoints(phone: dict) -> tuple[str, str]:
    if phone.get("transport") == "lan":
        return _host_url(phone.get("host"), phone.get("control_port", CONTROL_PORT)), _host_url(phone.get("host"), GEN_PORT)
    if phone.get("transport") == "usb":
        return _usb_endpoints(phone, _usb_serial(phone))
    raise PhoneError("bad_param", "transport must be lan or usb")


def api(phone: dict) -> Api:
    return Api(*endpoints(phone))


def add_lan(host: str, name: str | None = None) -> dict:
    valid_host(host)
    nonce = secrets.token_hex(16)
    info = Api(_host_url(host, CONTROL_PORT), _host_url(host, GEN_PORT), trusted=False, challenge=nonce).info()
    if info.get("app") != "localdream":
        raise PhoneError("no_host_mode", "not a Local Dream host")
    link = link_status(info)
    if link["auth_required"] and not pairing_proved(info, nonce):
        raise PhoneError("not_paired")
    return _remember_lan(host, name or info.get("device"), CONTROL_PORT)


def _remember_lan(host: str, name: str | None, port: int) -> dict:
    with _registry() as data:
        lst = data["phones"]
        p = next((p for p in lst if p.get("host") == host), None)
        if p is None:
            title = name or "Android"
            p = {"id": _new_id(title, lst), "name": title, "host": host, "transport": "lan"}
            lst.append(p)
        else:
            p.update(transport="lan", name=p.get("name") or name or "Android")   # keep a name the owner already has
        if port != CONTROL_PORT:
            p["control_port"] = port
        else:
            p.pop("control_port", None)
        _migrate([p])
        return p.copy()


def _adopt(serial: str, transport: str, host: str | None = None, switch=False, *, allow_ignored=True) -> dict | None:
    hardware = _serialno(serial)
    if not hardware:
        raise PhoneError("no_adb", "device serial property unavailable")
    title = _prop(serial, "ro.product.marketname") or _prop(serial, "ro.product.model") or "Android"
    # A phone first added over Wi-Fi has no serial on record: its Wi-Fi address still says it is the
    # same phone, so plugging it in must not create a second record.
    wifi = host or _wifi_ip(serial)
    with _registry() as data:
        lst = data["phones"]
        if hardware in data.get("ignored_serials", []):
            if not allow_ignored:
                return None
            data["ignored_serials"].remove(hardware)
        p = next((p for p in lst if p.get("serialno") == hardware), None) \
            or next((p for p in lst if wifi and p.get("host") == wifi and not p.get("serialno")), None)
        if p is None:
            p = {"id": _new_id(title, lst), "name": title, "serialno": hardware, "transport": transport}
            lst.append(p)
        p["serialno"] = hardware
        if switch:
            p["transport"] = transport
        if host or (wifi and not p.get("host")):
            p["host"] = wifi
        _serials[p["id"]] = serial
        _migrate([p])
        return p.copy()


def add_usb(serial: str) -> dict:
    state = _devices().get(serial)
    if state != "device" or not _is_usb(serial):
        raise PhoneError("usb_unauthorized" if state == "unauthorized" else "usb_unplugged")
    p = _adopt(serial, "usb", switch=True)
    endpoints(p)
    return get_phone(p["id"])


def connect_usb(*, include_ignored=False) -> list[str]:
    if os.environ.get("LDPC_NO_ADB") == "1":
        return []
    done = []
    ignored = set(_read_registry().get("ignored_serials", []))
    for serial, state in _devices().items():
        if _is_usb(serial) and state == "device":
            if not include_ignored and _serialno(serial) in ignored:
                continue
            p = _adopt(serial, "usb", allow_ignored=include_ignored)
            if p is None:
                continue
            if p["transport"] == "usb":
                endpoints(p)
            done.append(p["id"])
    return done


MDNS_SERVICE = "_localdream._tcp.local"


def _dns_name(name: str) -> bytes:
    return b"".join(bytes([len(label.encode("utf-8"))]) + label.encode("utf-8")
                    for label in name.rstrip(".").split(".")) + b"\0"


def mdns_query(name=MDNS_SERVICE, kind=12) -> bytes:
    # QU requests a unicast reply too, useful when another process owns port 5353.
    return struct.pack("!6H", 0, 0, 1, 0, 0, 0) + _dns_name(name) + struct.pack("!HH", kind, 0x8001)


def _read_dns_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels, seen, end = [], set(), None
    while True:
        if offset in seen or offset >= len(packet):
            raise ValueError("invalid DNS name")
        seen.add(offset)
        size = packet[offset]
        offset += 1
        if size & 0xc0 == 0xc0:
            if offset >= len(packet):
                raise ValueError("truncated DNS pointer")
            end = end or offset + 1
            offset = ((size & 0x3f) << 8) | packet[offset]
        elif size & 0xc0 or offset + size > len(packet):
            raise ValueError("invalid DNS label")
        elif size == 0:
            return ".".join(labels), end or offset
        else:
            labels.append(packet[offset:offset + size].decode("utf-8"))
            offset += size
        if len(seen) > 128:
            raise ValueError("DNS name too long")


def parse_mdns_packet(packet: bytes) -> list[tuple]:
    """Read compressed PTR/SRV/A records; discard malformed datagrams as a whole."""
    try:
        _, flags, questions, answers, authority, additional = struct.unpack_from("!6H", packet)
        if not flags & 0x8000 or flags & 0xf:
            return []
        offset, records = 12, []
        for _ in range(questions):
            _, offset = _read_dns_name(packet, offset)
            offset += 4
        for _ in range(answers + authority + additional):
            name, offset = _read_dns_name(packet, offset)
            kind, cls, ttl, size = struct.unpack_from("!HHIH", packet, offset)
            offset += 10
            end = offset + size
            if end > len(packet):
                raise ValueError("truncated DNS record")
            data = None
            if cls & 0x7fff == 1 and ttl:
                if kind == 12:
                    data, consumed = _read_dns_name(packet, offset)
                    if consumed != end:
                        raise ValueError("invalid PTR record")
                elif kind == 33 and size >= 7:
                    _, _, port = struct.unpack_from("!HHH", packet, offset)
                    target, consumed = _read_dns_name(packet, offset + 6)
                    if consumed != end:
                        raise ValueError("invalid SRV record")
                    data = (target.lower(), port)
                elif kind == 1 and size == 4:
                    data = socket.inet_ntoa(packet[offset:end])
            if data is not None:
                records.append((name.lower(), kind, data))
            offset = end
        return records
    except (ValueError, UnicodeError, struct.error):
        return []


def mdns_discover(timeout=2.0) -> list[dict]:
    instances, servers, addresses, asked = {}, {}, {}, set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            try:
                sock.bind(("", 5353))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton("224.0.0.251") + socket.inet_aton("0.0.0.0"))
            except OSError:
                # A separate socket avoids trying to rebind a partially initialized listener.
                return _mdns_unicast(timeout)
            sock.sendto(mdns_query(), ("224.0.0.251", 5353))
            _collect_mdns(sock, timeout, instances, servers, addresses, asked)
    except OSError:
        pass
    return _mdns_results(instances, servers, addresses)


def _mdns_unicast(timeout) -> list[dict]:
    instances, servers, addresses = {}, {}, {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.bind(("", 0))
            sock.sendto(mdns_query(), ("224.0.0.251", 5353))
            _collect_mdns(sock, timeout, instances, servers, addresses, set())
    except OSError:
        pass
    return _mdns_results(instances, servers, addresses)


def _collect_mdns(sock, timeout, instances, servers, addresses, asked):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sock.settimeout(max(0.001, end - time.monotonic()))
        try:
            packet, _ = sock.recvfrom(65535)
        except socket.timeout:
            break
        for name, kind, data in parse_mdns_packet(packet):
            if kind == 12 and name == MDNS_SERVICE and data.lower().endswith("." + MDNS_SERVICE):
                instances[data.lower()] = data[:-(len(MDNS_SERVICE) + 1)]
            elif kind == 33 and name.endswith("." + MDNS_SERVICE):
                servers[name] = data
            elif kind == 1:
                addresses[name] = data
        # NSD may split PTR, SRV and A into separate replies. Ask for missing records.
        queries = [(name, 33) for name in instances if name not in servers]
        queries += [(servers[name][0], 1) for name in instances if name in servers
                    and servers[name][0] not in addresses]
        for query in queries:
            if query not in asked:
                sock.sendto(mdns_query(*query), ("224.0.0.251", 5353))
                asked.add(query)


def _mdns_results(instances, servers, addresses) -> list[dict]:
    return [{"name": title, "host": addresses[servers[name][0]], "port": servers[name][1]}
            for name, title in instances.items() if name in servers and servers[name][0] in addresses
            and 1 <= servers[name][1] <= 65535]


def discover(*, adopt=False) -> list[dict]:
    """Merge NSD hosts with saved endpoints; only adopt a phone that accepts our token."""
    records, candidates = phones(), {}
    for item in mdns_discover():
        candidates[(item["host"], item["port"])] = dict(item)
    for phone in records:
        if phone.get("transport") == "usb" and phone.get("usb_ports"):
            key = ("127.0.0.1", phone["usb_ports"][0])
            candidates.setdefault(key, {"name": phone["name"], "host": key[0], "port": key[1],
                                        "phone_id": phone["id"], "transport": "usb"})
        elif phone.get("host"):
            key = (phone["host"], phone.get("control_port", CONTROL_PORT))
            candidates.setdefault(key, {"name": phone["name"], "host": key[0], "port": key[1]})
            candidates[key]["phone_id"] = phone["id"]
    found = []
    for item in candidates.values():
        nonce = secrets.token_hex(16)
        client = Api(_host_url(item["host"], item["port"]), _host_url(item["host"], GEN_PORT),
                     trusted=False, challenge=nonce)
        try:
            info = client.info(timeout=1.5)
            if info.get("app") != "localdream":
                continue
            link = link_status(info)
            # token_ok is only meaningful after local verification, never as a claim
            # supplied by an unauthenticated multicast peer.
            verified = pairing_proved(info, nonce)
            link["token_ok"] = verified
            item.update(name=info.get("device") or item["name"], desktop_link=link, features=app_features(info),
                        state="not_paired" if link["auth_required"] and not link["token_ok"] else "ready")
            if adopt and verified and item.get("transport") != "usb":
                phone = _remember_lan(item["host"], item["name"], item["port"])
                item["phone_id"] = phone["id"]
                client.trusted = True
                list_models(phone, client)
        except PhoneError as e:
            item.update(state=e.code, desktop_link=link_status({}))
        found.append(item)
    return found


def phone_memory(phone: dict) -> int | None:
    """Read MemAvailable using attached adb only: never call connect(), pair or forward."""
    try:
        serial = _attached_serial(phone, _adb_raw)
        if serial:
            text = _adb_raw("-s", serial, "shell", "cat", "/proc/meminfo")
            match = re.search(r"^MemAvailable:\s+(\d+)\s+kB\s*$", text, re.M)
            return int(match[1]) // 1024 if match else None
    except PhoneError:
        pass
    return None


RAM_NEED_MB = {"dit": 4200, "sdxl": 4000, "anima": 4000, "sd15": 1500, "sd15cpu": 1500}
REFERENCE_EXTRA_MB = 1500  # Per-image estimate, pending measurement; reference_images are capped at 1 MP.
MAX_REFERENCES = 3  # Bound phone RAM/DiT sequence growth; native has no count limit.
MAX_REFERENCE_BYTES = 6 * 1024 * 1024  # Three base64 payloads fit the studio's 32 MiB cap.
MAX_REFERENCE_PIXELS = 16_000_000
MAX_REFERENCE_OUTPUT_PIXELS = 1024 * 1024  # Match the official app's reference sequence budget.


def reference_count(request: dict) -> int:
    refs = request.get("reference_images")
    return (len(refs) if isinstance(refs, list) else 0) + int(bool(
        request.get("image") or request.get("image_b64") or request.get("img2img")))


def memory_status(phone: dict, model: dict) -> dict:
    available = phone_memory(phone)
    family = capabilities(model, False)["family"]
    needed = RAM_NEED_MB[family]
    return {"available_mb": available, "needed_mb": needed,
            "reference_extra_mb": REFERENCE_EXTRA_MB if family in ("dit", "sdxl", "anima") else 0,
            "low": available is not None and available < needed}


def memory_warning(memory: dict) -> str:
    return (f"The phone has {memory['available_mb'] / 1000:.1f} GB free; this model usually needs about "
            f"{memory['needed_mb'] / 1000:.1f} GB. Close other apps on the phone first.")


APP_WARNINGS = {
    "recommend_foreground": "Open Local Dream on the phone's screen before big models.",
    "recommend_unlock": "Unlock the phone and keep Local Dream open.",
}


def app_warnings(app_state) -> list[str]:
    """Official builds omit app_state; unknown values are not warnings."""
    state = app_state if isinstance(app_state, dict) else {}
    return (["recommend_foreground"] if state.get("foreground") is False else []) + (
        ["recommend_unlock"] if state.get("locked") is True else [])


def _attached_serial(phone: dict, probe) -> str | None:
    """Match this phone without connecting, pairing or creating forwards."""
    devs = {p[0]: p[1] for line in probe("devices").splitlines() if len(p := line.split()) >= 2}
    for candidate, state in devs.items():
        if state != "device":
            continue
        matches = candidate == _serials.get(phone.get("id"))
        if not matches and _is_usb(candidate) and phone.get("serialno"):
            matches = probe("-s", candidate, "shell", "getprop", "ro.serialno").strip() == phone["serialno"]
        elif not matches and not _is_usb(candidate) and phone.get("host"):
            matches = candidate.rsplit(":", 1)[0] == phone["host"]
            if not matches and "._adb" in candidate:
                matches = bool(re.search(r"inet " + re.escape(phone["host"]) + r"/",
                                        probe("-s", candidate, "shell", "ip -4 -o addr show wlan0")))
        if matches:
            return candidate
    return None


def foreground_before_work(phone: dict, app_state) -> dict:
    """Best effort on an attached transport. Never dismiss or bypass a keyguard."""
    state = dict(app_state) if isinstance(app_state, dict) else {}
    if state.get("locked") is True:
        return state
    deadline = time.monotonic() + 5

    def probe(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PhoneError("timeout")
        return _adb_raw(*args, timeout=remaining)

    def keyguard(serial):
        window = probe("-s", serial, "shell", "dumpsys", "window")
        trust = probe("-s", serial, "shell", "dumpsys", "trust")
        current = "\n".join(line for line in trust.splitlines() if "(current)" in line)
        flags = re.findall(r"(?:mShowingLockscreen|isKeyguardShowing|mKeyguardShowing|mDreamingLockscreen)"
                           r"(?:\(\))?\s*[=:]\s*(true|false)\b", window, re.I)
        flags += re.findall(r"deviceLocked\s*[=:]\s*(1|0|true|false)\b", current, re.I)
        return any(flag.lower() in ("true", "1") for flag in flags) if flags else None

    try:
        serial = _attached_serial(phone, probe)
        if not serial:
            return state
        locked = keyguard(serial)
        if locked is True:
            return {**state, "locked": True}
        if locked is None:
            return state                              # unknown keyguard: do not send input
        probe("-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
        # Recheck after wakeup in case the screen activated the keyguard.
        locked = keyguard(serial)
        if locked is not False:
            return {**state, "locked": True} if locked else state
        output = probe("-s", serial, "shell", "am", "start", "-a", "android.intent.action.MAIN",
                       "-c", "android.intent.category.LAUNCHER", "-n", f"{PKG}/.MainActivity")
        if re.search(r"Error|Exception|Permission Denial", output, re.I):
            return state
        return {**state, "foreground": True, "locked": False}
    except PhoneError:
        return state


def smaller_request(request: dict, caps: dict) -> dict | None:
    """One supported step down, without changing any other generation settings."""
    sizes = caps.get("sizes", {})
    defaults = caps.get("defaults", {})
    w, h = request.get("width") or defaults.get("width"), request.get("height") or defaults.get("height")
    if not w or not h:
        return None
    kind = sizes.get("kind")
    if kind == "list":
        choices = sizes.get("list", [])
        if [w, h] not in choices or choices.index([w, h]) == 0:
            return None
        nw, nh = choices[choices.index([w, h]) - 1]
        if nw * nh >= w * h:
            return None
        return {"width": nw, "height": nh}
    if kind in ("dit", "square"):
        step, minimum = sizes["step"], sizes["min"]
        nw, nh = [max(minimum, int(n * .75) // step * step) for n in (w, h)]
        return {"width": nw, "height": nh} if nw * nh < w * h else None
    if kind == "fixed" and request.get("aspect_ratio", "1:1") != "1:1":
        return {"width": sizes["size"], "height": sizes["size"], "aspect_ratio": "1:1"}
    return None


def phone_job_clock(phone: dict) -> float | None:
    """Capture phone wall time before work, on an attached transport within 5 s."""
    deadline = time.monotonic() + 5

    def probe(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PhoneError("timeout")
        return _adb_raw(*args, timeout=remaining)

    try:
        serial = _attached_serial(phone, probe)
        return float(probe("-s", serial, "shell", "date", "+%s").strip()) if serial else None
    except (PhoneError, ValueError):
        return None


EXIT_REASONS = {3: "LOW_MEMORY", 4: "CRASH", 5: "CRASH_NATIVE"}


def _latest_exit(text: str, started: float, ended: float = float("inf"), zone=None) -> dict | None:
    records = []
    for block in re.split(r"(?=ApplicationExitInfo\b)", text):
        reason = re.search(r"reason=(\d+)\s*\(([^)]+)\)", block)
        stamp = re.search(r"timestamp=(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?|\d{10,13})", block)
        if not reason or not stamp:
            continue
        try:
            raw = stamp[1]
            timestamp = datetime.fromisoformat(raw).replace(tzinfo=zone).timestamp() if "-" in raw else float(raw) / (1000 if len(raw) == 13 else 1)
        except ValueError:
            continue
        # Map by ApplicationExitInfo REASON_* number; labels can include "APP CRASH(NATIVE)".
        label = EXIT_REASONS.get(int(reason[1]), reason[2].strip().upper().replace(" ", "_"))
        records.append({"code": "exit_reason", "reason": label,
                        "timestamp": timestamp})
    newest = max(records, key=lambda r: r["timestamp"], default=None)
    return newest if newest and started < newest["timestamp"] <= ended else None


def _memory_apps(text: str) -> list[dict]:
    section = re.search(r"Total (?:PSS|RSS) by process:\s*\n(.*?)(?:\n\s*\n|\Z)", text, re.S)
    apps = {}
    if section:
        for kb, name in re.findall(r"([\d,]+)K:\s+([\w.]+)(?::[^\s(]+)?\s+\(pid", section[1]):
            if (name == PKG or name.startswith(("android.", "com.android.", "vendor.")) or name in (
                    "com.google.android.gms", "com.google.android.gsf")
                    or "." not in name):
                continue
            apps[name] = apps.get(name, 0) + int(kb.replace(",", ""))
    return [{"package": name, "mb": round(kb / 1024)}
            for name, kb in sorted(apps.items(), key=lambda pair: pair[1], reverse=True)[:5]]


def diagnose_failure(phone: dict, request: dict, error) -> dict:
    """Read-only, best-effort diagnosis; all adb probes share a five-second budget."""
    code = error.code if isinstance(error, PhoneError) else error.get("code", "unknown") if isinstance(error, dict) else str(error)
    model = next((m for m in phone.get("models", []) if m["id"] == request.get("model_id")), {})
    caps = request.get("capabilities") or (capabilities(model, phone.get("use_img2img", False)) if model else {})
    family = caps.get("family", "")
    count = reference_count(request)
    reference = count > 0
    evidence, apps, available, exit_record = [], [], None, None
    deadline = time.monotonic() + 5

    def probe(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PhoneError("timeout")
        return _adb_raw(*args, timeout=remaining)

    serial, public_storage = None, False
    try:
        serial = _attached_serial(phone, probe)
    except PhoneError:
        pass
    if serial:
        phone_now, zone = None, None
        try:
            phone_now = float(probe("-s", serial, "shell", "date", "+%s").strip())
            zone = datetime.strptime(probe("-s", serial, "shell", "date", "+%z").strip(), "%z").tzinfo
        except (PhoneError, ValueError):
            pass
        phone_started = request.get("phone_started")
        # If one clock read failed, anchor the monotonic job duration to the
        # other phone-clock reading. Never compare Android time to PC wall time.
        duration = (max(0, time.monotonic() - request["started_monotonic"])
                    if request.get("started_monotonic") is not None else None)
        if phone_now is None and phone_started is not None and duration is not None:
            phone_now = phone_started + duration
        if phone_started is None and phone_now is not None and duration is not None:
            phone_started = phone_now - duration
        model_id = request.get("model_id", "")
        # adb shell joins its arguments: accept only a safe single path component.
        if family in ("dit", "sdxl") and isinstance(model_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", model_id):
            try:
                probe("-s", serial, "shell", "test", "-d", f"/sdcard/Download/LocalDream/models/{model_id}")
                public_storage = True
            except PhoneError:
                pass
        for command in (("dumpsys", "activity", "exit-info", PKG), ("cat", "/proc/meminfo"), ("dumpsys", "meminfo")):
            try:
                text = probe("-s", serial, "shell", *command)
                if command[0] == "cat":
                    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", text, re.M)
                    available = int(match[1]) // 1024 if match else None
                elif command[1] == "activity":
                    # Without a phone-clock window, do not attribute historical exits.
                    if phone_now is not None and phone_started is not None:
                        numeric = not re.search(r"timestamp=\d{4}-", text)
                        if zone is not None or numeric:
                            exit_record = _latest_exit(text, phone_started, phone_now + 1, zone)
                else:
                    apps = _memory_apps(text)
            except PhoneError:
                continue
    reason = exit_record["reason"] if exit_record else None
    needed = RAM_NEED_MB.get(family, RAM_NEED_MB["sd15"])
    if reference and family in ("dit", "sdxl", "anima"):
        needed += REFERENCE_EXTRA_MB * count
    state = request.get("app_state") if isinstance(request.get("app_state"), dict) else {}
    causes = []
    if reason == "LOW_MEMORY" or (code == "app_closed" and available is not None and available < needed):
        causes.append("out_of_memory")
    if reason == "CRASH_NATIVE":
        causes.append("native_crash")
    if exit_record and reason not in ("LOW_MEMORY", "CRASH_NATIVE"):
        causes.append("app_closed")
    if public_storage:
        # Shared storage can affect model mapping even when available RAM looks sufficient.
        causes.append("models_in_public_storage")
        evidence.append("models_in_public_storage")
    if state.get("foreground") is False:
        causes.append("app_not_foreground")
    if code in ("unreachable", "timeout", "connection_refused", "usb_unplugged"):
        causes.append("network")
    if code == "load_failed":
        causes.append("load_failed")
    if code == "worker_stopped":
        causes.append("worker_stopped")
    if code in ("no_host_mode", "host_mode_manual") or (code == "app_closed" and not exit_record):
        causes.append("host_mode_off")
    if not causes:
        causes.append("unknown")
    # A recorded native crash is more specific than a low-RAM inference, and a large
    # model in public storage explains a LOW_MEMORY kill better than RAM does.
    if reason == "CRASH_NATIVE":
        cause = "native_crash"
    elif public_storage:
        cause = "models_in_public_storage"
    else:
        cause = causes[0]
    if exit_record:
        evidence.append(exit_record)
    if state.get("foreground") is False:
        evidence.append("app_not_foreground")
    if state.get("locked") is True:
        evidence.append("phone_locked")
    if reference and family in ("dit", "sdxl", "anima"):
        evidence.append("reference_uses_more_memory")
    if available is not None:
        evidence.append({"code": "memory_available", "mb": available})
    if not serial:
        evidence.append("adb_unavailable")
    evidence.append({"code": "request", "family": family, "model": model.get("name", ""),
                     "width": request.get("width"), "height": request.get("height"), "steps": request.get("steps"),
                     "reference": reference, "mask": bool(request.get("mask") or request.get("mask_b64"))})
    smaller = smaller_request(request, caps)
    actions = ["retry"]
    if public_storage:
        actions.append("move_to_app_storage")
    actions.extend(app_warnings(state))
    if set(causes) & {"out_of_memory", "load_failed"} or cause == "unknown":
        if reference:
            actions.append("retry_without_reference")
        if smaller:
            actions.append("retry_smaller")
    if "out_of_memory" in causes:
        actions.extend(["close_apps", "reopen_host_mode", "use_other_model"])
    if set(causes) & {"native_crash", "host_mode_off", "app_closed"}:
        actions.append("reopen_host_mode")
    if "network" in causes:
        actions.append("check_wifi")
    if set(causes) & {"native_crash", "load_failed", "worker_stopped"} or cause == "unknown":
        actions.append("use_other_model")
    order = ("retry_without_reference", "retry_smaller", "move_to_app_storage", "recommend_foreground",
             "recommend_unlock", "close_apps", "reopen_host_mode", "check_wifi", "use_other_model", "retry")
    return {"cause": cause, "causes": causes, "evidence": evidence,
            "actions": [action for action in order if action in actions], "apps": apps, "smaller": smaller}


def _mdns(host: str) -> str | None:
    for line in _adb_raw("mdns", "services").splitlines():
        parts = line.split()
        if len(parts) >= 3 and "_adb-tls-connect" in parts[-2] and parts[-1].rsplit(":", 1)[0] == host:
            return parts[-1]
    return None


def _address(address: str) -> str:
    try:
        host, port = address.strip().rsplit(":", 1)
        valid_host(host)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError
        return f"{host}:{int(port)}"
    except (AttributeError, ValueError, PhoneError):
        raise PhoneError("pair_input", "enter an address as HOST:PORT") from None


def pair(address: str, code: str, connect_address: str | None = None) -> dict:
    address = _address(address)
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code.strip()):
        raise PhoneError("pair_input")
    if connect_address is not None:
        connect_address = _address(connect_address)
    if "Successfully paired" not in _adb_raw("pair", address, code.strip()):
        raise PhoneError("pair_failed")
    host = address.rsplit(":", 1)[0]
    target = connect_address or _mdns(host)
    if not target:
        raise PhoneError("pair_connect_port", 'enter the "IP address & Port" from Wireless debugging')
    if "connected" not in _adb_raw("connect", target).lower():
        raise PhoneError("pair_failed", "paired but adb connect failed")
    return _adopt(target, "lan", host=host)


def connect(phone: dict) -> str | None:
    """Attach adb for host-mode UI without scanning ports."""
    devs = _devices()
    try:
        return _usb_serial(phone, devs)
    except PhoneError:
        if phone.get("transport") == "usb":
            return None
    host = phone.get("host")
    if not host:
        return None
    existing = _serials.get(phone["id"])
    if existing and devs.get(existing) == "device":
        return existing
    for serial, state in devs.items():
        if state == "device" and serial.rsplit(":", 1)[0] == host:
            _serials[phone["id"]] = serial
            return serial
    target = _mdns(host)
    if target and "connected" in _adb_raw("connect", target).lower():
        _serials[phone["id"]] = target
        return target
    return None


def adb(phone: dict, *args: str, check=True) -> str:
    try:
        serial = connect(phone)
        if not serial:
            raise PhoneError("no_adb")
        return _adb_raw("-s", serial, *args)
    except PhoneError:
        if check:
            raise
        return ""


def remove_phone(pid: str) -> None:
    p = get_phone(pid)
    with phone_lock(p), _registry() as data:
        if p.get("usb_ports"):
            try:
                rows = _forwards()
            except PhoneError as e:
                if e.code != "no_adb":
                    raise
                rows = []
            for serial, local, remote in rows:
                if (local, remote) not in [(f"tcp:{n}", f"tcp:{r}") for n, r in zip(p["usb_ports"], (CONTROL_PORT, GEN_PORT))]:
                    continue
                owned = serial in (_serials.get(pid), p.get("usb_forward_serial"))
                if not owned:
                    try:
                        owned = _prop(serial, "ro.serialno") == p.get("serialno")
                    except PhoneError:
                        owned = False
                if owned:
                    _adb_raw("-s", serial, "forward", "--remove", local)
        data["phones"] = [x for x in data["phones"] if x["id"] != pid]
        if p.get("serialno"):
            ignored = data.setdefault("ignored_serials", [])
            if p["serialno"] not in ignored:
                ignored.append(p["serialno"])
        _serials.pop(pid, None)


def set_transport(pid: str, transport: str) -> dict:
    p = get_phone(pid)
    if transport not in ("lan", "usb"):
        raise PhoneError("bad_param")
    if transport == "lan":
        valid_host(p.get("host"))
    if transport == "usb" and not p.get("serialno"):
        raise PhoneError("usb_unplugged")
    with phone_lock(p):
        return _update(pid, transport=transport)


def phone_status() -> dict:
    try:
        adb_path()
        adb_available = True
    except PhoneError:
        adb_available = False
    devs = _devices()
    # Polling must neither migrate the on-disk registry nor repair USB forwards.
    with _registry_mutex:
        registry = _read_registry()
        records = registry["phones"]
        _migrate(records)
    authorized = {s: _serialno(s) for s, state in devs.items() if _is_usb(s) and state == "device"}
    out = []
    for p in records:
        state = "unreachable"
        features = {}
        try:
            if p["transport"] == "usb":
                serial = next((s for s, hw in authorized.items() if hw == p.get("serialno")), None)
                if not serial:
                    raise PhoneError("usb_unauthorized" if any(_is_usb(s) and t == "unauthorized" for s, t in devs.items()) else "usb_unplugged")
                ports = p.get("usb_ports")
                if (not isinstance(ports, list) or len(ports) != 2 or
                        any(type(n) is not int or not 1024 <= n <= 65535 for n in ports) or
                        p.get("usb_forward_serial", serial) != serial):
                    raise PhoneError("unreachable")
                client = Api(*(f"http://127.0.0.1:{n}" for n in ports))
            else:
                client = Api(_host_url(p.get("host"), p.get("control_port", CONTROL_PORT)), _host_url(p.get("host"), GEN_PORT))
            try:
                info = client.info(timeout=1.5)
                if info.get("app") == "localdream":
                    features = app_features(info)
                link = link_status(info)
                state = ("no_host_mode" if info.get("app") != "localdream" else
                         "not_paired" if link["auth_required"] and not link["token_ok"] else "ready")
            except PhoneError as e:
                state = e.code if e.code in ("not_paired", "no_host_mode", "connection_refused", "timeout", "unreachable") else "unreachable"
        except PhoneError as e:
            state = e.code if e.code in ("usb_unplugged", "usb_unauthorized", "unreachable") else "unreachable"
        out.append({"id": p["id"], "name": p["name"], "transport": p["transport"], "host": p.get("host"),
                    "state": state, "features": features, "has_usb": bool(p.get("serialno")),
                    "has_lan": bool(p.get("host")), "has_adb": bool(
                        any(state == "device" and (s == _serials.get(p["id"])
                            or (_is_usb(s) and authorized.get(s) == p.get("serialno") and p.get("serialno"))
                            or (not _is_usb(s) and s.rsplit(":", 1)[0] == p.get("host")))
                            for s, state in devs.items()))})
    known = {p.get("serialno") for p in records} | set(registry.get("ignored_serials", []))
    return {"phones": out, "usb_new": any(hw not in known for hw in authorized.values()),
            "adb_available": adb_available,
            "usb_waiting": any(_is_usb(s) and t == "unauthorized" for s, t in devs.items())}


# --- phone screen and model loading --------------------------------------------------------------
def screen(phone: dict) -> list[tuple[str, int, int]]:
    adb(phone, "shell", "uiautomator", "dump", "/sdcard/ldpc-ui.xml")
    try:
        root = ET.fromstring(adb(phone, "shell", "cat", "/sdcard/ldpc-ui.xml"))
        out = []
        for node in root.iter("node"):
            coords = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if coords:
                x1, y1, x2, y2 = map(int, coords.groups())
                for attr in ("text", "content-desc"):
                    if node.get(attr):
                        out.append((node.get(attr), (x1 + x2) // 2, (y1 + y2) // 2))
        return out
    except ET.ParseError:
        raise PhoneError("host_mode_manual", "UI dump failed; open Device link on the phone") from None


def host_up(phone: dict, timeout=3) -> bool:
    try:
        return api(phone).info(timeout).get("app") == "localdream"
    except PhoneError:
        return False


def open_host_mode(phone: dict) -> None:
    if phone.get("transport") == "usb":
        _usb_serial(phone)
    try:
        serial = connect(phone)
    except PhoneError as e:
        if e.code != "no_adb":
            raise
        serial = None
    if not serial:
        raise PhoneError("host_mode_manual", "On the phone: Local Dream → ⋮ More → Device link → Enter host mode")
    try:
        command = adb(phone, "shell", "am", "start", "-n", f"{PKG}/.remote.HostModeCommandActivity",
                      "-a", f"{PKG}.action.HOST_MODE_START")
    except PhoneError as e:
        # adb can exit nonzero for an absent activity or a permission denial. Inspect only
        # those known failures; do not hide timeouts or transport failures as UI fallbacks.
        if e.code != "host_command_unavailable":
            raise
        command = "does not exist"
    if not re.search(r"does not exist|Permission Denial|Error type 3", command, re.I):
        if re.search(r"Error:|Exception", command, re.I):
            raise PhoneError("host_mode_manual")
        for _ in range(8):
            if host_up(phone, 1.5):
                return
            time.sleep(0.5)
        raise PhoneError("no_host_mode")
    # A screen that is merely off or swipe-locked looks "locked" too: wake it and dismiss the
    # keyguard first; whatever is still showing after that needs a PIN/pattern we never bypass.
    adb(phone, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb(phone, "shell", "wm", "dismiss-keyguard")
    time.sleep(1)
    window, trust = adb(phone, "shell", "dumpsys", "window"), adb(phone, "shell", "dumpsys", "trust")
    current_trust = "\n".join(line for line in trust.splitlines() if "(current)" in line)
    if (re.search(r"(?:mShowingLockscreen|isKeyguardShowing|mKeyguardShowing|mDreamingLockscreen)\s*[=:]\s*true", window, re.I)
            or re.search(r"deviceLocked\s*[=:]\s*(?:1|true)\b", current_trust, re.I)):
        raise PhoneError("phone_locked", "unlock the phone before opening host mode")
    adb(phone, "shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    # The app resumes on whatever screen it was left on, so tap the furthest step that is visible
    # (Enter host mode > Device link > More) and step back only when none of them is.
    steps = (("Enter host mode", "ホストモードを開始", "호스트 모드 시작", "进入受控模式"),
             ("Device link", "デバイス連携", "기기 연결", "设备互联"), ("More", "その他", "더보기", "更多"))
    try:
        for _ in range(8):
            if host_up(phone, 1.5):
                return
            time.sleep(1)
            seen = screen(phone)
            hit = next(((x, y, i) for i, labels in enumerate(steps) for text, x, y in seen if text in labels), None)
            if hit:
                adb(phone, "shell", "input", "tap", str(hit[0]), str(hit[1]))
                if hit[2] == 0:
                    break
            else:
                adb(phone, "shell", "input", "keyevent", "KEYCODE_BACK")
        else:
            raise PhoneError("host_mode_manual")
    except PhoneError as e:
        raise PhoneError("host_mode_manual", "open host mode manually on the phone") from e
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if host_up(phone, min(1.5, end - time.monotonic())):
            return
        time.sleep(0.5)
    raise PhoneError("no_host_mode")


def ensure_host(phone: dict, stage=_noop, client=None) -> None:
    # Generation never starts wireless adb or silently changes transports.
    info = (client or api(phone)).info()
    if info.get("app") != "localdream":
        raise PhoneError("no_host_mode")
    link = link_status(info)
    if link["auth_required"] and not link["token_ok"]:
        raise PhoneError("not_paired")


def list_models(phone: dict, client=None, *, persist=True) -> list[dict]:
    """Refresh a phone snapshot; callers can defer persisting the catalog."""
    catalog = (client or api(phone)).models()
    models = catalog.get("models", [])
    if (not isinstance(models, list) or any(not isinstance(m, dict)
            or not isinstance(m.get("id"), str) or not isinstance(m.get("name"), str) for m in models)):
        raise PhoneError("bad_param", "invalid model catalog")
    for m in models:
        capabilities(m, catalog.get("use_img2img", False))
    phone.update(models=catalog.get("models", []), use_img2img=catalog.get("use_img2img", False))
    if persist:
        saved = _update(phone["id"], models=phone["models"], use_img2img=phone["use_img2img"])
        phone.update(record_id=saved["record_id"], catalog_revision=saved["catalog_revision"])
    return phone["models"]


def _gen_up(phone: dict) -> bool:
    return api(phone).health()


def tokens(phone: dict, prompt: str):
    return api(phone).tokenize(prompt)


def select(phone: dict, model_id: str, w: int, h: int, stage=_noop, client=None):
    client = client or api(phone)

    def ready(s):
        return (s.get("state") == "running" and s.get("serving_model_id") == model_id
                and (s.get("width"), s.get("height")) == (w, h) and client.health())

    if ready(client.status()):
        return
    stage("load")
    client.select(model_id, w, h)
    end = time.monotonic() + LOAD_TIMEOUT
    while time.monotonic() < end:
        s = client.status()
        if s.get("state") == "error" and s.get("error_model_id") in (None, model_id):
            raise PhoneError("load_failed", s.get("message") or "model failed to load")
        if ready(s):
            return
        time.sleep(0.5)
    raise PhoneError("timeout", "model did not become healthy in 240 seconds")


# --- capabilities and validation -----------------------------------------------------------------
def capabilities(model: dict, use_img2img: bool) -> dict:
    if not isinstance(model, dict):
        raise PhoneError("bad_param", "invalid model catalog")
    family = ("dit" if model.get("dit_kind") else "anima" if model.get("is_anima") else
              "sdxl" if model.get("is_sdxl") else "sd15cpu" if model.get("run_on_cpu") else "sd15")
    if family == "sd15":
        resolutions = model.get("resolutions")
        resolutions = resolutions if isinstance(resolutions, list) else []
        sizes = {"kind": "list", "list": [size for size in resolutions
                 if isinstance(size, list) and len(size) == 2
                 and all(type(n) is int and 128 <= n <= 2048 for n in size)] or [[512, 512]]}
        default = model.get("generation_size") or 512
        w, h = [default, default] if [default, default] in sizes["list"] else sizes["list"][0]
    elif family == "dit":
        sizes = {"kind": "dit", "min": 512, "max": 2048, "step": 64, "presets": DIT_SIZES}
        w = h = model.get("generation_size") or 1024
    elif family == "sd15cpu":
        sizes = {"kind": "square", "min": 128, "max": 512, "step": 64}
        w = h = model.get("generation_size") or 256
    else:
        sizes = {"kind": "fixed", "size": 1024, "aspects": ["1:1", "3:4", "4:3"] if use_img2img else ["1:1"]}
        w = h = 1024
    d = model.get("defaults") or {}
    if not isinstance(d, dict):
        raise PhoneError("bad_param", "invalid model defaults")
    steps = d.get("steps", 20)
    # The native catalog also serializes integral defaults as floats.
    if (not _number(steps, 1, 50) or steps != int(steps)
            or not _number(d.get("cfg", 7.0), 1, 30)
            or not all(type(n) is int and 128 <= n <= 2048 for n in (w, h))
            or not all(isinstance(d.get(k, ""), str) for k in ("prompt", "negative_prompt"))
            or not isinstance(d.get("scheduler", "dpm"), str)):
        raise PhoneError("bad_param", "invalid model defaults")
    # All native SD/Anima pipelines share the mask path. Only advertise DiT
    # variants verified in the native server; a future kind may differ.
    inpaint = bool(use_img2img) and (family != "dit" or model.get("dit_kind") in ("zimage", "klein", "qwen21"))
    reference_edit = bool(use_img2img) and model.get("dit_kind") in ("klein", "qwen21")
    return {"family": family, "sizes": sizes, "img2img": bool(use_img2img), "inpaint": inpaint,
            **({"reference_edit": True, "max_references": MAX_REFERENCES} if reference_edit else {}),
            "defaults": {"prompt": d.get("prompt", ""), "negative_prompt": d.get("negative_prompt", ""),
                         "steps": int(steps), "cfg": d.get("cfg", 7.0), "scheduler": d.get("scheduler") or "dpm",
                         "width": w, "height": h, "aspect_ratio": "1:1"}}


def _number(value, lo, hi, integer=False) -> bool:
    return (type(value) is int if integer else type(value) in (int, float)) and math.isfinite(value) and lo <= value <= hi


def validate(model, width, height, aspect, steps, cfg, strength, seed):
    c = capabilities(model, True)
    if not _number(width, 1, 2048, True) or not _number(height, 1, 2048, True):
        raise PhoneError("bad_size")
    family = c["family"]
    if ((family == "sd15" and [width, height] not in c["sizes"]["list"])
            or (family == "sd15cpu" and (width != height or not 128 <= width <= 512 or width % 64))
            or (family in ("sdxl", "anima") and (width, height) != (1024, 1024))
            or (family == "dit" and any(n < 512 or n > 2048 or n % 64 for n in (width, height)))):
        raise PhoneError("bad_size", "resolution is not supported by this model")
    if aspect not in (None, "1:1", "3:4", "4:3") or (aspect not in (None, "1:1") and family not in ("sdxl", "anima")):
        raise PhoneError("bad_size", "aspect ratio is not supported by this model")
    # Upstream settings ranges; seed is an unsigned 32-bit native value.
    for field, value, low, high, integer in (("steps", steps, 1, 50, True), ("cfg", cfg, 1, 30, False),
                                             ("strength", strength, 0, 1, False), ("seed", seed, 0, 4294967295, True)):
        if field == "seed" and value is None:
            continue
        if not _number(value, low, high, integer):
            raise PhoneError("bad_param", f"{field}: {low}..{high}")


def image_data(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PhoneError("bad_image")
    if value.startswith("data:"):
        if not re.match(r"data:image/(png|jpeg|webp);base64,", value):
            raise PhoneError("bad_image")
        value = value.split(",", 1)[1]
    if len(value) > 4 * ((MAX_REFERENCE_BYTES + 2) // 3):
        raise PhoneError("bad_image", "image exceeds 6 MiB")
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) > MAX_REFERENCE_BYTES:
            raise PhoneError("bad_image", "image exceeds 6 MiB")
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ("PNG", "JPEG", "MPO", "WEBP") or img.width * img.height > MAX_REFERENCE_PIXELS:
                raise PhoneError("bad_image", "use PNG, JPEG or WebP, at most 16 MP")
            img.verify()
    except (ValueError, OSError, Image.DecompressionBombError):
        raise PhoneError("bad_image", "invalid reference image") from None
    return value


def reference_data(values) -> list[str]:
    """Validate inputs, then orient/downscale references to native-compatible RGB JPEG."""
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_REFERENCES:
        raise PhoneError("bad_param", f"reference_images must contain 1..{MAX_REFERENCES} images")
    images = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise PhoneError("bad_image")
        if value.startswith("data:"):
            if not re.match(r"data:image/(png|jpeg|webp);base64,", value):
                raise PhoneError("bad_image")
            value = value.split(",", 1)[1]
        if len(value) > 4 * ((MAX_REFERENCE_BYTES + 2) // 3):
            raise PhoneError("too_large", "reference image exceeds 6 MiB")
        try:
            raw = base64.b64decode(value, validate=True)
            if len(raw) > MAX_REFERENCE_BYTES:
                raise PhoneError("too_large", "reference image exceeds 6 MiB")
            with Image.open(io.BytesIO(raw)) as img:
                if img.format not in ("PNG", "JPEG", "MPO", "WEBP"):
                    raise PhoneError("bad_image", "references must be PNG, JPEG or WebP")
                if img.width * img.height > MAX_REFERENCE_PIXELS:
                    raise PhoneError("too_large", "reference image exceeds 16 megapixels")
                img.load()
                with ImageOps.exif_transpose(img) as oriented:
                    width, height = oriented.size
                    pixels = width * height
                    if pixels > MAX_REFERENCE_OUTPUT_PIXELS:
                        # Also bound one-pixel-wide inputs after integer rounding.
                        scale = min(math.sqrt(MAX_REFERENCE_OUTPUT_PIXELS / pixels),
                                    MAX_REFERENCE_OUTPUT_PIXELS / max(width, height))
                        oriented.thumbnail((max(1, int(width * scale)), max(1, int(height * scale))),
                                           Image.Resampling.LANCZOS)
                    with oriented.convert("RGB") as rgb:
                        encoded = io.BytesIO()
                        rgb.save(encoded, format="JPEG", quality=95)
        except (ValueError, OSError, Image.DecompressionBombError):
            raise PhoneError("bad_image", "invalid reference image") from None
        images.append(base64.b64encode(encoded.getvalue()).decode("ascii"))
    return images


def prepare(phone: dict, prompt: str, *, model_id=None, width=None, height=None, aspect_ratio=None,
            neg=None, steps=None, cfg=None, scheduler=None, seed=None, image_b64=None,
            strength=None, mask_b64=None, reference=None, reference_images=None) -> tuple[dict, dict]:
    """Resolve model defaults and validate before selecting or sending a generation."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise PhoneError("empty_prompt")
    models = phone.get("models", [])
    if not models:
        raise PhoneError("no_models")
    model = next((m for m in models if m["id"] == model_id), None) if model_id else models[0]
    if model is None:
        raise PhoneError("model_missing")
    c = capabilities(model, phone.get("use_img2img", False))
    if reference_images is not None:
        if not c.get("reference_edit"):
            raise PhoneError("bad_param", "reference editing requires Klein or Qwen21 with image-to-image enabled")
        if image_b64 is not None or mask_b64 is not None or strength is not None:
            raise PhoneError("bad_param", "choose reference editing or image-to-image/mask")
        reference_images = reference_data(reference_images)
    d = c["defaults"]
    width, height = d["width"] if width is None else width, d["height"] if height is None else height
    steps, cfg = d["steps"] if steps is None else steps, d["cfg"] if cfg is None else cfg
    neg, scheduler = d["negative_prompt"] if neg is None else neg, d["scheduler"] if scheduler is None else scheduler
    strength, aspect = 0.6 if strength is None else strength, d["aspect_ratio"] if aspect_ratio is None else aspect_ratio
    validate(model, width, height, aspect, steps, cfg, strength, seed)
    if not isinstance(neg, str) or not isinstance(scheduler, str) or not scheduler.strip() or len(scheduler) > 64:
        raise PhoneError("bad_param", "negative prompt and scheduler must be strings")
    if reference is not None and not isinstance(reference, str):
        raise PhoneError("bad_param", "reference name must be a string")
    if (image_b64 is not None or mask_b64 is not None or aspect != "1:1") and not c["img2img"]:
        raise PhoneError("img2img_disabled")
    if mask_b64 is not None and image_b64 is None:
        raise PhoneError("bad_param", "mask requires an image")
    if mask_b64 is not None and not c["inpaint"]:
        raise PhoneError("bad_param", "inpainting is not supported by this model")
    image_b64, mask_b64 = image_data(image_b64), image_data(mask_b64)
    body = {"prompt": prompt, "negative_prompt": neg, "steps": steps, "cfg": cfg, "scheduler": scheduler,
            "width": width, "height": height, "aspect_ratio": aspect, "use_opencl": False,
            "output_format": "raw", "show_diffusion_process": False}
    if seed is not None:
        body["seed"] = seed
    if image_b64 is not None:
        body.update(image=image_b64, denoise_strength=strength)
    if mask_b64 is not None:
        body["mask"] = mask_b64
    if reference_images is not None:
        body["reference_images"] = reference_images
    return model, body


# --- model unloading ---------------------------------------------------------------------------
def stop(phone: dict | None = None) -> None:
    if phone is None:
        if not phones():
            return
        phone = get_phone()
    with phone_lock(phone):
        _stop_worker(api(phone))


def _stop_worker(client: Api) -> None:
    model_id = client.status().get("serving_model_id")
    if model_id:
        client.stop(model_id)


# --- generation and atomic output ---------------------------------------------------------------
def decode_image(d: dict) -> Image.Image:
    try:
        w, h = d["width"], d["height"]
        if not _number(w, 1, 8192, True) or not _number(h, 1, 8192, True):
            raise ValueError("invalid dimensions")
        if not _number(d.get("seed"), 0, 4294967295, True):
            raise ValueError("invalid seed")
        raw, fmt = base64.b64decode(d["image"], validate=True), d.get("format", "raw")
        if fmt == "raw":
            c = d.get("channels", 3)
            if type(c) is not int or c not in (3, 4) or len(raw) != w * h * c:
                raise ValueError("raw byte length does not match dimensions/channels")
            return Image.frombytes("RGB" if c == 3 else "RGBA", (w, h), raw)
        if fmt not in ("jpeg", "png"):
            raise ValueError("unsupported image format")
        with Image.open(io.BytesIO(raw)) as img:
            if img.format != fmt.upper() or img.size != (w, h):
                raise ValueError("image format or dimensions do not match the result")
            img.load()
            return img.copy()
    except (KeyError, TypeError, ValueError, OSError, Image.DecompressionBombError) as e:
        raise PhoneError("bad_image", "invalid generated image") from e


def _save_image(img: Image.Image, meta: dict) -> Path:
    """Reserve a unique stem, then replace complete temporary PNG and JSON files."""
    tmp_png = tmp_meta = reservation = None
    try:
        day = inside_out(OUT / f"{datetime.now():%Y-%m-%d}")
        day.mkdir(parents=True, exist_ok=True)
        if not _number(meta["seed"], 0, 4294967295, True):
            raise PhoneError("bad_image", "invalid returned seed")
        stem, n = f"{datetime.now():%H%M%S}_{meta['seed']}", 1
        while True:
            base = day / (stem if n == 1 else f"{stem}_{n}")
            path, sidecar, reservation = base.with_suffix(".png"), base.with_suffix(".json"), base.with_suffix(".reserve")
            try:
                with reservation.open("x"):
                    pass
            except FileExistsError:
                reservation = None
                n += 1
                continue
            if path.exists() or sidecar.exists() or path.is_symlink() or sidecar.is_symlink():
                reservation.unlink()
                reservation = None
                n += 1
                continue
            break
        for suffix in (".png.tmp", ".json.tmp"):
            fd, name = tempfile.mkstemp(prefix=".ldpc-", suffix=suffix, dir=day)
            os.close(fd)
            if suffix == ".png.tmp":
                tmp_png = Path(name)
            else:
                tmp_meta = Path(name)
        img.save(tmp_png, format="PNG")
        with tmp_meta.open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_meta, sidecar)
        try:
            os.replace(tmp_png, path)
        except OSError:
            sidecar.unlink(missing_ok=True)
            raise
        return path.resolve()
    except OSError as e:
        raise PhoneError("save_failed", "could not save image and metadata") from e
    finally:
        for p in (tmp_png, tmp_meta, reservation):
            if p:
                p.unlink(missing_ok=True)


def generate(prompt: str, *, phone_id=None, model_id=None, width=None, height=None, aspect_ratio=None,
             neg=None, steps=None, cfg=None, scheduler=None, seed=None, image_b64=None, strength=None,
             mask_b64=None, reference=None, reference_images=None, progress=_noop, stage=_noop, warning=_noop, context=None) -> tuple[Path, dict]:
    phone = get_phone(phone_id)
    with phone_lock(phone):
        args = dict(model_id=model_id, width=width, height=height, aspect_ratio=aspect_ratio, neg=neg,
                    steps=steps, cfg=cfg, scheduler=scheduler, seed=seed, image_b64=image_b64,
                    strength=strength, mask_b64=mask_b64, reference=reference, reference_images=reference_images)
        if reference_images is not None and not phone.get("models"):
            # Reference support cannot be established locally; do not probe the phone.
            raise PhoneError("no_models", "refresh the model catalog before reference editing")
        if phone.get("models"):
            prepare(phone, prompt, **args)                   # known invalid input never reaches I/O
        stage("connect")
        client = api(phone)
        ensure_host(phone, stage, client)
        info = client.info()
        state = info.get("app_state") if isinstance(info.get("app_state"), dict) else {}
        if context is not None:
            context.setdefault("app_state", {}).update(state)
        list_models(phone, client)
        model, body = prepare(phone, prompt, **args)
        before = state
        foreground = foreground_before_work(phone, state)
        # adb may observe a keyguard on any phone. Only phones reporting app_state
        # participate in the app-state advisory and diagnosis contract.
        if app_features(info)["app_state"]:
            state = foreground
        if state.get("foreground") is True and before.get("foreground") is not True:
            try:
                refreshed = client.info(timeout=2).get("app_state")
                if isinstance(refreshed, dict):
                    state = refreshed
            except PhoneError:
                state = before                       # keep the warning if foreground is unconfirmed
        if context is not None:
            context["app_state"].clear()
            context["app_state"].update(state)
        warnings = [APP_WARNINGS[code] for code in app_warnings(state)]
        if warning is not _noop:
            memory = memory_status(phone, model)
            if reference_count(body) and capabilities(model, phone.get("use_img2img", False))["family"] in ("dit", "sdxl", "anima"):
                memory["needed_mb"] += REFERENCE_EXTRA_MB * reference_count(body)
                memory["low"] = memory["available_mb"] is not None and memory["available_mb"] < memory["needed_mb"]
            if memory["low"]:
                warnings.append(memory_warning(memory))
        for message in warnings:
            warning(message)
        select(phone, model["id"], body["width"], body["height"], stage, client)
        stage("generate")
        try:
            n, cap = client.tokenize(prompt)
        except PhoneError:
            n, cap = None, None
        t0 = time.monotonic()
        try:
            d = client.generate(body, progress)
        except PhoneError as e:
            if e.code == "stream_broken":
                # Keep transport evidence; losing /info alone cannot prove an app exit.
                ensure_host(phone, client=client)
                if client.status().get("state") in ("idle", "stopped", "error"):
                    raise PhoneError("worker_stopped") from e
            raise
        img = decode_image(d)
        stage("save")
        meta = {k: v for k, v in body.items() if k not in ("image", "mask", "reference_images")} | {
            "model": model["name"], "model_id": model["id"], "family": capabilities(model, phone.get("use_img2img", False))["family"],
            "phone": phone["name"], "phone_id": phone["id"], "transport": phone["transport"],
            "seed": d["seed"], "width": d["width"], "height": d["height"],
            "reference": Path(reference.replace("\\", "/")).name if reference else None, "img2img": image_b64 is not None,
            "inpaint": mask_b64 is not None, "prompt_tokens": n, "prompt_token_cap": cap,
            "phone_ms": d.get("generation_time_ms"), "wall_s": round(time.monotonic() - t0, 1),
            "app_version": info.get("version"), "warnings": warnings}
        if reference_images is not None:
            meta.update(reference_edit=True, reference_count=len(body["reference_images"]))
        return _save_image(img, meta), meta


# --- CLI -----------------------------------------------------------------------------------------
def b64file(path: str | None) -> str | None:
    return base64.b64encode(Path(path).read_bytes()).decode() if path else None


def parse_size(value: str):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,4}[xX]\d{1,4}", value):
        raise PhoneError("bad_size")
    return tuple(int(x) for x in value.lower().split("x"))


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PhoneError("bad_param", message)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    argv = [x for x in argv if x != "--json"]
    phone = None
    if "--phone" in argv:
        i = argv.index("--phone")
        if i + 1 < len(argv):
            phone = argv[i + 1]
            del argv[i:i + 2]
    ap = Parser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=Parser)
    for cmd in ("devices", "models", "stop", "host-mode"):
        sub.add_parser(cmd)
    lan = sub.add_parser("add-lan")
    lan.add_argument("host")
    lan.add_argument("--name")
    pairing = sub.add_parser("pair")
    pairing.add_argument("address")
    pairing.add_argument("code")
    pairing.add_argument("--connect")
    g = sub.add_parser("gen")
    g.add_argument("prompt")
    for name in ("model", "size", "aspect", "scheduler", "neg", "image", "mask"):
        g.add_argument("--" + name)
    for name, kind in (("steps", int), ("cfg", float), ("seed", int), ("strength", float)):
        g.add_argument("--" + name, type=kind)
    cli_warnings = []

    def warn(message):
        cli_warnings.append(message)
        if not json_mode:
            print(message, file=sys.stderr)

    try:
        a = ap.parse_args(argv)
        result = {"ok": True}
        if a.cmd == "devices":
            result.update(phone_status())
        elif a.cmd == "models":
            payload = []
            for p in ([get_phone(phone)] if phone else phones()):
                ensure_host(p)
                models = list_models(p)
                payload.append({"id": p["id"], "name": p["name"], "transport": p["transport"],
                                "models": [m | capabilities(m, p.get("use_img2img", False)) for m in models]})
            result["phones"] = payload
        elif a.cmd == "add-lan":
            result["phone"] = add_lan(a.host, a.name)
        elif a.cmd == "pair":
            result["phone"] = pair(a.address, a.code, a.connect)
        elif a.cmd == "host-mode":
            if not phone:
                raise PhoneError("bad_param", "host-mode requires --phone")
            open_host_mode(get_phone(phone))
        elif a.cmd == "stop":
            stop(get_phone(phone) if phone else None)
        else:
            w, h = parse_size(a.size) if a.size else (None, None)
            path, meta = generate(a.prompt, phone_id=phone, model_id=a.model, width=w, height=h,
                                  aspect_ratio=a.aspect, neg=a.neg, steps=a.steps, cfg=a.cfg, scheduler=a.scheduler,
                                  seed=a.seed, image_b64=b64file(a.image), strength=a.strength,
                                  mask_b64=b64file(a.mask), reference=a.image,
                                  warning=warn,
                                  progress=_noop if json_mode else lambda s, t: print(f"step {s}/{t}"),
                                  stage=_noop if json_mode else lambda s: print(f"[{s}]"))
            result.update(path=str(path), meta=meta, warnings=meta.get("warnings", []))
        if "phone" in result:
            result["phone"] = public_phone(result["phone"])
        if "phones" in result:
            result["phones"] = [public_phone(p) for p in result["phones"]]
        print(json.dumps(result, allow_nan=False) if json_mode else result.get("path") or json.dumps(result, indent=2))
        return 0
    except (PhoneError, OSError, ValueError) as e:
        error = e if isinstance(e, PhoneError) else PhoneError("bad_param", "invalid input or configuration")
        if json_mode:
            print(json.dumps({"ok": False, "error": error.code, "detail": error.detail, "warnings": cli_warnings}))
        else:
            print(f"{error.code}: {error.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
