# How Local Dream Desktop works

Local Dream Desktop uses Python 3.10+ and Pillow. The browser page is plain HTML,
CSS and JavaScript. The phone runs all inference through Local Dream's host mode.
There are no cloud services, accounts or telemetry in the companion.

## Host mode protocol

The client uses Local Dream's host mode protocol 1 (tested with 3.0.0-alpha.5;
earlier versions untested). The 3.0 model
catalog adds `dit_kind`; a missing field is treated as empty.

| Phone endpoint | Purpose |
|---|---|
| `:8808/info` | Identify the app, protocol, version and device |
| `:8808/models` | Read downloaded models, defaults and image-to-image support |
| `:8808/select` | POST a model ID, width and height to load it |
| `:8808/status` | Read the serving model, dimensions, state and error message |
| `:8808/stop` | POST the serving `model_id` to unload it; never an ID-less stop |
| `:8081/health` | Check the generation worker |
| `:8081/tokenize` | Count prompt tokens and read the model's limit, if any |
| `:8081/generate` | POST one generation request and read its event stream |

Selection is skipped when the requested model and dimensions already match a
running, healthy worker. After selection, the client waits up to 240 seconds for
both `running` and `/health`.

| Selection result | Desktop error and handling |
|---|---|
| Status error | `load_failed` with the phone's message |
| Error scoped to a previous model | Ignored while the new selection starts |
| `/select` HTTP 404 | `model_missing` |
| `/select` HTTP 500 with the official `backend start rejected` error | `load_failed` |

Generation sends the model's defaults unless overridden: prompt, negative prompt,
steps, cfg and scheduler, plus dimensions, aspect ratio, optional seed and optional
image/mask with denoise strength. It requests raw output with OpenCL and diffusion
previews disabled. Validation happens before selection or generation:

| Family | Sizes and limits |
|---|---|
| SD1.5 NPU | Model's resolution list, falling back to 512×512 if absent |
| SD1.5 CPU | Squares from 128 to 512 in steps of 64, defaulting to 256 |
| DiT | Each dimension from 512 to 2048 in steps of 64; the page includes six presets |
| SDXL/Anima | 1024×1024 canvas; non-square results use `aspect_ratio` 3:4 or 4:3, only with image-to-image support; saved dimensions come from the returned image |

Steps are integers 1–50; cfg is 1–30; strength is 0–1; seed is an unsigned 32-bit
integer. A mask requires a reference image.

The stream carries progress, complete or error events. Raw RGB/RGBA byte lengths
are checked against the returned dimensions; PNG/JPEG results are decoded with
Pillow.

A tokenizer failure does not prevent generation: both token metadata fields
are saved as null. A successful response with `max_length: 0` means no token cap.
The client never retries a generation after sending it.

Control requests time out after 8 seconds, health after 3 seconds and generation
stream reads after 900 seconds. The status poll uses a 1.5-second `/info` timeout.
Each adb subprocess has a 15-second timeout.

## Edit with reference images

When `/models` reports `use_img2img` and `dit_kind` is `klein` (FLUX.2 Klein) or
`qwen21` (Qwen Image 2.1), the desktop advertises `reference_edit: true` and
`max_references: 3`.

Z-Image, SD1.5, SDXL, Anima and unknown DiT kinds do not advertise reference editing.
The native parser has no count limit; the desktop's
three-image limit is a conservative bound on phone RAM and DiT sequence growth,
not a guarantee that every device can fit three images.

### Choosing references

1. Choose **Edit with references**.
2. Add one to three images in the desired order.
3. Describe the change using **image 1 / image 2** in the prompt.

Removing an image renumbers the remaining thumbnails. The prompt is passed through unchanged.
The model's catalog defaults still apply (Klein: 4 steps / CFG 1; Qwen21:
20 steps / CFG 1 unless overridden by the phone or user).

### Reference inputs and upload format

POST `/api/generate` accepts `reference_images`, a non-empty JSON array of base64
PNG, JPEG (including multi-picture JPEG/MPO) or WebP inputs, optionally with an
image data-URL prefix.

| Input limit | Value |
|---|---|
| Each input file | 6 MiB and 16,000,000 decoded pixels (16 megapixels) |
| Request cap | 32 MiB; three base64 input files fit inside it |
| Each downscaled reference | At most 1,048,576 pixels (about 1 MP) |

Type, file size, pixel count, image decoding and model support are validated
locally before any phone request. Pillow prepares each reference:

1. Apply EXIF orientation; use only the first picture of an MPO.
2. Downscale within the pixel limit, retaining aspect ratio within integer-pixel
   rounding, without cropping or upscaling.
3. Convert to RGB and re-encode as JPEG at quality 95, matching the official app's
   reference budget and upload format.

WebP is accepted as input but is never sent to the native decoder, which uses
stb_image and cannot decode it.

The native POST `:8081/generate` receives bare base64 JPEG `reference_images`,
without `image`, mask or denoise strength. The desktop rejects mixing this mode
with image-to-image/mask parameters.

### Single image and mask mode

The separate **Single image / mask (img2img)** mode keeps the existing `image`,
`denoise_strength` and optional `mask` path. Switching modes clears the painted
mask.

In the native Klein/Qwen21 pipeline:

- An `image` at strength 1 without a mask is itself a reference.
- Strength below 1 or a mask also uses it as the init image.

Inpainting stays on the image-plus-mask path. A canvas with no white pixels
exports no mask and shows a short hint, so the phone receives ordinary image-to-image.

Single-image sources and masks also have a 6 MiB file-size and 16,000,000-pixel
limit on the desktop backend. The page checks source file type and size before
reading it, then rejects an oversized decoded image before it can be submitted.

### Memory, metadata and test coverage

RAM estimates and failure diagnosis add `REFERENCE_EXTRA_MB` (currently 1500 MB,
not hardware-measured) per reference, including references downscaled to about
1 MP.

Reference bytes are excluded from saved
metadata and `/api/status`, including while running and after failures; metadata
records only `reference_edit` and `reference_count`.

Covered by automated fixtures. FLUX.2 Klein editing with two references and a
Z-Image Turbo mask also ran on a development build sharing the same native
engine; reference editing and DiT masks are not yet tested with the official app.

An inpainting mask ran on an SD1.5 NPU model with the official app 3.0.0-alpha.5
via the packaged Windows build.

## Official app control contract

Control contract from the official app source at commit `647dc6f`
([github.com/xororz/local-dream](https://github.com/xororz/local-dream/tree/647dc6f)), with paths relative to
`app/src/main/java/io/github/xororz/localdream/`:

- `remote/RemoteProtocol.kt:18` defines the two ports and protocol; `:46`
  serializes exactly `app`, `protocol`, `version` and `device` in `/info`.
- `remote/RemoteHostServer.kt:116` ignores headers other than content length;
  `:161` routes control requests without authentication. Errors use
  `{"error":"message"}` (`:204`).
- `remote/RemoteProtocol.kt:108` serializes model fields including `is_custom`
  and `generation_size`; `:198` defines the catalog's `use_img2img`, `models`
  and `upscalers`. The image-to-image preference defaults to true in
  `service/RemoteHostService.kt:156`. This enables reference, mask and aspect UI;
  these capabilities depend on the catalog.
- `service/RemoteHostService.kt:165` accepts selection and starts the backend
  asynchronously; `:216` returns status with nullable fields; `:259` ignores
  stale model-scoped stops and accepts legacy unconditional stops. The desktop
  always supplies a model ID.
- `service/RemoteHostService.kt:64` starts foreground host mode and returns
  `START_NOT_STICKY`; `:106` shuts down control and requests backend stop on exit; `:372` and `:378`
  are the Android start/stop entry points.

With host mode off, no host control server answers `/info`; opening the ordinary
app does not prove host readiness.

| Condition | Error and interpretation |
|---|---|
| Refused control connection | `connection_refused`: the address may be wrong or host mode may be off |
| Timeout or unreachable address | Retains its transport error |
| Reachable reply identifying another app | `no_host_mode` |
| Broken generation stream | Checked against the control info and status endpoints; transport failures stay transport failures |
| Broken generation stream; answering host reports an idle, stopped or failed generation worker | `worker_stopped` |

A lost connection alone does not prove an app exit or memory failure.

`remote/RemoteProtocol.kt:12` and `remote/RemoteApiClient.kt:16` delegate generation,
tokenization and health to the native backend. The Kotlin control contract does
not specify generation JSON, base64 encoding or individual SSE event payloads.

The shared generation fixture tests the desktop parser separately from the
source-checked control fixture. The mask contract below refers to the official
app source at commit `647dc6f`
([github.com/xororz/local-dream](https://github.com/xororz/local-dream/tree/647dc6f)).

Text-to-image and an inpainting mask have been tested on a real phone with the
official app 3.0.0-alpha.5 on an SD1.5 NPU model via the packaged Windows build.
Reference editing and DiT masks
have not yet been tested with the official app on a phone; other families are
untested on hardware with the official app.

### Inpainting masks

The page offers **Paint a mask** after a source image loads and the model's
`inpaint` capability is true. Brush/eraser strokes, clear and invert are undoable;
buttons and the labelled brush-size slider use native keyboard controls. Turning
the tool off sends the original reference without a mask.

A new reference, model,
canvas size or aspect clears the drawing. SDXL/Anima still apply their centered
aspect crop after generation, so paint inside that output region.

For masked requests only, the page center-crops the source to the native canvas,
displays that same source beneath the brush and sends both as PNG. The mask is
opaque black/white: white repaints, black preserves, gray means partial influence.

`studio.py` already forwards `mask` to `sdgen.prepare`; the client strips any
data-URL prefix and sends base64 image bytes as JSON `mask` alongside `image`,
dimensions, prompt and `denoise_strength` (0–1, default 0.6).

No additional mode flag is needed. Unmasked request payloads are unchanged, and neither image nor
mask bytes are stored in output metadata.

Native contract evidence from the official app source at commit `647dc6f`
([github.com/xororz/local-dream](https://github.com/xororz/local-dream/tree/647dc6f)), with paths relative to its root:

- `app/src/main/java/io/github/xororz/localdream/remote/RemoteApiClient.kt:14-28`
  sends generation directly to the host's native port; `app/src/main/cpp/src/main.cpp:480-488`
  parses `/generate` using the loaded pipeline's capabilities.
- `app/src/main/cpp/src/RequestParser.hpp:86` defines `denoise_strength`;
  `:188-205` decodes the source; `:233-265` requires img2img and decodes base64
  `mask`, averages RGB and divides by 255.
- `app/src/main/cpp/src/SDUtils.hpp:233-291` forces RGB and resizes/center-crops
  both inputs. Matching upload dimensions are not required by the parser;
  the page exports both at identical dimensions to keep the drawing aligned.
  PNG is supported; no raw-pixel buffer is sent.
- The desktop requires an explicit `image`; native SDXL/Anima aspect padding can
  also synthesize a base (`RequestParser.hpp:207-229`).
- `app/src/main/cpp/src/main.cpp:111` lists SD1.5 CPU/NPU, SDXL (including MNN),
  Anima, Z-Image, Klein and Qwen21.
- SD/Anima require a loaded VAE encoder (`Pipeline.hpp:920-921`);
  DiT requires img2img enabled (`PipelineDit.hpp:94-106`).
- Desktop `inpaint` combines `/models`'s `use_img2img` with these known families
  and DiT kinds; unknown DiT kinds and disabled img2img reject masks before any
  generation request. The catalog does not prove a particular encoder is loaded.
- Ultrafix explicitly rejects masks (`RequestParser.hpp:60-61`).
- `app/src/main/cpp/src/LaplacianBlend.hpp:123-126` weights generated pixels with
  the mask (white = generated). The blend runs **in the native server**, for both
  SD/Anima (`Pipeline.hpp:1251-1289`) and DiT (`PipelineDit.hpp:241-244,334-364`).
- `app/src/main/java/io/github/xororz/localdream/ui/screens/InpaintScreen.kt:284-316`
  rasterizes and base64-encodes a PNG in the app UI. No desktop Laplacian blend
  is needed for this native source; this is not an app-only postprocessing gap.

## Connections and adb

**LAN** sends HTTP directly to the configured host on ports 8808 and 8081. No adb
is needed for generation, model listing or stopping the worker.

**USB** uses adb forwarding to loopback ports on the computer. A port pair is
allocated once, starting at `usb_port_base` (default 47810), after checking other
forwards and whether ports are bindable.

Only this application's owned forwards
are removed when a phone is removed. USB never falls back to LAN or enables Wi-Fi.

The connection panel first asks the user to choose USB or Wi-Fi by IP and states
that Local Dream and a downloaded model are required. Wireless debugging pairing
is under **Advanced**.

Status reports `adb_available`; when false, USB setup links
to Android Platform-Tools and explains that Wi-Fi by IP still works. Each phone
reports `has_usb`, `has_lan` and `has_adb` so the page offers only usable transport
switches and replaces automatic host-mode recovery with manual phone instructions
when no adb transport is attached.

### Discovery and catalog refresh

The status poll runs one `adb devices` call and caches `ro.serialno` by adb serial.
It probes existing USB endpoints without listing forwards, creating them or writing
`phones.json`. Operations that need endpoints establish or repair forwards.

The background watcher polls every 4 seconds while a page has polled in the last
30 seconds, otherwise every 30 seconds. A newly authorized USB phone triggers a
separate connect operation to adopt it, prepare its forwards and cache models.

Removing a USB phone stores its hardware serial in `phones.json`'s
`ignored_serials` list. Automatic adoption skips these serials, including when
another phone triggers a reconnect. Explicit `add_usb` or POST `/api/connect`
(**Find it** / **Refresh**) restores them through
`reconnect(include_ignored=True)` and `connect_usb(include_ignored=True)`.

The background watcher's `reconnect()` keeps `include_ignored=False`.
Removal never deletes images or history.

Ready phones with an empty cached catalog are re-read in the background while
idle, at most once per minute per phone, including failed attempts. Network I/O
does not hold the job lock; results are saved only while idle and if the catalog
revision and phone record identity are unchanged.

Catalog writes increment a
persisted per-phone revision, and removing/re-adding a phone gives it a new record
identity. Comparison and saving share the registry's thread/process lock, so a
newer empty catalog also takes precedence over a delayed response.

**Refresh** beside Model uses POST `/api/connect` to re-read catalogs,
including non-empty catalogs that have changed on the phone.

### adb setup and host mode

adb discovery checks `LDPC_ADB`, PATH, `ANDROID_HOME`, `ANDROID_SDK_ROOT` and standard
SDK locations for Windows, Linux or macOS. adb is optional for LAN.

Set `LDPC_NO_ADB=1` to disable all adb discovery and commands, including automatic
USB adoption and forwarding. Status reports `adb_available: false`; LAN generation
still works. This developer/automation switch is also useful for Wi-Fi-only users
who want the app never to touch adb. It takes precedence over `LDPC_ADB` and SDK/PATH
discovery.

Wireless debugging pairing uses `adb pair`, then `adb connect` to an explicit
connection address or an mDNS `_adb-tls-connect` result for that IP. The pairing
port and connection port are different. There is no port scanning or automatic
conversion to unencrypted adb-over-TCP.

Opening host mode through adb can navigate the app's device-link screen.
The screen-navigation path wakes the screen and dismisses a swipe keyguard.

It checks the window keyguard state and `deviceLocked` only on the current user's
line in `dumpsys trust`. It never bypasses a PIN or force-stops the app.

It launches
Local Dream and navigates the English, Japanese, Korean or Chinese screen labels.
If navigation fails, it asks the user to open host mode manually.

### Memory refresh and unloading

**Unload model** frees phone memory between images; it does not cancel a running
generation. Advisory memory refresh uses one 15-second timer while visible and
idle, plus selection changes or an explicit refresh. At most one memory request
is in flight. Ordinary status polls do not request memory.

### Connection loss and quitting

If the desktop status endpoint stops answering, the page shows a connection-lost
state and stops rendering generation progress and clocks. It keeps checking for
reconnection and reconciles the job's current status without resending generation.

The page offers **Quit Local Dream Desktop** only when `/api/status` advertises
`capabilities.quit: true`. It posts to `/api/quit`; a busy response offers **Quit after this
image** (`after_job: true`) or **Cancel**. An acknowledged quit stops page polling
once the server stops; an idle quit shows the final close-tab message immediately.

Closing the tab leaves the server running.

| Quit request | Response or effect |
|---|---|
| Idle | HTTP 202 |
| Running job, unless `after_job` is explicitly true | HTTP 409 `job_running` |
| Scheduled quit | Waits for the job and its file save (or recorded failure), rejects new work with `shutting_down`, then closes the server and watcher |

Shutdown leaves adb and existing forwards running because other applications
may share them.

### Desktop status contract

GET `/api/status` returns an object with these fields; clients should accept
additional fields rather than require an exact key set:

| Field | Type and meaning |
|---|---|
| `app`, `version` | Strings identifying `local-dream-desktop` and the version from `paths.py` |
| `capabilities` | Object; `quit: true` advertises the page's Quit action |
| `quitting` | Boolean; exit has been accepted or scheduled |
| `busy` | Current operation label, or null when idle |
| `job` | Current job object, or null when idle; includes progress and the latest six step times |
| `error`, `diagnosis`, `last` | Latest error, diagnosis and result objects, or null |
| `now` | Server Unix time in seconds |
| `phones` | Object with public phone records, `usb_new`, `usb_waiting` and, after discovery, `adb_available` |

`quit` is nested inside `capabilities`, not a separate top-level field.
The launcher checks the exact app identity and version before opening a page
or reusing a running server.

### Security boundaries

The phone's HTTP ports are unauthenticated. Use a trusted LAN and never
forward them from a router to the internet. Disable debugging when no longer needed.

The browser server binds exclusively to `127.0.0.1:47865`, validates Host/Origin,
and rejects API requests with a `Sec-Fetch-Site` value other than `same-origin`
or `none`. Discovery uses POST only.

- Responses include a Content Security Policy, `X-Content-Type-Options: nosniff`
  and `Referrer-Policy: no-referrer`.
- The server caps JSON bodies at 32 MiB and checks that gallery paths stay inside
  the output folder, including resolved symlinks.

The canonical page URL is
`http://127.0.0.1:47865/`; `localdream.localhost` and `localhost` are also accepted
Host names. `LDPC_PORT` can override the loopback port, for example in smoke tests.

## Settings and storage

By default, settings are read from `local.json` next to `sdgen.py` for source
runs, or next to the executable in a portable package. Set `LDPC_CONFIG`
to another configuration file path to override it.

`paths.prepare_config()`
selects the executable-adjacent path before importing `sdgen` in a frozen build;
configuration is never read from the bundle's internal resource folder.

Missing or invalid config is
treated as empty. `local.example.json` is a comments-free template; `_config_help`
is explanatory text ignored by the code. The default output folder is
`~/LocalDreamDesktop`; `out` supports home and environment-variable expansion.

| File | Contents |
|---|---|
| `OUT/YYYY-MM-DD/HHMMSS_seed.png` | Generated image; numbered suffixes prevent collisions |
| Matching `.json` | Sent parameters without image/mask/reference payloads, model/phone IDs, returned seed and dimensions, reference basename or count, tokens, timings and app version |
| `OUT/phones.json` | Phone names, transport, LAN host or USB hardware serial, owned port pair and cached models |
| `OUT/locks/` | OS file locks for the registry and each phone |
| `OUT/studio.log` | Start, operation and error-code log |

Registry writes and completed image/metadata files use temporary files plus atomic
replacement. Old `adb_port` records are migrated when the registry is used by an
operation: obsolete ports are dropped. Status polling migrates only in memory.

Removing a phone preserves its images. The gallery
shows the latest 60 images; deleting an image also deletes its matching metadata.

HTTP and CLI responses expose an explicit public view of phone records. Record
identities, catalog revisions, hardware serials and USB forwarding bookkeeping
stay in the registry.

## Entry points and files

| File | Responsibility |
|---|---|
| `Local Dream Desktop.cmd` | Windows launcher, interpreter discovery and preflight |
| `local-dream-desktop.sh` | Linux/macOS launcher using python3; native platform testing remains pending |
| `assets/icon.png` | Original project icon included in the package |
| `local-dream-desktop.ico` | Original project icon used by packaged executables |
| `tools/make_icon.py` | Reproduce the original two-star artwork; takes one output path |
| `local_dream_desktop.py`, `paths.py` | Shared GUI/CLI entry point, version, configuration and resource paths |
| `studio.py`, `studio.html` | Loopback HTTP API, background watcher and five-language page |
| `sdgen.py` | Shared HTTP/adb client, validation, locks, output and CLI |
| `tools/check_env.py` | Check Python, Pillow, optional adb and port availability |
| `tools/install_skill.py` | Install/uninstall the optional `local-dream` skill; `--codex` selects Codex |
| `.claude/skills/local-dream/SKILL.md` | Skill template; also usable directly from the repository |
| `tests/fakeld.py`, `tests/fake_adb.py` | Local HTTP and adb fixtures |

### Portable packages and the assistant skill

The native PyInstaller one-directory package has two executables sharing an
`_internal` resource folder: **Local Dream Desktop.exe** opens the page on
Windows; **ldd.exe** is the console CLI. Linux/macOS candidates omit `.exe`.

Use `ldd --cli devices --json` (or `ldd devices --json`) for stdout and CLI exit
codes. The GUI also accepts `--cli`, but the console executable is intended for
redirected output. Source launchers use `local_dream_desktop.py` too.

Resources such as `studio.html`, the QR library and icon come from
`paths.resource_dir()`: the source root or the bundle's read-only resource
folder. Configuration stays beside the executable, and generated files stay in
the configured output folder. Keep the whole extracted folder together.

| Automation option | Effect |
|---|---|
| `LDPC_NO_BROWSER=1` / `--no-browser` | Suppress browser opening |
| `LDPC_NO_DIALOG=1` | Suppress native startup error dialogs for automation |

Startup errors include the log path; a windowed Windows launch uses a native
dialog when stderr is unavailable.

The package includes `tools/install_skill.py` and
`.claude/skills/local-dream/SKILL.md`. With Python 3.10+ available:

1. Run `python tools/install_skill.py` from the source or package folder to
   install the optional skill for Claude Code, or add `--codex` for Codex.
2. Use `--dry-run` to preview the target, `--uninstall` to remove this installer's
   copy, or `--force` to replace an unmanaged skill.

Targets are the assistant's per-user
`skills/local-dream/SKILL.md` location. The installer rejects symlinked targets
and writes atomically.

Rendered commands point to that installation's `ldd.exe --cli` / `ldd --cli`
when present, otherwise `python sdgen.py`; they never embed the builder's
checkout. Only the installer needs Python for a portable installation.

Reinstall the skill if the source or package folder moves. An unrendered
repository template explains how to resolve the CLI relative to its own root.
Native build steps and third-party notices are described in
[Release packaging](../packaging/README.md).

Every CLI command accepts `--json`: one object on stdout and exit 0 on success,
or exit 1 with `ok:false`, `error` and `detail`. The browser reserves its busy state
before returning HTTP 202. Per-phone OS locks prevent collisions between CLI,
page and skill. The page runs one job at a time.

### Memory estimates

Before generation, available memory is read from `/proc/meminfo`'s `MemAvailable`
only through an already attached matching USB or wireless adb transport;
this check never opens an adb connection. Without adb the reading is null.

GET `/api/memory?phone_id=…&model_id=…` returns `available_mb`, `needed_mb`, `reference_extra_mb` and
`low`. The page shows the memory estimate under the model without blocking generation.

`gen --json` includes `warnings` even on failure; ordinary CLI generation prints
them to stderr before loading.

| Family | Base RAM estimate (not hardware-measured) |
|---|---|
| DiT | 4200 MB |
| SDXL/Anima | 4000 MB |
| SD1.5 NPU/CPU | 1500 MB |

A reference adds an estimated 1500 MB for DiT/SDXL/Anima;
that overhead still needs hardware measurement.
Both estimates come from the backend; the page uses the returned reference overhead.

### Foreground check before selection

Before model selection, generation looks for an already attached matching adb
transport and checks the current keyguard. Only an unlocked keyguard permits
`KEYCODE_WAKEUP` and `am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -n io.github.xororz.localdream/.MainActivity`.

The launcher intent brings the existing task to the front.
A locked or unknown keyguard skips the launch. This step never unlocks, force-stops,
connects or pairs the phone.

### Failure diagnosis

On failure, `/api/status` includes `diagnosis` with a primary `cause`, all applicable
`causes`, evidence and manual action IDs.

Read-only adb probes share a five-second budget:

- Recent exit info and available memory.
- The five largest non-system apps.
- For DiT/SDXL, whether the model directory exists under
  `/sdcard/Download/LocalDream/models`.

Primary-cause selection:

1. A recent native crash gives `native_crash`.
2. Otherwise, a DiT/SDXL model directory found in public storage gives `models_in_public_storage`.
3. Otherwise, use the first applicable cause: `out_of_memory`, `app_closed`, `app_not_foreground`, `network`, `load_failed`, `worker_stopped`, `host_mode_off`, or `unknown`.

`out_of_memory` requires a recent low-memory exit, or an `app_closed` error with measured available RAM below the estimate. A low RAM reading alone does not establish it.

All applicable causes contribute actions, deduplicated in this order:
reference removal, smaller size, app storage, foreground, unlock, close apps,
host mode, Wi-Fi, another model, retry.

The error card offers supported smaller
sizes, reference removal and
translated guidance for app storage, foreground, unlock, host mode or Wi-Fi.
Rendering the card never executes an action; moving models and unlocking remain
manual. Without attached adb, diagnosis uses only the error and request.

### Phone-clock evidence

Job start and diagnosis each read `adb shell date +%s` through an already attached
matching transport within their five-second budget. Exit timestamps are bounded
by that phone-clock window; formatted timestamps use the phone's UTC offset.

If one clock read fails, the monotonic job duration and the other phone reading
define the window. With no phone-clock anchor, exit records are ignored.

## Tests

```sh
python -m unittest discover -s tests -v
node tests/check_page.js
```

Tests use loopback HTTP fixtures, fake adb and temporary output/configuration.
Child processes receive `LDPC_CONFIG` pointing at a temporary config, so they do
not load the developer's real `local.json`. No real phone is contacted.

Coverage includes:

- Transport ownership, read-only status and current-user lock detection.
- Model sizes/defaults, load errors, tokenizer failure and stream/decode errors.
- Metadata, atomic saves, process locks, CLI JSON, HTTP admission and path safety.

## Appendix: optional protocol extensions

The desktop checks optional fields in the phone's GET `/info` response.
The official app 3.0.0-alpha.5 does not report these fields.
The desktop uses capabilities reported by those fields, never the version string.
Missing fields mean unsupported extensions; unreachable phones have unknown capabilities.

### `desktop_link`

If `/info` reports a `desktop_link` object, the page offers QR pairing.
Without a known phone reporting this field, the entire QR panel and its copy
are absent and the page makes no pairing or discovery requests.
USB and manual IP entry remain available.

The QR contains a `localdream://pair` URL and is rendered locally using the
vendored MIT QR library.

While the panel is open, the page checks discovery
every two seconds. Discovery uses stdlib UDP mDNS for
`_localdream._tcp.local` (PTR, SRV and A records, with a two-second timeout),
merged with saved addresses. Automatic LAN adoption requires a verified proof;
multicast filtering can prevent discovery.

The desktop stores a random 32-character hexadecimal token and computer name
in `OUT/desktop-link.json` using atomic replacement.

Control requests to registered or just-verified hosts send it as `X-Desktop-Token`, including `/info`.
If the phone reports `desktop_link.auth_required`, pairing proof is required
to establish the connection. HTTP 401 becomes `not_paired`.

Tokens are excluded from logs and image metadata.
The pairing endpoint uses the desktop server's Host/Origin checks.
Native generation port 8081 remains unauthenticated.

Discovery probes omit the token and send a fresh 32-character hexadecimal nonce
in `X-Desktop-Challenge`. The phone can return `desktop_link.proofs`: lowercase
hexadecimal HMAC-SHA256 values using each paired token's UTF-8 bytes as key and
the nonce's UTF-8 bytes as message, rather than hex-decoding either.

The desktop uses `hmac.compare_digest` to verify a proof before automatic
registration or sending the token. A claimed `token_ok` or service announcement
alone is insufficient.

The adb host-mode helper also tries the shell command
`HostModeCommandActivity` with `HOST_MODE_START`. An absent activity or
permission denial falls back to screen navigation; other failures remain errors.

### `app_state`

If `/info` reports an `app_state` object, the desktop uses its `foreground` and
`locked` fields for advisories and failure diagnosis. Model-storage diagnosis
comes from attached adb probes rather than `app_state`.
Warnings do not block generation. The snapshot is captured before model loading,
after any best-effort foreground attempt, and exposed in the running job.

GET `/api/memory` can also include `app_state`; the page applies running-job
updates only to the selected phone and model. Missing state produces no
app-state warnings or diagnosis inferences.
