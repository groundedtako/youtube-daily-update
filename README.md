# YouTube Daily Update

Codex skill and CLI routine for building a daily YouTube intelligence brief from a curated channel list.

It discovers recent uploads with the YouTube Data API, fetches timestamped captions with `yt-dlp`, summarizes transcripts with `summarize`, extracts ticker/entity matches from optional `Stocks/*/meta.json` files, and writes local Markdown artifacts under `youtube-db/`.

## What It Produces

- `youtube-db/daily/YYYY-MM-DD.md`: daily brief with processed, skipped, failed, review queue, and source links.
- `youtube-db/videos/.../summary.md`: per-video grounded summary.
- `youtube-db/videos/.../quotes.md`: timestamped quote candidates.
- `youtube-db/videos/.../transcript.clean.md`: cleaned timestamped transcript.
- `youtube-db/runs/YYYY-MM-DD.json`: resumable run manifest.
- `youtube-db/indexes/videos.jsonl`: processed-video index for dedupe.

The routine does not write into `Stocks/`. Stock metadata is read only for alias matching.

## Dependencies

- Python 3.11+
- Python package: `requests`
- CLI tools on `PATH`: `yt-dlp`, `summarize`
- Environment: `YOUTUBE_API_KEY`
- Optional: a research repo with `Stocks/*/meta.json` for ticker aliases

Install Python dependencies:

```bash
python3 -m pip install -r scripts/youtube-monitor/requirements.txt
```

Install CLI dependencies:

```bash
brew install yt-dlp
```

Install `summarize` from <https://github.com/steipete/summarize>.

## Setup

Create an env file:

```bash
cp scripts/.env.example scripts/.env
```

Then set:

```bash
YOUTUBE_API_KEY=...
```

Create a channel config:

```bash
cp youtube-db/config/channels.example.json youtube-db/config/channels.json
```

Edit `youtube-db/config/channels.json` to include the channels you want.

## Run

Default daily run:

```bash
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5
```

Force a rerun for the same date:

```bash
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5 --force
```

Refresh the daily brief from existing artifacts:

```bash
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5 --refresh-report
```

Refresh quotes and insight extraction from existing artifacts:

```bash
scripts/youtube-monitor/run.sh --refresh-quotes
```

Discovery-only dry run:

```bash
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5 --dry-run
```

When the skill is installed outside the research repo, pass explicit paths:

```bash
scripts/youtube-monitor/run.sh \
  --repo-root /path/to/research-repo \
  --db-dir /path/to/research-repo/youtube-db \
  --config /path/to/research-repo/youtube-db/config/channels.json \
  --env-file /path/to/research-repo/scripts/.env
```

## Behavior

- Skips videos shorter than 3 minutes.
- Normal runs do not rerun a completed day.
- Interrupted runs resume pending or failed manifest items.
- Quote extraction groups adjacent summary timestamps into coherent quote blocks.
- Failed transcript or summary attempts are recorded in the daily brief.

## Codex Skill

This repository can be installed as a Codex skill. The skill entrypoint is `SKILL.md`; the executable routine lives under `scripts/youtube-monitor/`.

