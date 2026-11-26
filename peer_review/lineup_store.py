"""Atomic, lock-protected, reversible persistence for the active 3-seat Peer Review lineup."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import ensure_private_dir, read_regular_file_bounded, state_lock, write_private_file
from .models import Lineup

MAX_STATE_JSON_BYTES = 4 * 1024 * 1024  # 4 MiB cap on persisted state JSON files


class LineupStore:
    """Manages active_lineup.json and versioned history snapshots atomically."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = ensure_private_dir(Path(state_dir))
        self.active_path = self.state_dir / "active_lineup.json"
        self.history_dir = ensure_private_dir(self.state_dir / "history")

    def load_active(self) -> Lineup | None:
        try:
            raw_bytes = read_regular_file_bounded(self.active_path, MAX_STATE_JSON_BYTES)
        except FileNotFoundError:
            return None
        raw = json.loads(raw_bytes.decode("utf-8"))
        return Lineup.from_dict(raw)

    def activate(self, lineup: Lineup) -> Lineup:
        """Atomically activate a new lineup under an exclusive lock and archive the previous one."""
        with state_lock(self.state_dir):
            return self._activate_unlocked(lineup)

    def _activate_unlocked(self, lineup: Lineup) -> Lineup:
        current = self.load_active()
        next_rev = (current.revision + 1) if current else max(1, lineup.revision)
        activated = Lineup(
            schema_version=lineup.schema_version,
            revision=next_rev,
            activated_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            index_version=lineup.index_version,
            source_url=lineup.source_url,
            k_cost_doubling=lineup.k_cost_doubling,
            quality_buffer=lineup.quality_buffer,
            seats=list(lineup.seats),
            incomplete=lineup.incomplete,
            incomplete_reason=lineup.incomplete_reason,
        )
        if current is not None:
            hist_path = self.history_dir / f"lineup-rev-{current.revision:04d}.json"
            write_private_file(hist_path, json.dumps(current.to_dict(), indent=2) + "\n")

        write_private_file(self.active_path, json.dumps(activated.to_dict(), indent=2) + "\n")
        return activated

    def rollback(self, target_revision: int | None = None) -> Lineup:
        """Restore the previous (or specified) archived lineup atomically under lock."""
        with state_lock(self.state_dir):
            archived = sorted(self.history_dir.glob("lineup-rev-*.json"))
            if not archived:
                raise FileNotFoundError("No previous lineup revision found for rollback.")

            if target_revision is not None:
                chosen = self.history_dir / f"lineup-rev-{target_revision:04d}.json"
                if not chosen.exists():
                    raise FileNotFoundError(
                        f"Revision {target_revision} not found in {self.history_dir}"
                    )
            else:
                chosen = archived[-1]

            raw_bytes = read_regular_file_bounded(chosen, MAX_STATE_JSON_BYTES)
            restored = Lineup.from_dict(json.loads(raw_bytes.decode("utf-8")))
            return self._activate_unlocked(restored)
