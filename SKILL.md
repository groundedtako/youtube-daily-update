---
name: youtube-daily-update
description: Run a daily YouTube intelligence brief from a configured channel list, producing local Markdown summaries, timestamped quotes, review queues, ticker/entity matches, and resumable manifests. Use when the user asks to monitor YouTube channels, build or refresh a daily YouTube brief, repair quote extraction, or package YouTube transcript intelligence for research workflows.
---

# YouTube Daily Update

Use this skill to run or maintain the bundled YouTube monitor.

## Entry Points

- Main command: `scripts/youtube-monitor/run.sh`
- Python implementation: `scripts/youtube-monitor/youtube_monitor.py`
- Prompt template: `scripts/youtube-monitor/prompts/summary.md`
- Channel config example: `youtube-db/config/channels.example.json`

## Preconditions

Verify these before running a real monitor job:

```bash
command -v yt-dlp
command -v summarize
python3 -m pip install -r scripts/youtube-monitor/requirements.txt
```

The monitor also needs `YOUTUBE_API_KEY`, either in the environment or in an env file passed with `--env-file`.

## Standard Run

Run from the skill/repo root:

```bash
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5
```

If today's run is already completed, use `--refresh-report` instead of rerunning unless the user explicitly asks for `--force`.

## Useful Commands

```bash
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5 --refresh-report
scripts/youtube-monitor/run.sh --refresh-quotes
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5 --dry-run
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5 --force
```

When the skill is installed outside the target research repo, pass:

```bash
scripts/youtube-monitor/run.sh \
  --repo-root /path/to/research-repo \
  --db-dir /path/to/research-repo/youtube-db \
  --config /path/to/research-repo/youtube-db/config/channels.json \
  --env-file /path/to/research-repo/scripts/.env
```

## Output Contract

Read `youtube-db/daily/YYYY-MM-DD.md` after a run and report:

- processed, skipped, and failed counts
- highest-signal video insights
- detected stock-note matches
- transcript failures
- Review Queue highlights
- daily brief path

Do not automatically write findings into `Stocks/`; keep promotion manual unless the user explicitly asks for distribution.

## Maintenance Notes

- The monitor skips videos shorter than 3 minutes.
- The monitor reads `Stocks/*/meta.json` only for optional alias matching.
- The monitor should not depend on other local skills.
- Run `python3 scripts/youtube-monitor/test_youtube_monitor.py` after code changes.

