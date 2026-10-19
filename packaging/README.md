# Release packaging

## Build and smoke-test

1. Install build requirements in a dedicated Python 3.10+ environment.
2. Build natively on the target OS; cross-compilation is not supported.
3. Run the build and packaged smoke check:

```
python tools/build_release.py --python /path/to/venv/python
python tools/smoke_package.py --package dist/LocalDreamDesktop
```

## Package contents and CLI

PyInstaller is a build dependency only. The one-directory release includes a
windowed `Local Dream Desktop.exe` and console `ldd.exe` on Windows (without
`.exe` on Linux/macOS). Both use the same Python entry and `_internal` folder.

Use `ldd --cli devices --json` for redirected stdout and real exit codes;
`ldd devices --json` also works. Use the GUI executable for double-click startup.
Keep the whole extracted folder together. No Python installation is required.

### Optional assistant skill

The folder also includes `tools/install_skill.py` and its skill template. To
install the optional assistant skill:

1. Use Python 3.10+ to run `python tools/install_skill.py` from the extracted
   folder (`--codex` for Codex).
2. Reinstall the skill after moving the folder; it records this package's location.

The installer itself needs Python; the installed skill runs the packaged `ldd`
CLI without Python. Source installs keep using `python sdgen.py`.

### Licenses and version

The build reads Python and Pillow license texts from the selected environment
and fails if either cannot be found. `THIRD-PARTY-NOTICES.txt` includes those
texts and the PyInstaller bootloader exception notice. Project and QR-code
licenses are included as well.

Build output and archives are ignored by Git.
Only `paths.py` defines the application version. The build prints archive bytes.

## Platform archives

| Platform | Archive and launcher |
|---|---|
| Windows | `LocalDreamDesktop-<version>-windows-<arch>.zip` |
| Linux | `LocalDreamDesktop-<version>-linux-<arch>.tar.gz`; includes a shell launcher |

`<arch>` is `x64` for AMD64/x86_64, `arm64` for ARM64/AArch64, or the reported machine name otherwise.

### macOS candidate

macOS is an **UNTESTED, unsigned candidate**, packaged as a one-directory tar.gz
with a shell launcher. There is no signing or notarization. Gatekeeper may block
it.

After verifying the archive and trusting its origin:

1. Remove the quarantine attribute from the extracted folder with
   `xattr -dr com.apple.quarantine LocalDreamDesktop` if needed.
2. Run the shell launcher.

No macOS CI job is enabled.

## Startup and automation

Source launchers use `local_dream_desktop.py` too. The canonical browser address
is `http://127.0.0.1:47865/`; `localdream.localhost` remains accepted. Browser
opening waits for `/api/status` to report the exact app identity and version.

| Option or environment variable | Effect |
|---|---|
| `--no-browser` or `LDPC_NO_BROWSER=1` | Skips browser opening for automation |
| `LDPC_PORT` | Packaged smoke selects an isolated loopback port; normal launches keep port 47865 |
| `LDPC_NO_DIALOG=1` | Suppresses error dialogs in headless automation; errors still go to the log and stderr when available |
| `LDPC_NO_ADB=1` | Packaged smoke disables adb discovery, commands and USB adoption/forwarding even when an authorized phone is attached; this developer/automation switch also supports Wi-Fi-only use, with LAN generation still available |

Startup errors include the log location (`~/LocalDreamDesktop/studio.log`, or
the configured output folder) and use a native Windows error dialog in the
windowed executable.

Resources come from the bundle's read-only resource folder. Configuration uses
`LDPC_CONFIG`, otherwise `local.json` next to the executable (next to `sdgen.py`
for source installs). Output still defaults to `~/LocalDreamDesktop`.

## Shutdown

`POST /api/quit` uses the same Host/Origin/fetch-metadata checks as other writes.
It returns `409 job_running` during a job; `{"after_job": true}` schedules exit
after the job and its file save finish. Status advertises
`capabilities.quit: true` and `quitting`. Scheduled exit rejects new work.

Logs are closed after each write. Shutdown leaves adb and all adb forwards
running, so other applications' connections are preserved. It creates no
temporary files; sdgen cleans up its own temporary image/metadata files.
