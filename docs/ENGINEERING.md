# Engineering notes

Local Dream Desktop exists to make the phone app's host mode useful from a
computer: type longer prompts, paint masks with a mouse, and keep images and
settings together on disk. Inference stays on the phone.

## Small by design

The companion kept Python and one HTML page. There is no frontend framework or
build step for the page. Python's standard library handles HTTP, subprocesses,
storage and locking; Pillow handles images. PyInstaller is needed only at build
time, to ship a portable app without requiring users to install Python.

USB uses `adb forward` to loopback ports on the computer. Wi-Fi uses direct LAN
HTTP to the phone. The official phone host-mode ports have no authentication;
neither transport adds authentication to that protocol.

## Decisions that shape the implementation

- The desktop server binds only to loopback. Host, Origin and `Sec-Fetch-Site`
  checks restrict requests, and a Content Security Policy limits page content.
- Phone responses and image inputs are validated before use. File access is
  restricted to the output folder; phone registry bookkeeping stays out of API responses.
- A catalog revision and record identity prevent a delayed background model
  refresh from overwriting a newer catalog or a re-added phone.
- Removed USB serials are remembered and ignored by automatic discovery.
  Explicit **Find it** / **Refresh** restores them.
- Quit can wait for the current job and its file save. Scheduled quit rejects
  new work and leaves the shared adb server and forwards running.
- Optional protocol extensions are detected from `/info`, with their controls
  hidden when unsupported. A version number alone never enables them.

Protocol details, paths and failure handling are in [How it works](how-it-works.md).

## Evidence and limits

Real-device checks used one Windows 11 PC and official Local Dream
3.0.0-alpha.5 on one phone, over Wi-Fi with adb not installed or unreachable.
The packaged Windows build was extracted to a folder with spaces. Verified:

- First-run page and Wi-Fi onboarding; a wrong IP shows “Couldn't reach that
  address in time…”; connection by IP works.
- Text-to-image saves the image and its JSON. An SD1.5 NPU inpainting mask
  changes pixels inside the mask, with the outside approximately unchanged.
- History remains available after reloading the page.
- Closing the phone app shows manual host-mode steps without offering an adb
  action. Reopening host mode reconnects automatically within seconds.
- Quit through `/api/quit` stops the desktop app.

A development build sharing the same native engine also ran FLUX.2 Klein
editing with two references and a Z-Image Turbo mask; those two paths are not
yet tested with the official app.

Automated tests use loopback phone fixtures, fake adb, temporary storage and
browser-page checks. They cover USB forwarding, multi-phone handling,
reconnection, CLI JSON, quit and diagnosis.

| Platform or transport | Evidence and limits |
|---|---|
| Windows packaging | Automated smoke check in addition to the real packaged Windows run above |
| USB | Automated only; no real-hardware test |
| Linux | Package builds and packaged smoke checks (start, page, vendor, reopen, quit and CLI JSON) pass in CI on Ubuntu; not tried with a real phone |
| macOS | Source and packaged builds remain untested |

The [compatibility matrix](user-guide.md#compatibility)
lists the remaining gaps.

## How it was built

Most code was written by AI coding agents: Claude Code orchestrated the work,
and OpenAI Codex implemented and reviewed changes. The project owner directed
the work, made decisions and tested on real devices.

Validation combined
automated tests, independent AI reviews and real-device runs. That process
provides evidence for the paths exercised; it does not establish untested
platforms or models as working.
