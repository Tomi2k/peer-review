"""Multi-stage Peer Review orchestrator with frozen context packets and fault tolerance."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .adapters import AdapterError, ModelResponse, invoke_model
from .config import (
    ensure_private_dir,
    redact_secrets,
    sanitize_single_line,
    sanitize_terminal_text,
    validate_run_id,
    write_private_file,
)
from .context_packet import ContextPacket, build_context_packet
from .lineup_store import LineupStore
from .models import Lineup, SeatConfig
from .prompts import (
    build_clarification_check_prompt,
    build_clarification_round_prompt,
    build_moderator_synthesis_prompt,
    build_peer_critique_prompt,
    build_revision_prompt,
    build_round1_prompt,
)


@dataclass
class RunResult:
    run_id: str
    started_at_utc: str
    finished_at_utc: str
    packet_sha256: str
    complete: bool
    incomplete_reason: str
    frozen_lineup: list[dict[str, Any]]
    round1: dict[str, str] = field(default_factory=dict)
    critiques: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, str] = field(default_factory=dict)
    clarification_triggered: bool = False
    clarification_question: str = ""
    clarifications: dict[str, str] = field(default_factory=dict)
    failed_seats: list[dict[str, str]] = field(default_factory=list)
    final_answer: str = ""
    artifact_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_clarification_decision(raw_text: str) -> tuple[bool, str, str]:
    """Parse strict JSON returned by Moderator clarification check."""
    text = raw_text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return False, "", ""
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return False, "", ""
    needs = bool(obj.get("needs_clarification", False))
    dispute = str(obj.get("dispute_summary") or "").strip()
    question = str(obj.get("targeted_question") or "").strip()
    if needs and question:
        return True, dispute, question
    return False, "", ""


def _run_parallel_stage(
    seats: list[SeatConfig],
    prompts_by_seat: dict[str, str],
    config: dict[str, Any],
    stage_name: str,
    invoker: Callable[..., ModelResponse],
    cwd: str | None,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Execute one stage across seats concurrently and isolate failures."""
    outputs: dict[str, str] = {}
    failures: list[dict[str, str]] = []
    if not seats:
        return outputs, failures

    def _call(seat: SeatConfig) -> tuple[str, str]:
        resp = invoker(
            provider=seat.provider,
            backend=seat.backend,
            model_id=seat.model_id,
            effort=seat.effort,
            prompt=prompts_by_seat[seat.seat_id],
            config=config,
            cwd=cwd,
        )
        return seat.seat_id, resp.text

    with ThreadPoolExecutor(max_workers=len(seats)) as pool:
        future_map = {pool.submit(_call, seat): seat for seat in seats}
        for fut in as_completed(future_map):
            seat = future_map[fut]
            try:
                sid, text = fut.result()
                outputs[sid] = redact_secrets(text)
            except (AdapterError, ValueError, OSError, RuntimeError) as exc:
                failures.append(
                    {
                        "stage": stage_name,
                        "seat_id": seat.seat_id,
                        "creator": seat.creator_name,
                        "model_id": seat.model_id,
                        "effort": seat.effort,
                        "error": redact_secrets(str(exc)),
                    }
                )
    return outputs, failures


def run_peer_review(
    *,
    task: str,
    config: dict[str, Any],
    file_paths: list[str | Path] | None = None,
    constraints: list[str] | None = None,
    output_format: str = "",
    extra_context: str = "",
    task_type: str = "coding",
    lineup: Lineup | None = None,
    cwd: str | None = None,
    invoker: Callable[..., ModelResponse] = invoke_model,
    skip_clarification: bool = False,
) -> RunResult:
    """Execute a full 3-provider Peer Review with frozen context packet and frozen lineup."""
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store = LineupStore(config["state_dir"])
    active_lineup = lineup or store.load_active()
    if active_lineup is None or not active_lineup.seats:
        raise RuntimeError(
            "No active lineup configured. Run `peer-review-update` first to initialize the 3-provider lineup."
        )

    # Freeze lineup snapshot in memory for the ENTIRE run (immune to concurrent lineup updates).
    frozen_seats = list(active_lineup.seats)
    distinct_creators = {s.creator_slug for s in frozen_seats}

    packet: ContextPacket = build_context_packet(
        task=task,
        seats=frozen_seats,
        file_paths=file_paths,
        constraints=constraints,
        output_format=output_format,
        extra_context=extra_context,
        task_type=task_type,
    )
    packet.verify_checksum()

    failed_seats: list[dict[str, str]] = []
    incomplete_reasons: list[str] = []
    if len(distinct_creators) < 3 or active_lineup.incomplete:
        incomplete_reasons.append(
            active_lineup.incomplete_reason
            or f"Active lineup has only {len(distinct_creators)} distinct provider(s); 3 required."
        )

    # Isolate child CLIs in an empty 0700 temp directory under state_dir when --cwd is omitted,
    # preventing untrusted workspace hooks/configs (.claude, .codex, .gemini) from executing.
    with contextlib.ExitStack() as stack:
        if cwd is None:
            tmp_root = str(ensure_private_dir(Path(config["state_dir"]) / "tmp"))
            effective_cwd = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="peer-review-run-", dir=tmp_root)
            )
            os.chmod(effective_cwd, 0o700)
        else:
            effective_cwd = cwd

        # Stage 1: Independent initial solutions in parallel (strictly isolated).
        packet.verify_checksum()
        r1_prompt = build_round1_prompt(packet)
        r1_prompts = {s.seat_id: r1_prompt for s in frozen_seats}
        round1, r1_fail = _run_parallel_stage(
            frozen_seats, r1_prompts, config, "round1_independent", invoker, effective_cwd
        )
        failed_seats.extend(r1_fail)

        surviving_r1 = [s for s in frozen_seats if s.seat_id in round1]

        # Stage 2a: Mutual Peer Critique (only if >= 2 solutions exist to cross-review).
        critiques: dict[str, str] = {}
        if len(surviving_r1) >= 2:
            packet.verify_checksum()
            crit_prompts = {
                s.seat_id: build_peer_critique_prompt(packet, s.seat_id, round1)
                for s in surviving_r1
            }
            critiques, c_fail = _run_parallel_stage(
                surviving_r1, crit_prompts, config, "round2_peer_critique", invoker, effective_cwd
            )
            failed_seats.extend(c_fail)

        # Stage 2b: Revision based on peer critiques.
        revisions: dict[str, str] = {}
        surviving_r2 = [s for s in surviving_r1 if s.seat_id in critiques]
        if len(surviving_r2) >= 2:
            packet.verify_checksum()
            rev_prompts: dict[str, str] = {}
            for seat in surviving_r2:
                incoming = {
                    other_id: crit_text
                    for other_id, crit_text in critiques.items()
                    if other_id != seat.seat_id
                }
                rev_prompts[seat.seat_id] = build_revision_prompt(
                    packet, round1[seat.seat_id], incoming
                )
            revisions, rev_fail = _run_parallel_stage(
                surviving_r2, rev_prompts, config, "round2_revision", invoker, effective_cwd
            )
            failed_seats.extend(rev_fail)
        else:
            revisions = dict(round1)

        mod_cfg = config.get("moderator", {})
        mod_mode = str(mod_cfg.get("mode") or "provider").strip().lower()
        max_clar = min(1, max(0, int(config.get("review", {}).get("max_clarification_rounds", 1))))

        def _call_moderator(prompt: str) -> str:
            resp = invoker(
                provider=str(mod_cfg["provider"]),
                backend=str(mod_cfg["backend"]),
                model_id=str(mod_cfg["model_id"]),
                effort=str(mod_cfg.get("effort", "high")),
                prompt=prompt,
                config=config,
                cwd=effective_cwd,
            )
            return redact_secrets(resp.text)

        # Stage 3 (optional): At most ONE Moderator-triggered targeted clarification round.
        clarification_triggered = False
        clarification_question = ""
        clarifications: dict[str, str] = {}
        if (
            not skip_clarification
            and max_clar == 1
            and mod_mode == "provider"
            and len(revisions) >= 2
        ):
            try:
                packet.verify_checksum()
                check_raw = _call_moderator(build_clarification_check_prompt(packet, revisions))
                needs, dispute, question = _parse_clarification_decision(check_raw)
                dispute = redact_secrets(dispute)
                question = redact_secrets(question)
                if needs and question:
                    clarification_triggered = True
                    clarification_question = question
                    clar_seats = [s for s in frozen_seats if s.seat_id in revisions]
                    clar_prompt = build_clarification_round_prompt(packet, question, dispute)
                    clar_prompts = {s.seat_id: clar_prompt for s in clar_seats}
                    clarifications, cl_fail = _run_parallel_stage(
                        clar_seats, clar_prompts, config, "round3_clarification", invoker, effective_cwd
                    )
                    failed_seats.extend(cl_fail)
            except (AdapterError, ValueError, OSError, RuntimeError) as exc:
                failed_seats.append(
                    {
                        "stage": "round3_clarification_check",
                        "seat_id": "moderator",
                        "creator": str(mod_cfg.get("provider", "moderator")),
                        "model_id": str(mod_cfg.get("model_id", "")),
                        "effort": str(mod_cfg.get("effort", "")),
                        "error": redact_secrets(str(exc)),
                    }
                )

        if failed_seats:
            names = ", ".join(f"{f['seat_id']}@{f['stage']}" for f in failed_seats)
            incomplete_reasons.append(f"Participant failure(s): {names}")

        is_complete = (len(incomplete_reasons) == 0) and (len(revisions) >= 3)
        incomplete_reason = "; ".join(incomplete_reasons)
        if not is_complete and not incomplete_reason:
            incomplete_reason = f"Only {len(revisions)} of 3 participants completed revisions."

        # Stage 4: Moderator synthesis.
        packet.verify_checksum()
        synth_prompt = build_moderator_synthesis_prompt(
            packet=packet,
            revisions=revisions,
            critiques=critiques,
            clarifications=clarifications if clarification_triggered else None,
            lineup_summary=packet.frozen_lineup,
            failed_seats=failed_seats,
        )

        if not revisions:
            final_answer = (
                "## Run Status: INCOMPLETE\n\n"
                "All participants failed during Round 1. No peer-reviewed answer could be produced.\n\n"
                f"Failures: {json.dumps(failed_seats, indent=2)}\n"
            )
        elif mod_mode == "caller":
            final_answer = (
                "<!-- MODERATOR_MODE=caller: synthesize using the bundle saved in artifact_dir -->\n"
                f"Run ID: {packet.run_id}\n"
                f"Packet SHA-256: {packet.packet_sha256}\n"
                f"Status: {'COMPLETE' if is_complete else 'INCOMPLETE — ' + incomplete_reason}\n"
            )
        else:
            try:
                final_answer = sanitize_terminal_text(redact_secrets(_call_moderator(synth_prompt)))
                if not is_complete and "incomplete" not in final_answer.lower() and "unvollständig" not in final_answer.lower():
                    final_answer = (
                        f"> **WARNING — RUN INCOMPLETE:** {sanitize_single_line(incomplete_reason, max_len=300)}\n\n" + final_answer
                    )
            except (AdapterError, ValueError, OSError, RuntimeError) as exc:
                safe_err = sanitize_single_line(redact_secrets(str(exc)), max_len=300)
                is_complete = False
                incomplete_reason = (incomplete_reason + "; " if incomplete_reason else "") + f"Moderator failed: {safe_err}"
                final_answer = (
                    f"> **WARNING — RUN INCOMPLETE (Moderator failed):** {safe_err}\n\n"
                    "Raw revised solutions are archived in the run directory.\n"
                )

    final_answer = sanitize_terminal_text(final_answer)
    incomplete_reason = sanitize_single_line(incomplete_reason, max_len=300)
    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_run_id = validate_run_id(packet.run_id)
    runs_root = ensure_private_dir(Path(config["state_dir"]) / "runs")
    run_dir = runs_root / safe_run_id
    if run_dir.parent != runs_root:
        raise ValueError(f"Refusing run_id outside runs directory: {packet.run_id!r}")
    ensure_private_dir(run_dir)

    result = RunResult(
        run_id=packet.run_id,
        started_at_utc=started,
        finished_at_utc=finished,
        packet_sha256=packet.packet_sha256,
        complete=is_complete,
        incomplete_reason=incomplete_reason,
        frozen_lineup=packet.frozen_lineup,
        round1=round1,
        critiques=critiques,
        revisions=revisions,
        clarification_triggered=clarification_triggered,
        clarification_question=clarification_question,
        clarifications=clarifications,
        failed_seats=failed_seats,
        final_answer=final_answer,
        artifact_dir=str(run_dir),
    )

    write_private_file(run_dir / "context_packet.json", json.dumps(packet.to_dict(), indent=2, ensure_ascii=False) + "\n")
    write_private_file(run_dir / "moderator_prompt.md", synth_prompt)
    write_private_file(run_dir / "run_result.json", json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n")
    write_private_file(run_dir / "final_answer.md", final_answer + "\n")
    return result
