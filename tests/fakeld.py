"""Threaded Local Dream fixture: both planes bind to ephemeral loopback ports."""
import base64
import hashlib
import hmac
import io
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image


def model(family="sd15", **fields):
    value = {"id": family, "name": "Example " + family, "description": "Test model",
             "defaults": {"prompt": "default prompt", "negative_prompt": "model negative",
                          "steps": 4.0, "cfg": 5.0, "scheduler": "euler"},
             "resolutions": [[512, 512], [512, 768]] if family == "sd15" else [],
             "run_on_cpu": family == "sd15cpu", "is_sdxl": family == "sdxl",
             "is_anima": family == "anima", "dit_kind": "zimage" if family == "dit" else "",
             "is_custom": False,
             "generation_size": 256 if family == "sd15cpu" else 512 if family == "sd15" else 1024}
    value.update(fields)
    return value


def result(fmt="raw", channels=3, width=2, height=3, **fields):
    image = Image.new("RGB" if channels == 3 else "RGBA", (width, height), (20, 30, 40) if channels == 3 else (20, 30, 40, 255))
    if fmt == "raw":
        raw = image.tobytes()
    else:
        buffer = io.BytesIO()
        image.save(buffer, format=fmt.upper())
        raw = buffer.getvalue()
    value = {"type": "complete", "format": fmt, "channels": channels, "width": width, "height": height,
             "image": base64.b64encode(raw).decode(), "seed": 7, "generation_time_ms": 25}
    value.update(fields)
    return value


class FakeLD:
    def __init__(self):
        self.models = [model()]
        self.use_img2img = True
        self.app = "localdream"
        self.requests = []
        self.headers = []
        self.app_state = None
        self.desktop_link = None
        self.control_unauthorized = False
        self.control_redirect = None
        self.proof_token = None
        self.proof_override = None
        self.challenges = []
        self.state = {"state": "idle", "serving_model_id": None, "width": None, "height": None,
                      "message": None, "error_model_id": None}
        self.last_selected_model_id = None
        self.select_statuses = []
        self.mode = "complete"
        self.output = result()
        self.health_pending = 0
        self.select_error = None
        self.select_http_error = None
        self.tokenize_error = None
        self.http_error = None
        self.delay = 0.2
        self.started, self.release = threading.Event(), threading.Event()
        self.closed = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def info(self):
                info = {"app": owner.app, "protocol": 1, "version": "3.example", "device": "Example phone"}
                if owner.app_state is not None:
                    info["app_state"] = dict(owner.app_state)
                if owner.desktop_link is not None:
                    info["desktop_link"] = dict(owner.desktop_link)
                    nonce = self.headers.get("X-Desktop-Challenge")
                    if self.command == "GET" and nonce and owner.proof_token:
                        info["desktop_link"]["proofs"] = [hmac.new(owner.proof_token.encode("utf-8"),
                            nonce.encode("utf-8"), hashlib.sha256).hexdigest()]
                    if owner.proof_override is not None:
                        info["desktop_link"]["proofs"] = owner.proof_override
                return self.json(info)

            def do_GET(self):
                self.path = urllib.parse.urlsplit(self.path).path
                owner.requests.append(("GET", self.path, None))
                owner.headers.append((self.path, self.headers.get("X-Desktop-Token")))
                owner.challenges.append(self.headers.get("X-Desktop-Challenge"))
                if owner.control_redirect and self.server.server_address[1] == owner.ports[0]:
                    self.send_response(302)
                    self.send_header("Location", owner.control_redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if owner.control_unauthorized and self.path != "/info" and self.server.server_address[1] == owner.ports[0]:
                    return self.json({"error": "not paired"}, 401)
                control = self.server.server_address[1] == owner.ports[0]
                if ((self.path in ("/info", "/models", "/status") and not control)
                        or (self.path == "/health" and control)):
                    return self.json({"error": "not found"}, 404)
                if self.path == "/info":
                    self.info()
                elif self.path == "/models":
                    self.json({"models": owner.models, "use_img2img": owner.use_img2img, "upscalers": []})
                elif self.path == "/status":
                    self.json(owner.select_statuses.pop(0) if owner.select_statuses else owner.state)
                elif self.path == "/health":
                    pending = owner.health_pending
                    owner.health_pending = max(0, pending - 1)
                    self.json({"ok": not pending}, 503 if pending else 200)
                else:
                    self.json({"error": "not found"}, 404)

            def do_POST(self):
                self.path = urllib.parse.urlsplit(self.path).path
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                owner.requests.append(("POST", self.path, body))
                owner.headers.append((self.path, self.headers.get("X-Desktop-Token")))
                if owner.control_unauthorized and self.server.server_address[1] == owner.ports[0]:
                    return self.json({"error": "not paired"}, 401)
                control = self.server.server_address[1] == owner.ports[0]
                if ((self.path in ("/info", "/select", "/stop") and not control)
                        or (self.path in ("/tokenize", "/generate") and control)):
                    return self.json({"error": "not found"}, 404)
                if self.path == "/info":
                    return self.info()
                if self.path == "/select":
                    if owner.select_http_error:
                        detail = {400: "DiT resolution must be 512..2048 in 64-pixel steps",
                                  404: "model not found", 500: "backend start rejected"}
                        return self.json({"error": detail.get(owner.select_http_error, "example failure")},
                                         owner.select_http_error)
                    if body.get("model_id") != "__upscaler__" and not any(
                            m["id"] == body.get("model_id") for m in owner.models):
                        return self.json({"error": "model not found"}, 404)
                    owner.last_selected_model_id = body["model_id"]
                    owner.state = {"state": "error" if owner.select_error else "running", "serving_model_id": body["model_id"],
                                   "width": body.get("width", 512), "height": body.get("height", 512),
                                   "message": owner.select_error,
                                   "error_model_id": body["model_id"] if owner.select_error else None}
                    return self.json({"ok": True})
                if self.path == "/stop":
                    if body.get("model_id") and body["model_id"] != owner.last_selected_model_id:
                        return self.json({"ok": True, "ignored": True})
                    owner.last_selected_model_id = None
                    owner.state.update(state="idle", serving_model_id=None, width=None, height=None,
                                       message=None, error_model_id=None)
                    return self.json({"ok": True})
                if self.path == "/tokenize":
                    if owner.tokenize_error == "truncated":
                        self.send_response(200)
                        self.send_header("Content-Length", "100")
                        self.end_headers()
                        self.wfile.write(b"{}")
                        self.close_connection = True
                        return
                    if owner.tokenize_error == "invalid":
                        return self.json({"count": "invalid", "max_length": 0})
                    if owner.tokenize_error:
                        return self.json({"error": "tokenizer unavailable"}, owner.tokenize_error)
                    return self.json({"count": len(body["prompt"].split()), "max_length": 0})
                if self.path != "/generate":
                    return self.json({"error": "not found"}, 404)
                owner.started.set()
                if owner.http_error:
                    return self.json({"error": {"message": "example failure", "type": "test"}}, owner.http_error)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    if owner.mode == "block":
                        # The test (or fixture cleanup) releases this event. A wall-clock
                        # deadline can end generation before the busy assertions run.
                        owner.release.wait()
                    if owner.mode == "timeout":
                        time.sleep(owner.delay)
                    self.wfile.write(b'event: progress\ndata: {"type":"progress","step":1,"total_steps":4}\n\n')
                    self.wfile.flush()
                    if owner.mode == "premature":
                        return
                    if owner.mode == "error":
                        self.wfile.write(b'event: error\ndata: {"type":"error","message":"example failure"}\n\n')
                    elif owner.mode == "invalid":
                        self.wfile.write(b"event: complete\ndata: {broken\n\n")
                    else:
                        self.wfile.write(b"event: complete\ndata: " + json.dumps(owner.output).encode() + b"\n\n")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        for server in self.servers:
            server.daemon_threads = True
        self.ports = [s.server_address[1] for s in self.servers]
        self.bases = tuple(f"http://127.0.0.1:{p}" for p in self.ports)
        self.threads = []

    def __enter__(self):
        for server in self.servers:
            t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def __exit__(self, *_):
        if self.closed:
            return
        self.closed = True
        self.release.set()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for t in self.threads:
            t.join(2)

    def posts(self, path):
        return [body for method, route, body in self.requests if method == "POST" and route == path]
