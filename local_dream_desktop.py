"""Shared GUI/CLI entry point. No browser or phone work happens on import."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from paths import output_dir, prepare_config


def show_error(message: str, log_path: Path) -> None:
    text = f"Local Dream Desktop could not start.\n{message}\nLog file: {log_path}"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")
    except OSError:
        text += "\nThe log file could not be written."
    if sys.platform == "win32" and sys.stderr is None and os.environ.get("LDPC_NO_DIALOG") != "1":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Local Dream Desktop", 0x10)
    elif sys.stderr is not None:
        print(text, file=sys.stderr)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    prepare_config()  # Must precede importing sdgen (including through studio).
    console_exe = getattr(sys, "frozen", False) and Path(sys.executable).stem.lower() == "ldd"
    cli = console_exe or args[:1] == ["--cli"]
    if args[:1] == ["--cli"]:
        args.pop(0)
    if cli:
        import sdgen
        return sdgen.main(args)
    log_path = output_dir() / "studio.log"
    try:
        no_browser = os.environ.get("LDPC_NO_BROWSER") == "1"
        if args == ["--no-browser"]:
            no_browser = True
        elif args:
            raise ValueError("Use --no-browser for headless startup or ldd --cli for commands.")
        import studio
        log_path = studio.sdgen.OUT / "studio.log"
        return studio.main(open_browser=not no_browser)
    except Exception as error:
        # Do not include arbitrary exception text: configuration/URLs may contain secrets.
        module = sys.modules.get("studio")
        message = str(error) if module and isinstance(error, module.StartupError) else "Startup failed. Check Python/Pillow and output folder permissions."
        show_error(message, log_path)
        return 1


if __name__ == "__main__":
    sys.exit(main())
