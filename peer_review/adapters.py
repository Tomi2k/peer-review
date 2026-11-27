"""Pluggable model execution adapters: direct provider APIs and read-only local CLIs."""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    MAX_HTTP_ERROR_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    NO_REDIRECT_OPENER,
    _open_regular_file_fd_nofollow,
    ensure_private_dir,
    expand_path,
    read_regular_file_bounded,
    redact_secrets,
    resolve_secret,
    sanitize_url_for_log,
    validate_effort,
    validate_model_id,
)

MAX_CLI_OUTPUT_BYTES = 4 * 1024 * 1024  # 4 MiB cap on CLI output files
AGY_READONLY_SETTINGS_JSON = json.dumps(
    {
        "toolPermission": "request-review",
        "permissions": {
            "allow": [],
            "deny": [
                "write_file(*)",
                "read_file(*)",
                "command(*)",
                "read_url(*)",
                "execute_url(*)",
                "download_url(*)",
                "mcp(*)",
            ],
        },
    },
    separators=(",", ":"),
)

# Strict allowlist for provider extra_args when review.read_only=True (default).
# Any flag not explicitly allowlisted (including permission, tool, config, sandbox,
# hook, MCP, plugin, and working-directory flags) is rejected fail-closed.
ALLOWED_READONLY_BOOL_FLAGS = frozenset({
    "--verbose",
    "--quiet",
    "--no-color",
})
ALLOWED_READONLY_KV_FLAGS = frozenset({
    "--color",
    "--max-budget-usd",
})
SAFE_FLAG_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Minimal safe baseline allowlist passed to local CLIs (prevents leaking GITHUB_TOKEN, AWS_*, DB creds, etc.).
SAFE_ENV_ALLOWLIST = frozenset({
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "COLORTERM",
    "TZ",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "CODEX_HOME",
    "GROK_HOME",
    "CLAUDE_CONFIG_DIR",
})


class AdapterError(RuntimeError):
    """Raised when a model invocation fails or returns an unusable response."""


@dataclass(frozen=True)
class ModelResponse:
    text: str
    backend: str
    provider: str
    model_id: str
    effort: str
    usage: dict[str, Any]


def _validate_https_endpoint(url: str) -> str:
    raw = str(url or "")
    if not raw or re.search(r"[\x00-\x1f\x7f-\x9f\s]", raw):
        raise AdapterError("Refusing malformed remote endpoint containing whitespace or control characters")
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.hostname:
        raise AdapterError(f"Refusing endpoint without hostname: {sanitize_url_for_log(raw)}")
    if parsed.username is not None or parsed.password is not None:
        raise AdapterError(f"Refusing endpoint containing embedded credentials: {sanitize_url_for_log(raw)}")
    if parsed.scheme == "https":
        return raw
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return raw
    raise AdapterError(f"Refusing non-HTTPS remote endpoint: {sanitize_url_for_log(raw)}")


def _validate_extra_args(extra: Any, read_only: bool) -> list[str]:
    if not extra:
        return []
    if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
        raise AdapterError("provider extra_args must be a list of strings")
    out: list[str] = []
    for raw in extra:
        stripped = raw.strip()
        if not stripped:
            raise AdapterError("provider extra_args entries must be non-empty strings")
        if read_only:
            if "=" in stripped:
                flag_name, flag_val = stripped.split("=", 1)
                if (
                    flag_name.lower() not in ALLOWED_READONLY_KV_FLAGS
                    or not SAFE_FLAG_VALUE_RE.match(flag_val)
                ):
                    raise AdapterError(
                        f"Disallowed extra_args flag under review.read_only=True: {raw!r}"
                    )
            elif stripped.lower() not in ALLOWED_READONLY_BOOL_FLAGS:
                raise AdapterError(
                    f"Disallowed extra_args flag under review.read_only=True: {raw!r}"
                )
        out.append(stripped)
    return out


def _private_tmp_root(config: dict[str, Any]) -> str:
    return str(ensure_private_dir(Path(config["state_dir"]) / "tmp"))


def _scrubbed_cli_env(config: dict[str, Any], provider_cfg: dict[str, Any]) -> dict[str, str]:
    """Construct child CLI environment from a strict baseline allowlist plus explicit passthrough_env."""
    keep_env = set(SAFE_ENV_ALLOWLIST)
    keep_env.update(str(k) for k in (provider_cfg.get("passthrough_env") or []))
    env = {k: v for k, v in os.environ.items() if k in keep_env}
    env["TMPDIR"] = _private_tmp_root(config)
    # Prevent child coding-agent CLIs from recursively re-invoking the peer-review skill/CLI.
    env["PEER_REVIEW_NESTED"] = "1"
    return env


def _http_post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    _validate_https_endpoint(url)
    safe_url = sanitize_url_for_log(url)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with NO_REDIRECT_OPENER.open(req, timeout=timeout_seconds) as resp:
            raw_bytes = resp.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_HTTP_RESPONSE_BYTES:
                raise AdapterError(f"HTTP response exceeded size cap from {safe_url}")
            raw = raw_bytes.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_bytes = exc.read(MAX_HTTP_ERROR_BYTES) if exc.fp else b""
        body = redact_secrets(err_bytes.decode("utf-8", errors="replace")[:500])
        raise AdapterError(f"HTTP {exc.code} from {safe_url}: {body}") from exc
    except ValueError as exc:
        # http.client.InvalidHeader subclasses ValueError and can embed raw secret header bytes.
        raise AdapterError(f"Invalid HTTP header or request configuration for {safe_url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"Request failed for {safe_url}: {redact_secrets(str(exc))}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"Non-JSON response from {safe_url}") from exc
    if not isinstance(parsed, dict):
        raise AdapterError(f"Unexpected non-object JSON response from {safe_url}")
    return parsed


def _resolve_provider_key(provider_cfg: dict[str, Any], default_env: str) -> str:
    env_name = provider_cfg.get("api_key_env", default_env)
    try:
        key = resolve_secret(
            env_var=env_name,
            secret_command=provider_cfg.get("api_key_command"),
        )
    except ValueError as exc:
        raise AdapterError(f"Invalid API key for {env_name}: {exc}") from exc
    if not key:
        raise AdapterError(
            f"Missing API key (env {env_name} or api_key_command in provider config)."
        )
    return key


def _call_anthropic_api(
    model_id: str,
    effort: str,
    prompt: str,
    provider_cfg: dict[str, Any],
    timeout_seconds: int,
) -> ModelResponse:
    api_key = _resolve_provider_key(provider_cfg, "ANTHROPIC_API_KEY")
    base_url = str(provider_cfg.get("api_base_url") or "https://api.anthropic.com/v1/messages")
    payload: dict[str, Any] = {
        "model": model_id,
        "max_tokens": int(provider_cfg.get("max_tokens", 8192)),
        "messages": [{"role": "user", "content": prompt}],
    }
    if effort not in ("none", "default"):
        payload["output_config"] = {"effort": effort}
    data = _http_post_json(
        base_url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": str(provider_cfg.get("anthropic_version", "2023-06-01")),
        },
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    chunks = [
        blk.get("text", "")
        for blk in data.get("content", [])
        if isinstance(blk, dict) and blk.get("type") == "text"
    ]
    text = "\n".join(c for c in chunks if c).strip()
    if not text:
        raise AdapterError("Anthropic API returned empty text")
    return ModelResponse(text=text, backend="api", provider="anthropic", model_id=model_id, effort=effort, usage=data.get("usage", {}))


def _call_openai_api(
    model_id: str,
    effort: str,
    prompt: str,
    provider_cfg: dict[str, Any],
    timeout_seconds: int,
    provider_name: str = "openai",
    default_env: str = "OPENAI_API_KEY",
    default_url: str = "https://api.openai.com/v1/responses",
) -> ModelResponse:
    api_key = _resolve_provider_key(provider_cfg, default_env)
    url = str(provider_cfg.get("api_base_url") or default_url)
    use_chat = "/chat/completions" in url

    if use_chat:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
        }
        if effort not in ("none", "default"):
            payload["reasoning_effort"] = effort
        data = _http_post_json(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        choices = data.get("choices") or []
        text = str((choices[0].get("message") or {}).get("content", "")).strip() if choices else ""
    else:
        payload = {
            "model": model_id,
            "input": prompt,
        }
        if effort not in ("none", "default"):
            payload["reasoning"] = {"effort": effort}
        data = _http_post_json(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        text = str(data.get("output_text") or "").strip()
        if not text:
            parts: list[str] = []
            for item in data.get("output", []):
                if isinstance(item, dict):
                    for content in item.get("content", []):
                        if isinstance(content, dict) and content.get("type") in ("output_text", "text"):
                            parts.append(str(content.get("text", "")))
            text = "\n".join(p for p in parts if p).strip()

    if not text:
        raise AdapterError(f"{provider_name} API returned empty text")
    return ModelResponse(text=text, backend="api", provider=provider_name, model_id=model_id, effort=effort, usage=data.get("usage", {}))


def _call_google_api(
    model_id: str,
    effort: str,
    prompt: str,
    provider_cfg: dict[str, Any],
    timeout_seconds: int,
) -> ModelResponse:
    if ":" in model_id or "/" in model_id:
        raise AdapterError(f"Invalid Google model_id for REST path: {model_id!r}")
    api_key = _resolve_provider_key(provider_cfg, "GEMINI_API_KEY")
    base = str(
        provider_cfg.get("api_base_url")
        or "https://generativelanguage.googleapis.com/v1beta/models"
    ).rstrip("/")
    url = f"{base}/{urllib.parse.quote(model_id, safe='.-_')}:generateContent"
    gen_cfg: dict[str, Any] = {}
    if effort not in ("none", "default"):
        gen_cfg["thinkingConfig"] = {"thinkingLevel": effort.upper()}
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    if gen_cfg:
        payload["generationConfig"] = gen_cfg
    data = _http_post_json(
        url,
        headers={"x-goog-api-key": api_key},
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    cands = data.get("candidates") or []
    parts = (cands[0].get("content") or {}).get("parts", []) if cands else []
    text = "\n".join(
        str(p.get("text", ""))
        for p in parts
        if isinstance(p, dict) and not p.get("thought")
    ).strip()
    if not text:
        raise AdapterError("Google Gemini API returned empty text")
    return ModelResponse(
        text=text,
        backend="api",
        provider="google",
        model_id=model_id,
        effort=effort,
        usage=data.get("usageMetadata", {}),
    )


def _run_cli(
    cmd: list[str],
    *,
    stdin_text: str | None = None,
    timeout_seconds: int = 600,
    cwd: str | None = None,
    output_file: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Execute a local CLI in its own process group with shell=False, scrubbed env, and redacted errors."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise AdapterError(f"CLI executable not found: {cmd[0]}") from exc
    except OSError as exc:
        raise AdapterError(f"CLI failed to launch {cmd[0]}: {redact_secrets(str(exc))}") from exc

    try:
        stdout_text, stderr_text = proc.communicate(input=stdin_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        raise AdapterError(f"CLI timed out after {timeout_seconds}s: {cmd[0]}") from exc
    except Exception:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        raise

    # Always check exit code BEFORE reading any output_file so failed runs never masquerade as success.
    if proc.returncode != 0:
        combined = (stderr_text or stdout_text or "").strip()
        err = redact_secrets(combined[-600:])
        raise AdapterError(f"CLI {cmd[0]} exited {proc.returncode}: {err}")

    if output_file:
        try:
            raw_out = read_regular_file_bounded(output_file, MAX_CLI_OUTPUT_BYTES)
        except FileNotFoundError:
            raw_out = b""
        except ValueError as exc:
            raise AdapterError(f"CLI {cmd[0]} output_file rejected: {exc}") from exc
        text = raw_out.decode("utf-8", errors="replace").strip()
        if text:
            return text

    text = (stdout_text or "").strip()
    if not text:
        err = redact_secrets((stderr_text or "").strip()[-500:])
        raise AdapterError(f"CLI {cmd[0]} returned empty output. stderr: {err}")
    return text


def _parse_agy_stream_json(raw_stdout: str) -> tuple[str, dict[str, Any]]:
    """Extract final response text and usage from `agy --output-format stream-json` NDJSON."""
    result_text = ""
    deltas: list[str] = []
    usage: dict[str, Any] = {}
    last_error = ""
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("event") == "step_update" and isinstance(evt.get("step_update"), dict):
            su = evt["step_update"]
            if su.get("step_type") == "agent_response" and isinstance(su.get("text_delta"), str):
                deltas.append(su["text_delta"])
        elif evt.get("event") == "result" and isinstance(evt.get("result"), dict):
            res = evt["result"]
            finished_reason = str(res.get("finished_reason") or "").lower()
            if finished_reason == "print_timeout":
                raise AdapterError("agy stream-json hit print_timeout (partial response rejected)")
            if res.get("status") == "ERROR" or res.get("error"):
                last_error = redact_secrets(str(res.get("error") or res.get("status")))
            if isinstance(res.get("response"), str) and res["response"].strip():
                result_text = res["response"].strip()
            if isinstance(res.get("usage"), dict):
                usage = res["usage"]
    if last_error and not result_text:
        raise AdapterError(f"agy stream-json error: {last_error}")
    if not result_text and deltas:
        result_text = "".join(deltas).strip()
    if not result_text:
        raise AdapterError("agy stream-json returned empty response")
    return result_text, usage


def _validate_settings_file(raw_path: Any) -> str:
    """Validate an explicit agy settings file via dir_fd + O_NOFOLLOW (regular, owned, not writable)."""
    expanded = expand_path(str(raw_path))
    if str(expanded).startswith("-"):
        raise AdapterError(f"Invalid settings_path: {raw_path!r}")
    try:
        fd = _open_regular_file_fd_nofollow(expanded)
    except (OSError, ValueError) as exc:
        raise AdapterError(f"Unreadable or symlinked settings_path {raw_path!r}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AdapterError(f"settings_path must be a regular file: {raw_path!r}")
        if hasattr(os, "geteuid") and st.st_uid not in (os.geteuid(), 0):
            raise AdapterError(f"Refusing settings_path not owned by current user or root: {raw_path!r}")
        if (st.st_mode & 0o022) != 0:
            raise AdapterError(f"Refusing group/world-writable settings_path: {raw_path!r}")
    finally:
        os.close(fd)
    return str(expanded)


def _call_cli_adapter(
    provider: str,
    model_id: str,
    effort: str,
    prompt: str,
    provider_cfg: dict[str, Any],
    config: dict[str, Any],
    timeout_seconds: int,
    cwd: str | None,
) -> ModelResponse:
    """Invoke supported local coding-agent CLIs strictly in read-only/plan/sandbox mode."""
    read_only = bool(config.get("review", {}).get("read_only", True))
    extra_args = _validate_extra_args(provider_cfg.get("extra_args"), read_only=read_only)
    cli_env = _scrubbed_cli_env(config, provider_cfg)
    cli_type = str(provider_cfg.get("cli_type") or provider).strip().lower()
    binary = str(provider_cfg.get("binary") or "").strip()

    with contextlib.ExitStack() as stack:
        if cwd is None:
            effective_cwd = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="peer-review-cwd-", dir=_private_tmp_root(config))
            )
            os.chmod(effective_cwd, 0o700)
        else:
            effective_cwd = cwd

        if cli_type in ("anthropic", "claude", "claude-code"):
            exe = binary or "claude"
            cmd = [
                exe,
                *extra_args,
                "-p",
                "--model",
                model_id,
                "--setting-sources",
                "user",
                "--permission-mode",
                "plan",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--output-format",
                "text",
            ]
            if read_only:
                cmd.extend(["--strict-mcp-config", "--tools", ""])
            if effort not in ("none", "default"):
                cmd.extend(["--effort", effort])
            text = _run_cli(cmd, stdin_text=prompt, timeout_seconds=timeout_seconds, cwd=effective_cwd, env=cli_env)
            return ModelResponse(text=text, backend="cli", provider=provider, model_id=model_id, effort=effort, usage={})

        if cli_type in ("openai", "codex"):
            exe = binary or "codex"
            with tempfile.TemporaryDirectory(prefix="peer-review-codex-", dir=_private_tmp_root(config)) as tdir:
                os.chmod(tdir, 0o700)
                out_path = str(Path(tdir) / "out.md")
                cmd = [
                    exe,
                    "exec",
                    *extra_args,
                    "-m",
                    model_id,
                    "-s",
                    "read-only",
                    "--skip-git-repo-check",
                    "-o",
                    out_path,
                ]
                if read_only:
                    cmd.append("--ignore-user-config")
                if effort not in ("none", "default"):
                    cmd.extend(["-c", f"model_reasoning_effort={effort}"])
                cmd.append("-")
                text = _run_cli(
                    cmd,
                    stdin_text=prompt,
                    timeout_seconds=timeout_seconds,
                    cwd=effective_cwd,
                    output_file=out_path,
                    env=cli_env,
                )
            return ModelResponse(text=text, backend="cli", provider=provider, model_id=model_id, effort=effort, usage={})

        if cli_type in ("google", "agy", "antigravity", "antigravity-cli"):
            exe = binary or "agy"
            # Pass prompt via stream-json stdin so prompt bytes NEVER appear in argv/ps and cannot hit E2BIG.
            cmd = [
                exe,
                *extra_args,
                "-p",
                "",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--model",
                model_id,
                "--mode",
                "plan",
                "--sandbox",
                "--print-timeout",
                f"{timeout_seconds}s",
            ]
            if effort not in ("none", "default"):
                cmd.extend(["--effort", effort])
            settings_path = provider_cfg.get("settings_path")
            if read_only:
                if settings_path:
                    raise AdapterError(
                        f"Custom settings_path for provider {provider!r} is disallowed under review.read_only=True "
                        "(built-in inline read-only policy is applied automatically)."
                    )
                cmd.extend(["--settings", AGY_READONLY_SETTINGS_JSON])
            elif settings_path:
                cmd.extend(["--settings", _validate_settings_file(settings_path)])
            ndjson_in = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
            raw_out = _run_cli(
                cmd,
                stdin_text=ndjson_in,
                timeout_seconds=timeout_seconds + 30,
                cwd=effective_cwd,
                env=cli_env,
            )
            text, usage = _parse_agy_stream_json(raw_out)
            return ModelResponse(text=text, backend="cli", provider=provider, model_id=model_id, effort=effort, usage=usage)

        if cli_type in ("xai", "grok", "grok-build"):
            exe = binary or "grok"
            with tempfile.TemporaryDirectory(prefix="peer-review-grok-", dir=_private_tmp_root(config)) as tdir:
                os.chmod(tdir, 0o700)
                prompt_path = str(Path(tdir) / "prompt.md")
                fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(prompt)
                cmd = [
                    exe,
                    *extra_args,
                    "--prompt-file",
                    prompt_path,
                    "--model",
                    model_id,
                    "--permission-mode",
                    "plan",
                    "--no-memory",
                    "--no-subagents",
                    "--disable-web-search",
                    "--output-format",
                    "plain",
                ]
                if read_only:
                    cmd.extend(["--tools", ""])
                if effort not in ("none", "default"):
                    cmd.extend(["--reasoning-effort", effort])
                text = _run_cli(cmd, stdin_text="", timeout_seconds=timeout_seconds, cwd=effective_cwd, env=cli_env)
            return ModelResponse(text=text, backend="cli", provider=provider, model_id=model_id, effort=effort, usage={})

    raise AdapterError(f"Unsupported CLI type: {cli_type!r}")


def _call_custom_command(
    provider: str,
    model_id: str,
    effort: str,
    prompt: str,
    provider_cfg: dict[str, Any],
    config: dict[str, Any],
    timeout_seconds: int,
    cwd: str | None,
) -> ModelResponse:
    """Invoke a user-configured command template (argv list; prompt passed on stdin)."""
    read_only = bool(config.get("review", {}).get("read_only", True))
    if read_only and not provider_cfg.get("allow_in_read_only", False):
        raise AdapterError(
            f"Custom command backend for provider {provider!r} is disabled under review.read_only=True "
            f"(set providers.{provider}.allow_in_read_only=true to opt in explicitly)."
        )
    template = provider_cfg.get("command")
    if not isinstance(template, list) or not all(isinstance(p, str) and p for p in template):
        raise AdapterError(f"Provider {provider!r} command backend requires a non-empty argv list")
    cli_env = _scrubbed_cli_env(config, provider_cfg)
    cmd = [
        part.replace("{model_id}", model_id).replace("{effort}", effort)
        for part in template
    ]
    with contextlib.ExitStack() as stack:
        if cwd is None:
            effective_cwd = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="peer-review-cmd-", dir=_private_tmp_root(config))
            )
            os.chmod(effective_cwd, 0o700)
        else:
            effective_cwd = cwd
        text = _run_cli(cmd, stdin_text=prompt, timeout_seconds=timeout_seconds, cwd=effective_cwd, env=cli_env)
    return ModelResponse(text=text, backend="command", provider=provider, model_id=model_id, effort=effort, usage={})


def invoke_model(
    *,
    provider: str,
    backend: str,
    model_id: str,
    effort: str,
    prompt: str,
    config: dict[str, Any],
    cwd: str | None = None,
    timeout_seconds: int | None = None,
) -> ModelResponse:
    """Dispatch a single model invocation with strict input validation."""
    clean_model = validate_model_id(model_id)
    clean_effort = validate_effort(effort)
    timeout = int(timeout_seconds or config.get("review", {}).get("timeout_seconds", 600))
    provider_cfg = dict(config.get("providers", {}).get(provider, {}))
    norm_backend = str(backend).strip().lower()

    if norm_backend == "cli":
        return _call_cli_adapter(provider, clean_model, clean_effort, prompt, provider_cfg, config, timeout, cwd)
    if norm_backend == "command":
        return _call_custom_command(provider, clean_model, clean_effort, prompt, provider_cfg, config, timeout, cwd)
    if norm_backend == "api":
        api_style = str(provider_cfg.get("api_style") or provider).strip().lower()
        if api_style == "anthropic":
            return _call_anthropic_api(clean_model, clean_effort, prompt, provider_cfg, timeout)
        if api_style == "openai":
            return _call_openai_api(clean_model, clean_effort, prompt, provider_cfg, timeout)
        if api_style == "google":
            return _call_google_api(clean_model, clean_effort, prompt, provider_cfg, timeout)
        if api_style in ("xai", "openai-compatible"):
            return _call_openai_api(
                clean_model,
                clean_effort,
                prompt,
                provider_cfg,
                timeout,
                provider_name=provider,
                default_env="XAI_API_KEY" if api_style == "xai" else "OPENAI_API_KEY",
                default_url="https://api.x.ai/v1/chat/completions",
            )
        raise AdapterError(f"Unsupported API style: {api_style!r}")

    raise AdapterError(f"Unsupported backend: {norm_backend!r}")
