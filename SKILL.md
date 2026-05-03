---
name: youtube-daily-update
description: Run a daily YouTube intelligence brief from a configured channel list, producing local Markdown summaries, timestamped quotes, review queues, entity/topic matches, and resumable manifests. Default setup is investing-focused, but the prompt and aliases can be adapted for any research workflow. Use when the user asks to monitor YouTube channels, build or refresh a daily YouTube brief, repair quote extraction, or package YouTube transcript intelligence.
---

# YouTube Daily Update

Use this skill to run or maintain the bundled YouTube monitor.

The default prompt is investing-focused. Treat that as a profile, not a hard
requirement: users can adapt the summary prompt and aliases for product,
technology, policy, academic, creator, or general knowledge monitoring.

## Entry Points

- Main command: `scripts/youtube-monitor/run.sh`
- Python implementation: `scripts/youtube-monitor/youtube_monitor.py`
- Prompt template: `scripts/youtube-monitor/prompts/summary.md`
- Channel config example: `youtube-db/config/channels.example.json`
- Alias config example: `youtube-db/config/aliases.example.json`

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
  --env-file /path/to/research-repo/scripts/.env \
  --aliases-file /path/to/research-repo/youtube-db/config/aliases.json
```

## Adapting to Non-Investing Use

For non-investing workflows:

- Edit `scripts/youtube-monitor/prompts/summary.md` to rename `Investment Relevance` to the user's decision lens.
- Create `youtube-db/config/aliases.json` from `aliases.example.json` and add topics, products, people, or organizations.
- Keep `--repo-root` pointed at the working project only when summarize should run from that project or when `Stocks/*/meta.json` should be read.
- Report "entity/topic matches" instead of "stock-note matches" unless the user is explicitly doing investment research.

## Output Contract

Read `youtube-db/daily/YYYY-MM-DD.md` after a run and report:

- processed, skipped, and failed counts
- highest-signal video insights
- detected entity/topic matches, or stock-note matches for investing workflows
- transcript failures
- Review Queue highlights
- daily brief path

For investing workflows, do not automatically write findings into `Stocks/`; keep promotion manual unless the user explicitly asks for distribution.

## Maintenance Notes

- The monitor skips videos shorter than 3 minutes.
- The monitor reads `youtube-db/config/aliases.json` for generic matching and `Stocks/*/meta.json` only for optional investing alias matching.
- The monitor should not depend on other local skills.
- Run `python3 scripts/youtube-monitor/test_youtube_monitor.py` after code changes.
