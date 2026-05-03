#!/usr/bin/env python3
"""One-command local review app launcher."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

import youtube_monitor as monitor


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_DB_DIR = REPO_ROOT / "youtube-db"


def latest_review_date(db_dir: Path) -> str:
    review_dir = db_dir / "review"
    candidates = sorted(
        path.stem
        for path in review_dir.glob("????-??-??.json")
        if path.is_file()
    )
    if not candidates:
        raise monitor.MonitorError(f"No review state files found under {review_dir}. Run or refresh the daily brief first.")
    return candidates[-1]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the local YouTube review app.")
    parser.add_argument("date", nargs="?", help="Review date. Defaults to the latest youtube-db/review/YYYY-MM-DD.json file.")
    parser.add_argument("--db-dir", type=Path, default=DEFAULT_DB_DIR)
    parser.add_argument("--host", default=monitor.DEFAULT_REVIEW_HOST)
    parser.add_argument("--port", type=int, default=monitor.DEFAULT_REVIEW_PORT)
    parser.add_argument("--no-open", action="store_true", help="Start the server without opening a browser.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    run_date = args.date or latest_review_date(args.db_dir)
    url = f"http://{args.host}:{args.port}/"
    if not args.no_open:
        webbrowser.open(url)
    print(f"YouTube review app: {url}")
    print(f"Review date: {run_date}")
    print("Press Ctrl+C to stop.")
    monitor.serve_review(args.db_dir, run_date, args.host, args.port)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except monitor.MonitorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
