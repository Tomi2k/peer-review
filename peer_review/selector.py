"""Deterministic Frontier-level filtering and K*log2(cost) model selection."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .config import ALLOWED_EFFORTS, validate_effort, validate_model_id
from .models import BenchmarkSnapshot, DecisionRecord, Lineup, SeatConfig, VariantRecord

EPSILON = 1e-12


def economic_advantage(
    score_new: float,
    cost_new: float,
    score_old: float,
    cost_old: float,
    k: float = 5.0,
) -> float:
    """Compute QualityGain - K * log2(Cost_new / Cost_old).

    Positive favors the new variant; 0 or negative keeps the active variant.
    """
    if not all(math.isfinite(x) for x in (score_new, cost_new, score_old, cost_old, k)):
        raise ValueError("All inputs must be finite numbers")
    if cost_new <= 0 or cost_old <= 0 or k <= 0:
        raise ValueError("Costs and K must be strictly positive")
    quality_gain = score_new - score_old
    cost_factor = cost_new / cost_old
    return quality_gain - k * math.log2(cost_factor)


def required_extra_points(cost_new: float, cost_old: float, k: float = 5.0) -> float:
    """Minimum score increase needed for a cost change to break even (K * log2(factor))."""
    if not all(math.isfinite(x) for x in (cost_new, cost_old, k)) or cost_new <= 0 or cost_old <= 0 or k <= 0:
        raise ValueError("Costs and K must be strictly positive finite numbers")
    return k * math.log2(cost_new / cost_old)


def selection_value(score: float, cost_usd: float, k: float = 5.0) -> float:
    """Rank candidates via Score - K * log2(Cost / 1 USD).

    Invariant: selection_value(new) - selection_value(old) == economic_advantage(new, old).
    """
    if not all(math.isfinite(x) for x in (score, cost_usd, k)) or cost_usd <= 0 or k <= 0:
        raise ValueError("Score, cost, and K must be finite with cost > 0 and K > 0")
    return score - k * math.log2(cost_usd / 1.0)


@dataclass
class EligibleCandidate:
    """Benchmark variant matched to an executable catalog entry in the target system."""

    variant: VariantRecord
    provider: str
    backend: str
    model_id: str
    effort: str
    context_window: int
    capabilities: list[str]
    sel_value: float | None = None

    def to_seat(self, seat_id: str) -> SeatConfig:
        return SeatConfig(
            seat_id=seat_id,
            creator_slug=self.variant.creator_slug,
            creator_name=self.variant.creator_name,
            provider=self.provider,
            backend=self.backend,
            model_id=self.model_id,
            model_version=self.variant.model_version,
            effort=self.effort,
            agent_software=self.variant.agent_name,
            variant_id=self.variant.variant_id,
            coding_agent_index=self.variant.coding_agent_index,
            cost_per_task_usd=self.variant.cost_per_task_usd,
            index_version=self.variant.index_version,
            source_url=self.variant.source_url,
            context_window=self.context_window,
            capabilities=list(self.capabilities),
        )


@dataclass
class ProposedChange:
    """Proposed seat assignment evaluated before smoke testing."""

    seat_id: str
    active_seat: SeatConfig | None
    candidate: EligibleCandidate
    advantage: float | None
    cost_factor: float | None
    req_points: float | None
    should_switch: bool
    reason: str


@dataclass
class SelectionResult:
    """Full deterministic outcome of Frontier filtering and cost-benefit ranking."""

    index_version: str
    source_url: str
    frontier_reference_score: float | None
    frontier_floor: float | None
    creator_best_scores: dict[str, float]
    incomplete: bool
    incomplete_reason: str
    data_gaps: list[str] = field(default_factory=list)
    selected_by_creator: dict[str, EligibleCandidate] = field(default_factory=dict)
    proposed_changes: list[ProposedChange] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    top_frontier_seats: list[SeatConfig] = field(default_factory=list)


def _catalog_matches_variant(entry: dict[str, Any], variant: VariantRecord) -> bool:
    """Match a configured executable target entry to an Artificial Analysis variant."""
    if not entry.get("enabled", True):
        return False
    if variant.effort.lower() not in ALLOWED_EFFORTS:
        return False
    if entry.get("creator_slug") and entry["creator_slug"].lower() != variant.creator_slug.lower():
        return False
    if entry.get("agent_slug") and entry["agent_slug"].lower() != variant.agent_slug.lower():
        return False

    # Enforce effort allowlist on BOTH variant_id and release matches (prevents untrusted scraped effort bypass).
    allowed_efforts = entry.get("efforts")
    if isinstance(allowed_efforts, list) and allowed_efforts:
        norm_allowed = {str(e).strip().lower() for e in allowed_efforts}
        if variant.effort.lower() not in norm_allowed:
            return False
    elif entry.get("effort"):
        if str(entry["effort"]).strip().lower() != variant.effort.lower():
            return False

    if entry.get("variant_id"):
        return str(entry["variant_id"]) == variant.variant_id

    rel_match = str(entry.get("match_model_release", "")).strip().lower()
    if rel_match and rel_match != variant.model_release.strip().lower():
        return False
    return bool(rel_match)


def match_eligible_candidates(
    snapshot: BenchmarkSnapshot,
    config: dict[str, Any],
) -> tuple[list[EligibleCandidate], list[str]]:
    """Filter snapshot variants to those usable in the target system and report data gaps."""
    sel_cfg = config.get("selection", {})
    min_ctx = int(sel_cfg.get("min_context_window", 128000))
    req_caps = {str(c).lower() for c in sel_cfg.get("required_capabilities", [])}
    allow_routers = bool(sel_cfg.get("allow_routers", False))
    catalog = config.get("catalog", [])

    candidates: list[EligibleCandidate] = []
    data_gaps: list[str] = []

    for variant in snapshot.variants:
        if variant.is_unavailable:
            continue
        if variant.is_router and not allow_routers:
            continue

        matched_entry = None
        for entry in catalog:
            if _catalog_matches_variant(entry, variant):
                matched_entry = entry
                break
        if matched_entry is None:
            continue

        ctx = int(matched_entry.get("context_window", 0))
        caps = [str(c).lower() for c in matched_entry.get("capabilities", [])]
        if ctx < min_ctx:
            continue
        if not req_caps.issubset(set(caps)):
            continue

        # Check completeness of benchmark components.
        if (
            variant.index_component_count is not None
            and variant.eval_count is not None
            and variant.eval_count < variant.index_component_count
        ):
            data_gaps.append(
                f"Incomplete benchmark coverage for {variant.model_version} ({variant.effort}, {variant.agent_name}): "
                f"{variant.eval_count}/{variant.index_component_count} components."
            )
            continue

        if (
            variant.coding_agent_index is None
            or variant.cost_per_task_usd is None
            or not math.isfinite(variant.coding_agent_index)
            or not math.isfinite(variant.cost_per_task_usd)
            or variant.cost_per_task_usd <= 0.0
        ):
            missing = []
            if variant.coding_agent_index is None or not math.isfinite(variant.coding_agent_index):
                missing.append("score")
            if variant.cost_per_task_usd is None or not math.isfinite(variant.cost_per_task_usd) or variant.cost_per_task_usd <= 0.0:
                missing.append("cost_per_task_usd")
            data_gaps.append(
                f"Data gap ({', '.join(missing)}) for {variant.creator_name} / "
                f"{variant.model_version} ({variant.effort}, {variant.agent_name})."
            )
            continue

        clean_model_id = validate_model_id(str(matched_entry["model_id"]))
        clean_effort = validate_effort(variant.effort)
        candidates.append(
            EligibleCandidate(
                variant=variant,
                provider=str(matched_entry["provider"]),
                backend=str(matched_entry["backend"]),
                model_id=clean_model_id,
                effort=clean_effort,
                context_window=ctx,
                capabilities=caps,
            )
        )

    return candidates, data_gaps


def _sort_key_candidate(
    cand: EligibleCandidate,
    active_variant_ids: set[str],
) -> tuple[float, int, float, str]:
    """Deterministic ranking with exact tie-breakers:
    1. Highest selection_value
    2. Active variant takes precedence
    3. Lower cost_per_task_usd
    4. Alphabetically first unique variant_id
    """
    assert cand.sel_value is not None and cand.variant.cost_per_task_usd is not None
    # Round selection_value only at 1e-12 scale so exact mathematical ties (like 85@2 vs 80@1) trigger tie-breaker.
    snapped = round(cand.sel_value, 12)
    is_active = 1 if cand.variant.variant_id in active_variant_ids else 0
    return (-snapped, -is_active, cand.variant.cost_per_task_usd, cand.variant.variant_id)


def evaluate_selection(
    snapshot: BenchmarkSnapshot,
    config: dict[str, Any],
    active_lineup: Lineup | None = None,
) -> SelectionResult:
    """Run Frontier pre-filtering and K*log2(cost) selection deterministically."""
    sel_cfg = config.get("selection", {})
    k = float(sel_cfg.get("k_cost_doubling", 5.0))
    quality_buffer = float(sel_cfg.get("quality_buffer", 3.0))

    candidates, data_gaps = match_eligible_candidates(snapshot, config)

    # Index all snapshot variants by id for active-seat comparison on the SAME index_version.
    variant_by_id = {v.variant_id: v for v in snapshot.variants}
    active_seats = list(active_lineup.seats) if active_lineup else []
    active_variant_ids = {s.variant_id for s in active_seats if s.variant_id}

    # Step 6: Frontier level determination BEFORE cost trade-off.
    creator_best: dict[str, float] = {}
    for cand in candidates:
        assert cand.variant.coding_agent_index is not None
        cs = cand.variant.creator_slug
        sc = cand.variant.coding_agent_index
        if cs not in creator_best or sc > creator_best[cs]:
            creator_best[cs] = sc

    sorted_creator_scores = sorted(creator_best.values(), reverse=True)
    if len(sorted_creator_scores) < 3:
        reason = (
            f"Only {len(sorted_creator_scores)} distinct model creator(s) have reliable, usable data "
            f"(3 required for Peer Review)."
        )
        return SelectionResult(
            index_version=snapshot.index_version,
            source_url=snapshot.source_url,
            frontier_reference_score=None,
            frontier_floor=None,
            creator_best_scores=creator_best,
            incomplete=True,
            incomplete_reason=reason,
            data_gaps=data_gaps,
        )

    frontier_ref = sorted_creator_scores[2]  # 3rd strongest creator best score
    frontier_floor = frontier_ref - quality_buffer

    # Pre-filter to Frontier variants (score >= frontier_floor).
    frontier_candidates: list[EligibleCandidate] = []
    for cand in candidates:
        assert cand.variant.coding_agent_index is not None and cand.variant.cost_per_task_usd is not None
        if cand.variant.coding_agent_index + EPSILON >= frontier_floor:
            cand.sel_value = selection_value(
                cand.variant.coding_agent_index,
                cand.variant.cost_per_task_usd,
                k=k,
            )
            frontier_candidates.append(cand)

    # Pick highest selection_value variant per creator (enforcing same agent/index comparability).
    by_creator: dict[str, list[EligibleCandidate]] = {}
    for cand in frontier_candidates:
        by_creator.setdefault(cand.variant.creator_slug, []).append(cand)

    selected_by_creator: dict[str, EligibleCandidate] = {}
    decisions: list[DecisionRecord] = []

    for cs, cands in by_creator.items():
        cands_sorted = sorted(cands, key=lambda c: _sort_key_candidate(c, active_variant_ids))
        winner = cands_sorted[0]
        selected_by_creator[cs] = winner

        # Detect top-scoring variant within the same creator & agent that was rejected on cost grounds.
        same_agent_cands = [
            c for c in cands if c.variant.agent_slug == winner.variant.agent_slug
        ]
        if same_agent_cands:
            peak = max(same_agent_cands, key=lambda c: c.variant.coding_agent_index or -1.0)
            if (
                peak.variant.variant_id != winner.variant.variant_id
                and (peak.variant.coding_agent_index or 0.0) > (winner.variant.coding_agent_index or 0.0)
                and peak.variant.cost_per_task_usd is not None
                and winner.variant.cost_per_task_usd is not None
            ):
                adv = economic_advantage(
                    peak.variant.coding_agent_index,  # type: ignore[arg-type]
                    peak.variant.cost_per_task_usd,
                    winner.variant.coding_agent_index,  # type: ignore[arg-type]
                    winner.variant.cost_per_task_usd,
                    k=k,
                )
                req_pts = required_extra_points(
                    peak.variant.cost_per_task_usd,
                    winner.variant.cost_per_task_usd,
                    k=k,
                )
                decisions.append(
                    DecisionRecord(
                        kind="rejected_peak",
                        seat_id=f"creator:{cs}",
                        creator_name=winner.variant.creator_name,
                        active_model=winner.variant.model_release,
                        active_effort=winner.effort,
                        active_score=winner.variant.coding_agent_index,
                        active_cost_usd=winner.variant.cost_per_task_usd,
                        candidate_model=peak.variant.model_release,
                        candidate_effort=peak.effort,
                        candidate_score=peak.variant.coding_agent_index,
                        candidate_cost_usd=peak.variant.cost_per_task_usd,
                        cost_factor=peak.variant.cost_per_task_usd / winner.variant.cost_per_task_usd,
                        economic_advantage=adv,
                        required_extra_points=req_pts,
                        k_cost_doubling=k,
                        index_version=snapshot.index_version,
                        source_url=snapshot.source_url,
                        reason="Higher-scoring variant rejected due to cost-benefit rule.",
                        candidate_creator_name=peak.variant.creator_name,
                    )
                )

    if len(selected_by_creator) < 3:
        return SelectionResult(
            index_version=snapshot.index_version,
            source_url=snapshot.source_url,
            frontier_reference_score=frontier_ref,
            frontier_floor=frontier_floor,
            creator_best_scores=creator_best,
            incomplete=True,
            incomplete_reason=(
                f"Only {len(selected_by_creator)} creator(s) passed Frontier floor {frontier_floor:.2f} "
                f"with valid cost data."
            ),
            data_gaps=data_gaps,
            selected_by_creator=selected_by_creator,
            decisions=decisions,
        )

    ranked_creators = sorted(
        selected_by_creator.values(),
        key=lambda c: _sort_key_candidate(c, active_variant_ids),
    )

    # Initial staffing: top 3 distinct creators.
    if not active_seats:
        top3 = ranked_creators[:3]
        proposed: list[ProposedChange] = []
        seats: list[SeatConfig] = []
        for idx, cand in enumerate(top3, 1):
            seat_id = f"seat-{idx}"
            seats.append(cand.to_seat(seat_id))
            proposed.append(
                ProposedChange(
                    seat_id=seat_id,
                    active_seat=None,
                    candidate=cand,
                    advantage=None,
                    cost_factor=None,
                    req_points=None,
                    should_switch=True,
                    reason="Initial frontier staffing.",
                )
            )
            decisions.append(
                DecisionRecord(
                    kind="initial",
                    seat_id=seat_id,
                    creator_name=cand.variant.creator_name,
                    active_model="",
                    active_effort="",
                    active_score=None,
                    active_cost_usd=None,
                    candidate_model=cand.variant.model_release,
                    candidate_effort=cand.effort,
                    candidate_score=cand.variant.coding_agent_index,
                    candidate_cost_usd=cand.variant.cost_per_task_usd,
                    cost_factor=None,
                    economic_advantage=None,
                    required_extra_points=None,
                    k_cost_doubling=k,
                    index_version=snapshot.index_version,
                    source_url=snapshot.source_url,
                    reason="Initial frontier selection.",
                    candidate_creator_name=cand.variant.creator_name,
                )
            )
        return SelectionResult(
            index_version=snapshot.index_version,
            source_url=snapshot.source_url,
            frontier_reference_score=frontier_ref,
            frontier_floor=frontier_floor,
            creator_best_scores=creator_best,
            incomplete=False,
            incomplete_reason="",
            data_gaps=data_gaps,
            selected_by_creator=selected_by_creator,
            proposed_changes=proposed,
            decisions=decisions,
            top_frontier_seats=seats,
        )

    # Update staffing against concrete active seats on the SAME snapshot index_version.
    def _active_metrics_on_snapshot(seat: SeatConfig) -> tuple[float | None, float | None, str | None]:
        """Resolve active seat's current score and cost on the SAME index_version."""
        live = variant_by_id.get(seat.variant_id)
        if live is not None:
            if live.coding_agent_index is None or live.cost_per_task_usd is None:
                return None, None, f"Active seat {seat.seat_id} ({seat.model_version}, {seat.effort}) lacks reliable score or cost on {snapshot.index_version}."
            return live.coding_agent_index, live.cost_per_task_usd, None
        # Fallback match by creator + model_version + effort + agent on same index_version.
        for v in snapshot.variants:
            if (
                v.creator_slug == seat.creator_slug
                and v.effort == seat.effort
                and v.agent_name.lower() == seat.agent_software.lower()
                and v.model_version.lower() == seat.model_version.lower()
            ):
                if v.coding_agent_index is None or v.cost_per_task_usd is None:
                    return None, None, f"Active seat {seat.seat_id} lacks reliable score or cost on {snapshot.index_version}."
                return v.coding_agent_index, v.cost_per_task_usd, None
        # If index_version changed and active variant isn't present in new index_version, cannot cross-compare old vs new version!
        if seat.index_version and seat.index_version != snapshot.index_version:
            return (
                None,
                None,
                f"Active seat {seat.seat_id} ({seat.model_version}, {seat.effort}) not found on new index version {snapshot.index_version}; retaining seat.",
            )
        if seat.coding_agent_index is None or seat.cost_per_task_usd is None or seat.cost_per_task_usd <= 0:
            return None, None, f"Active seat {seat.seat_id} has missing benchmark metrics; retaining seat."
        return seat.coding_agent_index, seat.cost_per_task_usd, None

    proposed_changes: list[ProposedChange] = []
    matched_creators: set[str] = set()
    matched_seat_ids: set[str] = set()
    active_sel_values: dict[str, float] = {}
    replaceable_seats: list[SeatConfig] = []

    # 1. Match same-creator proposals to their existing active seat first.
    for seat in active_seats:
        act_score, act_cost, gap_msg = _active_metrics_on_snapshot(seat)
        if gap_msg:
            data_gaps.append(gap_msg)
            matched_seat_ids.add(seat.seat_id)
            if seat.creator_slug in selected_by_creator:
                matched_creators.add(seat.creator_slug)
            decisions.append(
                DecisionRecord(
                    kind="hold_data_gap",
                    seat_id=seat.seat_id,
                    creator_name=seat.creator_name,
                    active_model=seat.model_version,
                    active_effort=seat.effort,
                    active_score=act_score,
                    active_cost_usd=act_cost,
                    candidate_model="",
                    candidate_effort="",
                    candidate_score=None,
                    candidate_cost_usd=None,
                    cost_factor=None,
                    economic_advantage=None,
                    required_extra_points=None,
                    k_cost_doubling=k,
                    index_version=snapshot.index_version,
                    source_url=snapshot.source_url,
                    reason=gap_msg,
                )
            )
            continue

        assert act_score is not None and act_cost is not None
        active_sel_values[seat.seat_id] = selection_value(act_score, act_cost, k=k)

        same_creator_cand = selected_by_creator.get(seat.creator_slug)
        if same_creator_cand is not None:
            matched_creators.add(seat.creator_slug)
            matched_seat_ids.add(seat.seat_id)
            replaceable_seats.append(seat)
            if same_creator_cand.variant.variant_id == seat.variant_id:
                continue
            assert same_creator_cand.variant.coding_agent_index is not None
            assert same_creator_cand.variant.cost_per_task_usd is not None
            # For same model effort comparison, require same agent_software & index_version.
            seat_release_norm = seat.model_version.split("(")[0].strip().lower()
            cand_release_norm = same_creator_cand.variant.model_release.strip().lower()
            if (
                cand_release_norm == seat_release_norm
                and seat.agent_software
                and same_creator_cand.variant.agent_name.lower() != seat.agent_software.lower()
            ):
                data_gaps.append(
                    f"Effort comparison skipped for {seat.model_version}: different agent software "
                    f"({seat.agent_software} vs {same_creator_cand.variant.agent_name})."
                )
                continue

            adv = economic_advantage(
                same_creator_cand.variant.coding_agent_index,
                same_creator_cand.variant.cost_per_task_usd,
                act_score,
                act_cost,
                k=k,
            )
            factor = same_creator_cand.variant.cost_per_task_usd / act_cost
            req_pts = required_extra_points(same_creator_cand.variant.cost_per_task_usd, act_cost, k=k)
            should_switch = adv > EPSILON
            proposed_changes.append(
                ProposedChange(
                    seat_id=seat.seat_id,
                    active_seat=seat,
                    candidate=same_creator_cand,
                    advantage=adv,
                    cost_factor=factor,
                    req_points=req_pts,
                    should_switch=should_switch,
                    reason=(
                        "Positive economic advantage within same creator."
                        if should_switch
                        else "Economic advantage <= 0; keeping active variant."
                    ),
                )
            )
            # Track effective post-same-creator selection value for potential cross-creator replacement check.
            effective_val = (
                same_creator_cand.sel_value
                if (should_switch and same_creator_cand.sel_value is not None)
                else active_sel_values[seat.seat_id]
            )
            active_sel_values[seat.seat_id] = effective_val
        else:
            replaceable_seats.append(seat)

    # 2. Compare new creators (descending selection_value) against remaining replaceable seats (ascending selection_value).
    # Seats whose creator dropped below Frontier are prioritized for replacement before healthy same-creator seats.
    new_creator_cands = [
        c for c in ranked_creators if c.variant.creator_slug not in matched_creators
    ]
    replaceable_sorted = sorted(
        replaceable_seats,
        key=lambda s: (
            0 if s.creator_slug not in selected_by_creator else 1,
            active_sel_values.get(s.seat_id, float("inf")),
        ),
    )

    # Map existing same-creator proposals by seat_id in case a new creator outperforms even the best same-creator option.
    proposal_by_seat = {p.seat_id: p for p in proposed_changes}

    for cand, seat in zip(new_creator_cands, replaceable_sorted):
        # Baseline is either the accepted same-creator upgrade or the active seat itself.
        existing_prop = proposal_by_seat.get(seat.seat_id)
        if existing_prop and existing_prop.should_switch:
            base_score = existing_prop.candidate.variant.coding_agent_index
            base_cost = existing_prop.candidate.variant.cost_per_task_usd
            base_seat = existing_prop.candidate.to_seat(seat.seat_id)
        else:
            base_score, base_cost, _ = _active_metrics_on_snapshot(seat)
            base_seat = seat
        if base_score is None or base_cost is None:
            continue
        assert cand.variant.coding_agent_index is not None and cand.variant.cost_per_task_usd is not None
        adv = economic_advantage(
            cand.variant.coding_agent_index,
            cand.variant.cost_per_task_usd,
            base_score,
            base_cost,
            k=k,
        )
        factor = cand.variant.cost_per_task_usd / base_cost
        req_pts = required_extra_points(cand.variant.cost_per_task_usd, base_cost, k=k)
        if adv > EPSILON:
            proposal_by_seat[seat.seat_id] = ProposedChange(
                seat_id=seat.seat_id,
                active_seat=seat,
                candidate=cand,
                advantage=adv,
                cost_factor=factor,
                req_points=req_pts,
                should_switch=True,
                reason=f"New creator {cand.variant.creator_name} beats seat {seat.seat_id} ({base_seat.creator_name}).",
            )

    final_proposals = list(proposal_by_seat.values())
    top3_seats = [c.to_seat(f"seat-{i}") for i, c in enumerate(ranked_creators[:3], 1)]

    return SelectionResult(
        index_version=snapshot.index_version,
        source_url=snapshot.source_url,
        frontier_reference_score=frontier_ref,
        frontier_floor=frontier_floor,
        creator_best_scores=creator_best,
        incomplete=False,
        incomplete_reason="",
        data_gaps=data_gaps,
        selected_by_creator=selected_by_creator,
        proposed_changes=final_proposals,
        decisions=decisions,
        top_frontier_seats=top3_seats,
    )
