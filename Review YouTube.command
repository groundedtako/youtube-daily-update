#!/bin/bash
# Double-click launcher for the latest YouTube review queue.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

python3 scripts/youtube-monitor/review_app.py "$@"
