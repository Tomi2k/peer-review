"""Command-line entrypoints for Peer Review execution and daily model updates."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Support direct execution via `python3 -I peer_review/cli.py`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from peer_review.config import load_config, sanitize_single_line, sanitize_terminal_text
from peer_review.context_packet import MAX_TASK_BYTES, check_not_sensitive_file, read_regular_file_bounded
from peer_review.lineup_store import LineupStore
from peer_review.models import BenchmarkSnapshot
from peer_review.review_runner import run_peer_review
from peer_review.updater import run_lineup_update


def _cmd_run(args: argparse.Namespace) -> int:
    if os.environ.get("PEER_REVIEW_NESTED") == "1":
        print(
            "ERROR: Refusing recursive peer-review run inside an active reviewer subprocess.",
            file=sys.stderr,
        )
        return 2
    cfg = load_config(args.config)
    task = args.task
    if args.task_file:
        task = read_regular_file_bounded(args.task_file, MAX_TASK_BYTES).decode("utf-8", errors="replace")
        check_not_sensitive_file(args.task_file, task)
    if not task:
        print("ERROR: provide --task or --task-file", file=sys.stderr)
        return 2

    res = run_peer_review(
        task=task,
        config=cfg,
        file_paths=args.file or [],
        constraints=args.constraint or [],
        output_format=args.output_format or "",
        extra_context=args.extra_context or "",
        task_type=args.task_type,
        cwd=args.cwd,
        skip_clarification=args.no_clarification,
    )
    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(sanitize_terminal_text(res.final_answer))
        safe_reason = sanitize_single_line(res.incomplete_reason, max_len=300)
        safe_dir = sanitize_single_line(res.artifact_dir, max_len=300)
        print(
            f"\n---\nRun ID: {sanitize_single_line(res.run_id, max_len=128)} | Packet SHA-256: {res.packet_sha256} | "
            f"Status: {'COMPLETE' if res.complete else 'INCOMPLETE (' + safe_reason + ')'}\n"
            f"Archive: {safe_dir}",
            file=sys.stderr,
        )
    return 0 if res.complete else 3


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = LineupStore(cfg["state_dir"])
    active = store.load_active()
    if active is None:
        print("No active lineup yet. Run `peer-review-update` to initialize.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(active.to_dict(), indent=2, ensure_ascii=False))
        return 0
    print(f"Active Peer Review Lineup (rev {active.revision}, index {active.index_version})")
    print(f"Activated: {active.activated_at_utc} | K={active.k_cost_doubling} | Buffer={active.quality_buffer}")
    for s in active.seats:
        sc = f"{s.coding_agent_index:.2f}" if s.coding_agent_index is not None else "n/a"
        co = f"${s.cost_per_task_usd:.2f}" if s.cost_per_task_usd is not None else "n/a"
        print(f"  - {s.seat_id}: {s.creator_name} | {s.model_version} ({s.effort}) [{s.backend}:{s.model_id}] | score={sc} cost={co}")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    if args.skip_smoke_test and not args.dry_run:
        print("ERROR: --skip-smoke-test is only allowed together with --dry-run.", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    snap = None
    if args.snapshot:
        raw_bytes = read_regular_file_bounded(args.snapshot, 16 * 1024 * 1024)
        raw = json.loads(raw_bytes.decode("utf-8"))
        snap = BenchmarkSnapshot.from_dict(raw)

    outcome = run_lineup_update(
        cfg,
        snapshot=snap,
        dry_run=args.dry_run,
        skip_smoke_test=args.skip_smoke_test,
    )
    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False))
    else:
        status = "OK" if outcome.ok else "FAILED"
        print(
            f"[peer-review-update] {status} | changed={outcome.changed} | "
            f"index={outcome.index_version} | notifications={len(outcome.emitted_notifications)}"
        )
        if outcome.error:
            print(f"Error: {sanitize_single_line(outcome.error, max_len=400)}", file=sys.stderr)
        for gap in outcome.data_gaps:
            print(f"Data gap: {sanitize_single_line(gap, max_len=300)}", file=sys.stderr)
        for notif in outcome.emitted_notifications:
            print("\n" + sanitize_terminal_text(notif["message"]))
    return 0 if outcome.ok else 1


def _cmd_rollback(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = LineupStore(cfg["state_dir"])
    restored = store.rollback(target_revision=args.revision)
    print(f"Rolled back cleanly to new revision {restored.revision} (index {restored.index_version}).")
    for s in restored.seats:
        print(f"  - {s.seat_id}: {s.creator_name} {s.model_version} ({s.effort})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="peer-review", description="Multi-provider Frontier Peer Review CLI")
    parser.add_argument("--config", help="Path to config.json (default: ~/.config/peer-review/config.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run a 3-provider Peer Review on a task")
    p_run.add_argument("--task", help="Task prompt text")
    p_run.add_argument("--task-file", help="Read task prompt from a file")
    p_run.add_argument("--file", action="append", help="Attach a context file (repeatable)")
    p_run.add_argument("--constraint", action="append", help="User constraint (repeatable)")
    p_run.add_argument("--output-format", default="", help="Requested output format")
    p_run.add_argument("--extra-context", default="", help="Additional context string")
    p_run.add_argument("--task-type", choices=["coding", "general"], default="coding")
    p_run.add_argument(
        "--cwd",
        help="Working directory for CLI backends (default: isolated empty 0700 temp dir; NEVER point --cwd at an untrusted repository)",
    )
    p_run.add_argument("--no-clarification", action="store_true", help="Disable optional clarification round")
    p_run.add_argument("--json", action="store_true", help="Output structured JSON")
    p_run.set_defaults(func=_cmd_run)

    p_status = sub.add_parser("status", help="Show the active 3-provider lineup")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=_cmd_status)

    p_upd = sub.add_parser("update", help="Fetch Artificial Analysis data, smoke-test, and update lineup")
    p_upd.add_argument("--snapshot", help="Use a local BenchmarkSnapshot JSON file instead of Firecrawl")
    p_upd.add_argument("--dry-run", action="store_true", help="Evaluate without activating or notifying")
    p_upd.add_argument("--skip-smoke-test", action="store_true", help="Skip live smoke tests (for offline inspection only)")
    p_upd.add_argument("--json", action="store_true")
    p_upd.set_defaults(func=_cmd_update)

    p_rb = sub.add_parser("rollback", help="Atomically revert to a previous lineup revision")
    p_rb.add_argument("--revision", type=int, help="Specific archived revision number (default: latest)")
    p_rb.set_defaults(func=_cmd_rollback)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
