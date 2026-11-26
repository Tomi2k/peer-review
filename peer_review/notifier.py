"""Decision formatting, deduplication, durable local logging, and optional webhook delivery."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    NO_REDIRECT_OPENER,
    ensure_private_dir,
    read_regular_file_bounded,
    redact_secrets,
    require_nofollow,
    resolve_secret,
    sanitize_single_line,
    sanitize_url_for_log,
    state_lock,
    write_private_file,
)
from .models import DecisionRecord


def _fmt_num(value: float | None, decimals: int = 2, decimal_comma: bool = False) -> str:
    if value is None:
        return "n/a"
    s = f"{value:.{decimals}f}"
    return s.replace(".", ",") if decimal_comma else s


def _escape_slack_mrkdwn(text: str) -> str:
    """Prevent Slack mention/link injection (<!channel>, <!here>, <url|label>)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def decision_fingerprint(decision: DecisionRecord) -> str:
    """Deterministic SHA-256 fingerprint so unchanged decisions are only notified once."""
    payload = {
        "kind": decision.kind,
        "seat_id": decision.seat_id,
        "creator_name": decision.creator_name,
        "candidate_creator_name": decision.candidate_creator_name,
        "active_model": decision.active_model,
        "active_effort": decision.active_effort,
        "active_score": round(decision.active_score, 4) if decision.active_score is not None else None,
        "active_cost_usd": round(decision.active_cost_usd, 4) if decision.active_cost_usd is not None else None,
        "candidate_model": decision.candidate_model,
        "candidate_effort": decision.candidate_effort,
        "candidate_score": round(decision.candidate_score, 4) if decision.candidate_score is not None else None,
        "candidate_cost_usd": round(decision.candidate_cost_usd, 4) if decision.candidate_cost_usd is not None else None,
        "k_cost_doubling": round(decision.k_cost_doubling, 4),
        "index_version": decision.index_version,
        "smoke_test_passed": decision.smoke_test_passed,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def format_decision_message(decision: DecisionRecord, language: str = "en") -> str:
    """Format a clear, self-contained, redacted decision message in German or English."""
    de = language.strip().lower().startswith("de")
    gain = (
        decision.candidate_score - decision.active_score
        if decision.candidate_score is not None and decision.active_score is not None
        else None
    )
    creator = sanitize_single_line(decision.creator_name, 80)
    cand_creator = sanitize_single_line(decision.candidate_creator_name or creator, 80)
    act_model = sanitize_single_line(decision.active_model, 160)
    act_effort = sanitize_single_line(decision.active_effort, 24)
    cand_model = sanitize_single_line(decision.candidate_model, 160)
    cand_effort = sanitize_single_line(decision.candidate_effort, 24)
    src_url = sanitize_single_line(sanitize_url_for_log(decision.source_url), 200)
    idx_ver = sanitize_single_line(decision.index_version, 40)
    smoke_detail = sanitize_single_line(redact_secrets(decision.smoke_test_detail), 240)
    reason = sanitize_single_line(redact_secrets(decision.reason), 240)

    if de:
        if decision.kind == "rejected_peak":
            headline = (
                f"{act_effort.capitalize()} bleibt aktiv ({creator} {act_model}). "
                f"{cand_effort.capitalize()} ({cand_model}) erzielt "
                f"{_fmt_num(gain, 2, True)} zusätzliche Coding-Punkte und kostet pro Aufgabe "
                f"{_fmt_num(decision.candidate_cost_usd, 2, True)} statt {_fmt_num(decision.active_cost_usd, 2, True)} USD. "
                f"Für den {_fmt_num(decision.cost_factor, 2, True)}-fachen Preis wären nach deiner Regel "
                f"mehr als {_fmt_num(decision.required_extra_points, 2, True)} zusätzliche Punkte erforderlich."
            )
        elif decision.kind == "switch":
            headline = (
                f"Wechsel auf {decision.seat_id} aktiviert: {cand_creator} "
                f"{cand_model} ({cand_effort}) ersetzt {creator} {act_model} ({act_effort})."
            )
        elif decision.kind == "hold_smoke_failed":
            if act_model:
                headline = (
                    f"Wechsel auf {decision.seat_id} gestoppt (Funktionstest fehlgeschlagen): "
                    f"{creator} {act_model} ({act_effort}) bleibt aktiv."
                )
            else:
                headline = (
                    f"Erstbesetzung auf {decision.seat_id} gestoppt (Funktionstest fehlgeschlagen): "
                    f"{cand_creator} {cand_model} ({cand_effort})."
                )
        elif decision.kind == "hold_data_gap":
            headline = (
                f"Datenlücke auf {decision.seat_id}: {creator} {act_model} "
                f"({act_effort}) bleibt aktiv. {reason}"
            )
        else:
            headline = f"Erstbesetzung {decision.seat_id}: {creator} {cand_model} ({cand_effort})."

        details = (
            f"Hersteller: {creator}"
            + (f" -> {cand_creator}" if cand_creator and cand_creator != creator else "")
            + f" | Aktiv: {act_model or '-'} ({act_effort or '-'}) "
            f"Score {_fmt_num(decision.active_score, 2, True)}, Kosten {_fmt_num(decision.active_cost_usd, 2, True)} USD"
            + f" | Kandidat: {cand_model or '-'} ({cand_effort or '-'}) "
            f"Score {_fmt_num(decision.candidate_score, 2, True)}, Kosten {_fmt_num(decision.candidate_cost_usd, 2, True)} USD"
            + f" | Kostenfaktor: {_fmt_num(decision.cost_factor, 2, True)}x"
            + f" | Vorteil: {_fmt_num(decision.economic_advantage, 2, True)} (K={_fmt_num(decision.k_cost_doubling, 1, True)})"
            + f" | Quelle: {src_url} ({idx_ver})"
        )
        if decision.smoke_test_passed is not None:
            status = "bestanden" if decision.smoke_test_passed else "fehlgeschlagen"
            details += f" | Funktionstest: {status} ({smoke_detail})"
        return f"{headline}\n{details}"

    if decision.kind == "rejected_peak":
        headline = (
            f"{act_effort.capitalize()} stays active ({creator} {act_model}). "
            f"{cand_effort.capitalize()} ({cand_model}) gains "
            f"{_fmt_num(gain, 2)} Coding Index points and costs ${_fmt_num(decision.candidate_cost_usd, 2)} "
            f"instead of ${_fmt_num(decision.active_cost_usd, 2)} per task. "
            f"For a {_fmt_num(decision.cost_factor, 2)}x price increase, your rule requires more than "
            f"{_fmt_num(decision.required_extra_points, 2)} additional points."
        )
    elif decision.kind == "switch":
        headline = (
            f"Seat {decision.seat_id} switched: {cand_creator} "
            f"{cand_model} ({cand_effort}) replaces {creator} {act_model} ({act_effort})."
        )
    elif decision.kind == "hold_smoke_failed":
        if act_model:
            headline = (
                f"Seat {decision.seat_id} switch aborted (smoke test failed): "
                f"{creator} {act_model} ({act_effort}) stays active."
            )
        else:
            headline = (
                f"Initial staffing on {decision.seat_id} aborted (smoke test failed): "
                f"{cand_creator} {cand_model} ({cand_effort})."
            )
    elif decision.kind == "hold_data_gap":
        headline = f"Data gap on {decision.seat_id}: {creator} {act_model} ({act_effort}) stays active. {reason}"
    else:
        headline = f"Initial lineup {decision.seat_id}: {creator} {cand_model} ({cand_effort})."

    details = (
        f"Creator: {creator}"
        + (f" -> {cand_creator}" if cand_creator and cand_creator != creator else "")
        + f" | Active: {act_model or '-'} ({act_effort or '-'}) "
        f"score {_fmt_num(decision.active_score, 2)}, cost ${_fmt_num(decision.active_cost_usd, 2)}"
        + f" | Candidate: {cand_model or '-'} ({cand_effort or '-'}) "
        f"score {_fmt_num(decision.candidate_score, 2)}, cost ${_fmt_num(decision.candidate_cost_usd, 2)}"
        + f" | Cost factor: {_fmt_num(decision.cost_factor, 2)}x"
        + f" | Advantage: {_fmt_num(decision.economic_advantage, 2)} (K={_fmt_num(decision.k_cost_doubling, 1)})"
        + f" | Source: {src_url} ({idx_ver})"
    )
    if decision.smoke_test_passed is not None:
        status = "passed" if decision.smoke_test_passed else "failed"
        details += f" | Smoke test: {status} ({smoke_detail})"
    return f"{headline}\n{details}"


class DecisionNotifier:
    """Persists notifications locally with locking + SHA-256 deduplication and optional webhook delivery."""

    def __init__(self, state_dir: str | Path, config: dict[str, Any]):
        self.state_dir = ensure_private_dir(Path(state_dir))
        self.cfg = config.get("notifications", {})
        self.language = str(self.cfg.get("language") or config.get("language") or "en")
        self.seen_path = self.state_dir / "notified_decisions.json"
        self.jsonl_path = self.state_dir / "notifications.jsonl"
        self.md_path = self.state_dir / "notifications.md"

    def _load_seen(self) -> dict[str, str]:
        try:
            raw = read_regular_file_bounded(self.seen_path, 4 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def _save_seen(self, seen: dict[str, str]) -> None:
        write_private_file(self.seen_path, json.dumps(seen, indent=2, sort_keys=True) + "\n")

    def _send_webhook(self, message: str, record: dict[str, Any]) -> tuple[bool, str]:
        try:
            url = resolve_secret(
                env_var=self.cfg.get("webhook_url_env", "PEER_REVIEW_WEBHOOK_URL"),
                secret_command=self.cfg.get("webhook_url_command"),
            )
        except (ValueError, OSError, subprocess.SubprocessError):
            return False, "webhook_secret_error"
        if not url:
            return False, "no_webhook_configured"
        if re.search(r"[\x00-\x1f\x7f-\x9f\s]", url):
            return False, "webhook_rejected_malformed"
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False, "webhook_rejected_non_https"
        if parsed.username is not None or parsed.password is not None:
            return False, "webhook_rejected_userinfo"

        fmt = str(self.cfg.get("webhook_format") or "slack").lower()
        body = (
            {"text": _escape_slack_mrkdwn(message)}
            if fmt == "slack"
            else {"message": message, "decision": record}
        )
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with NO_REDIRECT_OPENER.open(req, timeout=20) as resp:
                return (200 <= resp.status < 300), f"http_{resp.status}"
        except urllib.error.HTTPError as exc:
            return False, f"http_{exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f"transport_error:{type(exc).__name__}"

    def notify(self, decisions: list[DecisionRecord]) -> list[dict[str, Any]]:
        """Send/log each unseen decision once under an exclusive state lock."""
        with state_lock(self.state_dir):
            seen = self._load_seen()
            emitted: list[dict[str, Any]] = []
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            for dec in decisions:
                fp = decision_fingerprint(dec)
                if fp in seen:
                    continue
                msg = format_decision_message(dec, language=self.language)
                safe_record = {
                    k: redact_secrets(v) if isinstance(v, str) else v
                    for k, v in dec.to_dict().items()
                }
                webhook_ok, webhook_status = self._send_webhook(msg, safe_record)
                entry = {
                    "timestamp_utc": now,
                    "fingerprint": fp,
                    "kind": dec.kind,
                    "message": msg,
                    "webhook_sent": webhook_ok,
                    "webhook_status": webhook_status,
                    "decision": safe_record,
                }
                if self.cfg.get("local_log", True):
                    self._append_private(self.jsonl_path, json.dumps(entry, ensure_ascii=False) + "\n")
                    md_block = f"## {now} — {dec.kind}\n\n{msg}\n\n---\n\n"
                    self._append_private(self.md_path, md_block)
                seen[fp] = now
                emitted.append(entry)

            if emitted:
                self._save_seen(seen)
            return emitted

    @staticmethod
    def _append_private(path: Path, text: str) -> None:
        nofollow = require_nofollow()
        if path.is_symlink():
            raise OSError(f"Refusing to append to symlink: {path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | nofollow | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"Refusing to append to non-regular file: {path}")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fd = -1  # ownership transferred to file object
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
