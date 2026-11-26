"""Tests for daily updater, smoke-test gating, atomic activation/rollback, and dedup notifications."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from peer_review.config import default_config
from peer_review.lineup_store import LineupStore
from peer_review.models import BenchmarkSnapshot, VariantRecord
from peer_review.smoke_test import SmokeTestResult
from peer_review.updater import run_lineup_update


def _v(vid: str, creator: str, name: str, rel: str, effort: str, score: float, cost: float) -> VariantRecord:
    return VariantRecord(
        variant_id=vid,
        creator_slug=creator,
        creator_name=name,
        model_release=rel,
        model_version=f"{rel} ({effort.capitalize()})",
        agent_slug="cli",
        agent_name="CLI",
        effort=effort,
        coding_agent_index=score,
        cost_per_task_usd=cost,
        index_version="v2.1",
        source_url="https://artificialanalysis.ai/agents/coding-agents",
        fetched_at_utc="2026-11-26T00:00:00Z",
        eval_count=3,
        index_component_count=3,
    )


def _cfg(state_dir: str) -> dict:
    cfg = default_config()
    cfg["state_dir"] = state_dir
    cfg["notifications"]["language"] = "de"
    cfg["catalog"] = [
        {"creator_slug": "alpha", "match_model_release": "Alpha-1", "efforts": ["high", "max"], "provider": "alpha", "backend": "cli", "model_id": "alpha-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        {"creator_slug": "alpha", "match_model_release": "Alpha-2", "efforts": ["high"], "provider": "alpha", "backend": "cli", "model_id": "alpha-2", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        {"creator_slug": "beta", "match_model_release": "Beta-1", "efforts": ["high"], "provider": "beta", "backend": "cli", "model_id": "beta-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        {"creator_slug": "gamma", "match_model_release": "Gamma-1", "efforts": ["high"], "provider": "gamma", "backend": "cli", "model_id": "gamma-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
    ]
    return cfg


class UpdaterTest(unittest.TestCase):
    def test_failed_smoke_test_preserves_active_lineup_and_dedups_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            snap1 = BenchmarkSnapshot(
                index_version="v2.1",
                source_url="https://artificialanalysis.ai/agents/coding-agents",
                fetched_at_utc="2026-11-26T00:00:00Z",
                variants=[
                    _v("a1-high", "alpha", "Alpha", "Alpha-1", "high", 80.0, 1.00),
                    _v("a1-max", "alpha", "Alpha", "Alpha-1", "max", 80.5, 3.00),
                    _v("b1-high", "beta", "Beta", "Beta-1", "high", 82.0, 1.50),
                    _v("g1-high", "gamma", "Gamma", "Gamma-1", "high", 80.0, 1.20),
                ],
            )
            pass_smoke = lambda seat, c: SmokeTestResult(True, "ok", "abc123")
            out1 = run_lineup_update(cfg, snapshot=snap1, smoke_tester=pass_smoke)
            self.assertTrue(out1.ok)
            self.assertTrue(out1.changed)
            # Rejected peak (Max vs High) must be notified in clear German wording.
            rej_msgs = [n["message"] for n in out1.emitted_notifications if n["kind"] == "rejected_peak"]
            self.assertEqual(len(rej_msgs), 1)
            self.assertIn("High bleibt aktiv", rej_msgs[0])
            self.assertIn("7,92", rej_msgs[0])

            # Re-running with identical snapshot MUST NOT emit duplicate notifications.
            out1_repeat = run_lineup_update(cfg, snapshot=snap1, smoke_tester=pass_smoke)
            self.assertTrue(out1_repeat.ok)
            self.assertFalse(out1_repeat.changed)
            self.assertEqual(len(out1_repeat.emitted_notifications), 0)

            # New Alpha-2 (+1.39 advantage) arrives, but smoke test FAILS -> Alpha-1 MUST stay active!
            snap2 = BenchmarkSnapshot(
                index_version="v2.1",
                source_url="https://artificialanalysis.ai/agents/coding-agents",
                fetched_at_utc="2026-11-27T00:00:00Z",
                variants=[
                    _v("a1-high", "alpha", "Alpha", "Alpha-1", "high", 80.0, 1.00),
                    _v("a2-high", "alpha", "Alpha", "Alpha-2", "high", 83.0, 1.25),
                    _v("b1-high", "beta", "Beta", "Beta-1", "high", 82.0, 1.50),
                    _v("g1-high", "gamma", "Gamma", "Gamma-1", "high", 80.0, 1.20),
                ],
            )
            fail_alpha2 = lambda seat, c: SmokeTestResult(seat.model_id != "alpha-2", "403 model rejected", "def456")
            out2 = run_lineup_update(cfg, snapshot=snap2, smoke_tester=fail_alpha2)
            self.assertTrue(out2.ok)
            self.assertFalse(out2.changed)
            store = LineupStore(tmp)
            active = store.load_active()
            assert active is not None
            alpha_seat = [s for s in active.seats if s.creator_slug == "alpha"][0]
            self.assertEqual(alpha_seat.model_id, "alpha-1")
            self.assertTrue(any(n["kind"] == "hold_smoke_failed" for n in out2.emitted_notifications))

            # Now Alpha-2 smoke test PASSES -> atomic activation to rev 2 + rollback back to rev 1!
            out3 = run_lineup_update(cfg, snapshot=snap2, smoke_tester=pass_smoke)
            self.assertTrue(out3.ok)
            self.assertTrue(out3.changed)
            active2 = store.load_active()
            assert active2 is not None
            self.assertEqual(active2.revision, 2)
            self.assertEqual([s for s in active2.seats if s.creator_slug == "alpha"][0].model_id, "alpha-2")

            rolled = store.rollback()
            self.assertEqual(rolled.revision, 3)
            self.assertEqual([s for s in rolled.seats if s.creator_slug == "alpha"][0].model_id, "alpha-1")
            self.assertTrue(Path(tmp, "notifications.jsonl").exists())
            self.assertTrue(Path(tmp, "notifications.md").exists())


if __name__ == "__main__":
    unittest.main()
