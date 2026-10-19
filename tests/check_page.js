// Dependency-free page smoke: QR pairing, translated copy and advisory memory UI.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const zlib = require("node:zlib");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "studio.html"), "utf8");
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).join("\n");
const elements = new Map(), timers = new Map(), intervals = new Map(), requests = [];
let timerID = 0, paired = false, official = false, statusJob = null, focusedElement = null;
let statusOffline = false, customStatus = null, quitBusy = false, memoryGate = null, fileReads = 0;
let galleryItems = [];
const statusContract = process.argv.includes("--status-contract");
if (statusContract) customStatus = JSON.parse(fs.readFileSync(0, "utf8"));
let lanFailure = false;
function element() {
  return { value: "", textContent: "", innerHTML: "", hidden: false, disabled: false,
    dataset: {}, classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    attributes: {}, setAttribute(key, value) { this.attributes[key] = String(value); },
    getAttribute(key) { return this.attributes[key] ?? null; }, removeAttribute(key) { delete this.attributes[key]; },
    focus() { focusedElement = this; },
    querySelector(selector) {
      const match = selector.match(/^\[data-remove-reference="(\d+)"\]$/);
      if (!match) return [".open", ".imp", ".del"].includes(selector) ? element() : null;
      if (!this.innerHTML.includes(`data-remove-reference="${match[1]}"`)) return null;
      return { ...element(), dataset: { removeReference: match[1] } };
    }, querySelectorAll() { return []; }, appendChild() {}, addEventListener() {} };
}
// Small canvas double: exercise brush pixels, replay and PNG export without a
// browser dependency. Browser layout, antialiasing and capture need manual QA.
function canvasElement() {
  const canvas = { ...element(), width: 300, height: 150, captured: null, calls: [] };
  let pixels, dot, points = [];
  const data = () => {
    if (!pixels || pixels.length !== canvas.width * canvas.height * 4) pixels = new Uint8ClampedArray(canvas.width * canvas.height * 4);
    return pixels;
  };
  const color = () => ctx.fillStyle === "#fff" || ctx.strokeStyle === "#fff" ? 255 : 0;
  function circle(x, y, radius) {
    const value = color(), buffer = data();
    for (let py = Math.max(0, Math.floor(y - radius)); py < Math.min(canvas.height, y + radius); py++) {
      for (let px = Math.max(0, Math.floor(x - radius)); px < Math.min(canvas.width, x + radius); px++) {
        if ((px - x) ** 2 + (py - y) ** 2 > radius ** 2) continue;
        const i = (py * canvas.width + px) * 4;
        buffer.set([value, value, value, 255], i);
      }
    }
  }
  const ctx = {
    fillStyle: "#000", strokeStyle: "#000",
    fillRect() { const buffer = data(), value = this.fillStyle === "#fff" ? 255 : 0;
      for (let i = 0; i < buffer.length; i += 4) buffer.set([value, value, value, 255], i); },
    drawImage(...args) { canvas.calls.push(args); this.fillRect(); },
    beginPath() { points = []; }, arc(x, y, r) { dot = [x, y, r]; }, fill() { circle(...dot); },
    moveTo(x, y) { points.push([x, y]); }, lineTo(x, y) { points.push([x, y]); },
    stroke() { for (let i = 1; i < points.length; i++) {
      const [x, y] = points[i - 1], [ex, ey] = points[i], steps = Math.max(1, Math.ceil(Math.hypot(ex - x, ey - y)));
      for (let n = 0; n <= steps; n++) circle(x + (ex - x) * n / steps, y + (ey - y) * n / steps, this.lineWidth / 2);
    } },
    getImageData() { return { data: new Uint8ClampedArray(data()) }; },
    putImageData(image) { pixels = new Uint8ClampedArray(image.data); },
  };
  canvas.getContext = (kind) => { assert.equal(kind, "2d"); return ctx; };
  canvas.getBoundingClientRect = () => ({ left: 10, top: 20, width: 256, height: 256 });
  canvas.setPointerCapture = (id) => { canvas.captured = id; };
  canvas.hasPointerCapture = (id) => canvas.captured === id;
  canvas.releasePointerCapture = () => { canvas.captured = null; };
  canvas.toDataURL = (mime) => {
    assert.equal(mime, "image/png");
    const chunk = (name, bytes) => {
      const body = Buffer.concat([Buffer.from(name), bytes]), out = Buffer.alloc(body.length + 8);
      out.writeUInt32BE(bytes.length); body.copy(out, 4);
      let crc = 0xffffffff;
      for (const byte of body) { crc ^= byte; for (let n = 0; n < 8; n++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
      out.writeUInt32BE((crc ^ 0xffffffff) >>> 0, out.length - 4); return out;
    };
    const header = Buffer.alloc(13); header.writeUInt32BE(canvas.width); header.writeUInt32BE(canvas.height, 4); header[8] = 8; header[9] = 6;
    const rows = Buffer.alloc((canvas.width * 4 + 1) * canvas.height), buffer = Buffer.from(data());
    for (let y = 0; y < canvas.height; y++) buffer.copy(rows, y * (canvas.width * 4 + 1) + 1, y * canvas.width * 4, (y + 1) * canvas.width * 4);
    return "data:image/png;base64," + Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), chunk("IHDR", header), chunk("IDAT", zlib.deflateSync(rows)), chunk("IEND", Buffer.alloc(0))]).toString("base64");
  };
  return canvas;
}
const context = vm.createContext({ console, URLSearchParams, navigator: { language: "en" },
  localStorage: { getItem() { return null; }, setItem() {} },
  document: { hidden: false, documentElement: {},
    getElementById(id) {
      if (["desktoppair", "pairqr", "pairnote"].includes(id) && !elements.get("stage")?.innerHTML.includes(`id="${id}"`)) return null;
      if (!elements.has(id)) elements.set(id, ["maskcanvas", "maskbase"].includes(id) ? canvasElement() : element());
      return elements.get(id);
    },
    querySelectorAll() { return []; }, addEventListener() {}, createElement: element },
  window: { scrollTo() {} },
  setTimeout(fn, delay) { const id = ++timerID; timers.set(id, { fn, delay }); return id; },
  clearTimeout(id) { timers.delete(id); }, setInterval(fn, delay) { const id = ++timerID; intervals.set(id, { fn, delay }); return id; },
  async fetch(url, opts = {}) {
    requests.push({ url: String(url), method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null });
    if (url === "/api/status" && statusOffline) throw new Error("offline fixture");
    if (url === "/api/phones/add_lan" && lanFailure) {
      return { ok: false, async json() { return { error: "timeout" }; } };
    }
    if (url === "/api/quit") {
      const busy = quitBusy && !JSON.parse(opts.body || "{}").after_job;
      // Same code studio.py sends for POST /api/quit during a job (seen missing in a real packaged run).
      return { ok: !busy, status: busy ? 409 : 200, async json() { return busy ? { error: "job_running" } : { quitting: true }; } };
    }
    if (String(url).startsWith("/api/memory") && memoryGate) await memoryGate;
    let data;
    if (url === "/api/desktop-link") data = { url: "localdream://pair?t=" + "0".repeat(32) + "&n=Example" };
    else if (url === "/api/gallery") data = galleryItems;
    else if (url === "/api/discover") data = paired ? [{ host: "192.0.2.50", phone_id: "example", state: "ready", desktop_link: { token_ok: true } }] : [];
    else if (String(url).startsWith("/api/memory")) data = { available_mb: 1000, needed_mb: 1500, low: true };
    else if (url === "/api/status") data = customStatus || { now: Date.now() / 1000, job: statusJob, phones: { phones: paired || official ? [{ id: "example", name: "Example", transport: "lan", state: "ready", features: { desktop_link: !official, app_state: !official } }] : [] } };
    else data = [];
    return { ok: true, async json() { return data; } };
  }, Date, FileReader: class {
    readAsDataURL(file) {
      fileReads++;
      this.result = file.data;
      Promise.resolve().then(() => file.fail ? this.onerror() : this.onload());
    }
  } });
vm.runInContext(fs.readFileSync(path.join(root, "vendor/qrcode.js"), "utf8"), context);
vm.runInContext(script, context);
const tick = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  await tick();
  if (statusContract) {
    // stdin is the actual HTTP response from studio.Server, not a page fixture.
    await vm.runInContext("pollStatus()", context);
    assert.equal(elements.get("quit").hidden, false, "real /api/status must expose Quit");
    console.log("Real server/page status contract passed.");
    return;
  }
  const copy = vm.runInContext("I18N", context);
  const keys = ["unofficial", "refresh_models", "c_scan", "c_recommended", "c_scan_hint", "c_searching", "c_none", "c_discovery_failed", "c_paired", "c_qr_label", "not_paired", "low_memory"];
  keys.push(...Object.keys(vm.runInContext("DIAG_COPY.en", context)));
  keys.push("mask_paint", "mask_hint", "mask_size", "mask_brush", "mask_eraser", "mask_clear", "mask_invert", "mask_undo", "mask_canvas");
  keys.push(...Object.keys(vm.runInContext("EDIT_COPY.en", context)));
  keys.push(...Object.keys(vm.runInContext("CONNECTION_COPY.en", context)));
  assert.ok(html.includes("<title>Local Dream Desktop (unofficial)</title>"));
  assert.match(html, /class="brand"[^\n]*<small data-i="unofficial"><\/small>/);
  const unofficialLabels = { en: "unofficial", pt: "não oficial", es: "no oficial", zh: "非官方", ja: "非公式" };
  for (const language of ["en", "pt", "es", "zh", "ja"]) {
    for (const key of keys) assert.ok(copy[language][key], `${language}/${key}`);
    assert.ok(copy[language].low_memory("1", "1.5").includes("GB"));
    assert.equal(copy[language].unofficial, unofficialLabels[language]);
    context.testLanguage = language;
    vm.runInContext("lang = testLanguage; applyLang();", context);
    assert.equal(context.document.title, `Local Dream Desktop (${unofficialLabels[language]})`);
  }
  vm.runInContext('lang = "en"; applyLang();', context);
  // No known capability means no QR panel, copy, identity or discovery request.
  const beforeUnknown = requests.length;
  vm.runInContext('phones = { phones: [] }; view = { kind: "connect" }; renderStage();', context);
  await tick();
  assert.equal(context.document.getElementById("desktoppair"), null);
  assert.ok(!elements.get("stage").innerHTML.includes(copy.en.c_scan));
  assert.equal(requests.length, beforeUnknown);
  vm.runInContext('phones.phones = [{ id: "extension", features: { desktop_link: true } }]; renderStage();', context);
  await tick();
  assert.ok(elements.get("pairqr").innerHTML.includes("<svg"));
  assert.equal(elements.get("pairnote").textContent, copy.en.c_none);
  assert.ok(requests.some((r) => r.url === "/api/discover" && r.method === "POST"));
  const pairingPoll = [...timers.values()].find((timer) => timer.fn.name === "check");
  assert.ok(pairingPoll && pairingPoll.delay <= 2000);
  // Stop pairing when leaving the panel, even with a scheduled request.
  const before = requests.length;
  vm.runInContext('phones = { phones: [] }; view = { kind: "empty" }; renderStage();', context);
  await pairingPoll.fn();
  assert.equal(requests.length, before);
  elements.get("go").disabled = false;
  vm.runInContext('sel = { phone: { id: "example" }, model: { id: "sd15" } };', context);
  await vm.runInContext("refreshMemory()", context);
  assert.equal(elements.get("memrow").hidden, false);
  assert.ok(elements.get("memory").textContent.includes("1.0 GB"));
  vm.runInContext('lang = "pt"; renderMemory();', context);
  assert.ok(elements.get("memory").textContent.startsWith("O celular tem"));
  assert.equal(elements.get("go").disabled, false); // advisory only: generation stays available
  vm.runInContext('sel = null; renderMemory(); lang = "en";', context);
  assert.equal(elements.get("memrow").hidden, true);
  // The reference estimate updates immediately and can turn a sufficient base estimate low.
  vm.runInContext('sel = { phone: { id: "example" }, model: { id: "dit", family: "dit" } }; memoryKey = "example/dit"; memoryData = { available_mb: 5000, needed_mb: 4200, reference_extra_mb: 1500 }; refData = "reference"; renderMemory();', context);
  assert.ok(elements.get("memory").textContent.includes("5.7 GB"));
  assert.ok(elements.get("memory").textContent.includes(copy.en.mem_reference));
  assert.ok(elements.get("memory").textContent.includes("Close"));
  vm.runInContext('$("clearref").onclick();', context);
  assert.ok(elements.get("memory").textContent.includes("4.2 GB"));
  assert.ok(!elements.get("memory").textContent.includes(copy.en.mem_reference));
  for (const family of ["sdxl", "anima", "sd15"]) {
    vm.runInContext(`sel.model.family = "${family}"; refData = "reference"; renderMemory();`, context);
    assert.equal(elements.get("memory").textContent.includes(copy.en.mem_reference), family !== "sd15");
  }
  // Phone state warnings appear below Generate and never disable it.
  for (const language of ["en", "pt", "es", "zh", "ja"]) {
    context.testLanguage = language;
    vm.runInContext('lang = testLanguage; sel.model.family = "dit"; memoryData.app_state = { foreground: false, locked: true }; renderMemory();', context);
    assert.equal(elements.get("appwarnings").hidden, false);
    assert.equal(elements.get("appwarnings").textContent,
      copy[language].h_recommend_foreground + "\n" + copy[language].h_recommend_unlock);
    assert.equal(elements.get("go").disabled, false);
  }
  vm.runInContext('lang = "en"; memoryData.app_state = { foreground: true, locked: false }; renderMemory();', context);
  assert.equal(elements.get("appwarnings").hidden, true);
  vm.runInContext('delete memoryData.app_state; renderMemory();', context);
  assert.equal(elements.get("appwarnings").hidden, true); // official build
  // Running job state is scoped to both phone and model, preserving selected RAM.
  vm.runInContext('requestPending = true;', context); // isolate state updates from idle RAM refresh
  const selectedMemory = vm.runInContext('JSON.stringify(memoryData)', context);
  for (const [phone_id, model_id] of [["example", "other-model"], ["other-phone", "dit"]]) {
    statusJob = { phone_id, model_id, app_state: { foreground: false, locked: true } };
    await vm.runInContext("pollStatus()", context);
    assert.equal(vm.runInContext('JSON.stringify(memoryData)', context), selectedMemory);
    assert.equal(elements.get("appwarnings").hidden, true);
  }
  statusJob = { phone_id: "example", model_id: "dit", app_state: { foreground: false, locked: true } };
  await vm.runInContext("pollStatus()", context);
  assert.equal(elements.get("appwarnings").hidden, false);
  assert.equal(vm.runInContext('memoryData.available_mb', context), 5000);
  statusJob = { phone_id: "example", model_id: "dit", app_state: {} };
  await vm.runInContext("pollStatus()", context);
  assert.equal(elements.get("appwarnings").hidden, true); // do not retain a previous phone's state
  statusJob = null;
  vm.runInContext('requestPending = false;', context);
  vm.runInContext('sel = null; renderMemory();', context);
  assert.equal(elements.get("appwarnings").hidden, true);
  const request = { prompt: "original prompt", phone_id: "example", model_id: "dit", image: "reference", strength: .7,
    mask: "mask", reference_name: "example.png", width: 1024, height: 1024, seed: 7, cfg: 5, steps: 9, scheduler: "euler" };
  const diagnosis = { cause: "out_of_memory", evidence: ["reference_uses_more_memory", { code: "memory_available", mb: 500 }, "adb_unavailable", { code: "request", mask: true }],
    actions: ["retry", "retry_without_reference", "retry_smaller", "close_apps", "reopen_host_mode", "check_wifi", "use_other_model"],
    apps: [{ package: "com.example.browser", mb: 500 }], smaller: { width: 768, height: 768 } };
  context.originalRequest = request; context.diagnosis = diagnosis;
  const resetFailure = () => vm.runInContext('waiting = requestPending = false; lastRequest = { ...originalRequest }; view = { kind: "error", diagnosis, phone_id: "example" }; renderStage();', context);
  const beforeFailure = requests.length;
  resetFailure();
  assert.equal(requests.length, beforeFailure); // rendering never takes an action
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.d_out_of_memory));
  assert.equal((elements.get("stage").innerHTML.match(/<p class="muted">/g) || []).length, 4); // three evidence lines + manual host-mode instructions
  for (const action of ["retry", "retry_without_reference", "retry_smaller"]) {
    resetFailure();
    await vm.runInContext(`diagnosisAction("${action}")`, context);
    const sent = requests.filter((r) => r.url === "/api/generate").at(-1).body;
    const expected = { ...request };
    if (action === "retry_without_reference") for (const key of ["image", "strength", "mask", "reference_name"]) delete expected[key];
    if (action === "retry_smaller") Object.assign(expected, diagnosis.smaller);
    assert.deepEqual(sent, expected);
  }
  resetFailure();
  const beforeHints = requests.length;
  await vm.runInContext('diagnosisAction("close_apps")', context);
  assert.ok(elements.get("stage").innerHTML.includes("com.example.browser · 500 MB"));
  await vm.runInContext('diagnosisAction("check_wifi")', context);
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.h_check_wifi));
  await vm.runInContext('diagnosisAction("use_other_model")', context);
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.h_use_other_model));
  assert.equal(requests.length, beforeHints);
  // The new repair actions only reveal translated guidance.
  for (const language of ["en", "pt", "es", "zh", "ja"]) {
    context.testLanguage = language;
    vm.runInContext("lang = testLanguage", context);
    for (const action of ["move_to_app_storage", "recommend_foreground", "recommend_unlock"]) {
      resetFailure();
      const beforeAction = requests.length;
      await vm.runInContext(`diagnosisAction("${action}")`, context);
      assert.equal(requests.length, beforeAction);
      context.expectedHint = copy[language][`h_${action}`];
      assert.ok(elements.get("stage").innerHTML.includes(vm.runInContext("esc(expectedHint)", context)));
    }
    for (const cause of ["app_not_foreground", "models_in_public_storage"]) {
      context.testCause = cause;
      vm.runInContext('view = { kind: "error", diagnosis: { cause: testCause, evidence: [testCause, "phone_locked"], actions: [] } }; renderStage();', context);
      assert.ok(elements.get("stage").innerHTML.includes(copy[language][`d_${cause}`]));
    }
  }
  vm.runInContext('lang = "en";', context);
  // Mask tools require both a loaded reference and an explicitly supported model.
  assert.ok(html.includes('for="brushsize"'));
  for (const id of ["paintmask", "maskbrush", "maskeraser", "maskclear", "maskinvert", "maskundo"]) {
    assert.match(html, new RegExp(`<button type="button"[^>]*id="${id}"[^>]*data-i="mask_`));
  }
  vm.runInContext('waiting = requestPending = false; sel = { phone: { id: "example", state: "ready" }, model: { id: "sd15", family: "sd15", img2img: true, inpaint: true, sizes: { kind: "list", list: [[512, 512], [512, 768]] } } }; opts.size = "512x512"; opts.aspect_ratio = "1:1"; refData = null; refReady = false; renderMask();', context);
  assert.equal(elements.get("masktool").hidden, true);
  vm.runInContext('refData = "source"; refReady = true; $("refimg").naturalWidth = 800; $("refimg").naturalHeight = 600; renderMask();', context);
  assert.equal(elements.get("masktool").hidden, false);
  assert.equal(elements.get("maskeditor").hidden, true);
  assert.equal(vm.runInContext("exportMask()", context), null);
  elements.get("paintmask").onclick();
  assert.equal(elements.get("maskeditor").hidden, false);
  assert.equal(vm.runInContext("exportMask()", context), null);
  assert.equal(elements.get("maskempty").hidden, false);
  assert.deepEqual(elements.get("maskbase").calls.at(-1).slice(1).map((n) => n || 0), [-85, 0, 682, 512]);
  const canvas = elements.get("maskcanvas"), ctx = canvas.getContext("2d");
  const pixel = (x, y) => Array.from(ctx.getImageData().data.slice((y * canvas.width + x) * 4, (y * canvas.width + x) * 4 + 4));
  assert.deepEqual(pixel(100, 100), [0, 0, 0, 255]);
  elements.get("brushsize").value = "20"; elements.get("brushsize").oninput();
  assert.equal(elements.get("brushsizev").textContent, "20 px");
  const point = (id, x, y) => ({ button: 0, pointerId: id, clientX: x, clientY: y, preventDefault() {} });
  canvas.onpointerdown(point(1, 60, 70)); // CSS-to-bitmap scale maps to (100,100).
  canvas.onpointermove(point(2, 90, 100)); // another pointer cannot extend the stroke
  canvas.onpointermove(point(1, 80, 90));
  canvas.onpointerup(point(1, 80, 90));
  assert.equal(canvas.captured, null);
  assert.deepEqual(pixel(100, 100), [255, 255, 255, 255]);
  assert.deepEqual(pixel(140, 140), [255, 255, 255, 255]);
  assert.deepEqual(pixel(160, 160), [0, 0, 0, 255]);
  elements.get("maskeraser").onclick();
  canvas.onpointerdown(point(1, 60, 70)); canvas.onpointercancel(point(1, 60, 70));
  assert.deepEqual(pixel(100, 100), [0, 0, 0, 255]);
  elements.get("maskundo").onclick();
  assert.deepEqual(pixel(100, 100), [255, 255, 255, 255]);
  elements.get("maskinvert").onclick();
  assert.deepEqual(pixel(100, 100), [0, 0, 0, 255]);
  assert.deepEqual(pixel(160, 160), [255, 255, 255, 255]);
  elements.get("maskclear").onclick();
  assert.deepEqual(pixel(160, 160), [0, 0, 0, 255]);
  assert.equal(vm.runInContext("exportMask()", context), null); // command history alone is not paint
  assert.equal(elements.get("maskempty").hidden, false);
  elements.get("maskundo").onclick();
  const encodedMask = vm.runInContext("exportMask()", context), png = Buffer.from(encodedMask, "base64");
  assert.ok(!encodedMask.startsWith("data:"));
  assert.equal(png.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");
  assert.equal(png.readUInt32BE(16), 512); assert.equal(png.readUInt32BE(20), 512);
  assert.equal(png[25], 6); // RGBA PNG, with an opaque black/white backing.
  const pixels = ctx.getImageData().data;
  for (let i = 3; i < pixels.length; i += 4) assert.equal(pixels[i], 255);
  vm.runInContext('$("prompt").value = "garden";', context);
  await elements.get("f").onsubmit({ preventDefault() {} });
  const maskedRequest = requests.filter((r) => r.url === "/api/generate").at(-1).body;
  assert.equal(maskedRequest.mask, encodedMask);
  assert.equal(maskedRequest.image, elements.get("maskbase").toDataURL("image/png").split(",")[1]);
  vm.runInContext('waiting = requestPending = false;', context);
  elements.get("paintmask").onclick(); // disable mask, retaining the original source request
  await elements.get("f").onsubmit({ preventDefault() {} });
  const plainRequest = requests.filter((r) => r.url === "/api/generate").at(-1).body;
  assert.equal(plainRequest.image, "source"); assert.ok(!("mask" in plainRequest));
  vm.runInContext('waiting = requestPending = false; $("paintmask").onclick(); opts.size = "512x768"; renderMask();', context);
  assert.equal(canvas.height, 768);
  assert.equal(vm.runInContext("maskCommands.length", context), 0);
  assert.equal(elements.get("maskundo").disabled, true);
  assert.equal(vm.runInContext("exportMask()", context), null);
  await elements.get("f").onsubmit({ preventDefault() {} });
  const emptyMaskRequest = requests.filter((r) => r.url === "/api/generate").at(-1).body;
  assert.equal(emptyMaskRequest.image, "source");
  assert.ok(!("mask" in emptyMaskRequest));
  assert.equal(elements.get("maskempty").hidden, false);
  vm.runInContext('waiting = requestPending = false;', context);
  // Known native families use the advertised capability, never a UI allowlist.
  for (const family of ["sd15", "sd15cpu", "sdxl", "anima", "dit", "future"]) {
    context.testFamily = family;
    vm.runInContext('sel.model.family = testFamily; sel.model.inpaint = testFamily !== "future"; renderMask();', context);
    assert.equal(elements.get("masktool").hidden, family === "future");
  }
  vm.runInContext('sel.model.inpaint = false; renderMask();', context);
  assert.equal(vm.runInContext("exportMask()", context), null);
  vm.runInContext('sel.model.inpaint = true; renderMask(); $("paintmask").onclick(); $("clearref").onclick();', context);
  assert.equal(elements.get("masktool").hidden, true);
  assert.equal(vm.runInContext("exportMask()", context), null);
  // Pure reference editing uses the advertised capability, preserves order and never sends strength.
  assert.match(html, /<input[^>]*id="referencefiles"[^>]*multiple/);
  assert.match(html, /<select[^>]*id="refmode"[^>]*aria-controls=/);
  assert.doesNotMatch(html, /<textarea[^>]*aria-describedby="referencehint"/);
  assert.equal(elements.get("prompt").getAttribute("aria-describedby"), null);
  vm.runInContext('sel.model = { id: "klein", family: "dit", img2img: true, inpaint: true, reference_edit: true, max_references: 3, sizes: { kind: "dit", min: 512, max: 2048, step: 64, presets: [[512, 512]] } }; opts.size = "512x512"; opts.aspect_ratio = "1:1"; refData = "old-source"; refReady = true; renderSizes();', context);
  assert.equal(elements.get("refmodewrap").hidden, false);
  elements.get("refmode").value = "references"; elements.get("refmode").onchange();
  assert.equal(elements.get("singleimage").hidden, true);
  assert.equal(elements.get("referenceedit").hidden, false);
  assert.equal(elements.get("referencehint").hidden, false);
  assert.equal(elements.get("prompt").getAttribute("aria-describedby"), "referencehint");
  assert.equal(elements.get("masktool").hidden, true);
  assert.equal(vm.runInContext("exportMask()", context), null);
  const beforeEmptyReferences = requests.length;
  await elements.get("f").onsubmit({ preventDefault() {} });
  assert.equal(requests.length, beforeEmptyReferences);
  assert.equal(elements.get("referenceerror").textContent, copy.en.edit_count(3));
  const referenceFiles = [1, 2, 3].map((n) => ({ type: "image/png", size: 42, data: `data:image/png;base64,image-${n}` }));
  context.referenceFiles = referenceFiles;
  await vm.runInContext("addReferences(referenceFiles)", context);
  assert.equal(vm.runInContext("references.length", context), 3);
  assert.equal(elements.get("addreferences").disabled, true);
  for (let n = 1; n <= 3; n++) {
    assert.ok(elements.get("referencelist").innerHTML.includes(`image ${n}`));
    assert.ok(elements.get("referencelist").innerHTML.includes(`aria-label="Remove · image ${n}"`));
  }
  await vm.runInContext("addReferences([referenceFiles[0]])", context);
  assert.equal(vm.runInContext("references.length", context), 3);
  assert.equal(elements.get("referenceerror").textContent, copy.en.edit_count(3));
  await elements.get("f").onsubmit({ preventDefault() {} });
  const edited = requests.filter((r) => r.url === "/api/generate").at(-1).body;
  assert.deepEqual(edited.reference_images, referenceFiles.map((file) => file.data));
  for (const key of ["image", "mask", "strength", "denoise_strength"]) assert.ok(!(key in edited));
  vm.runInContext('waiting = requestPending = false; memoryKey = "example/klein"; memoryData = { available_mb: 7000, needed_mb: 4200, reference_extra_mb: 1500 }; renderMemory();', context);
  assert.ok(elements.get("memory").textContent.includes("8.7 GB"));
  elements.get("referencelist").onclick({ target: { closest: () => ({ dataset: { removeReference: "1" } }) } });
  assert.equal(focusedElement.dataset.removeReference, "1"); // next thumbnail's rebuilt remove button
  assert.equal(vm.runInContext("references.length", context), 2);
  assert.ok(elements.get("referencelist").innerHTML.includes('image-3'));
  assert.ok(!elements.get("referencelist").innerHTML.includes('image 3'));
  assert.ok(elements.get("memory").textContent.includes("7.2 GB"));
  assert.equal(elements.get("addreferences").disabled, false);
  for (const invalid of [{ type: "image/gif", size: 1 }, { type: "image/png", size: 6 * 1024 * 1024 + 1 }]) {
    context.invalidReference = invalid;
    await vm.runInContext("addReferences([invalidReference])", context);
    assert.equal(vm.runInContext("references.length", context), 2);
    assert.equal(elements.get("referenceerror").textContent, copy.en.edit_limit(3));
  }
  context.brokenReference = { ...referenceFiles[0], fail: true };
  await vm.runInContext("addReferences([brokenReference])", context);
  assert.equal(vm.runInContext("referenceLoading", context), false);
  assert.equal(elements.get("referenceerror").textContent, copy.en.bad_image);
  await elements.get("f").onsubmit({ preventDefault() {} });
  const afterRemoval = requests.filter((r) => r.url === "/api/generate").at(-1).body;
  assert.deepEqual(afterRemoval.reference_images, [referenceFiles[0].data, referenceFiles[2].data]);
  vm.runInContext('waiting = requestPending = false;', context);
  // Retry without references drops the array too, while keeping the prompt/settings.
  context.referenceRequest = afterRemoval;
  vm.runInContext('lastRequest = { ...referenceRequest }; view = { kind: "error", diagnosis: { actions: ["retry_without_reference"] } };', context);
  await vm.runInContext('diagnosisAction("retry_without_reference")', context);
  const withoutReferences = { ...afterRemoval }; delete withoutReferences.reference_images;
  assert.deepEqual(requests.filter((r) => r.url === "/api/generate").at(-1).body, withoutReferences);
  vm.runInContext('waiting = requestPending = false;', context);
  // Returning to the image/mask mode keeps inpainting on the image-plus-mask path for Klein.
  elements.get("refmode").value = "image"; elements.get("refmode").onchange();
  assert.equal(elements.get("singleimage").hidden, false);
  assert.equal(elements.get("referenceedit").hidden, true);
  assert.equal(elements.get("prompt").getAttribute("aria-describedby"), null);
  assert.equal(elements.get("masktool").hidden, false);
  elements.get("paintmask").onclick();
  canvas.onpointerdown(point(1, 60, 70)); canvas.onpointerup(point(1, 60, 70));
  await elements.get("f").onsubmit({ preventDefault() {} });
  const kleinMasked = requests.filter((r) => r.url === "/api/generate").at(-1).body;
  assert.ok(kleinMasked.image && kleinMasked.mask);
  assert.equal(kleinMasked.strength, vm.runInContext("opts.strength", context));
  assert.ok(!("reference_images" in kleinMasked));
  vm.runInContext('waiting = requestPending = false;', context);
  elements.get("refmode").value = "references"; elements.get("refmode").onchange();
  assert.equal(vm.runInContext("maskEnabled", context), false);
  assert.equal(elements.get("prompt").getAttribute("aria-describedby"), "referencehint");
  elements.get("referencelist").onclick({ target: { closest: () => ({ dataset: { removeReference: "1" } }) } });
  assert.equal(focusedElement, elements.get("addreferences")); // last thumbnail: no next remove button
  elements.get("referencelist").onclick({ target: { closest: () => ({ dataset: { removeReference: "0" } }) } });
  assert.equal(focusedElement, elements.get("addreferences")); // empty list remains keyboard accessible
  vm.runInContext('sel.model.reference_edit = false; renderSizes();', context);
  assert.equal(elements.get("refmodewrap").hidden, true);
  assert.equal(elements.get("referenceedit").hidden, true);
  assert.equal(vm.runInContext("editMode", context), "image");
  assert.equal(elements.get("prompt").getAttribute("aria-describedby"), null);
  vm.runInContext('$("clearref").onclick();', context);
  resetFailure();
  vm.runInContext('phones.phones = [{ id: "example", has_adb: true }];', context);
  await vm.runInContext('diagnosisAction("reopen_host_mode")', context);
  assert.deepEqual(requests.filter((r) => r.url === "/api/host_mode").at(-1).body, { phone_id: "example" });
  // Fixed canvas retry only changes the requested aspect and canvas fields.
  resetFailure();
  vm.runInContext('view.diagnosis = { ...diagnosis, smaller: { width: 1024, height: 1024, aspect_ratio: "1:1" } }; lastRequest.aspect_ratio = "3:4";', context);
  await vm.runInContext('diagnosisAction("retry_smaller")', context);
  assert.equal(requests.filter((r) => r.url === "/api/generate").at(-1).body.aspect_ratio, "1:1");
  vm.runInContext('waiting = false; refData = null; sel = null;', context);
  // Official hosts expose only USB/IP and Android's optional wireless debugging.
  // Capability changes must remove the QR subtree, including a previously open panel.
  official = true;
  await vm.runInContext("pollStatus()", context);
  for (const language of ["en", "pt", "es", "zh", "ja"]) {
    context.testLanguage = language;
    const beforeOfficial = requests.length;
    vm.runInContext('lang = testLanguage; view = { kind: "connect" }; renderStage();', context);
    await tick();
    assert.equal(context.document.getElementById("desktoppair"), null);
    assert.equal(context.document.getElementById("pairqr"), null);
    assert.equal(context.document.getElementById("pairnote"), null);
    assert.equal(requests.length, beforeOfficial);
    assert.ok(!elements.get("stage").innerHTML.includes(copy[language].c_scan));
    assert.ok(!elements.get("stage").innerHTML.includes(copy[language].c_scan_hint));
    assert.ok(elements.get("stage").innerHTML.includes('id="lanf"'));
    assert.ok(elements.get("stage").innerHTML.includes(copy[language].c_usb));
  }
  vm.runInContext('lang = "en"; phones.phones.push({ id: "extension", features: { desktop_link: true } }); renderStage();', context);
  await tick();
  assert.ok(context.document.getElementById("desktoppair"));
  const beforeCollapsed = requests.length;
  elements.get("desktoppair").open = false;
  elements.get("desktoppair").ontoggle();
  const collapsedPoll = [...timers.values()].find((timer) => timer.fn.name === "check");
  if (collapsedPoll) await collapsedPoll.fn();
  assert.equal(requests.length, beforeCollapsed);
  elements.get("desktoppair").open = true;
  elements.get("desktoppair").ontoggle();
  await tick();
  assert.ok(requests.slice(beforeCollapsed).some((r) => r.url === "/api/desktop-link"));
  // A reported app_state alone must not enable pairing.
  vm.runInContext('phones.phones = [{ id: "state-only", features: { app_state: true } }]; renderStage();', context);
  const beforeStateOnly = requests.length;
  await tick();
  assert.equal(context.document.getElementById("desktoppair"), null);
  assert.equal(requests.length, beforeStateOnly);
  // A discovered phone that accepts the token closes the panel with success.
  official = false; paired = true;
  await vm.runInContext("pollStatus()", context);
  vm.runInContext('view = { kind: "connect" }; renderStage();', context);
  await tick(); await tick();
  assert.equal(vm.runInContext("view.paired", context), true);
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.c_paired));
  // Every untrusted interpolation must remain text, even if a phone/server
  // supplies markup in fields usually holding numeric values or filenames.
  context.attack = '\"><img src=x onerror="alert(1)">';
  const escaped = vm.runInContext("esc(attack)", context);
  vm.runInContext(`
    waiting = false; requestPending = false; lang = "en";
    phones = { phones: [{ id: attack, name: attack, state: "ready" }] };
    catalog = [{ id: attack, name: attack, state: "ready", models: [
      { id: attack, name: attack, description: attack, sizes: { kind: "list", list: [[attack, attack]] } }
    ] }];
    pickPhone(catalog[0]); opts.size = "512x512";
    renderDevices(); renderModels(); renderSizes();
  `, context);
  for (const id of ["device", "phoneactions", "model", "sizes"]) {
    assert.ok(!elements.get(id).innerHTML.includes("<img src=x"), id);
    assert.ok(elements.get(id).innerHTML.includes(escaped), id);
  }
  assert.equal(elements.get("mdesc").textContent, context.attack);
  assert.ok(elements.get("sizes").innerHTML.includes(`data-size="${escaped}x${escaped}"`));
  vm.runInContext('sel.model.sizes = { kind: "fixed", size: attack }; renderSizes();', context);
  assert.ok(!elements.get("sizes").innerHTML.includes("<img src=x"));
  assert.ok(elements.get("sizes").innerHTML.includes(`<small>${escaped}`));
  vm.runInContext(`
    view = { kind: "image", item: { url: attack, meta: {
      width: attack, height: attack, seed: attack, model: attack, phone: attack
    } } }; renderStage();
  `, context);
  assert.ok(!elements.get("stage").innerHTML.includes("<img src=x"));
  assert.ok(elements.get("stage").innerHTML.includes(`src="${escaped}"`));
  assert.ok(elements.get("stage").innerHTML.includes(`href="${escaped}"`));
  assert.ok(elements.get("stage").innerHTML.includes(`${escaped}×${escaped}`));
  assert.ok(elements.get("stage").innerHTML.includes(`${copy.en.seed_lbl} ${escaped}`));
  vm.runInContext('job = { started: 0, stages: [["generate", 0, null]], progress: [attack, attack] }; renderProgress($("stage"));', context);
  assert.ok(!elements.get("stage").innerHTML.includes("<img src=x"));
  assert.ok(elements.get("stage").innerHTML.includes(`step ${escaped} of ${escaped}`));
  vm.runInContext('view = { kind: "error", msg: attack }; renderStage();', context);
  assert.ok(!elements.get("stage").innerHTML.includes("<img src=x"));
  galleryItems = [{ url: context.attack, meta: { prompt: context.attack } }];
  vm.runInContext('globalThis.tiles = []; $("grid").appendChild = (tile) => tiles.push(tile);', context);
  await vm.runInContext("loadGallery()", context);
  assert.ok(!context.tiles[0].innerHTML.includes("<img src=x"));
  assert.ok(context.tiles[0].innerHTML.includes(`src="${escaped}"`));
  // Empty catalogs must be re-read even when the phone state stays ready.
  paired = true; galleryItems = [];
  vm.runInContext(`
    job = null; waiting = false; requestPending = false; view = { kind: "empty" };
    phones = { phones: [{ id: "example", state: "ready", transport: "lan" }] };
    catalog = [{ id: "example", name: "Example", state: "ready", transport: "lan", models: [] }];
    pickPhone(catalog[0]); renderDevices();
  `, context);
  assert.equal(elements.get("modelrefresh").disabled, false); // usable with no models
  const beforeEmptyPoll = requests.length;
  await vm.runInContext("pollStatus()", context);
  assert.ok(requests.slice(beforeEmptyPoll).some((r) => r.url === "/api/models"));
  const beforeRefresh = requests.length;
  await elements.get("modelrefresh").onclick();
  assert.ok(requests.slice(beforeRefresh).some((r) => r.url === "/api/connect" && r.method === "POST"));
  assert.ok(requests.slice(beforeRefresh).some((r) => r.url === "/api/models"));
  // Onboarding starts with a choice; both sets of steps are hidden until chosen.
  vm.runInContext('waiting = requestPending = false; phones = { phones: [], adb_available: false }; view = { kind: "connect" }; renderStage();', context);
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.c_required));
  assert.equal((elements.get("stage").innerHTML.match(/<section class="way" hidden>/g) || []).length, 2);
  assert.ok(elements.get("stage").innerHTML.includes(`<summary>${copy.en.c_advanced}</summary>`));
  elements.get("chooseusb").onclick();
  assert.equal((elements.get("stage").innerHTML.match(/<section class="way" hidden>/g) || []).length, 1);
  assert.ok(elements.get("usbnote").innerHTML.includes(copy.en.c_no_adb));
  assert.ok(elements.get("usbnote").innerHTML.includes("https://developer.android.com/tools/releases/platform-tools"));
  elements.get("choosewifi").onclick();
  assert.equal(vm.runInContext("view.connection", context), "wifi");
  // Exercise both an immediate HTTP rejection and an accepted background connect
  // that fails after a busy poll. Neither may discard the selected mode or IP.
  for (const immediate of [true, false]) {
    const host = "192.0.2.77";
    elements.get("lhost").value = host;
    elements.get("lhost").oninput();
    lanFailure = immediate;
    await elements.get("lanf").onsubmit({ preventDefault() {} });
    if (!immediate) {
      customStatus = { now: Date.now() / 1000, busy: "connecting", phones: { phones: [], adb_available: false } };
      await vm.runInContext("pollStatus()", context);
      assert.equal(vm.runInContext("view.kind", context), "progress");
      customStatus.busy = null;
      customStatus.error = { code: "timeout" };
      // A real DOM replaces the input when progress gives way to the form.
      elements.get("lhost").value = "";
      await vm.runInContext("pollStatus()", context);
    }
    assert.equal(vm.runInContext("view.kind", context), "connect");
    assert.equal(vm.runInContext("view.connection", context), "wifi");
    assert.equal(vm.runInContext("view.host", context), host);
    assert.equal(elements.get("lhost").value, host);
    assert.equal(elements.get("cerr").hidden, false);
    assert.equal(elements.get("cerr").textContent, copy.en.timeout);
    assert.equal((elements.get("stage").innerHTML.match(/<section class="way" hidden>/g) || []).length, 1);
    assert.match(elements.get("stage").innerHTML, /<form id="lanf"[\s\S]*id="lhost"[\s\S]*id="cerr"[\s\S]*<\/form>/);
    vm.runInContext("renderStage()", context);
    assert.equal(elements.get("lhost").value, host);
  }
  lanFailure = false; customStatus = null;
  // LAN recovery uses instructions unless an attached adb transport is reported.
  vm.runInContext('phones = { phones: [{ id: "example", name: "Example", transport: "lan", state: "no_host_mode", has_lan: true, has_usb: false, has_adb: false }] }; catalog = []; sel = null; renderDevices();', context);
  assert.ok(elements.get("phoneactions").innerHTML.includes(vm.runInContext('esc(t("c_host_path"))', context)));
  assert.ok(!elements.get("phoneactions").innerHTML.includes('data-action="host_mode"'));
  assert.ok(!elements.get("phoneactions").innerHTML.includes('data-action="transport"'));
  const manualBefore = requests.length;
  vm.runInContext('view = { kind: "error", phone_id: "example", diagnosis: { cause: "host_mode_off", actions: ["reopen_host_mode"] } }; renderStage();', context);
  await vm.runInContext('diagnosisAction("reopen_host_mode")', context);
  assert.equal(requests.length, manualBefore);
  assert.ok(!elements.get("stage").innerHTML.includes('data-diagnosis="reopen_host_mode"'));
  vm.runInContext('phones.phones[0].has_adb = true; phones.phones[0].has_usb = true; renderDevices();', context);
  assert.ok(elements.get("phoneactions").innerHTML.includes('data-action="host_mode"'));
  assert.ok(elements.get("phoneactions").innerHTML.includes('data-action="transport"'));
  vm.runInContext('phones.phones[0].transport = "usb"; phones.phones[0].has_lan = false; renderDevices();', context);
  assert.ok(!elements.get("phoneactions").innerHTML.includes('data-action="transport"'));
  vm.runInContext('phones.phones[0].has_lan = true; renderDevices();', context);
  assert.ok(elements.get("phoneactions").innerHTML.includes('data-action="transport"'));
  assert.ok(vm.runInContext('errorMessage({ code: "bad_param", detail: "steps: 1..50" })', context).includes("1..50"));
  assert.ok(!vm.runInContext('errorMessage({ code: "bad_param", detail: attack })', context).includes(context.attack));
  assert.notEqual(copy.en.worker_stopped, copy.en.app_closed);
  assert.ok(copy.en.app_closed.includes("possible"));
  assert.ok(copy.en.d_host_mode_off.includes("may"));
  // Invalid type/oversize files are rejected before FileReader; decoded pixels are capped too.
  const beforeReads = fileReads;
  context.badFiles = [{ type: "image/svg+xml", size: 10 }, { type: "image/png", size: 6 * 1024 * 1024 + 1 }];
  vm.runInContext('for (const file of badFiles) setRef(file, "example");', context);
  assert.equal(fileReads, beforeReads);
  assert.equal(elements.get("referenceerror").textContent, copy.en.image_limit);
  vm.runInContext('setRef({ type: "image/png", size: 100, data: "data:image/png;base64,fixture" }, "example.png");', context);
  await tick();
  vm.runInContext('$("refimg").naturalWidth = 4001; $("refimg").naturalHeight = 4000; $("refimg").onload();', context);
  assert.equal(vm.runInContext("refData", context), null);
  assert.equal(elements.get("referenceerror").textContent, copy.en.image_limit);
  // Only one 15-second memory timer; polls/model-list refreshes do not trigger it.
  assert.equal([...intervals.values()].filter((timer) => timer.delay === 15000).length, 1);
  vm.runInContext('waiting = requestPending = false; sel = { phone: { id: "example" }, model: { id: "sd15" } };', context);
  let releaseMemory;
  memoryGate = new Promise((resolve) => { releaseMemory = resolve; });
  const beforeMemory = requests.filter((r) => r.url.startsWith("/api/memory")).length;
  const pendingMemory = vm.runInContext("refreshMemory()", context);
  await vm.runInContext("refreshMemory()", context);
  assert.equal(requests.filter((r) => r.url.startsWith("/api/memory")).length, beforeMemory + 1);
  releaseMemory(); await pendingMemory; memoryGate = null;
  // Changing models during a request queues one refresh, never overlaps or applies stale RAM.
  memoryGate = new Promise((resolve) => { releaseMemory = resolve; });
  const changingMemory = vm.runInContext("refreshMemory()", context);
  vm.runInContext('sel.model.id = "other-model"; modelChanged();', context);
  assert.equal(requests.filter((r) => r.url.startsWith("/api/memory")).length, beforeMemory + 2);
  memoryGate = null; releaseMemory(); await changingMemory; await tick();
  assert.equal(requests.filter((r) => r.url.startsWith("/api/memory")).length, beforeMemory + 3);
  assert.equal(vm.runInContext("memoryKey", context), "example/other-model");
  vm.runInContext('waiting = true;', context);
  await vm.runInContext("refreshMemory()", context);
  assert.equal(requests.filter((r) => r.url.startsWith("/api/memory")).length, beforeMemory + 3);
  // Keep nodes intact when status/model markup has not changed.
  vm.runInContext('waiting = false; sel.phone.models = [sel.model]; renderModels(); renderStatus();', context);
  function countMarkupWrites(el) {
    let value = el.innerHTML, writes = 0;
    Object.defineProperty(el, "innerHTML", { configurable: true, get() { return value; }, set(next) { writes++; value = next; } });
    return () => writes;
  }
  const modelWrites = countMarkupWrites(elements.get("model")), statusWrites = countMarkupWrites(elements.get("status"));
  vm.runInContext('renderModels(); renderStatus(); renderModels(); renderStatus();', context);
  assert.equal(modelWrites(), 0); assert.equal(statusWrites(), 0);
  assert.ok(context.tiles[0].innerHTML.includes('aria-label="Open image'));
  // A lost backend removes moving progress and disables generation; recovery reconciles without resubmission.
  const statusPhone = { id: "example", name: "Example", transport: "lan", state: "ready", has_lan: true, has_usb: false, features: {} };
  customStatus = { now: Date.now() / 1000, quit: true, busy: "generating", job: { phone_id: "example", model_id: "sd15", started: Date.now() / 1000, stages: [["generate", Date.now() / 1000]], progress: [2, 10] }, phones: { phones: [statusPhone] } };
  context.statusFixture = customStatus;
  vm.runInContext('phones = statusFixture.phones; catalog = [{ ...phones.phones[0], models: [{ id: "sd15", name: "Example" }] }]; pickPhone(catalog[0]); waiting = true; waitingFor = "generating"; view = { kind: "progress" };', context);
  await vm.runInContext("pollStatus()", context);
  assert.equal(elements.get("quit").hidden, false);
  const genCount = requests.filter((r) => r.url === "/api/generate").length;
  statusOffline = true;
  await vm.runInContext("pollStatus()", context);
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.backend_lost));
  assert.ok(!elements.get("stage").innerHTML.includes('class="times"'));
  assert.equal(elements.get("go").disabled, true);
  const lostMarkup = elements.get("stage").innerHTML;
  [...intervals.values()].find((timer) => timer.delay === 1000).fn();
  assert.equal(elements.get("stage").innerHTML, lostMarkup);
  statusOffline = false;
  await vm.runInContext("pollStatus()", context);
  assert.ok(elements.get("stage").innerHTML.includes('class="times"'));
  customStatus.busy = null; customStatus.last = { url: "/example.png", meta: { prompt: "example" } };
  await vm.runInContext("pollStatus()", context);
  assert.equal(vm.runInContext("view.kind", context), "image");
  assert.equal(requests.filter((r) => r.url === "/api/generate").length, genCount);
  const memoryBeforePoll = requests.filter((r) => r.url.startsWith("/api/memory")).length;
  const writesBeforePoll = statusWrites();
  await vm.runInContext("pollStatus(); loadModels()", context);
  assert.equal(requests.filter((r) => r.url.startsWith("/api/memory")).length, memoryBeforePoll);
  assert.equal(statusWrites(), writesBeforePoll);
  customStatus.quit = false; await vm.runInContext("pollStatus()", context);
  assert.equal(elements.get("quit").hidden, true);
  customStatus.capabilities = { quit: true }; await vm.runInContext("pollStatus()", context);
  assert.equal(elements.get("quit").hidden, false);
  customStatus.capabilities.quit = false; await vm.runInContext("pollStatus()", context);
  assert.equal(elements.get("quit").hidden, true);
  customStatus.quit = true; customStatus.busy = "generating";
  await vm.runInContext("pollStatus()", context);
  quitBusy = true;
  await elements.get("quit").onclick();
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.quit_after));
  await vm.runInContext("pollStatus()", context);
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.quit_after)); // polling must not dismiss the decision
  elements.get("quitcancel").onclick();
  assert.equal(vm.runInContext("quitting", context), false);
  await elements.get("quit").onclick();
  await elements.get("quitafter").onclick();
  assert.deepEqual(requests.filter((r) => r.url === "/api/quit").at(-1).body, { after_job: true });
  assert.equal(vm.runInContext("quitting", context), true);
  assert.equal(elements.get("go").disabled, true);
  statusOffline = true; await vm.runInContext("pollStatus()", context);
  assert.ok(elements.get("stage").innerHTML.includes(copy.en.stopped));
  const stoppedRequests = requests.length;
  await vm.runInContext("pollStatus(); schedulePoll(0); refreshMemory()", context);
  assert.equal(requests.length, stoppedRequests);
  assert.equal(timers.has(vm.runInContext("pollTimer", context)), false);
  // Idle quit also terminates polling immediately after an acknowledged response.
  statusOffline = false; quitBusy = false;
  vm.runInContext('stopped = quitting = waiting = false;', context);
  await vm.runInContext("quitDesktop()", context);
  assert.equal(vm.runInContext("stopped", context), true);
  assert.deepEqual(requests.filter((r) => r.url === "/api/quit").at(-1).body, {});
  console.log("Page smoke passed: five languages, onboarding, transport/error recovery, image limits, memory concurrency, focus stability, quit/busy/after-job and existing editing/pairing/diagnosis flows.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
