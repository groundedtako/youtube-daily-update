#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import youtube_monitor as ym
import review_app


class YouTubeMonitorTests(unittest.TestCase):
    def test_parse_iso8601_duration(self) -> None:
        self.assertEqual(ym.parse_iso8601_duration("PT1H02M03S"), 3723)
        self.assertEqual(ym.parse_iso8601_duration("PT14M"), 840)
        self.assertEqual(ym.parse_iso8601_duration("PT45S"), 45)
        self.assertEqual(ym.parse_iso8601_duration("bad"), 0)

    def test_channel_configs_accept_plain_handle_list(self) -> None:
        configs = ym.channel_configs({"channels": ["@DwarkeshPatel"]})
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].handle, "@DwarkeshPatel")
        self.assertEqual(configs[0].label, "@DwarkeshPatel")
        self.assertIsNone(configs[0].channel_id)

    def test_channel_configs_skip_blacklisted_channels(self) -> None:
        configs = ym.channel_configs(
            {
                "channels": ["@DwarkeshPatel", "@OutdoorBoys", "@Tech.explain1"],
                "blacklist_channels": ["outdoorboys", "@tech.explain1"],
            }
        )
        self.assertEqual([item.handle for item in configs], ["@DwarkeshPatel"])

    def test_add_channel_to_blacklist_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.json"
            ym.write_json(path, {"channels": ["@DrewCohenMoney"], "blacklist_channels": []})

            ym.add_channel_to_blacklist(path, "@DrewCohenMoney")

            config = ym.read_json(path)
            self.assertEqual(config["blacklist_channels"], ["@DrewCohenMoney"])
            self.assertEqual(ym.channel_configs(config), [])

    def test_prepare_manifest_does_not_rerun_completed_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "2026-05-02.json"
            existing = {"run_date": "2026-05-02", "status": "completed", "videos": []}
            ym.write_json(path, existing)
            manifest, should_run = ym.prepare_manifest(path, "2026-05-02", [], force=False)

        self.assertFalse(should_run)
        self.assertEqual(manifest["status"], "completed")

    def test_prepare_manifest_resets_interrupted_processing_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "2026-05-02.json"
            existing = {
                "run_date": "2026-05-02",
                "status": "in_progress",
                "videos": [{"video_id": "abc", "status": "processing"}],
            }
            ym.write_json(path, existing)
            manifest, should_run = ym.prepare_manifest(path, "2026-05-02", [], force=False)

        self.assertTrue(should_run)
        self.assertEqual(manifest["videos"][0]["status"], "pending")
        self.assertTrue(manifest["videos"][0]["resumed_from_interrupted_processing"])

    def test_discover_candidates_reports_progress_and_processed_skips(self) -> None:
        class FakeClient:
            def resolve_channel(self, config: ym.ChannelConfig) -> dict[str, str]:
                return {
                    "channel_id": "channel-1",
                    "channel_title": "Channel One",
                    "uploads_playlist_id": "uploads-1",
                }

            def latest_uploads(self, playlist_id: str, max_results: int) -> list[dict[str, object]]:
                return [
                    {
                        "contentDetails": {"videoId": "new-video"},
                        "snippet": {"title": "New Video", "description": "new"},
                    },
                    {
                        "contentDetails": {"videoId": "old-video"},
                        "snippet": {"title": "Old Video", "description": "old"},
                    },
                ]

            def video_details(self, video_ids: list[str]) -> dict[str, dict[str, object]]:
                return {
                    "new-video": {
                        "snippet": {
                            "title": "New Video",
                            "description": "new",
                            "publishedAt": "2026-05-02T00:00:00Z",
                        },
                        "contentDetails": {"duration": "PT4M"},
                    },
                    "old-video": {
                        "snippet": {
                            "title": "Old Video",
                            "description": "old",
                            "publishedAt": "2026-05-01T00:00:00Z",
                        },
                        "contentDetails": {"duration": "PT1M"},
                    },
                }

        events: list[dict[str, object]] = []
        candidates, skipped = ym.discover_candidates(
            client=FakeClient(),
            channels=[ym.ChannelConfig("@channel", "@channel", None, None)],
            processed={"old-video": {"video_id": "old-video"}},
            lookback_count=2,
            progress=events.append,
        )

        self.assertEqual([candidate.video_id for candidate in candidates], ["new-video"])
        self.assertEqual([item["video_id"] for item in skipped], ["old-video"])
        self.assertEqual(skipped[0]["reason"], "duration_below_180_seconds")
        self.assertEqual([event["event"] for event in events], ["discovery_start", "channel_start", "channel_done", "discovery_done"])
        self.assertEqual(events[2]["new_count"], 1)
        self.assertEqual(events[2]["skipped_count"], 1)
        self.assertEqual(events[3]["candidate_count"], 1)

    def test_extract_entity_mentions_maps_aliases_and_unmapped_symbols(self) -> None:
        mentions = ym.extract_entity_mentions(
            "Nvidia and TSMC discussed $CRWV, while AI capex rose.",
            {"NVDA": ["Nvidia", "GPU"], "TSM": ["TSMC", "Taiwan Semiconductor"]},
        )
        self.assertEqual(
            mentions["mapped"],
            [
                {"symbol": "NVDA", "matched_aliases": ["Nvidia"]},
                {"symbol": "TSM", "matched_aliases": ["TSMC"]},
            ],
        )
        self.assertEqual(mentions["unmapped_symbols"], ["CRWV"])

    def test_common_entity_aliases_detect_amazon_from_title(self) -> None:
        mentions = ym.extract_entity_mentions(
            "Why You Would Have Missed Amazon in 2000",
            ym.COMMON_ENTITY_ALIASES,
        )
        self.assertEqual(mentions["mapped"], [{"symbol": "AMZN", "matched_aliases": ["Amazon"]}])

    def test_load_aliases_file_accepts_generic_entities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aliases.json"
            ym.write_json(
                path,
                {
                    "aliases": {
                        "AI_INFRA": ["AI infrastructure", "GPU clusters"],
                        "NVDA": {"name": "Nvidia", "aliases": ["NVIDIA Corporation"]},
                    }
                },
            )

            aliases = ym.load_aliases_file(path)

        self.assertEqual(aliases["AI_INFRA"], ["AI_INFRA", "AI infrastructure", "GPU clusters"])
        self.assertEqual(aliases["NVDA"], ["NVDA", "Nvidia", "NVIDIA Corporation"])

    def test_merge_aliases_combines_sources(self) -> None:
        self.assertEqual(
            ym.merge_aliases({"NVDA": ["NVDA", "Nvidia"]}, {"NVDA": ["NVIDIA Corporation"], "TSM": ["TSMC"]}),
            {"NVDA": ["NVDA", "Nvidia", "NVIDIA Corporation"], "TSM": ["TSM", "TSMC"]},
        )

    def test_parse_vtt_strips_tags_and_dedupes(self) -> None:
        content = """WEBVTT

00:00:01.000 --> 00:00:03.000
<c>AI capex</c> is rising

00:00:03.500 --> 00:00:05.000
<c>AI capex</c> is rising

00:00:06.000 --> 00:00:08.000
TSMC <00:00:06.500><c>CoWoS</c> is constrained
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.vtt"
            path.write_text(content, encoding="utf-8")
            cues = ym.parse_vtt(path)

        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "AI capex is rising")
        self.assertEqual(cues[1].text, "TSMC CoWoS is constrained")
        self.assertEqual(cues[1].start_seconds, 6.0)

    def test_parse_vtt_collapses_overlapping_partial_cues(self) -> None:
        content = """WEBVTT

00:00:01.000 --> 00:00:02.000
of the revenue, wouldn't they rather

00:00:02.000 --> 00:00:03.000
of the revenue, wouldn't they rather drop the government than the AI?
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.vtt"
            path.write_text(content, encoding="utf-8")
            cues = ym.parse_vtt(path)

        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "of the revenue, wouldn't they rather drop the government than the AI?")

    def test_cue_window_merges_adjacent_overlapping_cues(self) -> None:
        cues = [
            ym.VttCue(67, 69, "War, which constitutes a tiny fraction"),
            ym.VttCue(69, 71, "which constitutes a tiny fraction of the revenue, wouldn't they rather"),
            ym.VttCue(71, 73, "of the revenue, wouldn't they rather drop the government than the AI?"),
        ]
        self.assertEqual(
            ym.cue_window(cues, 0),
            "War, which constitutes a tiny fraction of the revenue, wouldn't they rather drop the government than the AI?",
        )

    def test_parse_summary_insights_extracts_timestamped_claims(self) -> None:
        summary = """## Core Take
Text.

## Key Insights
- [0:02] First claim.
- [0:30–0:36] Second *claim*.

## Investment Relevance
Text.
"""
        self.assertEqual(
            ym.parse_summary_insights(summary),
            [
                {"timestamp": "0:02", "claim": "First claim."},
                {"timestamp": "0:30", "claim": "Second claim."},
            ],
        )

    def test_concise_core_take_extracts_only_core_take(self) -> None:
        summary = """## Core Take
This is the useful daily brief sentence that should appear.

## Key Insights
- This should not be copied into the compact daily report.
"""
        self.assertEqual(
            ym.concise_core_take(summary),
            "This is the useful daily brief sentence that should appear.",
        )

    def test_concise_summary_section_extracts_relevance(self) -> None:
        summary = """## Core Take
Useful but saturated.

## Investment Relevance
This is investable because it separates durable business quality from stale market timing.

## Watch Worthiness
Medium — useful if this is not already in your base rate.
"""
        self.assertEqual(
            ym.concise_summary_section(summary, "Investment Relevance"),
            "This is investable because it separates durable business quality from stale market timing.",
        )

    def test_decision_lens_summary_falls_back_when_missing(self) -> None:
        self.assertIn(
            "No separate decision-lens section",
            ym.decision_lens_summary("## Core Take\nUseful judgment."),
        )

    def test_concise_summary_bullets_extracts_limited_items(self) -> None:
        summary = """## Key Insights
- [1:00] First useful insight with **emphasis**.
- [2:00] Second useful insight.
- [3:00] Third useful insight.
- [4:00] Fourth useful insight.
"""
        self.assertEqual(
            ym.concise_summary_bullets(summary, "Key Insights", max_items=2),
            ["[1:00] First useful insight with emphasis.", "[2:00] Second useful insight."],
        )

    def test_quote_highlights_uses_top_insights(self) -> None:
        self.assertEqual(
            ym.quote_highlights(
                [
                    {"timestamp": "1:00", "quote": "&gt;&gt; A direct &gt;&gt; quote.", "url": "https://example.com/1"},
                    {"timestamp": "2:00", "claim": "A fallback claim.", "url": "https://example.com/2"},
                ]
            ),
            [
                {"timestamp": "1:00", "text": "A direct quote.", "url": "https://example.com/1"},
                {"timestamp": "2:00", "text": "A fallback claim.", "url": "https://example.com/2"},
            ],
        )

    def test_review_text_helpers_do_not_truncate_when_disabled(self) -> None:
        long_text = " ".join(["long"] * 120)
        summary = f"## Core Take\n{long_text}\n\n## Key Insights\n- {long_text}"

        self.assertEqual(ym.concise_core_take(summary, max_chars=None), long_text)
        self.assertEqual(ym.concise_summary_bullets(summary, "Key Insights", max_chars=None), [long_text])

    def test_concise_core_take_avoids_full_transcript_fallback(self) -> None:
        transcript_like = "\n".join(["# Source-Grounded Summary", *[f"[{idx}:00] word" for idx in range(500)]])

        self.assertIn("No structured Core Take", ym.concise_core_take(transcript_like, max_chars=None))

    def test_preference_feedback_downranks_similar_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            stale_item = {
                "title": "Indexing Advice For Beginners",
                "channel": "Finance Channel",
                "entities": ["SPY"],
                "claim": "Index funds are best for most people.",
                "core_take": "Basic indexing advice.",
                "decision_lens": "Useful but saturated.",
            }
            fresh_item = {
                "title": "Semiconductor Test Equipment Cycle",
                "channel": "Chip Channel",
                "entities": ["NVDA"],
                "claim": "AI test intensity is rising.",
                "core_take": "Specific semiconductor insight.",
                "decision_lens": "Potentially actionable.",
            }
            ym.write_json(
                db_dir / "review" / "2026-05-01.json",
                {"items": [{**stale_item, "review_id": "W1", "video_id": "old", "source_url": "x", "artifact_dir": "a"}]},
            )
            ym.append_feedback_records(db_dir, "2026-05-01", [{"review_id": "W1", "action": "down", "reason_codes": ["indexing_saturated"], "raw_text": "W1 down indexing_saturated"}])

            stale_score, stale_reasons = ym.preference_adjustment(db_dir, stale_item)
            fresh_score, _ = ym.preference_adjustment(db_dir, fresh_item)

            self.assertLess(stale_score, 0)
            self.assertEqual(fresh_score, 0)
            self.assertTrue(any(reason.startswith("down:") for reason in stale_reasons))

    def test_build_review_items_sorts_by_preference_adjusted_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            ym.write_json(
                db_dir / "review" / "2026-05-01.json",
                {
                    "items": [
                        {
                            "review_id": "W1",
                            "video_id": "old",
                            "title": "Indexing Advice For Beginners",
                            "channel": "Finance Channel",
                            "source_url": "x",
                            "artifact_dir": "a",
                            "entities": ["SPY"],
                            "claim": "Index funds are best.",
                            "core_take": "Basic indexing advice.",
                            "decision_lens": "Saturated.",
                        }
                    ]
                },
            )
            ym.append_feedback_records(db_dir, "2026-05-01", [{"review_id": "W1", "action": "down", "reason_codes": ["indexing_saturated"], "raw_text": "W1 down"}])

            def result(video_id: str, title: str, channel: str, claim: str) -> dict[str, object]:
                return {
                    "metadata": {
                        "video_id": video_id,
                        "title": title,
                        "channel": channel,
                        "channel_handle": channel,
                        "source_url": f"https://example.com/{video_id}",
                        "duration_seconds": 600,
                        "entity_mentions": {"mapped": [], "unmapped_symbols": []},
                    },
                    "output_dir": db_dir / "videos" / video_id,
                    "summary": f"## Core Take\n{claim}\n\n## Key Insights\n- {claim}",
                    "insights": [
                        {
                            "claim": claim,
                            "quote": claim,
                            "timestamp": "1:00",
                            "timestamp_seconds": 60,
                            "url": f"https://example.com/{video_id}#t=60",
                            "mentioned_entities": ["SPY"] if "Index" in title else ["NVDA"],
                            "score": 1,
                        }
                    ],
                }

            items, _ = ym.build_review_items(
                db_dir,
                [
                    result("stale", "Indexing Advice For Beginners", "Finance Channel", "Index funds are best for most people."),
                    result("fresh", "Semiconductor Test Equipment Cycle", "Chip Channel", "AI test intensity is rising."),
                ],
                max_items=2,
            )

            self.assertEqual(items[0]["video_id"], "fresh")
            self.assertLess(items[1]["preference_score"], 0)

    def test_parse_feedback_text_accepts_chat_commands(self) -> None:
        self.assertEqual(
            ym.parse_feedback_text("w1 down indexing_saturated\nW3 promote"),
            [
                {
                    "review_id": "W1",
                    "action": "down",
                    "reason_codes": ["indexing_saturated"],
                    "raw_text": "w1 down indexing_saturated",
                },
                {
                    "review_id": "W3",
                    "action": "promote",
                    "reason_codes": [],
                    "raw_text": "W3 promote",
                },
            ],
        )

    def test_write_daily_report_emits_review_ids_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            artifact_dir = db_dir / "videos" / "channel" / "2026-05-02--abc--video"
            result = {
                "metadata": {
                    "video_id": "abc",
                    "title": "Stock Expert Interview",
                    "channel": "The Diary Of A CEO",
                    "channel_handle": "@diaryofaceo",
                    "source_url": "https://www.youtube.com/watch?v=abc",
                    "duration_seconds": 600,
                    "entity_mentions": {"mapped": [], "unmapped_symbols": []},
                    "transcript_file": "transcript.clean.md",
                },
                "output_dir": artifact_dir,
                "summary": (
                    "## Core Take\nUseful but saturated indexing advice.\n\n"
                    "## Key Insights\n- Index funds are useful but familiar.\n- Behavior matters more than product choice.\n\n"
                    "## Investment Relevance\nThe opinion is sensible but does not change the investing action if indexing is already familiar.\n\n"
                    "## Watch Worthiness\nMedium — good primer, low novelty."
                ),
                "insights": [
                    {
                        "claim": "Index funds beat many active managers.",
                        "quote": "Index funds beat many active managers.",
                        "timestamp": "12:00",
                        "timestamp_seconds": 720,
                        "url": "https://www.youtube.com/watch?v=abc&t=720s",
                        "mentioned_entities": ["SPY"],
                    }
                ],
            }

            daily_path = ym.write_daily_report(db_dir, "2026-05-02", [result], [], [], False)

            daily = daily_path.read_text(encoding="utf-8")
            self.assertIn("**W1**", daily)
            self.assertIn("Review app: double-click `Review YouTube.command`", daily)
            self.assertIn("unreviewed-date dashboard", daily)
            self.assertIn("python3 scripts/youtube-monitor/review_app.py 2026-05-02", daily)
            self.assertIn("`W1 up | W1 down <reason> | W1 known | W1 promote`", daily)
            state = json.loads((db_dir / "review" / "2026-05-02.json").read_text(encoding="utf-8"))
            self.assertEqual(state["items"][0]["review_id"], "W1")
            self.assertEqual(state["items"][0]["core_take"], "Useful but saturated indexing advice.")
            self.assertIn("does not change", state["items"][0]["decision_lens"])
            self.assertEqual(state["items"][0]["key_insights"][0], "Index funds are useful but familiar.")
            self.assertEqual(state["items"][0]["quote_highlights"][0]["text"], "Index funds beat many active managers.")
            html = (db_dir / "review" / "2026-05-02.html").read_text(encoding="utf-8")
            self.assertIn("More like this", html)
            self.assertIn("Workflow action", html)
            self.assertIn("Highlighted Opinion", html)
            self.assertIn("Key Quotes", html)
            self.assertIn("Blacklist channel", html)
            self.assertIn("file://", html)

    def test_apply_feedback_text_enriches_jsonl_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            ym.write_json(
                db_dir / "review" / "2026-05-02.json",
                {
                    "run_date": "2026-05-02",
                    "items": [
                        {
                            "review_id": "W1",
                            "video_id": "abc",
                            "title": "Stock Expert Interview",
                            "channel": "The Diary Of A CEO",
                            "source_url": "https://www.youtube.com/watch?v=abc",
                            "artifact_dir": "videos/channel/abc",
                        }
                    ],
                },
            )

            count = ym.apply_feedback_text(db_dir, "2026-05-02", "W1 down indexing_saturated")

            self.assertEqual(count, 1)
            record = json.loads((db_dir / "review" / "feedback.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["action"], "down")
            self.assertEqual(record["reason_codes"], ["indexing_saturated"])
            self.assertEqual(record["video_id"], "abc")

    def test_latest_review_date_uses_newest_review_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            ym.write_json(db_dir / "review" / "2026-05-01.json", {"items": []})
            ym.write_json(db_dir / "review" / "2026-05-03.json", {"items": []})

            self.assertEqual(review_app.latest_review_date(db_dir), "2026-05-03")

    def test_unreviewed_date_summaries_hide_completed_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            ym.write_json(db_dir / "review" / "2026-05-01.json", {"items": [{"review_id": "W1"}]})
            ym.write_json(db_dir / "review" / "2026-05-02.json", {"items": [{"review_id": "W1"}, {"review_id": "W2"}]})
            ym.mark_review_date_complete(db_dir, "2026-05-01")

            summaries = ym.unreviewed_date_summaries(db_dir)

            self.assertEqual([item["run_date"] for item in summaries], ["2026-05-02"])
            self.assertEqual(summaries[0]["item_count"], 2)

    def test_review_dashboard_lists_unreviewed_dates(self) -> None:
        html = ym.render_review_dashboard_html(
            [{"run_date": "2026-05-02", "item_count": 2, "feedback_count": 1, "generated_at": "now"}],
            "http://127.0.0.1:12345/",
        )

        self.assertIn("YouTube Review Queue", html)
        self.assertIn("/date/2026-05-02", html)
        self.assertIn("1/2 items have feedback", html)


if __name__ == "__main__":
    unittest.main()
