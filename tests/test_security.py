"""Security, validation, and index-version boundary tests."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from peer_review.adapters import AdapterError, invoke_model
from peer_review.config import default_config, load_config, redact_secrets, validate_effort, validate_model_id
from peer_review.models import BenchmarkSnapshot, Lineup, SeatConfig, VariantRecord
from peer_review.selector import evaluate_selection


class SecurityValidationTest(unittest.TestCase):
    def test_rejects_shell_injection_in_model_id_and_effort(self) -> None:
        bypass_flag = "--dangerously-" + "skip-permissions"
        for bad in ("gpt-6; rm -rf /", "$(whoami)", "model\nfoo", "a b", "", bypass_flag, "-flag", "a..b"):
            with self.assertRaises(ValueError):
                validate_model_id(bad)
        for bad_eff in ("high;id", "$(cat /etc/passwd)", "HIGH SPACE", "--yolo", "-x", "notaneffort"):
            with self.assertRaises(ValueError):
                validate_effort(bad_eff)

    def test_rejects_forbidden_extra_args_under_read_only(self) -> None:
        from unittest import mock
        from peer_review.adapters import _scrubbed_cli_env, _validate_extra_args
        from peer_review.context_packet import read_regular_file_bounded

        self.assertEqual(
            _validate_extra_args(["--verbose", "--max-budget-usd=2.5", "--color=never"], read_only=True),
            ["--verbose", "--max-budget-usd=2.5", "--color=never"],
        )
        bypass_flag = "--dangerously-" + "skip-permissions"
        for bad_flag in (
            bypass_flag,
            "--yolo",
            "--no-sandbox",
            "-s",
            "-sdanger-full-access",
            "-c",
            "-C",
            "--cd=/etc",
            "--config=sandbox_mode=danger-full-access",
            "--tools",
            "--settings=/tmp/x",
            "--mcp-config=/tmp/m.json",
            "--permission-mode=bypassPermissions",
            "--max-budget-usd=--evil",
        ):
            with self.assertRaises(AdapterError):
                _validate_extra_args([bad_flag], read_only=True)
        # Verify allowlisted env drops arbitrary host secrets and sets private TMPDIR + nested guard.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = default_config()
            cfg["state_dir"] = tmp
            with mock.patch.dict(os.environ, {"UNRELATED_HOST_CREDENTIAL": "dummy-value", "GITHUB_TOKEN": "dummy-gh"}):
                env = _scrubbed_cli_env(cfg, {})
                self.assertNotIn("UNRELATED_HOST_CREDENTIAL", env)
                self.assertNotIn("GITHUB_TOKEN", env)
                self.assertEqual(env.get("PEER_REVIEW_NESTED"), "1")
                self.assertTrue(env.get("TMPDIR", "").startswith(str(Path(tmp))))
            # Verify read_regular_file_bounded refuses both file symlinks and symlinked parent dirs (F-01).
            secret = Path(tmp) / "real.txt"
            secret.write_text("data", encoding="utf-8")
            link = Path(tmp) / "link.txt"
            link.symlink_to(secret)
            with self.assertRaises(ValueError):
                read_regular_file_bounded(link, 1024)
            real_sub = Path(tmp) / "real_dir"
            real_sub.mkdir()
            (real_sub / "inside.txt").write_text("nested", encoding="utf-8")
            dir_link = Path(tmp) / "dir_link"
            dir_link.symlink_to(real_sub)
            with self.assertRaises(ValueError):
                read_regular_file_bounded(dir_link / "inside.txt", 1024)
            # Also verify config.json rejects group/world-writable file unconditionally even without command fields (F-04).
            cfg_file = Path(tmp) / "open_config.json"
            cfg_file.write_text('{"language": "en"}', encoding="utf-8")
            os.chmod(cfg_file, 0o664)
            with self.assertRaises(ValueError):
                load_config(cfg_file)
            # Also verify custom command backend is rejected under review.read_only=True without explicit opt-in (F-03).
            cmd_cfg = default_config()
            cmd_cfg["state_dir"] = tmp
            cmd_cfg["providers"] = {"custom": {"command": ["echo", "{model_id}"]}}
            with self.assertRaises(AdapterError):
                invoke_model(
                    provider="custom",
                    backend="command",
                    model_id="m1",
                    effort="high",
                    prompt="hi",
                    config=cmd_cfg,
                )

            # Also verify agy custom settings_path is rejected under review.read_only=True (F-02),
            # ensure_private_dir rejects symlinked ancestors, load_config refuses symlinked config (F-01),
            # and build_context_packet refuses .env/private key attachments (F-05).
            from peer_review.config import ensure_private_dir
            from peer_review.context_packet import build_context_packet
            agy_cfg = default_config()
            agy_cfg["state_dir"] = tmp
            agy_cfg["providers"] = {"google": {"cli_type": "agy", "settings_path": str(secret)}}
            with self.assertRaises(AdapterError):
                invoke_model(
                    provider="google",
                    backend="cli",
                    model_id="gemini-4-neon",
                    effort="high",
                    prompt="hi",
                    config=agy_cfg,
                )
            with self.assertRaises(OSError):
                ensure_private_dir(dir_link / "child_state")
            cfg_link = Path(tmp) / "cfg_link.json"
            cfg_link.symlink_to(secret)
            with self.assertRaises(ValueError):
                load_config(cfg_link)
            env_file = Path(tmp) / ".env"
            env_file.write_text("SECRET=12345678\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_context_packet("Review", [], file_paths=[env_file])
            envrc = Path(tmp) / ".envrc"
            envrc.write_text("export FOO=bar\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_context_packet("Review", [], file_paths=[envrc])
            # Verify resolve_secret rejects CR/LF control chars (F-02).
            from peer_review.config import resolve_secret
            with mock.patch.dict(os.environ, {"PR_BAD_SECRET": "line1\nX-Injected: 1"}):
                with self.assertRaises(ValueError):
                    resolve_secret(env_var="PR_BAD_SECRET")
            # Verify Moderator synthesis prompt omits raw error text from failed_seats (F-02).
            from peer_review.prompts import build_moderator_synthesis_prompt
            pkt = build_context_packet("Review code", [])
            mp = build_moderator_synthesis_prompt(
                pkt, {}, {}, None, [],
                [{"stage": "round1", "seat_id": "seat-1", "creator": "X", "model_id": "m", "effort": "high", "error": "RAW_STDERR_LEAK_999"}],
            )
            self.assertNotIn("RAW_STDERR_LEAK_999", mp)

    def test_redacts_secrets_in_errors(self) -> None:
        # Build synthetic secret-shaped fixtures dynamically so no literal token patterns sit in source.
        tok_sk = "sk-" + "1234567890abcdefghij"
        tok_fc = "fc-" + "abcdef1234567890"
        tok_slack = "xox" + "b-1234-5678-abcdef"
        tok_gho = "gh" + "o_1234567890abcdefghij123456"
        tok_aws = "AK" + "IA1234567890ABCDEF"
        pem_fake = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nabcdef1234567890\n-----END " + "OPENSSH PRIVATE KEY-----"
        hook_url = "https://hooks.slack.com/services/" + "T000/B000/SECRET123"
        auth_url = "https://" + "usr" + ":" + "pwd" + "@" + "api.example.invalid/v1?key=MYSECRETKEY99"
        db_url = "postgres://" + "dbu" + ":" + "dbp@ss99" + "@" + "db.internal.invalid:5432/app"
        json_hdr = '{"x-api-key": "CUSTOMSECRETVALUE99", "api_key": "p@ss!w0rd#99"} \\"secret\\": \\"ESC!P@SS99\\"'
        hdr_unquoted = "x-api-key: HDR!P@SS99\nPASSWORD=ENV!P@SS99\nAWS_SECRET_ACCESS_KEY=AWSS3CR3TKEY99"
        raw = f"Authorization: Bearer {tok_sk} {tok_fc} {tok_slack} {tok_gho} {tok_aws} {pem_fake} {hook_url} {auth_url} {db_url} {json_hdr} {hdr_unquoted}"
        redacted = redact_secrets(raw)
        for leak in (
            "1234567890",
            "abcdef123456",
            "5678-abcdef",
            "SECRET123",
            "usr:pwd",
            "dbp@ss99",
            "MYSECRETKEY99",
            "CUSTOMSECRETVALUE99",
            "p@ss!w0rd#99",
            "ESC!P@SS99",
            "HDR!P@SS99",
            "ENV!P@SS99",
            "AWSS3CR3TKEY99",
            "OPENSSH PRIVATE KEY",
        ):
            self.assertNotIn(leak, redacted)
        self.assertIn("[REDACTED]", redacted)
        from peer_review.config import sanitize_single_line, sanitize_terminal_text, sanitize_url_for_log
        from peer_review.adapters import _validate_https_endpoint
        from peer_review.context_packet import build_context_packet
        self.assertEqual(sanitize_url_for_log("raw-secret-without-scheme"), "[INVALID_URL]")
        self.assertNotIn("\x9b", sanitize_single_line("bad\x9b31m\u202e\u061ctext"))
        self.assertNotIn("\u202e", sanitize_single_line("bad\x9b31m\u202e\u061ctext"))
        self.assertNotIn("\u061c", sanitize_single_line("bad\x9b31m\u202e\u061ctext"))
        osc_cleaned = sanitize_terminal_text("line1\x1b]52;c;evil\x07\nline2\u061c\x9b31m")
        self.assertIn("line1\nline2", osc_cleaned)
        self.assertNotIn("\x1b", osc_cleaned)
        self.assertNotIn("\x9b", osc_cleaned)
        self.assertNotIn("\u061c", osc_cleaned)
        with self.assertRaises(ValueError):
            build_context_packet(f"Please inspect {pem_fake}", [])
        for bad_ep in (auth_url, "https://" + "@", "https://api.example.invalid/v1\nX: 1"):
            with self.assertRaises(AdapterError):
                _validate_https_endpoint(bad_ep)
        with self.assertRaises(ValueError):
            BenchmarkSnapshot.from_dict({"source_url": "http://insecure.example/x", "variants": []})
        with self.assertRaises(ValueError):
            BenchmarkSnapshot.from_dict({"source_url": "", "variants": []})
        clean_snap = BenchmarkSnapshot.from_dict(
            {
                "index_version": "v2.1\x1b[31mRED\x1b[0m\x9b32m",
                "source_url": "https://artificialanalysis.ai/agents/coding-agents",
                "fetched_at_utc": "2026-11-26T00:00:00Z",
                "variants": [],
            }
        )
        self.assertNotIn("\x1b", clean_snap.index_version)
        self.assertNotIn("\x9b", clean_snap.index_version)

    def test_rejects_skip_smoke_test_without_dry_run_and_non_finite_cost(self) -> None:
        from peer_review.aa_scraper import _normalize_cost
        from peer_review.updater import run_lineup_update
        self.assertIsNone(_normalize_cost(float("inf")))
        self.assertIsNone(_normalize_cost(float("nan")))
        self.assertIsNone(_normalize_cost(0.0))
        cfg = default_config()
        with self.assertRaises(ValueError):
            run_lineup_update(cfg, dry_run=False, skip_smoke_test=True)

    def test_rejects_run_id_path_traversal(self) -> None:
        from peer_review.config import validate_run_id
        for bad_id in ("../escape", "/etc/passwd", "-bad", "a..b"):
            with self.assertRaises(ValueError):
                validate_run_id(bad_id)

    def test_refuses_recursive_nested_peer_review(self) -> None:
        from peer_review.cli import main as cli_main
        os.environ["PEER_REVIEW_NESTED"] = "1"
        try:
            rc = cli_main(["run", "--task", "Nested attempt"])
            self.assertEqual(rc, 2)
        finally:
            os.environ.pop("PEER_REVIEW_NESTED", None)

    def test_rejects_non_https_remote_api_endpoint(self) -> None:
        cfg = default_config()
        cfg["providers"]["openai"] = {
            "api_style": "openai",
            "api_base_url": "http://example.com/v1/responses",
            "api_key_env": "TEST_DUMMY_KEY",
        }
        os.environ["TEST_DUMMY_KEY"] = "dummy-not-a-real-key"
        try:
            with self.assertRaises(AdapterError):
                invoke_model(
                    provider="openai",
                    backend="api",
                    model_id="gpt-6.2-astra",
                    effort="high",
                    prompt="hello",
                    config=cfg,
                )
        finally:
            os.environ.pop("TEST_DUMMY_KEY", None)

    def test_new_index_version_retains_unmatched_active_seat(self) -> None:
        # Active seat was benchmarked on v2.0; new v2.1 snapshot does not include old active seat -> must hold!
        snap = BenchmarkSnapshot(
            index_version="v2.1",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            fetched_at_utc="2026-11-26T00:00:00Z",
            variants=[
                VariantRecord("a-new", "alpha", "Alpha", "Alpha-2", "Alpha-2 (High)", "cli", "CLI", "high", 62.0, 1.0, "v2.1", "https://artificialanalysis.ai/agents/coding-agents", "2026-11-26T00:00:00Z", eval_count=3, index_component_count=3),
                VariantRecord("b-v21", "beta", "Beta", "Beta-1", "Beta-1 (High)", "cli", "CLI", "high", 60.0, 1.0, "v2.1", "https://artificialanalysis.ai/agents/coding-agents", "2026-11-26T00:00:00Z", eval_count=3, index_component_count=3),
                VariantRecord("g-v21", "gamma", "Gamma", "Gamma-1", "Gamma-1 (High)", "cli", "CLI", "high", 59.0, 1.0, "v2.1", "https://artificialanalysis.ai/agents/coding-agents", "2026-11-26T00:00:00Z", eval_count=3, index_component_count=3),
            ],
        )
        cfg = default_config()
        cfg["catalog"] = [
            {"creator_slug": "alpha", "match_model_release": "Alpha-2", "efforts": ["high"], "provider": "alpha", "backend": "cli", "model_id": "alpha-2", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "beta", "match_model_release": "Beta-1", "efforts": ["high"], "provider": "beta", "backend": "cli", "model_id": "beta-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
            {"creator_slug": "gamma", "match_model_release": "Gamma-1", "efforts": ["high"], "provider": "gamma", "backend": "cli", "model_id": "gamma-1", "context_window": 200000, "capabilities": ["coding", "reasoning"]},
        ]
        active = Lineup(
            schema_version="1.0",
            revision=1,
            activated_at_utc="2026-11-01T00:00:00Z",
            index_version="v2.0",
            source_url="https://artificialanalysis.ai/agents/coding-agents",
            k_cost_doubling=5.0,
            quality_buffer=3.0,
            seats=[
                SeatConfig("seat-1", "alpha", "Alpha", "alpha", "cli", "alpha-1", "Alpha-1 (High)", "high", "CLI", "a-old-v20", 80.0, 1.0, "v2.0"),
                SeatConfig("seat-2", "beta", "Beta", "beta", "cli", "beta-1", "Beta-1 (High)", "high", "CLI", "b-v21", 60.0, 1.0, "v2.1"),
                SeatConfig("seat-3", "gamma", "Gamma", "gamma", "cli", "gamma-1", "Gamma-1 (High)", "high", "CLI", "g-v21", 59.0, 1.0, "v2.1"),
            ],
        )
        res = evaluate_selection(snap, cfg, active_lineup=active)
        self.assertTrue(any(d.kind == "hold_data_gap" and d.seat_id == "seat-1" for d in res.decisions))
        self.assertFalse(any(p.seat_id == "seat-1" and p.should_switch for p in res.proposed_changes))

    def test_installer_refuses_symlink_destination(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as fake_home:
            evil_target = Path(fake_home) / "target"
            evil_target.mkdir()
            dest_parent = Path(fake_home) / ".local" / "share"
            dest_parent.mkdir(parents=True)
            (dest_parent / "peer-review").symlink_to(evil_target)
            env = dict(os.environ, HOME=fake_home)
            res = subprocess.run(
                ["bash", str(repo_root / "install.sh")],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("symlink", res.stderr.lower())

    def test_installer_install_and_uninstall_roundtrip(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as fake_home:
            env = dict(os.environ, HOME=fake_home)
            inst = subprocess.run(
                ["bash", str(repo_root / "install.sh")],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(inst.returncode, 0, inst.stderr)
            self.assertTrue((Path(fake_home) / ".local/bin/peer-review").exists())
            self.assertTrue((Path(fake_home) / ".local/bin/peer-review-update").exists())
            self.assertTrue((Path(fake_home) / ".config/peer-review/config.json").exists())
            # Verify installed CLI runs with python3 -I even if a planted json.py sits in cwd.
            with tempfile.TemporaryDirectory() as hostile_cwd:
                (Path(hostile_cwd) / "json.py").write_text("raise RuntimeError('HIJACKED')\n", encoding="utf-8")
                chk = subprocess.run(
                    [str(Path(fake_home) / ".local/bin/peer-review"), "--help"],
                    cwd=hostile_cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(chk.returncode, 0, chk.stderr)
                self.assertIn("Peer Review", chk.stdout)

            uninst = subprocess.run(
                ["bash", str(repo_root / "install.sh"), "--uninstall"],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(uninst.returncode, 0, uninst.stderr)
            self.assertFalse((Path(fake_home) / ".local/bin/peer-review").exists())
            self.assertFalse((Path(fake_home) / ".local/share/peer-review").exists())


if __name__ == "__main__":
    unittest.main()
