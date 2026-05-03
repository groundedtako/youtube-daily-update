#!/bin/bash
# Wrapper for launchd / Codex automation / manual execution.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

python3 youtube_monitor.py "$@"
