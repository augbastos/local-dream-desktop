"""Local Dream Desktop: loopback browser front end at http://127.0.0.1:47865.

The phone is the server; this page sends requests and displays the results.
"""
from __future__ import annotations

import json
import http.client
import re
import socket
import statistics
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from paths import APP_ID, __version__, prepare_config, resource_dir, server_port

prepare_config()
import sdgen

HOST, PORT = "127.0.0.1", server_port()
URL = f"http://{HOST}:{PORT}/"
BODY_CAP = 32 * 1024 * 1024
CATALOG_REFRESH_INTERVAL = 60
HTML = resource_dir() / "studio.html"
lock, state_lock = threading.Lock(), threading.RLock()
st = {"busy": None, "error": None, "diagnosis": None, "last": None, "last_poll": 0.0, "job": None,
      "phones": {"phones": [], "usb_waiting": False, "usb_new": False}}
_job_id = 0
_catalog_refresh_after: dict[str, float] = {}
_active_server = None


class StartupError(Exception):
    """An actionable startup error safe to display without private exception data."""


def is_our_status(payload) -> bool:
    return (isinstance(payload, dict) and payload.get("app") == APP_ID
            and payload.get("version") == __version__)


def status_at(port=PORT, timeout=1):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{HOST}:{port}/api/status", timeout=timeout) as response:
        return json.loads(response.read(1024 * 1024))


def log(msg: str) -> None:
    sdgen.OUT.mkdir(parents=True, exist_ok=True)
    with sdgen.inside_out(sdgen.OUT / "studio.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def stage(name: str) -> None:
    with state_lock:
        job, now = st["job"], time.time()
        if job is None:
            return
        if job["stages"] and job["stages"][-1][2] is None:
            job["stages"][-1][2] = now
        job["stages"].append([name, now, None])


def run(label: str, fn, estimate: float | None = None, phone_id=None, request=None, server=None) -> int | None:
    """Reserve busy/job synchronously, before the caller can return 202."""
    global _job_id
    with state_lock:
        server = server or _active_server
        if (server is not None and server.quitting) or not lock.acquire(blocking=False):
            return None
        _job_id += 1
        job_id = _job_id
        started = time.time()
        started_monotonic = time.monotonic()
        st.update(busy=label, error=None, diagnosis=None, job={"id": job_id, "started": started, "stages": [],
                   "progress": None, "step_times": [], "estimate_s": estimate, "phone_id": phone_id,
                   "model_id": request.get("model_id") if request is not None else None,
                   "app_state": request.setdefault("app_state", {}) if request is not None else None})

    def work():
        try:
            if request is not None:
                request["started_monotonic"] = started_monotonic
                request["phone_started"] = sdgen.phone_job_clock(sdgen.get_phone(phone_id))
            fn()
        except Exception as e:                                # the browser must hear about failures
            error = e if isinstance(e, sdgen.PhoneError) else sdgen.PhoneError("stream_broken", "operation failed")
            diagnosis = None
            if request is not None:
                try:
                    diagnosis = sdgen.diagnose_failure(sdgen.get_phone(phone_id), {**request, "started": started}, error)
                except Exception:
                    # Diagnosis must never hide the original failure, even if the phone was removed.
                    diagnosis = {"cause": "unknown", "causes": ["unknown"], "evidence": ["adb_unavailable"], "actions": ["retry"],
                                 "apps": [], "smaller": None}
            with state_lock:
                st["error"] = {"code": error.code, "detail": error.detail}
                st["diagnosis"] = diagnosis
            try:
                log(f"{label}: {error.code}")
            except OSError:
                pass
        finally:
            with state_lock:
                st.update(busy=None, job=None)
                lock.release()
                if server is not None and server.quitting:
                    server.begin_shutdown()

    try:
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        with state_lock:
            st.update(busy=None, job=None)
        lock.release()
        raise
    return job_id


def progress(step: int, total: int) -> None:
    with state_lock:
        if st["job"]:
            st["job"]["progress"] = [step, total]
            st["job"]["step_times"].append(time.time())


def out_path(url: str) -> Path:
    if not isinstance(url, str) or not re.fullmatch(r"/out/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_-]+\.png", url):
        raise sdgen.PhoneError("bad_param", "invalid image URL")
    return sdgen.inside_out(sdgen.OUT / url[5:])


def gallery(limit: int = 60) -> list[dict]:
    items = []
    candidates = []
    for png in sdgen.OUT.glob("*/*.png"):
        try:
            url = f"/out/{png.parent.name}/{png.name}"
            p = out_path(url)
            if p.is_file():
                candidates.append((p.stat().st_mtime, url, p))
        except (OSError, sdgen.PhoneError):
            continue
    for _, url, png in sorted(candidates, reverse=True)[:limit]:
        try:
            meta = json.loads(sdgen.inside_out(png.with_suffix(".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError, sdgen.PhoneError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        items.append({"url": url, "meta": meta})
    return items


def estimate(model_name: str | None, w: int, h: int, steps: int) -> float | None:
    """Median milliseconds per step per megapixel, scaled to this generation."""
    rates = []
    for it in gallery(200):
        m = it["meta"]
        try:
            if m.get("phone_ms") and m.get("steps") and m.get("width") and (model_name is None or m.get("model") == model_name):
                rates.append(m["phone_ms"] / m["steps"] / (m["width"] * m["height"] / 1e6))
        except (TypeError, KeyError, ZeroDivisionError):
            continue
    return round(statistics.median(rates) * steps * (w * h / 1e6) / 1000, 1) if rates else None


def refresh_phones() -> None:
    if _active_server is not None and _active_server.quitting:
        return
    st["phones"] = sdgen.phone_status()
    if _active_server is not None and _active_server.quitting:
        return
    if st["phones"]["usb_new"] and not st["busy"]:
        run("connecting", reconnect)
    ready = {p["id"] for p in st["phones"]["phones"] if p["state"] == "ready"}
    for p in sdgen.phones():
        if _active_server is not None and _active_server.quitting:
            return
        if p["id"] not in ready or p.get("models"):
            continue
        with state_lock:
            if st["busy"] or lock.locked():
                return
            now = time.monotonic()
            if now < _catalog_refresh_after.get(p["id"], 0):
                continue
            # Reserve the attempt before I/O, including failed or empty results.
            _catalog_refresh_after[p["id"]] = now + CATALOG_REFRESH_INTERVAL
        try:
            expected_catalog = (p["record_id"], p["catalog_revision"])
            # Fetch into this snapshot so a slow phone never reserves the job lock.
            sdgen.list_models(p, persist=False)
            if not lock.acquire(blocking=False):
                continue
            try:
                # Compare and store under the registry's thread/process lock, too:
                # even a newer empty catalog or a re-added phone wins this race.
                sdgen._update(p["id"], expected_catalog=expected_catalog,
                              models=p["models"], use_img2img=p["use_img2img"])
            finally:
                lock.release()
        except sdgen.PhoneError:
            log("watch: model refresh failed")


def reconnect(*, include_ignored=False) -> None:
    stage("connect")
    sdgen.connect_usb(include_ignored=include_ignored)
    for p in sdgen.phones():
        if sdgen.host_up(p, 1.5):
            sdgen.list_models(p)
    st["phones"] = sdgen.phone_status()


def watch(stop_event=None) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            refresh_phones()
        except Exception:
            try:
                log("watch: refresh failed")
            except OSError:
                pass
        delay = 4 if time.time() - st["last_poll"] < 30 else 30
        if stop_event is None:
            time.sleep(delay)
        else:
            stop_event.wait(delay)


def models_payload() -> list[dict]:
    states = {p["id"]: p for p in st["phones"]["phones"]}
    return [sdgen.public_phone({"id": p["id"], "name": p["name"], "transport": p["transport"],
             "state": states.get(p["id"], {}).get("state", "unreachable"),
             "features": states.get(p["id"], {}).get("features", {}),
             "models": [{"id": m["id"], "name": m["name"], "description": m.get("description", ""),
                         "dit_kind": m.get("dit_kind", ""), **sdgen.capabilities(m, p.get("use_img2img", False))}
                        for m in p.get("models", [])]}) for p in sdgen.phones()]


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        # A client that never sends a request must not prevent clean shutdown.
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def foreign(self) -> bool:
        port = self.server.server_address[1]
        allowed = {f"{h}:{port}" for h in ("localdream.localhost", "127.0.0.1", "localhost")}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in allowed:
            return True
        if self.path.startswith("/api/") and any(
                site not in ("same-origin", "none") for site in self.headers.get_all("Sec-Fetch-Site", [])):
            return True
        if origin is None:
            return False
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return True
        return parsed.scheme != "http" or parsed.netloc not in allowed or bool(parsed.path or parsed.query or parsed.fragment)

    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; "
                         "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.send(code, json.dumps(obj, allow_nan=False).encode(), "application/json")

    def error(self, error: sdgen.PhoneError, status=400):
        self.json({"error": error.code, "detail": error.detail}, status)

    def do_GET(self):
        if self.foreign():
            return self.error(sdgen.PhoneError("bad_host", "forbidden host or origin"), 403)
        try:
            if self.path == "/":
                return self.send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/vendor/qrcode.js":
                return self.send(200, (HTML.parent / "vendor" / "qrcode.js").read_bytes(), "text/javascript; charset=utf-8")
            if self.path == "/api/desktop-link":
                return self.json(sdgen.pairing_payload())
            if urllib.parse.urlsplit(self.path).path == "/api/memory":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                phone = sdgen.get_phone(query.get("phone_id", [None])[0])
                model_id = query.get("model_id", [None])[0]
                model = next((m for m in phone.get("models", []) if m["id"] == model_id), None)
                if model is None:
                    raise sdgen.PhoneError("model_missing")
                payload = sdgen.memory_status(phone, model)
                try:
                    state = sdgen.api(phone).info(timeout=2).get("app_state")
                    if isinstance(state, dict):
                        payload["app_state"] = state
                except sdgen.PhoneError:
                    pass                              # advisory must not block generation
                return self.json(payload)
            if self.path == "/api/status":
                with state_lock:
                    st["last_poll"] = time.time()
                    job = st["job"]
                    payload = {"app": APP_ID, "version": __version__,
                               "capabilities": {"quit": True}, "quitting": self.server.quitting,
                               "busy": st["busy"], "error": st["error"], "diagnosis": st["diagnosis"], "last": st["last"], "now": time.time(),
                               "phones": {**st["phones"], "phones": [sdgen.public_phone(p) for p in st["phones"]["phones"]]},
                               "job": job and {**job, "step_times": job["step_times"][-6:]}}
                    return self.json(payload)
            if self.path == "/api/models":
                return self.json(models_payload())
            if self.path == "/api/gallery":
                return self.json(gallery())
            if self.path.startswith("/out/"):
                p = out_path(self.path)
                if p.is_file():
                    return self.send(200, p.read_bytes(), "image/png")
            return self.error(sdgen.PhoneError("bad_param", "not found"), 404)
        except sdgen.PhoneError as e:
            self.error(e, 404 if self.path.startswith("/out/") else 400)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            self.error(sdgen.PhoneError("bad_param", "invalid catalog or response"), 400)
        except OSError:
            self.error(sdgen.PhoneError("bad_param", "not found"), 404)

    def do_POST(self):
        if self.foreign():
            return self.error(sdgen.PhoneError("bad_host", "forbidden host or origin"), 403)
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1 or self.headers.get("Transfer-Encoding"):
                raise sdgen.PhoneError("bad_param", "ambiguous request body")
            try:
                n = int(lengths[0]) if lengths else 0
            except ValueError:
                raise sdgen.PhoneError("bad_param", "invalid Content-Length") from None
            if n < 0:
                raise sdgen.PhoneError("bad_param", "invalid Content-Length")
            if n > BODY_CAP:
                return self.error(sdgen.PhoneError("too_large"), 413)
            self.connection.settimeout(10)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeError):
                raise sdgen.PhoneError("bad_param", "invalid JSON") from None
            if not isinstance(body, dict):
                raise sdgen.PhoneError("bad_param", "JSON object required")
            if self.path == "/api/quit":
                if "after_job" in body and not isinstance(body["after_job"], bool):
                    raise sdgen.PhoneError("bad_param", "after_job must be a boolean")
                with state_lock:
                    busy = st["busy"] is not None or lock.locked()
                    if busy and not body.get("after_job", False):
                        return self.error(sdgen.PhoneError("job_running", "A job is running. Wait, or quit with after_job: true."), 409)
                    self.server.quitting = True
                    self.server.stop_event.set()
                    self.json({"quitting": True, "after_job": busy}, 202)
                    self.server.shutdown_when_idle()
                return
            if self.server.quitting:
                return self.error(sdgen.PhoneError("shutting_down", "Local Dream Desktop is shutting down."), 409)
            if self.path == "/api/discover":
                found = sdgen.discover(adopt=not st["busy"])
                with state_lock:
                    st["phones"] = sdgen.phone_status()
                return self.json([sdgen.public_phone(p) for p in found])
            if self.path == "/api/tokenize":
                try:
                    p = sdgen.get_phone(body.get("phone_id"))
                    prompt = body.get("prompt", "")
                    if not isinstance(prompt, str):
                        raise sdgen.PhoneError("bad_param")
                    n, cap = sdgen.tokens(p, prompt) if not st["busy"] and sdgen._gen_up(p) else (None, 0)
                except sdgen.PhoneError:
                    n, cap = None, 0
                return self.json({"count": n, "max": cap})
            if st["busy"]:
                raise sdgen.PhoneError("busy")
            phone_id = body.get("phone_id")
            if phone_id is not None and not isinstance(phone_id, str):
                raise sdgen.PhoneError("bad_param", "phone_id must be a string")
            label, est, diagnosis_request = "connecting", None, None
            if self.path == "/api/generate":
                p = sdgen.get_phone(phone_id)
                args = {k: body.get(k) for k in ("model_id", "width", "height", "aspect_ratio", "neg", "steps", "cfg", "scheduler", "seed", "strength")}
                args.update(image_b64=body.get("image"), mask_b64=body.get("mask"), reference=body.get("reference_name"))
                if "reference_images" in body:
                    # Null is not a native reference array; omission keeps legacy requests unchanged.
                    if body["reference_images"] is None:
                        raise sdgen.PhoneError("bad_param", "reference_images must be a non-empty array")
                    args["reference_images"] = body["reference_images"]
                prompt = body.get("prompt")
                model, sent = sdgen.prepare(p, prompt, **args)
                diagnosis_request = {**sent, "model_id": model["id"],
                                     "capabilities": sdgen.capabilities(model, p.get("use_img2img", False))}
                with sdgen.phone_lock(p):                    # reject a CLI already using this phone
                    pass
                phone_id, label = p["id"], "generating"
                est = estimate(model["name"], sent["width"], sent["height"], sent["steps"])

                def fn():
                    path, meta = sdgen.generate(prompt, phone_id=phone_id, **args, progress=progress, stage=stage, context=diagnosis_request)
                    with state_lock:
                        st["last"] = {"url": f"/out/{path.parent.name}/{path.name}", "meta": meta}
                    log(f"generated {path.name}")
            elif self.path == "/api/delete":
                url = body.get("url")
                p = out_path(url)
                original = sdgen.OUT / url[5:]
                sidecar = sdgen.inside_out(p.with_suffix(".json"))
                if original.is_symlink() or p.with_suffix(".json").is_symlink() or not p.is_file():
                    return self.error(sdgen.PhoneError("bad_param", "not a regular image"), 404)
                p.unlink()
                sidecar.unlink(missing_ok=True)
                if (st["last"] or {}).get("url") == url:
                    st["last"] = None
                return self.json({"deleted": True})
            elif self.path == "/api/phones/add_lan":
                host, name = body.get("host"), body.get("name")
                sdgen.valid_host(host)
                if name is not None and not isinstance(name, str):
                    raise sdgen.PhoneError("bad_param")

                def fn():
                    stage("connect")
                    p = sdgen.add_lan(host, name)
                    sdgen.list_models(p)
                    st["phones"] = sdgen.phone_status()
            elif self.path == "/api/phones/transport":
                if not phone_id:
                    raise sdgen.PhoneError("bad_param", "phone_id required")
                return self.json({"phone": sdgen.public_phone(sdgen.set_transport(phone_id, body.get("transport")))})
            elif self.path == "/api/phones/remove":
                if not phone_id:
                    raise sdgen.PhoneError("bad_param", "phone_id required")
                sdgen.remove_phone(phone_id)
                return self.json({"removed": True})
            elif self.path == "/api/connect":
                fn = lambda: reconnect(include_ignored=True)
            elif self.path == "/api/pair":
                address = sdgen._address(body.get("address"))
                code = body.get("code")
                target = body.get("connect_address")
                if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code.strip()):
                    raise sdgen.PhoneError("pair_input")
                if target is not None:
                    sdgen._address(target)

                def fn():
                    stage("connect")
                    sdgen.pair(address, code, target)
            elif self.path == "/api/host_mode":
                if not phone_id:
                    raise sdgen.PhoneError("bad_param", "phone_id required")
                p = sdgen.get_phone(phone_id)
                fn = lambda: sdgen.open_host_mode(p)
            elif self.path == "/api/stop":
                label = "stopping"
                p = sdgen.get_phone(phone_id) if phone_id else None
                fn = lambda: sdgen.stop(p)
            else:
                return self.error(sdgen.PhoneError("bad_param", "not found"), 404)
            with state_lock:
                job_id = run(label, fn, est, phone_id, diagnosis_request, server=self.server)
            if job_id is None:
                raise sdgen.PhoneError("busy")
            self.json({"started": True, "job_id": job_id}, 202)
        except sdgen.PhoneError as e:
            self.error(e, 409 if e.code == "busy" else 400)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
            self.error(sdgen.PhoneError("bad_param", "invalid request"), 400)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = False

    def __init__(self, address, handler=Handler):
        if address[0] != HOST:
            raise ValueError("studio binds to loopback only")
        self.quitting = False
        self.stop_event = threading.Event()
        self._shutdown_started = False
        self._quit_waiter_started = False
        super().__init__(address, handler)

    def shutdown_when_idle(self):
        with state_lock:
            if self._quit_waiter_started:
                return
            self._quit_waiter_started = True

        def finish():
            # The catalog watcher also briefly owns this lock without a job.
            with lock:
                pass
            self.begin_shutdown()

        threading.Thread(target=finish, daemon=True).start()

    def begin_shutdown(self):
        with state_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self.quitting = True
            self.stop_event.set()
            # shutdown() must run outside serve_forever's own thread.
            threading.Thread(target=self.shutdown, daemon=True).start()

    def server_bind(self):
        # Windows SO_REUSEADDR permits another process to share a taken port.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main(open_browser=True, readiness_timeout=10) -> int:
    global _active_server
    try:
        srv = Server((HOST, PORT))
    except OSError as error:
        try:
            ours = is_our_status(status_at(PORT, timeout=3))
        except (OSError, ValueError, http.client.HTTPException):
            ours = False
        if ours:
            if open_browser:
                webbrowser.open(URL)
            return 0
        raise StartupError(f"Cannot listen on port {PORT}. Another program may be using it. Close it and try again.") from error
    _active_server = srv
    serving = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    watching = None
    try:
        log("studio started")
        serving.start()
        deadline = time.monotonic() + readiness_timeout
        while True:
            try:
                ready = is_our_status(status_at(PORT, timeout=0.5))
            except (OSError, ValueError, http.client.HTTPException):
                ready = False
            if ready:
                break
            if not serving.is_alive() or time.monotonic() >= deadline:
                raise StartupError("The server did not become ready. Close Local Dream Desktop and try again.")
            time.sleep(0.05)
        watching = threading.Thread(target=watch, args=(srv.stop_event,), daemon=True)
        watching.start()
        if open_browser:
            webbrowser.open(URL)
        serving.join()
        return 0
    except KeyboardInterrupt:
        # A terminal interrupt must also wait for any file save already in progress.
        with state_lock:
            srv.quitting = True
            srv.stop_event.set()
        with lock:
            pass
        return 0
    finally:
        srv.stop_event.set()
        if serving.is_alive():
            srv.shutdown()
            serving.join()
        srv.server_close()
        if watching is not None:
            watching.join(timeout=2)
        _active_server = None
        # log() opens/closes each write; sdgen owns and cleans its own temp files.
        # Leave adb and its forwards alone: they may be shared with other apps.
        log("studio stopped")


if __name__ == "__main__":
    from local_dream_desktop import main as entry_main
    raise SystemExit(entry_main())
