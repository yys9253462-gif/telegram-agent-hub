#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
        apt-get update
        apt-get install -y python3 ca-certificates
    else
        sudo apt-get update
        sudo apt-get install -y python3 ca-certificates
    fi
fi
if [ "$(id -u)" -eq 0 ]; then
    exec python3 "$SCRIPT_DIR/install.py" "$@"
else
    exec sudo python3 "$SCRIPT_DIR/install.py" "$@"
fi
