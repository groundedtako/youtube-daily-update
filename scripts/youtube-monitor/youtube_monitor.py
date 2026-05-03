#!/usr/bin/env python3
"""
Daily YouTube knowledge monitor.

Discovers new videos from followed YouTube channels, fetches timestamped VTT
transcripts with yt-dlp, writes a source-grounded summary, and emits a daily
Markdown brief under youtube-db/.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlencode, urlparse

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_DB_DIR = REPO_ROOT / "youtube-db"
DEFAULT_CONFIG = DEFAULT_DB_DIR / "config" / "channels.json"
DEFAULT_ENV = REPO_ROOT / "scripts" / ".env"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
MIN_VIDEO_DURATION_SECONDS = 180
FEEDBACK_ACTIONS = {"up", "down", "known", "promote"}
DEFAULT_REVIEW_HOST = "127.0.0.1"
DEFAULT_REVIEW_PORT = 8765
COMMON_ENTITY_ALIASES = {
    "AAPL": ["Apple"],
    "AMZN": ["Amazon"],
    "ADVANTEST": ["Advantest"],
    "GOOGL": ["Alphabet", "Google"],
    "META": ["Meta", "Facebook"],
    "MSFT": ["Microsoft"],
    "NFLX": ["Netflix"],
    "NVDA": ["Nvidia", "NVIDIA"],
    "TSLA": ["Tesla"],
}


class MonitorError(RuntimeError):
    """Expected runtime error with a human-readable message."""


@dataclass(frozen=True)
class ChannelConfig:
    handle: str
    label: str
    channel_id: str | None
    uploads_playlist_id: str | None


@dataclass(frozen=True)
class VideoCandidate:
    video_id: str
    title: str
    description: str
    channel_title: str
    channel_handle: str
    published_at: str
    url: str
    duration_seconds: int


@dataclass(frozen=True)
class VttCue:
    start_seconds: float
    end_seconds: float
    text: str


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"Missing config file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_progress(message: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def slugify(text: str, max_len: int = 72) -> str:
    lowered = text.lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return (cleaned or "untitled")[:max_len].strip("-") or "untitled"


def parse_iso8601_duration(value: str) -> int:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value,
    )
    if not match:
        return 0
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def format_duration(seconds: int) -> str:
    hours, rem = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def youtube_url(video_id: str, seconds: int | None = None) -> str:
    base = f"https://www.youtube.com/watch?v={video_id}"
    if seconds is None:
        return base
    return f"{base}&t={seconds}s"


def normalize_keyword(value: str) -> str:
    return value.casefold().strip()


def load_config(path: Path) -> dict[str, Any]:
    config = read_json(path)
    if not isinstance(config.get("channels"), list):
        raise MonitorError("Config must contain a channels array.")
    return config


def channel_configs(config: dict[str, Any]) -> list[ChannelConfig]:
    channels: list[ChannelConfig] = []
    blacklist = {
        normalize_channel_handle(handle)
        for handle in config.get("blacklist_channels", [])
        if isinstance(handle, str)
    }
    for raw in config["channels"]:
        if isinstance(raw, str):
            raw_config: dict[str, Any] = {"handle": raw}
        elif isinstance(raw, dict):
            raw_config = raw
        else:
            raise MonitorError("Each channel must be a handle string or an object.")

        handle = str(raw_config.get("handle", "")).strip()
        if not handle:
            raise MonitorError("Every channel config needs a handle.")
        if normalize_channel_handle(handle) in blacklist:
            continue
        channels.append(
            ChannelConfig(
                handle=handle,
                label=str(raw_config.get("label") or handle),
                channel_id=raw_config.get("channel_id"),
                uploads_playlist_id=raw_config.get("uploads_playlist_id"),
            )
        )
    return channels


def add_channel_to_blacklist(config_path: Path, channel_handle: str) -> str:
    config = load_config(config_path)
    normalized_target = normalize_channel_handle(channel_handle)
    if not normalized_target:
        raise MonitorError("Cannot blacklist an empty channel handle.")
    blacklist = [
        str(handle)
        for handle in config.get("blacklist_channels", [])
        if isinstance(handle, str) and handle.strip()
    ]
    if normalized_target not in {normalize_channel_handle(handle) for handle in blacklist}:
        blacklist.append(channel_handle if channel_handle.startswith("@") else f"@{channel_handle}")
    config["blacklist_channels"] = blacklist
    write_json(config_path, config)
    return blacklist[-1]


def normalize_channel_handle(handle: str) -> str:
    return handle.strip().lstrip("@").casefold()


class YouTubeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(params)
        query["key"] = self.api_key
        url = f"{YOUTUBE_API_BASE}/{endpoint}?{urlencode(query)}"
        response = self.session.get(url, timeout=30)
        if response.status_code >= 400:
            raise MonitorError(f"YouTube API error {response.status_code}: {response.text[:300]}")
        return response.json()

    def resolve_channel(self, config: ChannelConfig) -> dict[str, str]:
        if config.channel_id and config.uploads_playlist_id:
            return {
                "channel_id": config.channel_id,
                "channel_title": config.label,
                "uploads_playlist_id": config.uploads_playlist_id,
            }

        params: dict[str, str] = {"part": "snippet,contentDetails"}
        if config.channel_id:
            params["id"] = config.channel_id
        else:
            params["forHandle"] = config.handle.lstrip("@")

        data = self.get("channels", params)
        items = data.get("items", [])
        if not items:
            raise MonitorError(
                f"Could not resolve {config.handle}. Add channel_id and uploads_playlist_id to config."
            )
        channel = items[0]
        uploads = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads:
            raise MonitorError(f"Channel {config.handle} has no uploads playlist in API response.")
        return {
            "channel_id": channel["id"],
            "channel_title": channel.get("snippet", {}).get("title", config.label),
            "uploads_playlist_id": uploads,
        }

    def latest_uploads(self, playlist_id: str, max_results: int) -> list[dict[str, Any]]:
        data = self.get(
            "playlistItems",
            {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": str(max(1, min(max_results, 50))),
            },
        )
        return data.get("items", [])

    def video_details(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not video_ids:
            return {}
        data = self.get(
            "videos",
            {
                "part": "snippet,contentDetails",
                "id": ",".join(video_ids[:50]),
                "maxResults": "50",
            },
        )
        return {item["id"]: item for item in data.get("items", [])}


def load_processed(index_path: Path) -> dict[str, dict[str, Any]]:
    if not index_path.exists():
        return {}
    processed: dict[str, dict[str, Any]] = {}
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = record.get("video_id")
        if video_id:
            processed[video_id] = record
    return processed


def append_index_record(index_path: Path, record: dict[str, Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def manifest_path(db_dir: Path, run_date: str) -> Path:
    return db_dir / "runs" / f"{run_date}.json"


def candidate_to_manifest_item(video: VideoCandidate, status: str = "pending") -> dict[str, Any]:
    return {
        "video_id": video.video_id,
        "title": video.title,
        "description": video.description,
        "channel_title": video.channel_title,
        "channel_handle": video.channel_handle,
        "published_at": video.published_at,
        "url": video.url,
        "duration_seconds": video.duration_seconds,
        "status": status,
    }


def candidate_from_manifest_item(item: dict[str, Any]) -> VideoCandidate:
    return VideoCandidate(
        video_id=str(item["video_id"]),
        title=str(item.get("title", "")),
        description=str(item.get("description", "")),
        channel_title=str(item.get("channel_title", item.get("channel", ""))),
        channel_handle=str(item.get("channel_handle", "")),
        published_at=str(item.get("published_at", "")),
        url=str(item.get("url") or youtube_url(str(item["video_id"]))),
        duration_seconds=int(item.get("duration_seconds", 0)),
    )


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return read_json(path)


def create_manifest(run_date: str, candidates: list[VideoCandidate]) -> dict[str, Any]:
    return {
        "run_date": run_date,
        "status": "processing" if candidates else "discovering",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "discovery": {
            "status": "completed" if candidates else "pending",
            "total_channels": 0,
            "completed_channels": 0,
            "candidate_count": len(candidates),
            "skipped_processed_count": 0,
            "channels": [],
        },
        "videos": [candidate_to_manifest_item(video) for video in candidates],
    }


def prepare_manifest(
    path: Path,
    run_date: str,
    candidates: list[VideoCandidate],
    force: bool,
) -> tuple[dict[str, Any], bool]:
    existing = load_manifest(path)
    if existing and existing.get("status") == "completed" and not force:
        return existing, False
    if existing and not force:
        for item in existing.get("videos", []):
            if item.get("status") == "processing":
                item["status"] = "pending"
                item["resumed_from_interrupted_processing"] = True
        existing["status"] = "in_progress"
        existing["updated_at"] = utc_now()
        write_json(path, existing)
        return existing, True

    manifest = create_manifest(run_date, candidates)
    write_json(path, manifest)
    return manifest, True


def update_manifest_video(path: Path, manifest: dict[str, Any], video_id: str, fields: dict[str, Any]) -> None:
    for item in manifest.get("videos", []):
        if item.get("video_id") == video_id:
            item.update(fields)
            break
    manifest["updated_at"] = utc_now()
    write_json(path, manifest)


def update_manifest(path: Path, manifest: dict[str, Any], fields: dict[str, Any]) -> None:
    manifest.update(fields)
    manifest["updated_at"] = utc_now()
    write_json(path, manifest)


def finalize_manifest(path: Path, manifest: dict[str, Any]) -> None:
    statuses = [item.get("status") for item in manifest.get("videos", [])]
    manifest["status"] = "completed"
    manifest["completed_at"] = utc_now()
    manifest["updated_at"] = utc_now()
    manifest["summary"] = {
        "processed": statuses.count("processed"),
        "failed": statuses.count("failed"),
        "pending": statuses.count("pending"),
        "processing": statuses.count("processing"),
    }
    write_json(path, manifest)


def load_result_from_artifact(
    db_dir: Path,
    artifact_dir: str,
    stock_aliases: dict[str, list[str]],
) -> dict[str, Any] | None:
    out_dir = db_dir / artifact_dir
    metadata_path = out_dir / "metadata.json"
    summary_path = out_dir / "summary.md"
    insights_path = out_dir / "insights.json"
    if not metadata_path.exists() or not summary_path.exists() or not insights_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        insights = json.loads(insights_path.read_text(encoding="utf-8"))
        summary = summary_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        return None

    metadata["title_entity_mentions"] = extract_entity_mentions(str(metadata.get("title") or ""), stock_aliases)
    mention_text_parts: list[str] = [str(metadata.get("title") or ""), str(metadata.get("channel") or ""), summary]
    transcript_file = metadata.get("transcript_file")
    if transcript_file:
        transcript_path = out_dir / str(transcript_file)
        if transcript_path.exists():
            try:
                mention_text_parts.append(transcript_path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    metadata["entity_mentions"] = extract_entity_mentions("\n".join(mention_text_parts), stock_aliases)

    transcript_file = metadata.get("transcript_file")
    raw_vtt = metadata.get("raw_vtt")
    cues: list[VttCue] = []
    if transcript_file:
        transcript_path = out_dir / str(transcript_file)
        vtt_path = None
        if raw_vtt:
            candidate_vtt = out_dir / str(raw_vtt)
            if candidate_vtt.exists():
                vtt_path = candidate_vtt
        if vtt_path and vtt_path.exists():
            try:
                cues = parse_vtt(vtt_path)
            except (OSError, ValueError):
                cues = []
        elif transcript_path.exists():
            # We can't reconstruct cue timing without VTT; leave cues empty.
            cues = []

    # Recompute insights with the current parsing logic so quotes don't get stuck
    # on the first cue when summarize outputs timestamp ranges in parentheses.
    video = VideoCandidate(
        video_id=str(metadata.get("video_id") or ""),
        title=str(metadata.get("title") or ""),
        description="",
        channel_title=str(metadata.get("channel") or ""),
        channel_handle=str(metadata.get("channel_handle") or ""),
        published_at=str(metadata.get("published_at") or ""),
        url=str(metadata.get("source_url") or ""),
        duration_seconds=int(metadata.get("duration_seconds") or 0),
    )
    insights = extract_insights(video=video, cues=cues, entity_mentions=metadata["entity_mentions"], summary=summary)

    # Persist refreshed insights + quotes so the brief links remain accurate.
    try:
        write_json(insights_path, insights)
        write_quotes(out_dir / "quotes.md", video, insights)
    except OSError:
        pass

    return {"metadata": metadata, "output_dir": out_dir, "summary": summary, "insights": insights}


def failures_from_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for item in manifest.get("videos", []):
        if item.get("status") != "failed":
            continue
        failures.append(
            {
                "video_id": item.get("video_id"),
                "title": item.get("title"),
                "channel_title": item.get("channel_title"),
                "channel_handle": item.get("channel_handle"),
                "url": item.get("url"),
                "error": item.get("error"),
            }
        )
    return failures


def refresh_all_quotes(db_dir: Path, stock_aliases: dict[str, list[str]]) -> dict[str, int]:
    refreshed = 0
    skipped = 0
    failed = 0
    for artifact_dir in sorted((db_dir / "videos").glob("*/*")):
        if not artifact_dir.is_dir():
            continue
        metadata_path = artifact_dir / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        duration_seconds = int(metadata.get("duration_seconds") or 0)
        if 0 < duration_seconds < MIN_VIDEO_DURATION_SECONDS:
            skipped += 1
            continue
        rel = artifact_dir.relative_to(db_dir)
        result = load_result_from_artifact(db_dir, str(rel), stock_aliases)
        if result is None:
            skipped += 1
            continue
        refreshed += 1
    return {"refreshed": refreshed, "skipped": skipped, "failed": failed}


def load_results_from_artifacts(
    db_dir: Path,
    run_date: str,
    stock_aliases: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for artifact_dir in sorted((db_dir / "videos").glob(f"*/{run_date}--*")):
        if not artifact_dir.is_dir():
            continue
        rel = artifact_dir.relative_to(db_dir)
        result = load_result_from_artifact(db_dir, str(rel), stock_aliases)
        if result is None:
            continue
        metadata = result["metadata"]
        duration_seconds = int(metadata.get("duration_seconds") or 0)
        if 0 < duration_seconds < MIN_VIDEO_DURATION_SECONDS:
            skipped.append(
                {
                    "video_id": metadata.get("video_id"),
                    "channel_handle": metadata.get("channel_handle"),
                    "channel_title": metadata.get("channel"),
                    "title": metadata.get("title"),
                    "reason": f"duration_below_{MIN_VIDEO_DURATION_SECONDS}_seconds",
                }
            )
            continue
        results.append(result)
    results.sort(key=lambda item: str(item["metadata"].get("processed_at", "")))
    return results, skipped


def discover_candidates(
    client: YouTubeClient,
    channels: list[ChannelConfig],
    processed: dict[str, dict[str, Any]],
    lookback_count: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[VideoCandidate], list[dict[str, Any]]]:
    candidates: list[VideoCandidate] = []
    skipped: list[dict[str, Any]] = []

    if progress:
        progress({"event": "discovery_start", "total_channels": len(channels)})

    for index, channel in enumerate(channels, start=1):
        if progress:
            progress({"event": "channel_start", "index": index, "total_channels": len(channels), "handle": channel.handle})
        resolved = client.resolve_channel(channel)
        uploads = client.latest_uploads(resolved["uploads_playlist_id"], lookback_count)
        video_ids = [
            item.get("contentDetails", {}).get("videoId")
            or item.get("snippet", {}).get("resourceId", {}).get("videoId")
            for item in uploads
        ]
        video_ids = [video_id for video_id in video_ids if video_id]
        details = client.video_details(video_ids)
        channel_new_count = 0
        channel_skipped_count = 0

        for item in uploads:
            snippet = item.get("snippet", {})
            video_id = item.get("contentDetails", {}).get("videoId") or snippet.get("resourceId", {}).get("videoId")
            if not video_id:
                continue
            detail = details.get(video_id, {})
            detail_snippet = detail.get("snippet", {})
            title = detail_snippet.get("title") or snippet.get("title", "")
            description = detail_snippet.get("description") or snippet.get("description", "")
            duration_seconds = parse_iso8601_duration(detail.get("contentDetails", {}).get("duration", ""))
            if 0 < duration_seconds < MIN_VIDEO_DURATION_SECONDS:
                channel_skipped_count += 1
                skipped.append(
                    {
                        "video_id": video_id,
                        "channel_handle": channel.handle,
                        "channel_title": resolved["channel_title"],
                        "title": title,
                        "reason": f"duration_below_{MIN_VIDEO_DURATION_SECONDS}_seconds",
                    }
                )
                continue
            if video_id in processed:
                channel_skipped_count += 1
                skipped.append(
                    {
                        "video_id": video_id,
                        "channel_handle": channel.handle,
                        "channel_title": resolved["channel_title"],
                        "title": title,
                        "reason": "already_processed",
                    }
                )
                continue

            candidates.append(
                VideoCandidate(
                    video_id=video_id,
                    title=title,
                    description=description,
                    channel_title=resolved["channel_title"],
                    channel_handle=channel.handle,
                    published_at=detail_snippet.get("publishedAt") or snippet.get("publishedAt") or "",
                    url=youtube_url(video_id),
                    duration_seconds=duration_seconds,
                )
            )
            channel_new_count += 1

        if progress:
            progress(
                {
                    "event": "channel_done",
                    "index": index,
                    "total_channels": len(channels),
                    "handle": channel.handle,
                    "channel_title": resolved["channel_title"],
                    "new_count": channel_new_count,
                    "skipped_count": channel_skipped_count,
                    "candidate_count": len(candidates),
                    "skipped_processed_count": len(skipped),
                }
            )

    if progress:
        progress({"event": "discovery_done", "candidate_count": len(candidates), "skipped_processed_count": len(skipped)})

    return candidates, skipped


def run_command(args: list[str], cwd: Path, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )


def fetch_vtt(video: VideoCandidate, output_dir: Path) -> Path | None:
    output_dir.mkdir(parents=True, exist_ok=True)
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        raise MonitorError("yt-dlp not found. Install with: brew install yt-dlp")

    result = run_command(
        [
            yt_dlp,
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs",
            "en,en-US,en.*",
            "--sub-format",
            "vtt",
            "--skip-download",
            "--no-warnings",
            "-o",
            "%(id)s.%(ext)s",
            video.url,
        ],
        cwd=output_dir,
        timeout_seconds=180,
    )
    if result.returncode != 0:
        (output_dir / "yt-dlp-error.log").write_text(result.stderr, encoding="utf-8")
        return None

    candidates = sorted(output_dir.glob(f"{video.video_id}*.vtt"))
    if not candidates:
        return None
    preferred = [path for path in candidates if ".en" in path.name]
    return preferred[0] if preferred else candidates[0]


def strip_vtt_tags(text: str) -> str:
    text = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", text)
    text = re.sub(r"</?c[^>]*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_vtt(path: Path) -> list[VttCue]:
    cues: list[VttCue] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    timestamp_re = re.compile(
        r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s+-->\s+"
        r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
    )
    while i < len(lines):
        match = timestamp_re.search(lines[i])
        if not match:
            i += 1
            continue
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i])
            i += 1
        text = strip_vtt_tags(" ".join(text_lines))
        if text:
            cues.append(
                VttCue(
                    start_seconds=parse_vtt_timestamp(match.group("start")),
                    end_seconds=parse_vtt_timestamp(match.group("end")),
                    text=text,
                )
            )
        i += 1
    return dedupe_adjacent_cues(cues)


def parse_vtt_timestamp(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        return minutes * 60 + seconds
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def dedupe_adjacent_cues(cues: list[VttCue]) -> list[VttCue]:
    deduped: list[VttCue] = []
    for cue in cues:
        if not deduped:
            deduped.append(cue)
            continue
        previous = deduped[-1]
        prev_norm = normalize_for_dedupe(previous.text)
        cue_norm = normalize_for_dedupe(cue.text)
        overlaps = cue.start_seconds - previous.start_seconds <= 3.0
        if overlaps and (prev_norm == cue_norm or prev_norm in cue_norm or cue_norm in prev_norm):
            if len(cue.text) > len(previous.text):
                deduped[-1] = cue
            continue
        deduped.append(cue)
    return deduped


def normalize_for_dedupe(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def write_clean_transcript(path: Path, video: VideoCandidate, cues: list[VttCue], vtt_path: Path | None) -> None:
    lines = [
        "- [ ] read",
        "",
        f"# Transcript — {video.title}",
        "",
        f"**Source:** {video.url}",
        f"**Channel:** {video.channel_title}",
        f"**Published:** {video.published_at}",
        f"**Duration:** {format_duration(video.duration_seconds)}",
        f"**Raw VTT:** {vtt_path.name if vtt_path else 'fallback transcript only'}",
        "",
        "---",
        "",
    ]
    for cue in cues:
        lines.append(f"[{format_timestamp(cue.start_seconds)}] {cue.text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fallback_transcript(video: VideoCandidate, output_dir: Path) -> Path | None:
    summarize = shutil.which("summarize")
    if not summarize:
        return None
    result = run_command(
        [
            summarize,
            video.url,
            "--youtube",
            "auto",
            "--video-mode",
            "transcript",
            "--timestamps",
            "--extract",
            "--format",
            "md",
            "--plain",
        ],
        cwd=output_dir,
        timeout_seconds=300,
    )
    if result.returncode != 0 or not result.stdout.strip():
        (output_dir / "fallback-transcript-error.log").write_text(result.stderr, encoding="utf-8")
        return None
    path = output_dir / "transcript.fallback.md"
    path.write_text(result.stdout, encoding="utf-8")
    return path


def summarize_transcript(transcript_path: Path, output_path: Path, prompt_path: Path, cwd: Path) -> str:
    summarize = shutil.which("summarize")
    if not summarize:
        fallback = fallback_summary(transcript_path)
        output_path.write_text(fallback, encoding="utf-8")
        return fallback
    result = run_command(
        [
            summarize,
            str(transcript_path),
            "--length",
            "long",
            "--max-output-tokens",
            "2200",
            "--timeout",
            "4m",
            "--prompt-file",
            str(prompt_path),
            "--force-summary",
            "--plain",
        ],
        cwd=cwd,
        timeout_seconds=360,
    )
    if result.returncode != 0 or not result.stdout.strip():
        (output_path.parent / "summary-error.log").write_text(result.stderr, encoding="utf-8")
        fallback = fallback_summary(transcript_path)
        output_path.write_text(fallback, encoding="utf-8")
        return fallback
    output_path.write_text(result.stdout.strip() + "\n", encoding="utf-8")
    return result.stdout.strip()


def fallback_summary(transcript_path: Path) -> str:
    lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    cue_lines = [line for line in lines if re.match(r"\[\d", line)]
    selected = cue_lines[:8]
    body = "\n".join(f"- {line}" for line in selected) if selected else "- Transcript text was unavailable."
    return "\n".join(
        [
            "# Source-Grounded Summary",
            "",
            "Summarization CLI was unavailable or failed. This fallback preserves early timestamped transcript cues.",
            "",
            "## Extractive Notes",
            body,
        ]
    )


def load_stock_aliases(repo_root: Path) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for meta_path in sorted((repo_root / "Stocks").glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        symbol = str(meta.get("ticker") or meta_path.parent.name).strip().upper()
        if not symbol:
            continue
        values = {
            symbol,
            str(meta_path.parent.name),
            str(meta.get("company") or ""),
            str(meta.get("company_name") or ""),
            str(meta.get("name") or ""),
        }
        cleaned = [value.strip() for value in values if value and value.strip()]
        aliases.setdefault(symbol, [])
        aliases[symbol].extend(cleaned)
    return {symbol: sorted(set(values), key=str.casefold) for symbol, values in aliases.items()}


def load_aliases_file(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    data = read_json(path)
    raw_aliases = data.get("aliases", data)
    if not isinstance(raw_aliases, dict):
        raise MonitorError("Aliases file must be an object or contain an aliases object.")

    aliases: dict[str, list[str]] = {}
    for key, value in raw_aliases.items():
        symbol = str(key).strip().upper()
        if not symbol:
            continue
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = [str(item) for item in value if str(item).strip()]
        elif isinstance(value, dict):
            values = [
                str(item)
                for item in [
                    value.get("name"),
                    value.get("company"),
                    value.get("label"),
                    *value.get("aliases", []),
                ]
                if item is not None and str(item).strip()
            ]
        else:
            continue
        unique_values = {item.strip() for item in values if item.strip()}
        aliases[symbol] = [symbol, *sorted(unique_values - {symbol}, key=str.casefold)]
    return aliases


def merge_aliases(*sources: dict[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for source in sources:
        for symbol, values in source.items():
            merged.setdefault(symbol, [])
            merged[symbol].extend(values)
    normalized: dict[str, list[str]] = {}
    for symbol, values in merged.items():
        unique_values = {value for value in values if value}
        normalized[symbol] = [symbol, *sorted(unique_values - {symbol}, key=str.casefold)]
    return normalized


def alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    if re.fullmatch(r"[A-Z]{1,6}", alias):
        return re.compile(rf"(?<![A-Za-z0-9])(?:\${escaped}|{escaped})(?![A-Za-z0-9])")
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)


def extract_entity_mentions(text: str, aliases: dict[str, list[str]]) -> dict[str, Any]:
    mapped: list[dict[str, Any]] = []

    override_rules: list[dict[str, Any]] = [
        {
            "symbol": "SE",
            "matched_alias": "Sea Limited",
            "patterns": [
                re.compile(r"(?<![A-Za-z0-9])Sea Limited(?![A-Za-z0-9])", re.IGNORECASE),
                re.compile(r"(?<![A-Za-z0-9])Sea Ltd\\.?\\b", re.IGNORECASE),
            ],
            "requires_any": [],
        },
        # Common transcript/ASR mis-hearing for "Sea Limited" as "C Limited".
        # Guard with product-context terms to avoid mapping unrelated "C Limited" mentions.
        {
            "symbol": "SE",
            "matched_alias": "C Limited",
            "patterns": [re.compile(r"(?<![A-Za-z0-9])C Limited(?![A-Za-z0-9])", re.IGNORECASE)],
            "requires_any": [
                re.compile(r"(?<![A-Za-z0-9])Shopee(?![A-Za-z0-9])", re.IGNORECASE),
                re.compile(r"(?<![A-Za-z0-9])Garena(?![A-Za-z0-9])", re.IGNORECASE),
                re.compile(r"(?<![A-Za-z0-9])SeaMoney(?![A-Za-z0-9])", re.IGNORECASE),
                re.compile(r"(?<![A-Za-z0-9])Sea Money(?![A-Za-z0-9])", re.IGNORECASE),
            ],
        },
    ]
    for rule in override_rules:
        if rule["requires_any"] and not any(p.search(text) for p in rule["requires_any"]):
            continue
        if any(p.search(text) for p in rule["patterns"]):
            mapped.append({"symbol": rule["symbol"], "matched_aliases": [rule["matched_alias"]]})

    for symbol, symbol_aliases in sorted(aliases.items()):
        matches: list[str] = []
        for alias in symbol_aliases:
            if not alias:
                continue
            if alias_pattern(alias).search(text):
                matches.append(alias)
        if matches:
            mapped.append({"symbol": symbol, "matched_aliases": sorted(set(matches), key=str.casefold)})

    common_acronyms = {
        "AI",
        "API",
        "CEO",
        "CFO",
        "CPU",
        "DCF",
        "EPS",
        "ETF",
        "FCF",
        "GPU",
        "IRR",
        "LLM",
        "ROI",
        "ROIC",
        "SBC",
        "TAM",
        "USA",
    }
    known_symbols = {item["symbol"] for item in mapped}
    raw_symbols = set(re.findall(r"\$([A-Z]{1,6})(?![A-Za-z0-9])", text))
    raw_symbols.update(re.findall(r"\(([A-Z]{2,6})\)", text))
    unmapped = sorted(symbol for symbol in raw_symbols if symbol not in known_symbols and symbol not in common_acronyms)
    return {"mapped": mapped, "unmapped_symbols": unmapped}


def mentioned_symbols(entity_mentions: dict[str, Any]) -> list[str]:
    mapped = [item["symbol"] for item in entity_mentions.get("mapped", [])]
    unmapped = list(entity_mentions.get("unmapped_symbols", []))
    return sorted(set(mapped + unmapped))


def merge_overlapping_text(left: str, right: str) -> str:
    left_words = left.split()
    right_words = right.split()
    max_overlap = min(len(left_words), len(right_words), 12)
    for size in range(max_overlap, 0, -1):
        if [word.casefold() for word in left_words[-size:]] == [word.casefold() for word in right_words[:size]]:
            return " ".join([*left_words, *right_words[size:]])
    return f"{left} {right}".strip()


def cue_window(cues: list[VttCue], start_index: int, max_seconds: float = 10.0, max_words: int = 42) -> str:
    start = cues[start_index].start_seconds
    text = cues[start_index].text
    for cue in cues[start_index + 1 :]:
        if cue.start_seconds - start > max_seconds:
            break
        candidate = merge_overlapping_text(text, cue.text)
        if len(candidate.split()) > max_words:
            break
        text = candidate
        if text.endswith((".", "?", "!")) and len(text.split()) >= 12:
            break
    return text


def cue_span_window(
    cues: list[VttCue],
    start_index: int,
    end_index: int,
    max_seconds: float = 75.0,
    max_words: int = 220,
) -> str:
    start = cues[start_index].start_seconds
    text = cues[start_index].text
    for cue in cues[start_index + 1 :]:
        if cue.start_seconds - start > max_seconds:
            break
        candidate = merge_overlapping_text(text, cue.text)
        if len(candidate.split()) > max_words:
            break
        text = candidate
        if cue.start_seconds >= cues[end_index].start_seconds and text.endswith((".", "?", "!")):
            break
    return text


def parse_summary_insights(summary: str) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    in_key_insights = False
    for raw_line in summary.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_key_insights = line.casefold().startswith("## key insights")
            continue
        if not in_key_insights or not line.startswith("- "):
            continue
        text = line[2:].strip()
        timestamp = None
        claim = text

        bracketed = re.match(
            r"\[(?P<start>\d{1,2}:\d{2}(?::\d{2})?)(?:[–-]\d{1,2}:\d{2}(?::\d{2})?)?\]\s*(?P<claim>.+)",
            text,
        )
        if bracketed:
            timestamp = bracketed.group("start")
            claim = bracketed.group("claim").strip()
        else:
            # Common summarize output uses timestamp ranges in parentheses, sometimes multiple:
            # "(2:15–2:32; 47:13–47:26)". Capture the first timestamp anywhere.
            time_match = re.search(r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?)", text)
            if time_match:
                timestamp = time_match.group("start")
                claim = re.sub(r"\s*\([^)]*?\d{1,2}:\d{2}[^)]*\)\s*\.?\s*$", "", text).strip()
                if not claim:
                    claim = text
        claim = re.sub(r"[*_`]+", "", claim).strip()
        # Only accept candidates with a timestamp; otherwise downstream defaults to cue 0
        # and quotes become identical repetitions.
        if claim and timestamp:
            insights.append({"timestamp": timestamp, "claim": claim})
    return insights


def group_nearby_summary_candidates(
    candidates: list[dict[str, Any]],
    max_gap_seconds: int = 16,
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for candidate in candidates:
        seconds = timestamp_to_seconds(str(candidate["timestamp"]))
        candidate_with_seconds = {**candidate, "timestamp_seconds": seconds}
        if not groups:
            groups.append([candidate_with_seconds])
            continue
        previous_seconds = int(groups[-1][-1]["timestamp_seconds"])
        if seconds - previous_seconds <= max_gap_seconds:
            groups[-1].append(candidate_with_seconds)
        else:
            groups.append([candidate_with_seconds])
    return groups


def timestamp_to_seconds(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def closest_cue_index(cues: list[VttCue], seconds: int) -> int:
    return min(range(len(cues)), key=lambda index: abs(cues[index].start_seconds - seconds))


def extract_insights(
    video: VideoCandidate,
    cues: list[VttCue],
    entity_mentions: dict[str, Any],
    summary: str,
    max_items: int = 6,
) -> list[dict[str, Any]]:
    if not cues:
        return []
    summary_candidates = parse_summary_insights(summary)
    if summary_candidates:
        insights: list[dict[str, Any]] = []
        seen_claims: set[str] = set()
        for group in group_nearby_summary_candidates(summary_candidates):
            first_candidate = group[0]
            last_candidate = group[-1]
            first_index = closest_cue_index(cues, int(first_candidate["timestamp_seconds"]))
            last_index = closest_cue_index(cues, int(last_candidate["timestamp_seconds"]))
            cue = cues[first_index]
            if len(group) > 1:
                quote = cue_span_window(cues, first_index, last_index)
                claim = " ".join(str(candidate["claim"]).rstrip(".") + "." for candidate in group)
            else:
                quote = cue_window(cues, first_index)
                claim = str(first_candidate["claim"])
            normalized = normalize_for_dedupe(claim)
            if normalized in seen_claims:
                continue
            seen_claims.add(normalized)
            seconds = int(cue.start_seconds)
            insights.append(
                {
                    "claim": claim,
                    "quote": quote,
                    "timestamp": format_timestamp(cue.start_seconds),
                    "timestamp_seconds": seconds,
                    "url": youtube_url(video.video_id, seconds),
                    "mentioned_entities": mentioned_symbols(entity_mentions),
                    "score": 0,
                    "promotion_status": "pending-review",
                }
            )
            if len(insights) >= max_items:
                break
        return insights

    entity_terms: list[str] = []
    for item in entity_mentions.get("mapped", []):
        entity_terms.append(item["symbol"])
        entity_terms.extend(item.get("matched_aliases", []))
    entity_terms.extend(entity_mentions.get("unmapped_symbols", []))
    keywords = [normalize_keyword(item) for item in entity_terms]
    scored: list[tuple[int, int, str]] = []
    for index, cue in enumerate(cues):
        quote = cue_window(cues, index)
        text = quote.casefold()
        score = sum(2 for keyword in keywords if keyword and keyword in text)
        score += min(len(cue.text.split()) // 18, 3)
        if any(token in text for token in ("because", "therefore", "margin", "revenue", "growth", "risk", "customer")):
            score += 1
        scored.append((score, index, quote))
    scored.sort(key=lambda item: (item[0], cues[item[1]].start_seconds), reverse=True)

    insights: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for score, index, quote in scored:
        cue = cues[index]
        if score <= 0 and insights:
            continue
        normalized_text = normalize_for_dedupe(quote)
        if normalized_text in seen_text:
            continue
        seen_text.add(normalized_text)
        seconds = int(cue.start_seconds)
        insights.append(
            {
                "claim": quote,
                "quote": quote,
                "timestamp": format_timestamp(cue.start_seconds),
                "timestamp_seconds": seconds,
                "url": youtube_url(video.video_id, seconds),
                "mentioned_entities": mentioned_symbols(entity_mentions),
                "score": score,
                "promotion_status": "pending-review",
            }
        )
        if len(insights) >= max_items:
            break
    return insights


def video_output_dir(db_dir: Path, video: VideoCandidate, run_date: str) -> Path:
    channel_slug = slugify(video.channel_handle or video.channel_title, max_len=48)
    video_slug = slugify(video.title, max_len=64)
    return db_dir / "videos" / channel_slug / f"{run_date}--{video.video_id}--{video_slug}"


def process_video(
    video: VideoCandidate,
    db_dir: Path,
    run_date: str,
    prompt_path: Path,
    allow_transcript_fallback: bool,
    stock_aliases: dict[str, list[str]],
    repo_root: Path,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    out_dir = video_output_dir(db_dir, video, run_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    if stage_callback:
        stage_callback("fetch_vtt")
    vtt_path = fetch_vtt(video, out_dir)

    cues: list[VttCue] = []
    transcript_path = out_dir / "transcript.clean.md"
    transcript_status = "vtt"
    if vtt_path:
        if stage_callback:
            stage_callback("parse_vtt")
        cues = parse_vtt(vtt_path)
        write_clean_transcript(transcript_path, video, cues, vtt_path)
    elif allow_transcript_fallback:
        if stage_callback:
            stage_callback("fallback_transcript")
        fallback_path = fallback_transcript(video, out_dir)
        transcript_status = "fallback" if fallback_path else "missing"
        if fallback_path:
            transcript_path = fallback_path
    else:
        transcript_status = "missing"

    summary_path = out_dir / "summary.md"
    summary = ""
    insights: list[dict[str, Any]] = []
    title_entity_mentions = extract_entity_mentions(video.title, stock_aliases)
    entity_mentions: dict[str, Any] = {"mapped": [], "unmapped_symbols": []}
    if transcript_status != "missing":
        if stage_callback:
            stage_callback("summarize")
        summary = summarize_transcript(transcript_path, summary_path, prompt_path, repo_root)
        if stage_callback:
            stage_callback("extract_insights")
        mention_text_parts = [video.title, video.description, summary]
        if cues:
            mention_text_parts.append("\n".join(cue.text for cue in cues))
        else:
            mention_text_parts.append(transcript_path.read_text(encoding="utf-8", errors="replace"))
        entity_mentions = extract_entity_mentions("\n".join(mention_text_parts), stock_aliases)
        insights = extract_insights(video, cues, entity_mentions, summary)
    else:
        summary_path.write_text(
            "# Source-Grounded Summary\n\nTranscript unavailable; summary not generated.\n",
            encoding="utf-8",
        )

    metadata = {
        "video_id": video.video_id,
        "title": video.title,
        "channel": video.channel_title,
        "channel_handle": video.channel_handle,
        "published_at": video.published_at,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "source_url": video.url,
        "duration_seconds": video.duration_seconds,
        "duration": format_duration(video.duration_seconds),
        "title_entity_mentions": title_entity_mentions,
        "entity_mentions": entity_mentions,
        "transcript_status": transcript_status,
        "raw_vtt": vtt_path.name if vtt_path else None,
        "transcript_file": transcript_path.name if transcript_status != "missing" else None,
        "summary_file": summary_path.name,
        "insight_count": len(insights),
    }
    write_json(out_dir / "metadata.json", metadata)
    write_json(out_dir / "insights.json", insights)
    write_quotes(out_dir / "quotes.md", video, insights)
    if stage_callback:
        stage_callback("write_artifacts")

    return {
        "metadata": metadata,
        "output_dir": out_dir,
        "summary": summary,
        "insights": insights,
    }


def write_quotes(path: Path, video: VideoCandidate, insights: list[dict[str, Any]]) -> None:
    lines = ["- [ ] read", "", f"# Quotes — {video.title}", ""]
    if not insights:
        lines.append("No timestamped quote candidates extracted.")
    for item in insights:
        lines.extend(
            [
                f"- **{item['timestamp']}**",
                f"  > \"{item['quote']}\"",
                f"  > — [{video.channel_title}, {video.title} @ {item['timestamp']}]({item['url']})",
                "",
            ]
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def strip_inline_markdown(text: str) -> str:
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return html.unescape(text).strip()


def clean_review_text(text: str) -> str:
    cleaned = html.unescape(strip_inline_markdown(text))
    cleaned = re.sub(r"(?m)^\s*>+\s*", "", cleaned)
    cleaned = re.sub(r"\s*>>\s*", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_summary_section(summary: str, heading: str) -> list[str]:
    lines = summary.splitlines()
    in_section = False
    section_lines: list[str] = []
    target = heading.casefold()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            current = stripped.lstrip("#").strip().casefold()
            if in_section and current != target:
                break
            in_section = current == target
            continue
        if in_section and stripped:
            section_lines.append(stripped)
    return section_lines


def concise_core_take(summary: str, max_chars: int | None = 180) -> str:
    lines = extract_summary_section(summary, "Core Take")
    using_fallback = False
    if not lines:
        using_fallback = True
        lines = [line.strip() for line in summary.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    text = strip_inline_markdown(" ".join(lines))
    if not text:
        return "No summary generated."
    if using_fallback and len(text) > 1200:
        return "No structured Core Take was generated for this artifact; open the linked summary for the full transcript-derived fallback."
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def concise_summary_section(summary: str, heading: str, max_chars: int | None = 360) -> str:
    lines = extract_summary_section(summary, heading)
    text = strip_inline_markdown(" ".join(lines))
    if not text:
        return ""
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def decision_lens_summary(summary: str, max_chars: int | None = 420) -> str:
    for heading in (
        "Investment Relevance",
        "Research Relevance",
        "Product Relevance",
        "Policy Relevance",
        "Learning Relevance",
    ):
        text = concise_summary_section(summary, heading, max_chars=max_chars)
        if text:
            return text
    return "No separate decision-lens section was generated for this artifact; use the judgment and evidence sections."


def concise_summary_bullets(summary: str, heading: str, max_items: int = 3, max_chars: int | None = 220) -> list[str]:
    bullets: list[str] = []
    for line in extract_summary_section(summary, heading):
        cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line).strip()
        if not cleaned:
            continue
        text = strip_inline_markdown(cleaned)
        if max_chars is not None and len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "..."
        bullets.append(text)
        if len(bullets) >= max_items:
            break
    return bullets


def quote_highlights(insights: list[dict[str, Any]], max_items: int = 2, max_chars: int | None = 220) -> list[dict[str, str]]:
    highlights: list[dict[str, str]] = []
    for insight in insights[:max_items]:
        text = clean_review_text(str(insight.get("quote") or insight.get("claim") or ""))
        if not text:
            continue
        if max_chars is not None and len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "..."
        highlights.append(
            {
                "timestamp": str(insight.get("timestamp") or ""),
                "text": text,
                "url": str(insight.get("url") or ""),
            }
        )
    return highlights


def compact_artifact_links(rel_dir: Path, transcript_file: str | None) -> str:
    return (
        f"[summary](../{rel_dir}/summary.md) · "
        f"[quotes](../{rel_dir}/quotes.md)"
    )


def one_line_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def feedback_path(db_dir: Path) -> Path:
    return db_dir / "review" / "feedback.jsonl"


def review_state_path(db_dir: Path, run_date: str) -> Path:
    return db_dir / "review" / f"{run_date}.json"


def review_html_path(db_dir: Path, run_date: str) -> Path:
    return db_dir / "review" / f"{run_date}.html"


def reviewed_dates_path(db_dir: Path) -> Path:
    return db_dir / "review" / "reviewed_dates.json"


def review_id(index: int) -> str:
    return f"W{index}"


PREFERENCE_ACTION_WEIGHTS = {
    "up": 2.0,
    "down": -2.0,
    "known": -1.5,
}

PREFERENCE_STOPWORDS = {
    "about",
    "after",
    "again",
    "already",
    "because",
    "being",
    "between",
    "could",
    "from",
    "have",
    "into",
    "like",
    "more",
    "that",
    "the",
    "their",
    "this",
    "through",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
    "your",
}


def preference_terms(*values: Any) -> set[str]:
    terms: set[str] = set()
    for value in values:
        if isinstance(value, list):
            terms.update(preference_terms(*value))
            continue
        text = str(value or "").casefold()
        for token in re.findall(r"[a-z0-9][a-z0-9_+-]{2,}", text):
            if token not in PREFERENCE_STOPWORDS:
                terms.add(token)
    return terms


def load_feedback_records(db_dir: Path) -> list[dict[str, Any]]:
    path = feedback_path(db_dir)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(record.get("action", "")).casefold() in PREFERENCE_ACTION_WEIGHTS:
            records.append(record)
    return records


def feedback_similarity(record: dict[str, Any], item: dict[str, Any]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    similarity = 0.0

    record_entities = {
        str(entity).casefold()
        for entity in [
            *(record.get("entities") or []),
            *(record.get("all_entities") or []),
            *(record.get("title_entities") or []),
        ]
    }
    item_entities = {
        str(entity).casefold()
        for entity in [
            *(item.get("entities") or []),
            *(item.get("all_entities") or []),
            *(item.get("title_entities") or []),
        ]
    }
    if record_entities and item_entities:
        overlap = record_entities & item_entities
        if overlap:
            similarity += min(0.7, 0.25 * len(overlap))
            reasons.append(f"entity:{','.join(sorted(overlap)[:3])}")

    record_terms = preference_terms(
        record.get("title"),
        record.get("claim"),
        record.get("core_take"),
        record.get("decision_lens"),
        record.get("reason_codes", []),
    )
    item_terms = preference_terms(
        item.get("title"),
        item.get("claim"),
        item.get("core_take"),
        item.get("decision_lens"),
        item.get("key_insights", []),
    )
    if record_terms and item_terms:
        overlap = record_terms & item_terms
        if overlap:
            jaccard = len(overlap) / len(record_terms | item_terms)
            if jaccard >= 0.08:
                similarity += min(0.75, jaccard * 3)
                reasons.append(f"terms:{','.join(sorted(overlap)[:4])}")

    if (
        record.get("channel")
        and item.get("channel")
        and str(record["channel"]).casefold() == str(item["channel"]).casefold()
    ):
        similarity += 0.15
        reasons.append("same_channel")

    return min(similarity, 1.0), reasons


def preference_adjustment(db_dir: Path, item: dict[str, Any]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    for record in load_feedback_records(db_dir):
        similarity, matched_reasons = feedback_similarity(record, item)
        if similarity <= 0:
            continue
        action = str(record.get("action", "")).casefold()
        weight = PREFERENCE_ACTION_WEIGHTS[action]
        contribution = weight * similarity
        score += contribution
        reason = f"{action}:{contribution:+.2f}"
        if matched_reasons:
            reason += f" ({'; '.join(matched_reasons[:2])})"
        reasons.append(reason)
    return round(score, 3), reasons[:5]


def build_review_items(
    db_dir: Path,
    processed_results: list[dict[str, Any]],
    max_items: int = 15,
) -> tuple[list[dict[str, Any]], int]:
    promotion_items = [
        (result, result["metadata"], result["insights"][0])
        for result in processed_results
        if result["insights"]
    ]
    review_items: list[dict[str, Any]] = []
    for result, metadata, insight in promotion_items:
        output_dir = result["output_dir"]
        artifact_dir = str(output_dir.relative_to(db_dir)) if output_dir.is_relative_to(db_dir) else str(output_dir)
        title_entities = mentioned_symbols(metadata.get("title_entity_mentions", {}))
        all_entities = insight.get("mentioned_entities", [])
        display_entities = title_entities or all_entities
        item = {
            "review_id": "",
            "video_id": metadata["video_id"],
            "title": metadata["title"],
            "channel": metadata["channel"],
            "channel_handle": metadata.get("channel_handle", ""),
            "source_url": metadata["source_url"],
            "duration_seconds": metadata.get("duration_seconds", 0),
            "artifact_dir": artifact_dir,
            "entities": display_entities,
            "title_entities": title_entities,
            "all_entities": all_entities,
            "core_take": concise_core_take(result.get("summary", ""), max_chars=None),
            "decision_lens": decision_lens_summary(result.get("summary", ""), max_chars=None),
            "key_insights": concise_summary_bullets(result.get("summary", ""), "Key Insights", max_items=4, max_chars=None),
            "quote_highlights": quote_highlights(result.get("insights", []), max_items=3, max_chars=None),
            "watch_worthiness": concise_summary_section(result.get("summary", ""), "Watch Worthiness", max_chars=None)
            or "No watchworthiness score was generated for this artifact; future summaries include one.",
            "claim": insight["claim"],
            "timestamp": insight.get("timestamp", ""),
            "timestamp_seconds": insight.get("timestamp_seconds"),
            "insight_url": insight.get("url", metadata["source_url"]),
            "base_signal_score": insight.get("score", 0),
        }
        preference_score, preference_reasons = preference_adjustment(db_dir, item)
        item["preference_score"] = preference_score
        item["preference_reasons"] = preference_reasons
        item["review_sort_score"] = round(float(item["base_signal_score"]) + preference_score, 3)
        review_items.append(item)
    review_items.sort(key=lambda item: item["review_sort_score"], reverse=True)
    review_items = review_items[:max_items]
    for idx, item in enumerate(review_items, start=1):
        item["review_id"] = review_id(idx)
        item["feedback_hint"] = f"{review_id(idx)} up | {review_id(idx)} down <reason> | {review_id(idx)} known | {review_id(idx)} promote"
    return review_items, max(0, len(promotion_items) - max_items)


def write_review_state(db_dir: Path, run_date: str, items: list[dict[str, Any]]) -> Path:
    path = review_state_path(db_dir, run_date)
    write_json(
        path,
        {
            "run_date": run_date,
            "generated_at": utc_now(),
            "items": items,
        },
    )
    return path


def parse_feedback_text(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    chunks = re.split(r"[\n;]+", text)
    for raw_chunk in chunks:
        chunk = raw_chunk.strip()
        if not chunk:
            continue
        tokens = chunk.split()
        if len(tokens) < 2:
            raise MonitorError(f"Feedback needs '<review_id> <action>': {chunk}")
        candidate_id = tokens[0].upper()
        if not re.fullmatch(r"W\d+", candidate_id):
            raise MonitorError(f"Invalid review id '{tokens[0]}'. Expected W1, W2, ...")
        action = tokens[1].casefold()
        if action not in FEEDBACK_ACTIONS:
            raise MonitorError(
                f"Invalid feedback action '{tokens[1]}'. Expected one of: {', '.join(sorted(FEEDBACK_ACTIONS))}"
            )
        records.append(
            {
                "review_id": candidate_id,
                "action": action,
                "reason_codes": tokens[2:],
                "raw_text": chunk,
            }
        )
    return records


def load_review_state(db_dir: Path, run_date: str) -> dict[str, Any]:
    path = review_state_path(db_dir, run_date)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"Missing review state: {path}. Rebuild the daily report first.") from exc
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Invalid review state JSON in {path}: {exc}") from exc


def append_feedback_records(db_dir: Path, run_date: str, records: list[dict[str, Any]]) -> int:
    state = load_review_state(db_dir, run_date)
    items_by_id = {item["review_id"]: item for item in state.get("items", [])}
    path = feedback_path(db_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            item = items_by_id.get(record["review_id"])
            if item is None:
                raise MonitorError(f"Review id {record['review_id']} was not found for {run_date}.")
            enriched = {
                "recorded_at": utc_now(),
                "run_date": run_date,
                **record,
                "video_id": item["video_id"],
                "title": item["title"],
                "channel": item["channel"],
                "source_url": item["source_url"],
                "artifact_dir": item["artifact_dir"],
                "entities": item.get("entities", []),
                "title_entities": item.get("title_entities", []),
                "all_entities": item.get("all_entities", []),
                "claim": item.get("claim", ""),
                "core_take": item.get("core_take", ""),
                "decision_lens": item.get("decision_lens", ""),
            }
            handle.write(json.dumps(enriched, sort_keys=True) + "\n")
    return len(records)


def apply_feedback_text(db_dir: Path, run_date: str, text: str) -> int:
    return append_feedback_records(db_dir, run_date, parse_feedback_text(text))


def review_dates(db_dir: Path) -> list[str]:
    review_dir = db_dir / "review"
    return sorted(
        path.stem
        for path in review_dir.glob("????-??-??.json")
        if path.is_file()
    )


def load_reviewed_dates(db_dir: Path) -> set[str]:
    path = reviewed_dates_path(db_dir)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Invalid reviewed dates JSON in {path}: {exc}") from exc
    return {str(item) for item in data.get("reviewed_dates", [])}


def mark_review_date_complete(db_dir: Path, run_date: str) -> None:
    reviewed_dates = load_reviewed_dates(db_dir)
    reviewed_dates.add(run_date)
    write_json(
        reviewed_dates_path(db_dir),
        {
            "updated_at": utc_now(),
            "reviewed_dates": sorted(reviewed_dates),
        },
    )


def feedback_counts_by_date(db_dir: Path) -> dict[str, int]:
    path = feedback_path(db_dir)
    feedback_ids: dict[str, set[str]] = {}
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        run_date = str(record.get("run_date") or "")
        review_id_value = str(record.get("review_id") or "")
        if run_date and review_id_value:
            feedback_ids.setdefault(run_date, set()).add(review_id_value)
    return {run_date: len(ids) for run_date, ids in feedback_ids.items()}


def unreviewed_date_summaries(db_dir: Path) -> list[dict[str, Any]]:
    completed_dates = load_reviewed_dates(db_dir)
    feedback_counts = feedback_counts_by_date(db_dir)
    summaries: list[dict[str, Any]] = []
    for run_date in reversed(review_dates(db_dir)):
        if run_date in completed_dates:
            continue
        state = load_review_state(db_dir, run_date)
        item_count = len(state.get("items", []))
        summaries.append(
            {
                "run_date": run_date,
                "item_count": item_count,
                "feedback_count": min(feedback_counts.get(run_date, 0), item_count),
                "generated_at": state.get("generated_at", ""),
            }
        )
    return summaries


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def render_review_dashboard_html(date_summaries: list[dict[str, Any]], base_url: str) -> str:
    if date_summaries:
        rows = "\n".join(
            f"""
<article class="date-card">
  <div>
    <h2>{html.escape(item['run_date'])}</h2>
    <p>{html.escape(str(item['feedback_count']))}/{html.escape(str(item['item_count']))} items have feedback · generated {html.escape(str(item.get('generated_at') or 'unknown'))}</p>
  </div>
  <a class="open-date" href="/date/{html.escape(item['run_date'])}">Review date</a>
</article>
"""
            for item in date_summaries
        )
    else:
        rows = "<p class=\"empty\">No unreviewed dates. Completed dates are hidden from this list.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>YouTube Review Queue</title>
  <style>
    body {{ background: #111; color: #f4f4f4; font: 18px/1.55 -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 32px; }}
    main {{ max-width: 960px; margin: 0 auto; }}
    .date-card {{ align-items: center; background: #1e1e1e; border: 1px solid #333; border-radius: 20px; display: flex; justify-content: space-between; margin: 18px 0; padding: 24px; gap: 24px; }}
    h1 {{ margin-bottom: 4px; }}
    h2 {{ margin: 0 0 4px; }}
    p {{ color: #bbb; margin: 0; }}
    a {{ color: #8ab4ff; }}
    .open-date {{ background: #2a2a2a; border: 1px solid #444; border-radius: 999px; color: #fff; padding: 10px 14px; text-decoration: none; white-space: nowrap; }}
    .empty {{ background: #1e1e1e; border: 1px solid #333; border-radius: 20px; padding: 24px; }}
  </style>
</head>
<body>
<main>
  <h1>YouTube Review Queue</h1>
  <p>Local app: {html.escape(base_url)}. Pick an unreviewed date; completed dates are hidden.</p>
  {rows}
</main>
</body>
</html>
"""


def render_review_html(
    run_date: str,
    items: list[dict[str, Any]],
    base_url: str | None = None,
    feedback_endpoint: str = "/feedback",
    complete_endpoint: str | None = None,
    dashboard_url: str | None = None,
) -> str:
    app_url = base_url or f"http://{DEFAULT_REVIEW_HOST}:{DEFAULT_REVIEW_PORT}/"
    completion_html = ""
    if complete_endpoint:
        completion_html = f"""
  <section class="complete-review">
    <h2>Done with {html.escape(run_date)}?</h2>
    <p>Submit the date review to hide this date from the unreviewed dashboard. Individual button feedback is already saved as you click.</p>
    <button id="complete-review" data-complete-endpoint="{html.escape(complete_endpoint)}">Submit date review</button>
    <span id="complete-status"></span>
  </section>
"""
    dashboard_link = f'<p><a href="{html.escape(dashboard_url)}">Back to unreviewed dates</a></p>' if dashboard_url else ""
    item_cards = []
    for item in items:
        entities = ", ".join(item.get("entities", [])) or "No entity detected"
        core_take = item.get("core_take") or "No core take available."
        relevance = item.get("decision_lens") or item.get("investment_relevance") or "No relevance assessment available."
        watch_worthiness = item.get("watch_worthiness") or "No watchworthiness assessment available."
        preference_score = float(item.get("preference_score") or 0)
        preference_badge = "Preference: neutral"
        if preference_score >= 0.5:
            preference_badge = f"Preference: boosted {preference_score:+.1f}"
        elif preference_score <= -0.5:
            preference_badge = f"Preference: downranked {preference_score:+.1f}"
        key_insights = item.get("key_insights") or []
        quote_items = item.get("quote_highlights") or []
        key_insights_html = "\n".join(f"<li>{html.escape(str(insight))}</li>" for insight in key_insights) or "<li>No extracted key insights available.</li>"
        quotes_html = "\n".join(
            f"<blockquote><p>\"{html.escape(str(quote.get('text', '')))}\"</p>"
            f"<footer><a href=\"{html.escape(str(quote.get('url', item['insight_url'])))}\">"
            f"Open @ {html.escape(str(quote.get('timestamp') or 'source'))}</a></footer></blockquote>"
            for quote in quote_items
        ) or "<p>No quote highlights available.</p>"
        item_cards.append(
            f"""
<article class="card" data-review-id="{html.escape(item['review_id'])}">
  <div class="meta">{html.escape(item['review_id'])} · {html.escape(item['channel'])} · {html.escape(entities)} · {html.escape(preference_badge)}</div>
  <h2>{html.escape(item['title'])}</h2>
  <section>
    <h3>Summary Judgment</h3>
    <p>{html.escape(core_take)}</p>
  </section>
  <div class="detail-grid">
    <section class="panel panel-opinion">
      <h3>Highlighted Opinion</h3>
      <p>{html.escape(relevance)}</p>
    </section>
    <section class="panel">
      <h3>Key Insights</h3>
      <ul>{key_insights_html}</ul>
    </section>
    <section class="panel">
      <h3>Key Quotes</h3>
      {quotes_html}
    </section>
    <section class="panel">
      <h3>Watchworthiness</h3>
      <p>{html.escape(watch_worthiness)}</p>
    </section>
  </div>
  <section class="evidence">
    <h3>Primary Evidence</h3>
    <p>{html.escape(item['claim'])}</p>
  </section>
  <p><a href="{html.escape(item['insight_url'])}">Open source @ {html.escape(item.get('timestamp', 'start'))}</a></p>
  <input aria-label="reason" placeholder="optional reason, e.g. indexing_saturated" />
  <div class="actions">
    <button data-action="up" title="Preference signal: rank similar videos higher in future briefs.">👍 More like this</button>
    <button data-action="down" title="Preference signal: rank similar videos lower in future briefs.">👎 Less like this</button>
    <button data-action="known" title="Preference signal: useful topic, but already saturated for you.">💤 Already know this</button>
    <button data-action="promote" title="Workflow action: move this item into manual research/follow-up.">🎯 Promote</button>
    <button class="danger" data-channel-handle="{html.escape(item.get('channel_handle') or item.get('channel') or '')}" data-channel-name="{html.escape(item.get('channel') or '')}" title="Edit channels.json so this channel is skipped in future discovery.">🚫 Blacklist channel</button>
  </div>
  <div class="status"></div>
</article>
"""
        )
    cards = "\n".join(item_cards) or "<p>No review items for this date.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>YouTube Review — {html.escape(run_date)}</title>
  <style>
    body {{ background: #111; color: #f4f4f4; font: 18px/1.55 -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 32px; }}
    main {{ max-width: 1180px; margin: 0 auto; }}
    .card {{ background: #1e1e1e; border: 1px solid #333; border-radius: 24px; margin: 28px 0; padding: 36px; }}
    .meta {{ color: #aaa; font-size: 14px; }}
    h1 {{ margin-bottom: 4px; }}
    h2 {{ font-size: 32px; line-height: 1.2; margin: 12px 0 24px; }}
    h3 {{ color: #bbb; font-size: 13px; letter-spacing: .08em; margin: 18px 0 4px; text-transform: uppercase; }}
    section p {{ margin: 0; }}
    .detail-grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); margin: 22px 0; }}
    .panel {{ background: #171717; border: 1px solid #333; border-radius: 16px; padding: 16px; }}
    .panel-opinion {{ border-color: #4b5f8f; background: #171b26; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ margin: 6px 0; }}
    blockquote {{ border-left: 3px solid #7da7ff; color: #e8eefc; margin: 8px 0; padding-left: 12px; }}
    blockquote footer {{ color: #aaa; font-size: 14px; margin-top: 4px; }}
    .evidence {{ color: #ddd; }}
    a {{ color: #8ab4ff; }}
    input {{ box-sizing: border-box; width: 100%; padding: 10px; margin: 8px 0 12px; border-radius: 10px; border: 1px solid #555; background: #111; color: #fff; }}
    button {{ margin: 4px 8px 4px 0; padding: 10px 12px; border-radius: 999px; border: 1px solid #444; background: #2a2a2a; color: #fff; cursor: pointer; }}
    button:hover {{ background: #3a3a3a; }}
    button.danger {{ border-color: #744; color: #ffd8d8; }}
    .status {{ color: #9be28f; min-height: 20px; margin-top: 8px; }}
  </style>
</head>
<body>
<main>
  <h1>YouTube Review — {html.escape(run_date)}</h1>
  <p><strong>Persistence:</strong> buttons save only when this page is opened from the local review server at <code>{html.escape(app_url)}</code>. A <code>file://</code> tab is a static preview.</p>
  <p>Start the app with <code>scripts/youtube-monitor/run.sh --date {html.escape(run_date)} --serve-review</code>. Chat fallback works with commands like <code>W1 down indexing_saturated</code>.</p>
  <p><strong>Actions:</strong> <em>More/Less/Known</em> are ranking-preference signals for future briefs. <em>Promote</em> is an explicit workflow action: this deserves manual research follow-up, not merely more similar videos.</p>
  {dashboard_link}
  {cards}
  {completion_html}
</main>
<script>
document.addEventListener("click", async (event) => {{
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const card = button.closest(".card");
  const reason = card.querySelector("input").value.trim();
  const response = await fetch("{feedback_endpoint}", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{
      review_id: card.dataset.reviewId,
      action: button.dataset.action,
      reason_codes: reason ? reason.split(/\\s+/) : []
    }})
  }});
  const status = card.querySelector(".status");
  status.textContent = response.ok ? "Saved" : await response.text();
}});
document.addEventListener("click", async (event) => {{
  const button = event.target.closest("button[data-channel-handle]");
  if (!button) return;
  const channelName = button.dataset.channelName || button.dataset.channelHandle;
  if (!window.confirm(`Blacklist channel "${{channelName}}" for future runs?`)) return;
  const response = await fetch("/blacklist-channel", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ channel_handle: button.dataset.channelHandle }})
  }});
  const card = button.closest(".card");
  const status = card.querySelector(".status");
  status.textContent = response.ok ? "Channel added to blacklist" : await response.text();
}});
document.getElementById("complete-review")?.addEventListener("click", async (event) => {{
  const endpoint = event.target.dataset.completeEndpoint;
  const response = await fetch(endpoint, {{ method: "POST" }});
  const status = document.getElementById("complete-status");
  if (response.ok) {{
    status.textContent = "Marked reviewed";
    window.location.href = "{dashboard_url or '/'}";
  }} else {{
    status.textContent = await response.text();
  }}
}});
</script>
</body>
</html>
"""


def write_review_html(db_dir: Path, run_date: str, items: list[dict[str, Any]]) -> Path:
    path = review_html_path(db_dir, run_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_review_html(run_date, items), encoding="utf-8")
    return path


def make_review_handler(db_dir: Path, fixed_run_date: str | None, base_url: str, config_path: Path | None = None) -> type[BaseHTTPRequestHandler]:
    class ReviewHandler(BaseHTTPRequestHandler):
        def send_bytes(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def render_date_page(self, run_date: str) -> bytes:
            state = load_review_state(db_dir, run_date)
            return render_review_html(
                run_date,
                state.get("items", []),
                base_url=base_url,
                feedback_endpoint=f"/date/{run_date}/feedback",
                complete_endpoint=f"/date/{run_date}/complete",
                dashboard_url="/",
            ).encode("utf-8")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                if fixed_run_date:
                    self.send_bytes(200, self.render_date_page(fixed_run_date))
                else:
                    body = render_review_dashboard_html(unreviewed_date_summaries(db_dir), base_url).encode("utf-8")
                    self.send_bytes(200, body)
                return
            date_match = re.fullmatch(r"/date/(\d{4}-\d{2}-\d{2})", path)
            if date_match:
                self.send_bytes(200, self.render_date_page(date_match.group(1)))
                return
            state_match = re.fullmatch(r"/date/(\d{4}-\d{2}-\d{2})/state", path)
            if state_match:
                state = load_review_state(db_dir, state_match.group(1))
                payload = json.dumps(state, indent=2).encode("utf-8")
                self.send_bytes(200, payload, "application/json; charset=utf-8")
                return
            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            feedback_match = re.fullmatch(r"/date/(\d{4}-\d{2}-\d{2})/feedback", path)
            complete_match = re.fullmatch(r"/date/(\d{4}-\d{2}-\d{2})/complete", path)
            try:
                if path == "/blacklist-channel":
                    if config_path is None:
                        raise MonitorError("No channel config path was provided.")
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    added = add_channel_to_blacklist(config_path, str(payload.get("channel_handle", "")))
                    body = json.dumps({"blacklisted": added}).encode("utf-8")
                    self.send_bytes(200, body, "application/json; charset=utf-8")
                    return
                if feedback_match:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    record = {
                        "review_id": str(payload.get("review_id", "")).upper(),
                        "action": str(payload.get("action", "")).casefold(),
                        "reason_codes": [str(item) for item in payload.get("reason_codes", [])],
                        "raw_text": "review-ui",
                    }
                    if record["action"] not in FEEDBACK_ACTIONS or not re.fullmatch(r"W\d+", record["review_id"]):
                        raise MonitorError("Invalid feedback payload.")
                    append_feedback_records(db_dir, feedback_match.group(1), [record])
                    self.send_response(204)
                    self.end_headers()
                    return
                if complete_match:
                    mark_review_date_complete(db_dir, complete_match.group(1))
                    self.send_response(204)
                    self.end_headers()
                    return
            except Exception as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(exc).encode("utf-8"))
                return
            self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            log_progress(format % args)

    return ReviewHandler


def serve_review(
    db_dir: Path,
    run_date: str | None,
    host: str,
    port: int,
    open_browser: bool = False,
    config_path: Path | None = None,
) -> None:
    server = ReusableHTTPServer((host, port), BaseHTTPRequestHandler)
    actual_host, actual_port = server.server_address
    base_url = f"http://{actual_host}:{actual_port}/"
    open_url = f"{base_url}date/{run_date}" if run_date else base_url
    server.RequestHandlerClass = make_review_handler(db_dir, run_date, base_url, config_path=config_path)
    log_progress(f"review server {open_url}")
    if open_browser:
        webbrowser.open(open_url)
    server.serve_forever()


def write_daily_report(
    db_dir: Path,
    run_date: str,
    processed_results: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    dry_run: bool,
) -> Path:
    path = db_dir / "daily" / f"{run_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    review_items, remaining_review_items = build_review_items(db_dir, processed_results)
    state_path = write_review_state(db_dir, run_date, review_items)
    html_path = write_review_html(db_dir, run_date, review_items)
    lines = [
        "- [ ] read",
        "",
        f"# YouTube Intelligence Brief — {run_date}",
        "",
        f"**Generated:** {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"**Mode:** {'dry run' if dry_run else 'daily run'}",
        "",
        "## Executive Read",
        "",
        f"- Videos processed: {len(processed_results)}",
        f"- Videos skipped: {len(skipped)}",
        f"- Videos failed: {len(failures)}",
        f"- Source-backed quote candidates: {sum(len(item['insights']) for item in processed_results)}",
        f"- Review app: double-click `Review YouTube.command` or run `python3 scripts/youtube-monitor/review_app.py`; it opens the unreviewed-date dashboard. For this date only: `python3 scripts/youtube-monitor/review_app.py {run_date}`.",
        f"- CLI fallback: `scripts/youtube-monitor/run.sh --date {run_date} --feedback \"W1 up; W2 down <reason>\"`",
        f"- Static review preview: `{html_path.relative_to(db_dir)}`",
        f"- Review state: `{state_path.relative_to(db_dir)}`",
        "",
        "Use the video index for triage; save feedback with the review app or reply with compact commands such as `W1 up`, `W2 down indexing_saturated`, `W3 known`, or `W4 promote`.",
        "",
    ]

    if processed_results:
        lines.extend(
            [
                "## Video Index",
                "",
                "| Channel | Video | Matches | Take | Dive |",
                "|---|---|---:|---|---|",
            ]
        )
    for result in processed_results:
        metadata = result["metadata"]
        output_dir = result["output_dir"]
        rel_dir = output_dir.relative_to(db_dir)
        entities = mentioned_symbols(metadata.get("entity_mentions", {}))
        title_link = f"[{one_line_table_cell(metadata['title'])}]({metadata['source_url']})"
        lines.append(
            "| "
            f"{one_line_table_cell(metadata['channel'])} | "
            f"{title_link} | "
            f"{one_line_table_cell(', '.join(entities) if entities else '-')} | "
            f"{one_line_table_cell(concise_core_take(result['summary']))} | "
            f"{compact_artifact_links(rel_dir, metadata.get('transcript_file'))} |"
        )
    if processed_results:
        lines.append("")

    lines.extend(["## Review Queue", ""])
    if review_items:
        for item in review_items:
            entities = ", ".join(item.get("entities", [])) or "no entity detected"
            lines.extend(
                [
                    f"- **{item['review_id']}** · **{entities}** — {item['claim']}",
                    f"  Source: [{item['channel']} @ {item['timestamp']}]({item['insight_url']})",
                    f"  Feedback: `{item['feedback_hint']}`",
                ]
            )
        if remaining_review_items:
            lines.append(f"{remaining_review_items} more videos have quote candidates in their linked `quotes.md` files.")
    else:
        lines.append("No quote candidates today.")
    lines.append("")

    if skipped:
        lines.extend(["## Skipped", ""])
        for item in skipped[:40]:
            channel = item.get("channel") or item.get("channel_title") or "unknown"
            title = item.get("title") or item.get("video_title") or item.get("video_id") or "unknown"
            reason = item.get("reason") or item.get("error") or "unknown"
            lines.append(f"- {channel}: {title} — {reason}")
        if len(skipped) > 40:
            lines.append(f"- ...and {len(skipped) - 40} more.")
        lines.append("")

    if failures:
        lines.extend(["## Failures", ""])
        for item in failures:
            lines.append(f"- {item.get('channel_title', item.get('channel', 'unknown'))}: {item.get('title', item.get('video_id', 'unknown'))} — {item.get('error', 'unknown error')}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def ensure_layout(db_dir: Path) -> None:
    for child in ("config", "daily", "indexes", "videos", "review", "runs"):
        (db_dir / child).mkdir(parents=True, exist_ok=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the daily YouTube knowledge monitor.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db-dir", type=Path, default=DEFAULT_DB_DIR)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Research repo root used for stock alias discovery and summarize cwd.")
    parser.add_argument("--env-file", type=Path, default=None, help="Optional .env file. Defaults to <repo-root>/scripts/.env.")
    parser.add_argument("--aliases-file", type=Path, default=None, help="Optional JSON aliases file for entity matching. Defaults to <db-dir>/config/aliases.json when present.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lookback-count", type=int, default=3)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--force", action="store_true", help="Ignore a completed run manifest for this date.")
    parser.add_argument("--refresh-report", action="store_true", help="Rebuild the daily Markdown brief from the run manifest without reprocessing videos.")
    parser.add_argument("--refresh-quotes", action="store_true", help="Rebuild insights.json and quotes.md for all stored video artifacts under youtube-db/videos/.")
    parser.add_argument("--feedback", default=None, help="Append chat-style feedback, e.g. 'W1 down indexing_saturated; W3 promote'.")
    parser.add_argument("--feedback-file", type=Path, default=None, help="Append feedback commands from a text file.")
    parser.add_argument("--serve-review", action="store_true", help="Serve the local clickable review UI for --date.")
    parser.add_argument("--review-host", default=DEFAULT_REVIEW_HOST)
    parser.add_argument("--review-port", type=int, default=DEFAULT_REVIEW_PORT)
    parser.add_argument("--allow-transcript-fallback", action="store_true", default=None)
    parser.add_argument("--no-transcript-fallback", action="store_false", dest="allow_transcript_fallback")
    return parser.parse_args(argv)


def process_video_worker(
    video: VideoCandidate,
    db_dir: Path,
    run_date: str,
    prompt_path: Path,
    allow_fallback: bool,
    stock_aliases: dict[str, list[str]],
    repo_root: Path,
    index_path: Path,
    run_manifest_path: Path,
    manifest: dict[str, Any],
    index_lock: Lock,
    manifest_lock: Lock,
    dry_run: bool,
) -> tuple[str, dict[str, Any]]:
    def set_stage(stage: str) -> None:
        if dry_run:
            return
        with manifest_lock:
            update_manifest_video(
                run_manifest_path,
                manifest,
                video.video_id,
                {"stage": stage, "stage_updated_at": utc_now()},
            )

    with manifest_lock:
        if not dry_run:
            update_manifest_video(
                run_manifest_path,
                manifest,
                video.video_id,
                {"status": "processing", "stage": "starting", "started_at": utc_now()},
            )
    log_progress(f"video start {video.video_id} {video.channel_handle}: {video.title}")
    try:
        result = process_video(
            video=video,
            db_dir=db_dir,
            run_date=run_date,
            prompt_path=prompt_path,
            allow_transcript_fallback=allow_fallback,
            stock_aliases=stock_aliases,
            repo_root=repo_root,
            stage_callback=set_stage,
        )
        record = {
            **result["metadata"],
            "status": "processed",
            "run_date": run_date,
            "artifact_dir": str(result["output_dir"].relative_to(db_dir)),
        }
        if not dry_run:
            with index_lock:
                append_index_record(index_path, record)
            with manifest_lock:
                update_manifest_video(
                    run_manifest_path,
                    manifest,
                    video.video_id,
                    {
                        "status": "processed",
                        "stage": "done",
                        "finished_at": utc_now(),
                        "artifact_dir": record["artifact_dir"],
                        "transcript_status": result["metadata"].get("transcript_status"),
                        "insight_count": result["metadata"].get("insight_count", 0),
                    },
                )
        log_progress(
            "video done "
            f"{video.video_id} {video.channel_handle}: "
            f"transcript={result['metadata'].get('transcript_status')} "
            f"insights={result['metadata'].get('insight_count', 0)}"
        )
        return "processed", result
    except Exception as exc:
        failure = {
            "video_id": video.video_id,
            "title": video.title,
            "channel_title": video.channel_title,
            "channel_handle": video.channel_handle,
            "url": video.url,
            "error": str(exc),
        }
        if not dry_run:
            with manifest_lock:
                update_manifest_video(
                    run_manifest_path,
                    manifest,
                    video.video_id,
                    {"status": "failed", "stage": "failed", "finished_at": utc_now(), "error": str(exc)},
                )
        log_progress(f"video failed {video.video_id} {video.channel_handle}: {exc}")
        return "failed", failure


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repo_root = args.repo_root.resolve()
    load_env(args.env_file or repo_root / "scripts" / ".env")
    ensure_layout(args.db_dir)
    aliases_file = args.aliases_file or args.db_dir / "config" / "aliases.json"
    stock_aliases = merge_aliases(COMMON_ENTITY_ALIASES, load_stock_aliases(repo_root), load_aliases_file(aliases_file))

    if args.feedback or args.feedback_file:
        feedback_text = args.feedback or ""
        if args.feedback_file:
            feedback_text = "\n".join([feedback_text, args.feedback_file.read_text(encoding="utf-8")]).strip()
        count = apply_feedback_text(args.db_dir, args.date, feedback_text)
        log_progress(f"feedback saved count={count} path={feedback_path(args.db_dir)}")
        return 0

    if args.serve_review:
        serve_review(args.db_dir, args.date, args.review_host, args.review_port, config_path=args.config)
        return 0

    if args.refresh_quotes:
        summary = refresh_all_quotes(args.db_dir, stock_aliases)
        log_progress(
            "quotes refresh done "
            f"refreshed={summary['refreshed']} skipped={summary['skipped']} failed={summary['failed']}"
        )
        return 0

    config = load_config(args.config)
    channels = channel_configs(config)

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise MonitorError("YOUTUBE_API_KEY is required. Add it to scripts/.env or the environment.")

    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        raise MonitorError("yt-dlp is required for VTT capture. Install: brew install yt-dlp")

    allow_fallback = (
        args.allow_transcript_fallback
        if args.allow_transcript_fallback is not None
        else True
    )

    index_path = args.db_dir / "indexes" / "videos.jsonl"
    processed = load_processed(index_path)
    client = YouTubeClient(api_key)
    run_manifest_path = manifest_path(args.db_dir, args.date)
    skipped: list[dict[str, Any]] = []
    manifest: dict[str, Any] | None = None

    log_progress(
        f"run start date={args.date} channels={len(channels)} "
        f"lookback={args.lookback_count} workers={max(1, args.workers)} dry_run={args.dry_run}"
    )

    def discovery_progress(event: dict[str, Any]) -> None:
        nonlocal manifest
        name = event.get("event")
        if name == "discovery_start":
            log_progress(f"discovery start channels={event['total_channels']}")
        elif name == "channel_start":
            log_progress(
                f"discovery channel {event['index']}/{event['total_channels']} "
                f"start {event['handle']}"
            )
        elif name == "channel_done":
            log_progress(
                f"discovery channel {event['index']}/{event['total_channels']} "
                f"done {event['handle']} new={event['new_count']} "
                f"already_processed={event['skipped_count']} queued_total={event['candidate_count']}"
            )
        elif name == "discovery_done":
            log_progress(
                f"discovery done queued={event['candidate_count']} "
                f"already_processed={event['skipped_processed_count']}"
            )

        if args.dry_run or manifest is None:
            return
        discovery = manifest.setdefault("discovery", {})
        if name == "discovery_start":
            discovery.update(
                {
                    "status": "running",
                    "total_channels": event["total_channels"],
                    "completed_channels": 0,
                    "candidate_count": 0,
                    "skipped_processed_count": 0,
                    "channels": [],
                }
            )
            update_manifest(run_manifest_path, manifest, {"status": "discovering", "discovery": discovery})
        elif name == "channel_start":
            discovery.setdefault("channels", []).append(
                {
                    "handle": event["handle"],
                    "status": "running",
                    "started_at": utc_now(),
                }
            )
            update_manifest(run_manifest_path, manifest, {"discovery": discovery})
        elif name == "channel_done":
            for channel_item in discovery.setdefault("channels", []):
                if channel_item.get("handle") == event["handle"] and channel_item.get("status") == "running":
                    channel_item.update(
                        {
                            "status": "completed",
                            "finished_at": utc_now(),
                            "channel_title": event["channel_title"],
                            "new_count": event["new_count"],
                            "skipped_processed_count": event["skipped_count"],
                        }
                    )
                    break
            discovery.update(
                {
                    "completed_channels": event["index"],
                    "candidate_count": event["candidate_count"],
                    "skipped_processed_count": event["skipped_processed_count"],
                }
            )
            update_manifest(run_manifest_path, manifest, {"discovery": discovery})
        elif name == "discovery_done":
            discovery.update(
                {
                    "status": "completed",
                    "candidate_count": event["candidate_count"],
                    "skipped_processed_count": event["skipped_processed_count"],
                }
            )
            update_manifest(run_manifest_path, manifest, {"discovery": discovery})

    if args.dry_run:
        candidates, skipped = discover_candidates(
            client=client,
            channels=channels,
            processed=processed,
            lookback_count=args.lookback_count,
            progress=discovery_progress,
        )
        log_progress(f"dry run completed queued={len(candidates)} skipped={len(skipped)}")
        return 0
    else:
        existing = load_manifest(run_manifest_path)
        if existing and existing.get("status") == "completed" and not args.force:
            manifest = existing
            candidates = []
            should_run = False
        elif existing and existing.get("status") != "completed" and not args.force:
            manifest, should_run = prepare_manifest(run_manifest_path, args.date, [], force=False)
            candidates = [
                candidate_from_manifest_item(item)
                for item in manifest.get("videos", [])
                if item.get("status") != "processed"
            ]
            log_progress(f"resume run pending_or_failed={len(candidates)}")
        else:
            manifest = create_manifest(args.date, [])
            write_json(run_manifest_path, manifest)
            candidates, skipped = discover_candidates(
                client=client,
                channels=channels,
                processed=processed,
                lookback_count=args.lookback_count,
                progress=discovery_progress,
            )
            manifest["videos"] = [candidate_to_manifest_item(video) for video in candidates]
            manifest.setdefault("discovery", {}).update(
                {
                    "status": "completed",
                    "candidate_count": len(candidates),
                    "skipped_processed_count": len(skipped),
                }
            )
            update_manifest(run_manifest_path, manifest, {"status": "processing"})
            should_run = True

    if not should_run:
        daily_path = args.db_dir / "daily" / f"{args.date}.md"
        if args.refresh_report:
            refreshed_results, artifact_skips = load_results_from_artifacts(args.db_dir, args.date, stock_aliases)
            daily_path = write_daily_report(
                args.db_dir,
                args.date,
                refreshed_results,
                skipped=artifact_skips,
                failures=failures_from_manifest(manifest),
                dry_run=False,
            )
            log_progress(f"daily report refreshed {daily_path}")
            return 0
        log_progress(f"run already completed date={args.date}; use --force to override")
        log_progress(f"daily report {daily_path}")
        return 0

    if not args.dry_run:
        in_progress_candidates = [
            candidate_from_manifest_item(item)
            for item in manifest.get("videos", [])
            if item.get("status") != "processed"
        ]
        if in_progress_candidates:
            candidates = in_progress_candidates

    prompt_path = SCRIPT_DIR / "prompts" / "summary.md"
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    if not args.dry_run:
        for item in manifest.get("videos", []):
            if item.get("status") == "processed" and item.get("artifact_dir"):
                prior_result = load_result_from_artifact(args.db_dir, str(item["artifact_dir"]), stock_aliases)
                if prior_result:
                    results.append(prior_result)

    max_workers = max(1, args.workers)
    log_progress(
        f"processing start queued={len(candidates)} already_loaded={len(results)} "
        f"workers={max_workers}"
    )
    result_by_video_id = {result["metadata"]["video_id"]: result for result in results}
    index_lock = Lock()
    manifest_lock = Lock()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                process_video_worker,
                video,
                args.db_dir,
                args.date,
                prompt_path,
                allow_fallback,
                stock_aliases,
                repo_root,
                index_path,
                run_manifest_path,
                manifest,
                index_lock,
                manifest_lock,
                args.dry_run,
            ): video
            for video in candidates
        }
        for future in as_completed(future_map):
            status, payload = future.result()
            if status == "processed":
                result_by_video_id[payload["metadata"]["video_id"]] = payload
            else:
                failures.append(payload)
            completed_count = len(result_by_video_id) + len(failures)
            total_count = len(candidates) + len(results)
            log_progress(
                f"processing progress completed={completed_count}/{total_count} "
                f"processed={len(result_by_video_id)} failed={len(failures)}"
            )

    ordered_video_ids = [item.get("video_id") for item in manifest.get("videos", [])]
    results = [result_by_video_id[video_id] for video_id in ordered_video_ids if video_id in result_by_video_id]

    if not args.dry_run:
        with manifest_lock:
            finalize_manifest(run_manifest_path, manifest)

    daily_path = write_daily_report(args.db_dir, args.date, results, skipped, failures, args.dry_run)
    log_progress(f"run completed processed={len(results)} skipped={len(skipped)} failed={len(failures)}")
    log_progress(f"daily report {daily_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MonitorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
