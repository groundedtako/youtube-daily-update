#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import youtube_monitor as ym


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
                "summary": "## Core Take\nUseful but saturated indexing advice.",
                "insights": [
                    {
                        "claim": "Index funds beat many active managers.",
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
            self.assertIn("Review app: run `scripts/youtube-monitor/run.sh --date 2026-05-02 --serve-review`", daily)
            self.assertIn("`W1 up | W1 down <reason> | W1 known | W1 promote`", daily)
            state = json.loads((db_dir / "review" / "2026-05-02.json").read_text(encoding="utf-8"))
            self.assertEqual(state["items"][0]["review_id"], "W1")
            html = (db_dir / "review" / "2026-05-02.html").read_text(encoding="utf-8")
            self.assertIn("More like this", html)
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


if __name__ == "__main__":
    unittest.main()
