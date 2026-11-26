"""Daily benchmark check, pre-activation smoke tests, atomic switch, and notifications."""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .aa_scraper import ScrapeError, fetch_artificial_analysis_snapshot
from .adapters import ModelResponse, invoke_model
from .config import ensure_private_dir, redact_secrets, state_lock, write_private_file
from .lineup_store import LineupStore
from .models import BenchmarkSnapshot, DecisionRecord, Lineup, SeatConfig
from .notifier import DecisionNotifier
from .selector import evaluate_selection, match_eligible_candidates, selection_value as calc_sel_value
from .smoke_test import SmokeTestResult, run_candidate_smoke_test


@dataclass
class UpdateOutcome:
    ok: bool
    changed: bool
    incomplete: bool
    index_version: str
    source_url: str
    active_lineup: dict[str, Any] | None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    emitted_notifications: list[dict[str, Any]] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_lineup_update(
    config: dict[str, Any],
    *,
    snapshot: BenchmarkSnapshot | None = None,
    dry_run: bool = False,
    skip_smoke_test: bool = False,
    invoker: Callable[..., ModelResponse] = invoke_model,
    smoke_tester: Callable[..., SmokeTestResult] | None = None,
) -> UpdateOutcome:
    """Fetch Artificial Analysis data, apply Frontier + K*log2 rule, smoke-test, and activate atomically."""
    if skip_smoke_test and not dry_run:
        raise ValueError("skip_smoke_test is only permitted with dry_run=True")

    state_dir = ensure_private_dir(Path(config["state_dir"]))
    snapshots_dir = ensure_private_dir(state_dir / "snapshots")
    store = LineupStore(state_dir)
    notifier = DecisionNotifier(state_dir, config)

    # Acquire exclusive lock across snapshot read-evaluate-smoke-activate to prevent overlapping daily jobs.
    with state_lock(state_dir):
        active = store.load_active()

        # 1. Fetch benchmark snapshot (on API/scrape error, preserve active lineup untouched).
        try:
            snap = snapshot or fetch_artificial_analysis_snapshot(config)
        except (ScrapeError, ValueError, OSError, TypeError) as exc:
            return UpdateOutcome(
                ok=False,
                changed=False,
                incomplete=active.incomplete if active else True,
                index_version=active.index_version if active else "",
                source_url=str(config.get("source", {}).get("url", "")),
                active_lineup=active.to_dict() if active else None,
                error=redact_secrets(f"Benchmark fetch failed; active lineup unchanged: {exc}"),
            )

        snap_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        snap_name = f"snapshot-{snap_ts}-{secrets.token_hex(4)}.json"
        write_private_file(
            snapshots_dir / snap_name,
            json.dumps(snap.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )

        # 2. Deterministic Frontier filtering + K*log2(cost) selection.
        sel = evaluate_selection(snap, config, active_lineup=active)
        base_decisions: list[DecisionRecord] = [d for d in sel.decisions if d.kind != "initial"]

        if sel.incomplete:
            emitted = [] if dry_run else _notify_unlocked(notifier, base_decisions)
            return UpdateOutcome(
                ok=False,
                changed=False,
                incomplete=True,
                index_version=snap.index_version,
                source_url=snap.source_url,
                active_lineup=active.to_dict() if active else None,
                decisions=[d.to_dict() for d in base_decisions],
                emitted_notifications=emitted,
                data_gaps=sel.data_gaps,
                error=sel.incomplete_reason,
            )

        sel_cfg = config.get("selection", {})
        k = float(sel_cfg.get("k_cost_doubling", 5.0))
        quality_buffer = float(sel_cfg.get("quality_buffer", 3.0))

        tester = smoke_tester or (
            lambda seat, cfg: run_candidate_smoke_test(seat, cfg, invoker=invoker)
        )

        # For initial staffing only: ranked fallback candidates per creator inside the Frontier floor.
        initial_fallbacks_by_creator: dict[str, list[SeatConfig]] = {}
        if active is None and sel.frontier_floor is not None:
            all_elig, _ = match_eligible_candidates(snap, config)
            for cand in all_elig:
                if (
                    cand.variant.coding_agent_index is not None
                    and cand.variant.cost_per_task_usd is not None
                    and cand.variant.coding_agent_index + 1e-12 >= sel.frontier_floor
                ):
                    cand.sel_value = calc_sel_value(
                        cand.variant.coding_agent_index, cand.variant.cost_per_task_usd, k=k
                    )
                    initial_fallbacks_by_creator.setdefault(cand.variant.creator_slug, []).append(
                        cand.to_seat("pending")
                    )
            for seat_list in initial_fallbacks_by_creator.values():
                seat_list.sort(
                    key=lambda s: (
                        -round(
                            calc_sel_value(s.coding_agent_index or 0.0, s.cost_per_task_usd or 1.0, k=k),
                            12,
                        ),
                        s.cost_per_task_usd or 0.0,
                        s.variant_id,
                    )
                )

        # 3. Smoke-test proposed changes before ANY activation.
        seat_map: dict[str, SeatConfig] = {s.seat_id: s for s in active.seats} if active else {}
        any_switch = False
        initial_mode = active is None
        pending_activation_decisions: list[DecisionRecord] = []
        hold_decisions: list[DecisionRecord] = []

        for prop in sel.proposed_changes:
            if not prop.should_switch:
                continue
            primary_seat = prop.candidate.to_seat(prop.seat_id)
            try_seats = [primary_seat]
            if initial_mode:
                for fb in initial_fallbacks_by_creator.get(primary_seat.creator_slug, []):
                    if fb.variant_id != primary_seat.variant_id:
                        try_seats.append(SeatConfig(**{**fb.to_dict(), "seat_id": prop.seat_id}))

            for cand_seat in try_seats:
                if skip_smoke_test:
                    smoke = SmokeTestResult(
                        passed=True,
                        detail="Smoke test skipped in dry-run mode.",
                        packet_sha256="skipped",
                    )
                else:
                    smoke = tester(cand_seat, config)

                if smoke.passed:
                    seat_map[prop.seat_id] = cand_seat
                    any_switch = True
                    if initial_mode:
                        pending_activation_decisions.append(
                            DecisionRecord(
                                kind="initial",
                                seat_id=prop.seat_id,
                                creator_name=cand_seat.creator_name,
                                candidate_creator_name=cand_seat.creator_name,
                                active_model="",
                                active_effort="",
                                active_score=None,
                                active_cost_usd=None,
                                candidate_model=cand_seat.model_version,
                                candidate_effort=cand_seat.effort,
                                candidate_score=cand_seat.coding_agent_index,
                                candidate_cost_usd=cand_seat.cost_per_task_usd,
                                cost_factor=None,
                                economic_advantage=None,
                                required_extra_points=None,
                                k_cost_doubling=k,
                                index_version=snap.index_version,
                                source_url=snap.source_url,
                                smoke_test_passed=True,
                                smoke_test_detail=smoke.detail,
                                reason="Initial frontier selection verified by smoke test.",
                            )
                        )
                    elif prop.active_seat is not None:
                        pending_activation_decisions.append(
                            DecisionRecord(
                                kind="switch",
                                seat_id=prop.seat_id,
                                creator_name=prop.active_seat.creator_name,
                                candidate_creator_name=cand_seat.creator_name,
                                active_model=prop.active_seat.model_version,
                                active_effort=prop.active_seat.effort,
                                active_score=prop.active_seat.coding_agent_index,
                                active_cost_usd=prop.active_seat.cost_per_task_usd,
                                candidate_model=cand_seat.model_version,
                                candidate_effort=cand_seat.effort,
                                candidate_score=cand_seat.coding_agent_index,
                                candidate_cost_usd=cand_seat.cost_per_task_usd,
                                cost_factor=prop.cost_factor,
                                economic_advantage=prop.advantage,
                                required_extra_points=prop.req_points,
                                k_cost_doubling=k,
                                index_version=snap.index_version,
                                source_url=snap.source_url,
                                smoke_test_passed=True,
                                smoke_test_detail=smoke.detail,
                                reason=prop.reason,
                            )
                        )
                    break
                else:
                    act = prop.active_seat
                    hold_decisions.append(
                        DecisionRecord(
                            kind="hold_smoke_failed",
                            seat_id=prop.seat_id,
                            creator_name=act.creator_name if act else cand_seat.creator_name,
                            candidate_creator_name=cand_seat.creator_name,
                            active_model=act.model_version if act else "",
                            active_effort=act.effort if act else "",
                            active_score=act.coding_agent_index if act else None,
                            active_cost_usd=act.cost_per_task_usd if act else None,
                            candidate_model=cand_seat.model_version,
                            candidate_effort=cand_seat.effort,
                            candidate_score=cand_seat.coding_agent_index,
                            candidate_cost_usd=cand_seat.cost_per_task_usd,
                            cost_factor=prop.cost_factor,
                            economic_advantage=prop.advantage,
                            required_extra_points=prop.req_points,
                            k_cost_doubling=k,
                            index_version=snap.index_version,
                            source_url=snap.source_url,
                            smoke_test_passed=False,
                            smoke_test_detail=smoke.detail,
                            reason="Smoke test failed; previous active configuration retained.",
                        )
                    )

        # 4. Validate 3-distinct-creators invariant BEFORE activating or emitting switch notifications.
        new_seats = [seat_map[k_id] for k_id in sorted(seat_map.keys())]
        distinct_creators = {s.creator_slug for s in new_seats}

        activated_lineup = active
        changed = False
        final_decisions = list(base_decisions) + list(hold_decisions)

        if any_switch and len(new_seats) == 3 and len(distinct_creators) == 3:
            candidate_lineup = Lineup(
                schema_version="1.0",
                revision=(active.revision + 1) if active else 1,
                activated_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                index_version=snap.index_version,
                source_url=snap.source_url,
                k_cost_doubling=k,
                quality_buffer=quality_buffer,
                seats=new_seats,
                incomplete=False,
                incomplete_reason="",
            )
            if dry_run:
                activated_lineup = candidate_lineup
                changed = True
                final_decisions.extend(pending_activation_decisions)
            else:
                activated_lineup = store._activate_unlocked(candidate_lineup)
                changed = True
                final_decisions.extend(pending_activation_decisions)
        elif initial_mode and (len(new_seats) < 3 or len(distinct_creators) < 3):
            emitted = [] if dry_run else _notify_unlocked(notifier, final_decisions)
            return UpdateOutcome(
                ok=False,
                changed=False,
                incomplete=True,
                index_version=snap.index_version,
                source_url=snap.source_url,
                active_lineup=None,
                decisions=[d.to_dict() for d in final_decisions],
                emitted_notifications=emitted,
                data_gaps=sel.data_gaps,
                error=f"Initial staffing failed smoke test(s): only {len(distinct_creators)}/3 creators verified.",
            )

        emitted = [] if dry_run else _notify_unlocked(notifier, final_decisions)
        return UpdateOutcome(
            ok=True,
            changed=changed,
            incomplete=False,
            index_version=snap.index_version,
            source_url=snap.source_url,
            active_lineup=activated_lineup.to_dict() if activated_lineup else None,
            decisions=[d.to_dict() for d in final_decisions],
            emitted_notifications=emitted,
            data_gaps=sel.data_gaps,
        )


def _notify_unlocked(notifier: DecisionNotifier, decisions: list[DecisionRecord]) -> list[dict[str, Any]]:
    """Emit notifications while state_lock is already held by run_lineup_update."""
    # Re-use DecisionNotifier internals without re-acquiring flock on the same process fd.
    from .notifier import decision_fingerprint, format_decision_message

    seen = notifier._load_seen()
    emitted: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for dec in decisions:
        fp = decision_fingerprint(dec)
        if fp in seen:
            continue
        msg = format_decision_message(dec, language=notifier.language)
        safe_record = {
            k: redact_secrets(v) if isinstance(v, str) else v
            for k, v in dec.to_dict().items()
        }
        webhook_ok, webhook_status = notifier._send_webhook(msg, safe_record)
        entry = {
            "timestamp_utc": now,
            "fingerprint": fp,
            "kind": dec.kind,
            "message": msg,
            "webhook_sent": webhook_ok,
            "webhook_status": webhook_status,
            "decision": safe_record,
        }
        if notifier.cfg.get("local_log", True):
            notifier._append_private(notifier.jsonl_path, json.dumps(entry, ensure_ascii=False) + "\n")
            notifier._append_private(notifier.md_path, f"## {now} — {dec.kind}\n\n{msg}\n\n---\n\n")
        seen[fp] = now
        emitted.append(entry)
    if emitted:
        notifier._save_seen(seen)
    return emitted
