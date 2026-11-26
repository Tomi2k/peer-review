"""Data models for Peer Review orchestration and Artificial Analysis selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .config import normalize_cost, normalize_score, sanitize_single_line, validate_https_url


def _safe_opt_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    return int(raw)


@dataclass(frozen=True)
class VariantRecord:
    """Single published benchmark variant from Artificial Analysis Coding Agent Index."""

    variant_id: str
    creator_slug: str
    creator_name: str
    model_release: str
    model_version: str
    agent_slug: str
    agent_name: str
    effort: str
    coding_agent_index: float | None  # 0..100 scale, full precision
    cost_per_task_usd: float | None  # published average API cost per task in USD
    index_version: str
    source_url: str
    fetched_at_utc: str
    is_unavailable: bool = False
    is_router: bool = False
    eval_count: int | None = None
    index_component_count: int | None = None
    cost_notes: list[str] = field(default_factory=list)
    raw_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_source_url: str = "") -> "VariantRecord":
        if not isinstance(data, dict):
            raise ValueError("VariantRecord JSON entry must be an object")
        raw_url = str(data.get("source_url") or default_source_url or "").strip()
        safe_url = validate_https_url(raw_url)
        raw_notes = data.get("cost_notes", [])
        if not isinstance(raw_notes, list):
            raise ValueError("VariantRecord.cost_notes must be a list")
        return cls(
            variant_id=sanitize_single_line(str(data["variant_id"]), max_len=128),
            creator_slug=sanitize_single_line(str(data["creator_slug"]), max_len=64).lower(),
            creator_name=sanitize_single_line(str(data["creator_name"]), max_len=80),
            model_release=sanitize_single_line(str(data.get("model_release", "")), max_len=120),
            model_version=sanitize_single_line(str(data.get("model_version", "")), max_len=120),
            agent_slug=sanitize_single_line(str(data.get("agent_slug", "")), max_len=64).lower(),
            agent_name=sanitize_single_line(str(data.get("agent_name", "")), max_len=80),
            effort=sanitize_single_line(str(data.get("effort", "")), max_len=24).lower(),
            coding_agent_index=normalize_score(data.get("coding_agent_index")),
            cost_per_task_usd=normalize_cost(data.get("cost_per_task_usd")),
            index_version=sanitize_single_line(str(data.get("index_version", "")), max_len=40),
            source_url=safe_url,
            fetched_at_utc=sanitize_single_line(str(data.get("fetched_at_utc", "")), max_len=40),
            is_unavailable=bool(data.get("is_unavailable", False)),
            is_router=bool(data.get("is_router", False)),
            eval_count=_safe_opt_int(data.get("eval_count")),
            index_component_count=_safe_opt_int(data.get("index_component_count")),
            cost_notes=[sanitize_single_line(str(x), max_len=160) for x in raw_notes],
            raw_label=sanitize_single_line(str(data.get("raw_label", "")), max_len=160),
        )


@dataclass(frozen=True)
class BenchmarkSnapshot:
    """Published snapshot from Artificial Analysis with provenance."""

    index_version: str
    source_url: str
    fetched_at_utc: str
    variants: list[VariantRecord]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_version": self.index_version,
            "source_url": self.source_url,
            "fetched_at_utc": self.fetched_at_utc,
            "notes": list(self.notes),
            "variants": [v.to_dict() for v in self.variants],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkSnapshot":
        if not isinstance(data, dict):
            raise ValueError("BenchmarkSnapshot JSON root must be an object")
        raw_variants = data.get("variants", [])
        if not isinstance(raw_variants, list):
            raise ValueError("BenchmarkSnapshot.variants must be a list")
        raw_notes = data.get("notes", [])
        if not isinstance(raw_notes, list):
            raise ValueError("BenchmarkSnapshot.notes must be a list")
        safe_url = validate_https_url(str(data.get("source_url", "") or "").strip())
        return cls(
            index_version=sanitize_single_line(str(data.get("index_version", "")), max_len=40),
            source_url=safe_url,
            fetched_at_utc=sanitize_single_line(str(data.get("fetched_at_utc", "")), max_len=40),
            notes=[sanitize_single_line(str(x), max_len=200) for x in raw_notes],
            variants=[VariantRecord.from_dict(v, default_source_url=safe_url) for v in raw_variants],
        )


@dataclass(frozen=True)
class SeatConfig:
    """One active seat in the 3-provider peer review panel."""

    seat_id: str
    creator_slug: str
    creator_name: str
    provider: str
    backend: str  # "api" | "cli" | "command" | "mock"
    model_id: str
    model_version: str
    effort: str
    agent_software: str
    variant_id: str
    coding_agent_index: float | None = None
    cost_per_task_usd: float | None = None
    index_version: str = ""
    source_url: str = ""
    context_window: int | None = None
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_source_url: str = "") -> "SeatConfig":
        if not isinstance(data, dict):
            raise ValueError("SeatConfig JSON entry must be an object")
        raw_url = str(data.get("source_url") or default_source_url or "").strip()
        safe_url = validate_https_url(raw_url) if raw_url else ""
        raw_caps = data.get("capabilities", [])
        if not isinstance(raw_caps, list):
            raise ValueError("SeatConfig.capabilities must be a list")
        return cls(
            seat_id=sanitize_single_line(str(data["seat_id"]), max_len=64),
            creator_slug=sanitize_single_line(str(data["creator_slug"]), max_len=64).lower(),
            creator_name=sanitize_single_line(str(data.get("creator_name", data["creator_slug"])), max_len=80),
            provider=sanitize_single_line(str(data["provider"]), max_len=64),
            backend=sanitize_single_line(str(data["backend"]), max_len=32),
            model_id=sanitize_single_line(str(data["model_id"]), max_len=128),
            model_version=sanitize_single_line(str(data.get("model_version", data["model_id"])), max_len=160),
            effort=sanitize_single_line(str(data["effort"]), max_len=24).lower(),
            agent_software=sanitize_single_line(str(data.get("agent_software", "")), max_len=80),
            variant_id=sanitize_single_line(str(data.get("variant_id", "")), max_len=128),
            coding_agent_index=normalize_score(data.get("coding_agent_index")),
            cost_per_task_usd=normalize_cost(data.get("cost_per_task_usd")),
            index_version=sanitize_single_line(str(data.get("index_version", "")), max_len=40),
            source_url=safe_url,
            context_window=_safe_opt_int(data.get("context_window")),
            capabilities=[sanitize_single_line(str(x), max_len=64) for x in raw_caps],
        )


@dataclass(frozen=True)
class Lineup:
    """Versioned active 3-seat lineup persisted atomically."""

    schema_version: str
    revision: int
    activated_at_utc: str
    index_version: str
    source_url: str
    k_cost_doubling: float
    quality_buffer: float
    seats: list[SeatConfig]
    incomplete: bool = False
    incomplete_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "activated_at_utc": self.activated_at_utc,
            "index_version": self.index_version,
            "source_url": self.source_url,
            "k_cost_doubling": self.k_cost_doubling,
            "quality_buffer": self.quality_buffer,
            "incomplete": self.incomplete,
            "incomplete_reason": self.incomplete_reason,
            "seats": [s.to_dict() for s in self.seats],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Lineup":
        if not isinstance(data, dict):
            raise ValueError("Lineup JSON root must be an object")
        raw_seats = data.get("seats", [])
        if not isinstance(raw_seats, list):
            raise ValueError("Lineup.seats must be a list")
        raw_url = str(data.get("source_url", "") or "").strip()
        safe_url = validate_https_url(raw_url) if raw_url else ""
        return cls(
            schema_version=sanitize_single_line(str(data.get("schema_version", "1.0")), max_len=24),
            revision=int(data.get("revision", 1)),
            activated_at_utc=sanitize_single_line(str(data.get("activated_at_utc", "")), max_len=40),
            index_version=sanitize_single_line(str(data.get("index_version", "")), max_len=40),
            source_url=safe_url,
            k_cost_doubling=float(data.get("k_cost_doubling", 5.0)),
            quality_buffer=float(data.get("quality_buffer", 3.0)),
            incomplete=bool(data.get("incomplete", False)),
            incomplete_reason=sanitize_single_line(str(data.get("incomplete_reason", "")), max_len=300),
            seats=[SeatConfig.from_dict(s, default_source_url=safe_url) for s in raw_seats],
        )


@dataclass(frozen=True)
class DecisionRecord:
    """Structured audit record for any model switch, hold, data gap, or rejected peak."""

    kind: str  # "switch" | "rejected_peak" | "hold_data_gap" | "hold_smoke_failed" | "initial"
    seat_id: str
    creator_name: str
    active_model: str
    active_effort: str
    active_score: float | None
    active_cost_usd: float | None
    candidate_model: str
    candidate_effort: str
    candidate_score: float | None
    candidate_cost_usd: float | None
    cost_factor: float | None
    economic_advantage: float | None
    required_extra_points: float | None
    k_cost_doubling: float
    index_version: str
    source_url: str
    smoke_test_passed: bool | None = None
    smoke_test_detail: str = ""
    reason: str = ""
    candidate_creator_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
