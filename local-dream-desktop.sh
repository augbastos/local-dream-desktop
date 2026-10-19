#!/bin/sh
# Local Dream Desktop: run with sh local-dream-desktop.sh, or chmod +x and run directly.
set -eu
LDPC_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
if [ -x "$LDPC_DIR/Local Dream Desktop" ]; then
    exec "$LDPC_DIR/Local Dream Desktop" "$@"
fi
if [ -x "$LDPC_DIR/.venv/bin/python" ]; then
    LDPC_PY="$LDPC_DIR/.venv/bin/python"
else
    LDPC_PY=python3
fi
if ! command -v "$LDPC_PY" >/dev/null 2>&1; then
    echo 'Python 3.10 or newer is required. Install Python, then run:' >&2
    echo 'python3 -m venv .venv && .venv/bin/pip install -r requirements.txt' >&2
    exit 1
fi
"$LDPC_PY" "$LDPC_DIR/tools/check_env.py"
exec "$LDPC_PY" "$LDPC_DIR/local_dream_desktop.py" "$@"
