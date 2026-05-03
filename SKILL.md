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
- One-click review launcher: `Review YouTube.command`
- Review app launcher: `scripts/youtube-monitor/review_app.py`
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
python3 scripts/youtube-monitor/review_app.py
python3 scripts/youtube-monitor/review_app.py YYYY-MM-DD
scripts/youtube-monitor/run.sh --date YYYY-MM-DD --feedback "W1 down indexing_saturated; W3 promote"
scripts/youtube-monitor/run.sh --date YYYY-MM-DD --serve-review
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5 --dry-run
scripts/youtube-monitor/run.sh --lookback-count 3 --workers 5 --force
```

For clickable feedback, prefer `Review YouTube.command` or
`python3 scripts/youtube-monitor/review_app.py`. The app infers the latest
review date, starts a fresh local server on an available port, opens the
browser, and writes button clicks to `youtube-db/review/feedback.jsonl`.
Opening `youtube-db/review/YYYY-MM-DD.html` directly as `file://` is only a
static preview. `--serve-review` remains available for automation/debugging.

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
- review app launcher path when feedback is expected
- daily brief path

When the user gives watchworthiness feedback, parse natural commands from the daily brief review IDs:

- `W1 up`: more like this
- `W1 down indexing_saturated`: less like this, with reason code
- `W1 known`: already-known or saturated for the user
- `W1 promote`: promote to research/manual follow-up queue

Interpret `up`, `down`, and `known` as preference-learning signals for future
ranking. Interpret `promote` as a workflow action: the item deserves manual
research follow-up or distribution consideration. Do not treat `up` as an
instruction to promote.

Append feedback with `--feedback` or `--feedback-file`; do not rewrite historical briefs just to record feedback.

For investing workflows, do not automatically write findings into `Stocks/`; keep promotion manual unless the user explicitly asks for distribution.

## Maintenance Notes

- The monitor skips videos shorter than 3 minutes.
- The monitor writes review state, review HTML, and feedback JSONL under `youtube-db/review/`.
- Review cards should use the larger decision layout: summary judgment, highlighted opinion, key insights, key quotes, primary evidence, watchworthiness, and feedback actions; avoid fact-only cards for investment content.
- The summary prompt should be opinionated while remaining transcript-grounded: state what the video changes, confirms, or fails to change for the decision lens.
- The monitor reads `youtube-db/config/aliases.json` for generic matching and `Stocks/*/meta.json` only for optional investing alias matching.
- The monitor should not depend on other local skills.
- Run `python3 scripts/youtube-monitor/test_youtube_monitor.py` after code changes.
