# Local Dream Desktop user guide

Make images on your Android phone from a browser on your computer.
Your phone runs the models; your computer saves the pictures and their settings.
This is an unofficial companion, not affiliated with
[Local Dream](https://github.com/xororz/local-dream).

## Download

**Windows:** download the portable ZIP from [Releases](../../../releases), extract
the whole folder, then double-click **Local Dream Desktop.exe**. No Python
installation is needed. Keep the extracted files together.

SmartScreen may warn because the app is unsigned. If you trust the download,
choose **More info → Run anyway**.

**Linux:** a portable `tar.gz` is built and smoke-tested in CI (start, page, quit,
CLI), but not yet tried with a real phone; [running from source](#run-from-source)
works too. Extract the whole folder, keep its files together and use the included
shell launcher.

**macOS:** run from source; no tested package. The package described in
[Release packaging](../packaging/README.md) is an **UNTESTED, unsigned candidate**:
a one-directory `tar.gz` with a shell launcher, without signing or notarization.

Gatekeeper may block it. After verifying the archive and trusting its origin,
you may remove the quarantine attribute from the extracted folder:

```sh
xattr -dr com.apple.quarantine LocalDreamDesktop
```

Then run the shell launcher. No macOS CI job is enabled.

## Before you start

- Install [Local Dream](https://github.com/xororz/local-dream) with host mode (Device link) on a supported Android phone.
- Download a model inside the phone app.
- Put the phone and computer on the same Wi-Fi, **or** connect them with a USB cable.

## Connect

### Wi-Fi

1. Put the phone and computer on the same Wi-Fi.
2. On the phone, open **Local Dream → ⋮ More → Device link → Enter host mode**.
3. In the desktop page, choose **Connect over Wi-Fi**.
4. Type the IP shown on the phone and select **Connect**.

No extra software is needed for this connection.

### USB

1. Install [Android Platform-Tools](https://developer.android.com/tools/releases/platform-tools)
   and put `adb` on your PATH.
2. Enable **USB debugging** in the phone's developer options.
3. Plug in the cable and allow this computer on the phone.
4. Enter host mode through **Local Dream → ⋮ More → Device link → Enter host mode**.

The desktop finds authorized USB phones automatically; **Find it** searches again
and can restore a phone you removed. USB has automated tests only and has not
been tested on real hardware.

Keep host mode running while generating. Use **Refresh** beside Model after
downloading or removing models on the phone.

## Generate

Pick a model, write a prompt and select **Generate**.

- **Image-to-image:** add a picture to guide the result on a compatible model.
- **Mask brush:** paint the part of your source image you want to change.
- **Reference editing:** supply one to three pictures with FLUX.2 Klein or Qwen Image 2.1;
  see [compatibility](#compatibility) below.

## Quit

Select **Quit Local Dream Desktop** in the page. If an image is running, choose
**Quit after this image** to let it finish saving. Closing the browser tab does
not stop the desktop server. For macOS / Linux source runs, **Ctrl+C** in the
launcher's terminal also stops it.

## Where images are saved

Images go to **~/LocalDreamDesktop**, in folders by day, with JSON settings beside
each PNG. To change the folder, create `local.json` beside the executable
(beside `sdgen.py` for source runs), with an `out` setting, for example:

```json
{"out": "~/Pictures/LocalDream"}
```

Set `LDPC_CONFIG` to another configuration file path to override the default
`local.json` location. Configuration is never read from the package's internal
resource folder. Restart the desktop app after changing this file.

## Troubleshooting

| Message or symptom | What to do |
|---|---|
| “Nothing answered at that address” | Check the phone's IP and enter host mode again. |
| “Couldn't reach” / timeout | Check that both devices are on the same Wi-Fi and that the network allows them to communicate. |
| “ADB is unavailable” / “USB debugging not authorized” | Install Platform-Tools, check PATH and the cable, then tap **Allow** on the phone. Wi-Fi by IP works without adb. |
| “No model on the phone yet” | Download one in Local Dream, then select **Refresh** beside Model. |
| “The model failed to load” | Check the model in the phone app or choose another model. |
| Low memory / “Local Dream closed” | Close other phone apps, try a smaller supported size or fewer references, then reopen host mode and retry. A lost connection alone does not prove low memory. |
| “Connection lost” in the page | Start Local Dream Desktop again and reopen its page. Check the current job before retrying; generation is not automatically resent. |
| “Port 47865 is unavailable” | Another application may be using the port. Close that application or an old desktop instance, then launch again; check `studio.log` in the output folder if startup still fails. |

## Compatibility

**TESTED** means a real-device run, not support for every phone. The official-app
runs below used one Windows 11 PC and official Local Dream **3.0.0-alpha.5** on
one phone.

The packaged Windows build was extracted to a folder with spaces;
connection was over Wi-Fi, with adb not installed or unreachable.

| Status | Coverage |
|---|---|
| TESTED | Wi-Fi connection, entering host mode through the phone app, text-to-image and an inpainting mask on an SD1.5 NPU model with the official app. |
| TESTED | Packaged Windows build: first-run page and Wi-Fi onboarding; wrong IP shows “Couldn't reach that address in time…”; connection by IP. |
| TESTED | Packaged Windows build: text-to-image saved with its JSON; inpainting changes pixels inside the mask while leaving the outside approximately unchanged; history survives a page reload. |
| TESTED | Packaged Windows build: closing the phone app shows manual host-mode steps with no adb action offered; reopening host mode reconnects automatically within seconds. Quit through `/api/quit` stops the desktop app. |
| UNTESTED WITH THE OFFICIAL APP | FLUX.2 Klein reference editing with two references, and Z-Image Turbo inpainting masks. |
| AUTOMATED ONLY | USB forwarding and reconnection (not tested on real hardware), multiple phones, CLI JSON contract, quit while busy and failure diagnosis. |
| AUTOMATED ONLY | Linux package build and packaged smoke test (start, page, quit, CLI) on a CI Ubuntu runner. |
| UNTESTED | macOS source and package; Linux with a real phone. |
| UNTESTED | Qwen Image 2.1, SDXL, Anima, USB with Wi-Fi off on real hardware, and app versions other than 3.0.0-alpha.5. |
| UNSUPPORTED | Running models on the PC, iOS, and phones or chips unsupported by Local Dream itself. The phone app requires recent Snapdragon chips for DiT models. |

## Security

The official phone app's host mode has **no authentication**. Use a trusted
network or USB, and never expose its ports to the internet. The desktop server
binds to **127.0.0.1 only**. Turn off USB debugging when you no longer need it.

## Run from source

Install **Python 3.10+**, open a terminal in the source folder and run:

```sh
pip install -r requirements.txt
```

On Windows, double-click **Local Dream Desktop.cmd**. On Linux / macOS, run:

```sh
sh local-dream-desktop.sh
```

The launcher opens **http://127.0.0.1:47865/**. You can also run
`python local_dream_desktop.py` (`python3` on Linux / macOS).

## CLI

For scripts or coding assistants, the CLI supports `--json`:

```sh
python sdgen.py devices --json
python sdgen.py gen "A quiet forest at sunrise" --json
```

In the Windows package, use `ldd.exe --cli devices --json` instead.
The optional assistant skill and other developer details are documented in
[How it works](how-it-works.md). See also [Engineering notes](ENGINEERING.md)
and the [changelog](../CHANGELOG.md).

License and acknowledgments are in the [README](../README.md#license).
