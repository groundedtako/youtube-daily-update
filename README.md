# YouTube Daily Update

Codex skill and CLI routine for building a daily YouTube intelligence brief from a curated channel list.

The default prompt and reporting language are investing-focused because this was
originally built for an investment research workflow. The monitor itself is
general-purpose: it can summarize any channel list, extract timestamped quotes,
track entities or topics, and write local Markdown artifacts under `youtube-db/`.

It discovers recent uploads with the YouTube Data API, fetches timestamped
captions with `yt-dlp`, summarizes transcripts with `summarize`, extracts
entity matches from an optional aliases file and optional `Stocks/*/meta.json`
files, and writes local Markdown artifacts under `youtube-db/`.

## What It Produces

- `youtube-db/daily/YYYY-MM-DD.md`: daily brief with processed, skipped, failed, review queue, and source links.
- `youtube-db/videos/.../summary.md`: per-video grounded summary.
- `youtube-db/videos/.../quotes.md`: timestamped quote candidates.
- `youtube-db/videos/.../transcript.clean.md`: cleaned timestamped transcript.
- `youtube-db/runs/YYYY-MM-DD.json`: resumable run manifest.
- `youtube-db/indexes/videos.jsonl`: processed-video index for dedupe.
- `youtube-db/review/YYYY-MM-DD.json`: stable review IDs for feedback.
- `youtube-db/review/YYYY-MM-DD.html`: optional local click-review page.
- `youtube-db/review/feedback.jsonl`: appended watchworthiness feedback.

The routine does not write into `Stocks/`. Stock metadata is read only for alias
matching when that folder exists.

## Dependencies

- Python 3.11+
- Python package: `requests`
- CLI tools on `PATH`: `yt-dlp`, `summarize`
- Environment: `YOUTUBE_API_KEY`
- Optional: `youtube-db/config/aliases.json` for custom entity/topic aliases
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

Optional: create an aliases file for entities you care about:

```bash
cp youtube-db/config/aliases.example.json youtube-db/config/aliases.json
```

Aliases can represent tickers, topics, people, products, protocols, or anything
else you want surfaced in the daily brief. Example:

```json
{
  "aliases": {
    "AI_INFRA": ["AI infrastructure", "GPU clusters", "data center capex"],
    "ROBOTICS": ["humanoid robots", "robotics automation"]
  }
}
```

## Adapting It

For investing research, keep the default prompt and optionally point
`--repo-root` at a repo containing `Stocks/*/meta.json`.

For a general knowledge brief:

1. Edit `scripts/youtube-monitor/prompts/summary.md`.
2. Rename sections such as `Investment Relevance` to your own decision lens,
   for example `Research Relevance`, `Product Relevance`, or `Policy Relevance`.
3. Put your custom entity/topic aliases in `youtube-db/config/aliases.json`.
4. Use `--repo-root` only if you want summarize to run from a specific project
   folder or you have a `Stocks/` directory to read.

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

Record natural feedback from the daily brief:

```bash
scripts/youtube-monitor/run.sh --date 2026-05-02 --feedback "W1 down indexing_saturated; W3 promote"
```

Use the optional local click UI:

```bash
scripts/youtube-monitor/run.sh --date 2026-05-02 --serve-review
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
  --env-file /path/to/research-repo/scripts/.env \
  --aliases-file /path/to/research-repo/youtube-db/config/aliases.json
```

## Behavior

- Skips videos shorter than 3 minutes.
- Normal runs do not rerun a completed day.
- Interrupted runs resume pending or failed manifest items.
- Quote extraction groups adjacent summary timestamps into coherent quote blocks.
- Daily review items get stable IDs such as `W1`; feedback can be appended by chat-style command or by the local review UI.
- Failed transcript or summary attempts are recorded in the daily brief.

## Codex Skill

This repository can be installed as a Codex skill. The skill entrypoint is `SKILL.md`; the executable routine lives under `scripts/youtube-monitor/`.
