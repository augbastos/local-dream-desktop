---
name: local-dream
description: "Generate images on the user's Android phone through Local Dream Desktop; use for /local-dream <request> or when asked to generate an image with Local Dream."
argument-hint: "<image request>"
---

Interpret the free-form request as an image prompt and any requested device, model,
size, seed, or generation settings. Local Dream Desktop runs on Windows; Linux
and macOS still need native platform testing. The installer sets the installation
folder and CLI command below, for either source or an extracted portable package:

Installation: `{{LDPC_DIR}}`
CLI: `{{LDPC_CLI}}`

If the placeholders were not replaced, resolve the installation root containing
this `.claude` folder. Use its `ldd.exe --cli` (or `ldd --cli` on Linux/macOS) when
present; otherwise use `python sdgen.py` from that root with Python 3.10+
(`python3` where appropriate). Replace the command placeholder before running
examples. Quote paths with spaces; PowerShell needs `&` before a quoted executable.
Packaged CLI commands do not need Python. Never use an unrelated developer checkout.

When device or model information is needed, run:

```text
{{LDPC_CLI}} devices --json
{{LDPC_CLI}} models --phone DEVICE_ID --json
```

Choose a ready device and a compatible available model. For photographic or
realistic requests prefer models described as realistic. Prefer DiT for quality
on capable phones when the model is available and suitable. Ask only if a key
choice remains ambiguous; do not assume a missing or unreachable device is ready.

Respect model capabilities: SD1.5 NPU accepts only its listed resolutions;
DiT dimensions are 512..2048 in steps of 64; SD1.5 CPU is square 128..512 in
steps of 64; SDXL/Anima use a 1024 square canvas, with --aspect 3:4 or 4:3 only
when supported. If no size is requested, omit --size to use model defaults.
Do not silently replace an unsupported requested size. Omit unspecified steps,
cfg, scheduler and negative prompt so the CLI uses the model's defaults.

Generate with the chosen IDs and only the requested overrides:

```text
{{LDPC_CLI}} gen "PROMPT" --phone DEVICE_ID --model MODEL_ID --json
```

Other generation flags are --size WxH, --aspect 3:4, --steps N, --cfg N,
--scheduler NAME, --seed N, --neg TEXT, --image PATH, --strength N, --mask PATH.
Pass free-form text as a properly escaped argument; never interpolate it as shell
code. Image-to-image requires compatible capabilities; a mask requires an image.
Do not automatically retry a generation after a timeout or lost stream.

Report success only if the JSON contains `ok:true` and the returned `path` exists.
Report the real saved path and settings from `meta`, including model, size, seed,
steps, cfg and scheduler when present. Show the saved image using the Read tool
when available. If `ok:false`, report its actual `error` and `detail`; a missing
file or invalid response is a failure, not a successful generation. If the
session is remote or cannot reach this CLI, say so and do not claim it ran.
