"""Locations shared by source installs and the frozen application."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_ID = "local-dream-desktop"
__version__ = "0.1.0"


def server_port() -> int:
    port = int(os.environ.get("LDPC_PORT", "47865"))
    if not 1 <= port <= 65535:
        raise ValueError("LDPC_PORT must be between 1 and 65535")
    return port


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def config_path() -> Path:
    override = os.environ.get("LDPC_CONFIG")
    if override:
        return Path(override)
    folder = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return folder / "local.json"


def prepare_config() -> None:
    """Redirect sdgen's import-time config away from the read-only bundle."""
    if getattr(sys, "frozen", False) and not os.environ.get("LDPC_CONFIG"):
        # Set even when absent: a bundled local.json must never win over this path.
        os.environ["LDPC_CONFIG"] = str(config_path())


def output_dir() -> Path:
    try:
        config = json.loads(config_path().read_text(encoding="utf-8"))
        value = config.get("out") if isinstance(config, dict) else None
    except (OSError, ValueError):
        value = None
    return Path(os.path.expandvars(os.path.expanduser(value if isinstance(value, str) and value else "~/LocalDreamDesktop")))
