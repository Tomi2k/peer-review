"""Unit tests for Frontier filtering, K*log2(cost) selection, tie-breakers, and data gaps."""
from __future__ import annotations

import math
import unittest

from peer_review.config import default_config
from peer_review.models import BenchmarkSnapshot, Lineup, SeatConfig, VariantRecord
from peer_review.selector import (
    economic_advantage,
    evaluate_selection,
    required_extra_points,
    selection_value,
)


def _variant(
    vid: str,
    creator_slug: str,
    creator_name: str,
    release: str,
    effort: str,
    score: float | None,
    cost: float | None,
    *,
    agent_slug: str = "harness-a",
    agent_name: str = "Harness A",
    index_version: str = "v2.1",
    is_unavailable: bool = False,
    is_router: bool = False,
) -> VariantRecord:
    return VariantRecord(
        variant_id=vid,
        creator_slug=creator_slug,
        creator_name=creator_name,
        model_release=release,
        model_version=f"{release} ({effort.capitalize()})",
        agent_slug=agent_slug,
        agent_name=agent_name,
        effort=effort,
        coding_agent_index=score,
        cost_per_task_usd=cost,
        index_version=index_version,
        source_url="https://artificialanalysis.ai/agents/coding-agents",
        fetched_at_utc="2026-11-26T00:00:00Z",
        is_unavailable=is_unavailable,
        is_router=is_router,
        eval_count=3,
        index_component_count=3,
    )


def _base_config(catalog: list[dict], k: float = 5.0, buf: float = 3.0) -> dict:
    cfg = default_config()
    cfg["selection"]["k_cost_doubling"] = k
    cfg["selection"]["quality_buffer"] = buf
    cfg["catalog"] = catalog
    return cfg


class SelectorWorkedExamplesTest(unittest.TestCase):
    """Verifies the 3 mandatory worked examples from the specification (K = 5)."""

    def test_example_1_max_rejected_negative_7_42(self) -> None:
        # High: 80.0 @ $1.00 vs Max: 80.5 @ $3.00 -> 0.5 - 5*log2(3) ≈ -7.42
        adv = economic_advantage(80.5, 3.00, 80.0, 1.00, k=5.0)
        self.assertAlmostEqual(adv, 0.5 - 5.0 * math.log2(3.0), places=12)
        self.assertAlmostEqual(adv, -7.4248125036, places=6)
        self.assertLess(adv, 0.0)
        req = required_extra_points(3.00, 1.00, k=5.0)
        self.assertAlmostEqual(req, 7.9248125036, places=6)
        # Invariant: difference of selection values == direct economic advantage.
        self.assertAlmostEqual(
            selection_value(80.5, 3.00, 5.0) - selection_value(80.0, 1.00, 5.0),
            adv,
            places=12,
        )

    def test_example_2_new_variant_accepted_plus_1_39(self) -> None:
        # High: 80.0 @ $1.00 vs New: 83.0 @ $1.25 -> 3 - 5*log2(1.25) ≈ +1.39
        adv = economic_advantage(83.0, 1.25, 80.0, 1.00, k=5.0)
        self.assertAlmostEqual(adv, 3.0 - 5.0 * math.log2(1.25), places=12)
        self.assertAlmostEqual(adv, 1.3903595256, places=6)
        self.assertGreater(adv, 0.0)

    def test_example_3_exact_tie_zero_keeps_active(self) -> None:
        # Active: 80.0 @ $1.00 vs Alt: 85.0 @ $2.00 -> 5 - 5*log2(2) == 0.0 -> Active stays.
        adv = economic_advantage(85.0, 2.00, 80.0, 1.00, k=5.0)
        self.assertEqual(adv, 0.0)
        self.assertEqual(
            selection_value(85.0, 2.00, 5.0) - selection_value(80.0, 1.00, 5.0),
            0.0,
        )


class SelectorBehaviorTest(unittest.TestCase):
    def test_multi_effort_same_model_picks_best_value_and_reports_rejected_peak(self) -> None:
        # 3 creators: Alpha (84), Beta (82), Gamma (80) -> reference=80, floor=77.
        # Cheap model at 65 is excluded BEFORE cost optimization.
        snap = BenchmarkSnapshot(
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            fetched_at_utc="2026-11-26T00:00:00Z",
            variants=[
                _variant("alpha-high", "alpha", "Alpha", "Alpha-1", "high", 80.0, 1.00),
                _variant("alpha-max", "alpha", "Alpha", "Alpha-1", "max", 80.5, 3.00),
                _variant("alpha-cheap", "alpha", "Alpha", "Alpha-Mini", "low", 65.0, 0.05),
                _variant("beta-high", "beta", "Beta", "Beta-1", "high", 82.0, 1.50),
                _variant("gamma-high", "gamma", "Gamma", "Gamma-1", "high", 80.0, 1.20),
            ],
        )
        cfg = _base_config([
            {"creator_slug": "alpha", "match_model_release": "Alpha-1", "efforts": ["high", "max"], "provider": "alpha", "backend": "cli", "model_id": "alpha-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "alpha", "match_model_release": "Alpha-Mini", "efforts": ["low"], "provider": "alpha", "backend": "cli", "model_id": "alpha-mini", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "beta", "match_model_release": "Beta-1", "efforts": ["high"], "provider": "beta", "backend": "cli", "model_id": "beta-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "gamma", "match_model_release": "Gamma-1", "efforts": ["high"], "provider": "gamma", "backend": "cli", "model_id": "gamma-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        ])
        res = evaluate_selection(snap, cfg)
        self.assertFalse(res.incomplete)
        self.assertAlmostEqual(res.frontier_reference_score or 0, 80.0)
        self.assertAlmostEqual(res.frontier_floor or 0, 77.0)
        # Alpha picks high (80 @ $1), NOT max (80.5 @ $3) and NOT cheap (65 @ $0.05).
        self.assertEqual(res.selected_by_creator["alpha"].variant.variant_id, "alpha-high")
        rejected = [d for d in res.decisions if d.kind == "rejected_peak"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].active_effort, "high")
        self.assertEqual(rejected[0].candidate_effort, "max")
        self.assertAlmostEqual(rejected[0].economic_advantage or 0, -7.4248125, places=5)

    def test_exact_tie_keeps_active_seat(self) -> None:
        snap = BenchmarkSnapshot(
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            fetched_at_utc="2026-11-26T00:00:00Z",
            variants=[
                _variant("alpha-v1", "alpha", "Alpha", "Alpha-1", "high", 80.0, 1.00),
                _variant("alpha-v2", "alpha", "Alpha", "Alpha-2", "high", 85.0, 2.00),
                _variant("beta-v1", "beta", "Beta", "Beta-1", "high", 82.0, 1.50),
                _variant("gamma-v1", "gamma", "Gamma", "Gamma-1", "high", 80.0, 1.20),
            ],
        )
        cfg = _base_config([
            {"creator_slug": "alpha", "match_model_release": "Alpha-1", "efforts": ["high"], "provider": "alpha", "backend": "cli", "model_id": "alpha-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "alpha", "match_model_release": "Alpha-2", "efforts": ["high"], "provider": "alpha", "backend": "cli", "model_id": "alpha-2", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "beta", "match_model_release": "Beta-1", "efforts": ["high"], "provider": "beta", "backend": "cli", "model_id": "beta-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "gamma", "match_model_release": "Gamma-1", "efforts": ["high"], "provider": "gamma", "backend": "cli", "model_id": "gamma-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        ])
        active = Lineup(
            schema_version="1.0",
            revision=1,
            activated_at_utc="2026-11-25T00:00:00Z",
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            k_cost_doubling=5.0,
            quality_buffer=3.0,
            seats=[
                SeatConfig("seat-1", "alpha", "Alpha", "alpha", "cli", "alpha-1", "Alpha-1 (High)", "high", "Harness A", "alpha-v1", 80.0, 1.00, "v2.1"),
                SeatConfig("seat-2", "beta", "Beta", "beta", "cli", "beta-1", "Beta-1 (High)", "high", "Harness A", "beta-v1", 82.0, 1.50, "v2.1"),
                SeatConfig("seat-3", "gamma", "Gamma", "gamma", "cli", "gamma-1", "Gamma-1 (High)", "high", "Harness A", "gamma-v1", 80.0, 1.20, "v2.1"),
            ],
        )
        res = evaluate_selection(snap, cfg, active_lineup=active)
        self.assertEqual(res.selected_by_creator["alpha"].variant.variant_id, "alpha-v1")
        switches = [p for p in res.proposed_changes if p.should_switch]
        self.assertEqual(len(switches), 0)

    def test_missing_cost_reports_data_gap_and_retains_active_seat(self) -> None:
        snap = BenchmarkSnapshot(
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            fetched_at_utc="2026-11-26T00:00:00Z",
            variants=[
                # Active alpha seat lacks cost data on current snapshot!
                _variant("alpha-v1", "alpha", "Alpha", "Alpha-1", "high", 80.0, None),
                _variant("alpha-v2", "alpha", "Alpha", "Alpha-2", "high", 84.0, 1.00),
                _variant("beta-v1", "beta", "Beta", "Beta-1", "high", 82.0, 1.50),
                _variant("gamma-v1", "gamma", "Gamma", "Gamma-1", "high", 80.0, 1.20),
            ],
        )
        cfg = _base_config([
            {"creator_slug": "alpha", "match_model_release": "Alpha-1", "efforts": ["high"], "provider": "alpha", "backend": "cli", "model_id": "alpha-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "alpha", "match_model_release": "Alpha-2", "efforts": ["high"], "provider": "alpha", "backend": "cli", "model_id": "alpha-2", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "beta", "match_model_release": "Beta-1", "efforts": ["high"], "provider": "beta", "backend": "cli", "model_id": "beta-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "gamma", "match_model_release": "Gamma-1", "efforts": ["high"], "provider": "gamma", "backend": "cli", "model_id": "gamma-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        ])
        active = Lineup(
            schema_version="1.0",
            revision=1,
            activated_at_utc="2026-11-25T00:00:00Z",
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            k_cost_doubling=5.0,
            quality_buffer=3.0,
            seats=[
                SeatConfig("seat-1", "alpha", "Alpha", "alpha", "cli", "alpha-1", "Alpha-1 (High)", "high", "Harness A", "alpha-v1", 80.0, 1.00, "v2.1"),
                SeatConfig("seat-2", "beta", "Beta", "beta", "cli", "beta-1", "Beta-1 (High)", "high", "Harness A", "beta-v1", 82.0, 1.50, "v2.1"),
                SeatConfig("seat-3", "gamma", "Gamma", "gamma", "cli", "gamma-1", "Gamma-1 (High)", "high", "Harness A", "gamma-v1", 80.0, 1.20, "v2.1"),
            ],
        )
        res = evaluate_selection(snap, cfg, active_lineup=active)
        self.assertTrue(any("Data gap" in g or "lacks reliable" in g for g in res.data_gaps))
        self.assertTrue(any(d.kind == "hold_data_gap" and d.seat_id == "seat-1" for d in res.decisions))
        # Must NOT propose switching seat-1 when active seat lacks valid current cost!
        self.assertFalse(any(p.seat_id == "seat-1" and p.should_switch for p in res.proposed_changes))

    def test_fewer_than_three_creators_marks_incomplete(self) -> None:
        snap = BenchmarkSnapshot(
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            fetched_at_utc="2026-11-26T00:00:00Z",
            variants=[
                _variant("alpha-v1", "alpha", "Alpha", "Alpha-1", "high", 84.0, 1.00),
                _variant("beta-v1", "beta", "Beta", "Beta-1", "high", 82.0, 1.50),
            ],
        )
        cfg = _base_config([
            {"creator_slug": "alpha", "match_model_release": "Alpha-1", "efforts": ["high"], "provider": "alpha", "backend": "cli", "model_id": "alpha-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "beta", "match_model_release": "Beta-1", "efforts": ["high"], "provider": "beta", "backend": "cli", "model_id": "beta-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        ])
        res = evaluate_selection(snap, cfg)
        self.assertTrue(res.incomplete)
        self.assertIn("2 distinct model creator", res.incomplete_reason)

    def test_new_creator_replaces_weakest_seat_in_order(self) -> None:
        snap = BenchmarkSnapshot(
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            fetched_at_utc="2026-11-26T00:00:00Z",
            variants=[
                _variant("alpha-v1", "alpha", "Alpha", "Alpha-1", "high", 84.0, 1.00),
                _variant("beta-v1", "beta", "Beta", "Beta-1", "high", 82.0, 1.00),
                _variant("gamma-v1", "gamma", "Gamma", "Gamma-1", "high", 80.0, 2.00),  # sel_val = 75
                _variant("delta-v1", "delta", "Delta", "Delta-1", "high", 83.0, 1.00),  # sel_val = 83
            ],
        )
        cfg = _base_config([
            {"creator_slug": "alpha", "match_model_release": "Alpha-1", "efforts": ["high"], "provider": "alpha", "backend": "cli", "model_id": "alpha-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "beta", "match_model_release": "Beta-1", "efforts": ["high"], "provider": "beta", "backend": "cli", "model_id": "beta-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "gamma", "match_model_release": "Gamma-1", "efforts": ["high"], "provider": "gamma", "backend": "cli", "model_id": "gamma-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "delta", "match_model_release": "Delta-1", "efforts": ["high"], "provider": "delta", "backend": "cli", "model_id": "delta-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        ])
        active = Lineup(
            schema_version="1.0",
            revision=1,
            activated_at_utc="2026-11-25T00:00:00Z",
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            k_cost_doubling=5.0,
            quality_buffer=3.0,
            seats=[
                SeatConfig("seat-1", "alpha", "Alpha", "alpha", "cli", "alpha-1", "Alpha-1 (High)", "high", "Harness A", "alpha-v1", 84.0, 1.00, "v2.1"),
                SeatConfig("seat-2", "beta", "Beta", "beta", "cli", "beta-1", "Beta-1 (High)", "high", "Harness A", "beta-v1", 82.0, 1.00, "v2.1"),
                SeatConfig("seat-3", "gamma", "Gamma", "gamma", "cli", "gamma-1", "Gamma-1 (High)", "high", "Harness A", "gamma-v1", 80.0, 2.00, "v2.1"),
            ],
        )
        res = evaluate_selection(snap, cfg, active_lineup=active)
        switches = [p for p in res.proposed_changes if p.should_switch]
        self.assertEqual(len(switches), 1)
        self.assertEqual(switches[0].seat_id, "seat-3")
        self.assertEqual(switches[0].candidate.variant.creator_slug, "delta")


if __name__ == "__main__":
    unittest.main()
