"""Reproducible pre-activation smoke test for candidate model + effort pairs."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from .adapters import AdapterError, ModelResponse, invoke_model
from .config import ensure_private_dir, redact_secrets
from .context_packet import build_context_packet
from .models import SeatConfig

SMOKE_TASK = (
    "Review the Python snippet below and answer in 3 short bullet points: "
    "(1) identify the exact bug, (2) show the 1-line fix, (3) state how to test it.\n"
    "```python\n"
    "def divide_discount(total: float, count: int) -> float:\n"
    "    return total / count\n"
    "```"
)


@dataclass(frozen=True)
class SmokeTestResult:
    passed: bool
    detail: str
    packet_sha256: str
    response_preview: str = ""


def run_candidate_smoke_test(
    seat: SeatConfig,
    config: dict[str, Any],
    *,
    invoker: Callable[..., ModelResponse] = invoke_model,
    timeout_seconds: int = 120,
) -> SmokeTestResult:
    """Verify that model_id + effort work on a frozen context packet and return a usable review."""
    packet_hash = "unbuilt"
    try:
        safe_creator = re.sub(r"[^A-Za-z0-9._-]", "-", str(seat.creator_slug or "unknown")).strip("-") or "unknown"
        safe_effort = re.sub(r"[^A-Za-z0-9._-]", "-", str(seat.effort or "default")).strip("-") or "default"
        packet = build_context_packet(
            task=SMOKE_TASK,
            seats=[seat],
            constraints=["Keep your answer under 120 words.", "Include the word ZERO_CHECK in your answer."],
            output_format="3 bullet points",
            task_type="coding",
            run_id=f"smoke-{safe_creator[:40]}-{safe_effort[:24]}",
        )
        packet.verify_checksum()
        packet_hash = packet.packet_sha256
        prompt = (
            "This is a read-only smoke test for a Peer Review seat.\n"
            "Follow every constraint strictly.\n\n"
            f"{packet.render_for_model()}\n"
        )
        tmp_root = str(ensure_private_dir(Path(config["state_dir"]) / "tmp"))
        with tempfile.TemporaryDirectory(prefix="peer-review-smoke-", dir=tmp_root) as clean_cwd:
            os.chmod(clean_cwd, 0o700)
            resp = invoker(
                provider=seat.provider,
                backend=seat.backend,
                model_id=seat.model_id,
                effort=seat.effort,
                prompt=prompt,
                config=config,
                cwd=clean_cwd,
                timeout_seconds=timeout_seconds,
            )
    except (AdapterError, ValueError, OSError) as exc:
        return SmokeTestResult(
            passed=False,
            detail=redact_secrets(f"Invocation failed for {seat.model_id} ({seat.effort}): {exc}"),
            packet_sha256=packet_hash,
        )

    text = (resp.text or "").strip()
    if len(text) < 30:
        return SmokeTestResult(
            passed=False,
            detail=f"Response too short ({len(text)} chars) for {seat.model_id} ({seat.effort}).",
            packet_sha256=packet.packet_sha256,
            response_preview=text,
        )
    lower = text.lower()
    if "zero_check" not in lower:
        return SmokeTestResult(
            passed=False,
            detail=f"Model {seat.model_id} ({seat.effort}) ignored packet constraint marker ZERO_CHECK.",
            packet_sha256=packet.packet_sha256,
            response_preview=text[:200],
        )
    if not any(kw in lower for kw in ("zero", "0", "division", "count", "zerodivision")):
        return SmokeTestResult(
            passed=False,
            detail=f"Model {seat.model_id} ({seat.effort}) did not identify the division-by-zero defect.",
            packet_sha256=packet.packet_sha256,
            response_preview=text[:200],
        )

    return SmokeTestResult(
        passed=True,
        detail=f"Smoke test passed for {seat.model_id} ({seat.effort}), packet {packet.packet_sha256[:12]}.",
        packet_sha256=packet.packet_sha256,
        response_preview=text[:200],
    )
