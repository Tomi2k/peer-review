"""Tests for Artificial Analysis parser and Firecrawl response handling."""
from __future__ import annotations

import unittest

from peer_review.aa_scraper import ScrapeError, _extract_json_from_markdown, parse_extracted_payload


class ScraperParserTest(unittest.TestCase):
    def test_parses_scores_and_zero_cost_safely(self) -> None:
        payload = {
            "indexVersion": "v2.1",
            "count": 3,
            "items": [
                {
                    "id": "v-neon",
                    "agentName": "Antigravity CLI",
                    "agentSlug": "antigravity-cli",
                    "provider": "google",
                    "shortLabel": "Antigravity CLI - Gemini 4 Neon (xhigh)",
                    "modelRelease": "Gemini 4 Neon",
                    "modelName": "Gemini 4 Neon (Xhigh)",
                    "effortSlug": "xhigh",
                    "creators": [{"slug": "google", "name": "Google"}],
                    "isUnavailable": False,
                    "isRouter": False,
                    "indexComponentCount": 3,
                    "evalCount": 3,
                    "indexScore": 0.58639559,
                    "meanCostUsd": 6.8612,
                },
                {
                    "id": "v-zero-cost",
                    "agentName": "Test CLI",
                    "agentSlug": "test-cli",
                    "provider": "alpha",
                    "modelRelease": "Alpha 1",
                    "modelName": "Alpha 1 (High)",
                    "effortSlug": "high",
                    "creators": [{"slug": "alpha", "name": "Alpha"}],
                    "indexScore": 0.80,
                    "meanCostUsd": 0.0,  # Zero cost MUST become None so it cannot trigger switch.
                },
                {
                    "id": "v-unavail",
                    "agentName": "Muse Code",
                    "agentSlug": "muse-code",
                    "provider": "meta",
                    "modelRelease": "Muse Wonder Full",
                    "modelName": "Muse Wonder Full (Max)",
                    "effortSlug": "max",
                    "creators": [{"slug": "meta", "name": "Meta"}],
                    "isUnavailable": True,
                    "indexScore": 0.5447,
                    "meanCostUsd": None,
                },
            ],
        }
        snap = parse_extracted_payload(payload)
        self.assertEqual(snap.index_version, "v2.1")
        self.assertEqual(len(snap.variants), 3)
        by_id = {v.variant_id: v for v in snap.variants}
        self.assertAlmostEqual(by_id["v-neon"].coding_agent_index or 0, 58.639559, places=6)
        self.assertAlmostEqual(by_id["v-neon"].cost_per_task_usd or 0, 6.8612, places=6)
        self.assertIsNone(by_id["v-zero-cost"].cost_per_task_usd)
        self.assertIn("missing_or_zero_cost", by_id["v-zero-cost"].cost_notes)
        self.assertTrue(by_id["v-unavail"].is_unavailable)

    def test_extract_json_from_fenced_markdown(self) -> None:
        md = '```\n{"indexVersion":"v2.1","items":[{"id":"x"}]}\n```'
        obj = _extract_json_from_markdown(md)
        self.assertEqual(obj["indexVersion"], "v2.1")

    def test_rejects_empty_items(self) -> None:
        with self.assertRaises(ScrapeError):
            parse_extracted_payload({"indexVersion": "v2.1", "items": []})


if __name__ == "__main__":
    unittest.main()
