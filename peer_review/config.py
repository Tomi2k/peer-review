"""Configuration loading, validation, secret resolution, locking, and safe private I/O."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

DEFAULT_CONFIG_PATH = Path("~/.config/peer-review/config.json")
DEFAULT_STATE_DIR = Path("~/.local/state/peer-review")
DEFAULT_SOURCE_URL = "https://artificialanalysis.ai/agents/coding-agents"

MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024  # 16 MiB cap on HTTP JSON responses
MAX_HTTP_ERROR_BYTES = 4096

SECRET_REDACT_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[A-Za-z0-9+/=\s]{16,}"),
    re.compile(
        r"\b(sk-[A-Za-z0-9_-]{12,}|fc-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|"
        r"xai-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|"
        r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"glpat-[A-Za-z0-9_-]{16,}|(?:AKIA|ASIA)[0-9A-Z]{16,})\b"
    ),
    re.compile(r"(?i)(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"https?://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
    re.compile(
        r"""(?i)(x-api-key|x-goog-api-key|api[_-]?key|token|secret|password)(?:\\?['"])?\s*[:=]\s*(?:\\"[^"\r\n]{6,}\\"|\\'[^'\r\n]{6,}\\'|"[^"\r\n]{6,}"|'[^'\r\n]{6,}'|[^\s"'`,;{}\[\]]{6,})"""
    ),
    re.compile(r"(https?://)([^/@\s]+@)"),
    re.compile(r"([?&](?:key|api_key|token|access_token|secret)=)([^&\s]+)", re.I),
]

# Strictly disallow leading '-' (prevents CLI flag injection) and path traversal.
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
SAFE_EFFORT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ALLOWED_EFFORTS = frozenset({"none", "default", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})

_O_NOFOLLOW_RAW = getattr(os, "O_NOFOLLOW", 0)
SYSTEM_ROOT_SYMLINK_NAMES = frozenset({"tmp", "var", "etc", "bin", "sbin", "lib", "lib64"})
SYSTEM_ROOT_SYMLINKS = frozenset(Path("/") / name for name in SYSTEM_ROOT_SYMLINK_NAMES)


def require_nofollow() -> int:
    """Fail closed if the OS does not expose O_NOFOLLOW for symlink-safe file opens."""
    if _O_NOFOLLOW_RAW == 0:
        raise OSError("os.O_NOFOLLOW is required for symlink-safe file operations on this platform")
    return _O_NOFOLLOW_RAW


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed on 3xx redirects to prevent SSRF, HTTPS downgrade, and credential forwarding."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            sanitize_url_for_log(req.full_url),
            code,
            f"HTTP redirect ({code}) blocked for security",
            headers,
            None,
        )


NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def redact_secrets(text: str) -> str:
    """Mask private keys, common credential tokens, webhook URLs, userinfo, and query secrets."""
    out = str(text)
    out = SECRET_REDACT_PATTERNS[0].sub("[REDACTED_PRIVATE_KEY]", out)
    out = SECRET_REDACT_PATTERNS[1].sub("[REDACTED_PRIVATE_KEY]", out)
    out = SECRET_REDACT_PATTERNS[2].sub("[REDACTED]", out)
    out = SECRET_REDACT_PATTERNS[3].sub(r"\1 [REDACTED]", out)
    out = SECRET_REDACT_PATTERNS[4].sub("https://hooks.slack.com/services/[REDACTED]", out)
    out = SECRET_REDACT_PATTERNS[5].sub(r"\1=[REDACTED]", out)
    out = SECRET_REDACT_PATTERNS[6].sub(r"\1[REDACTED]@", out)
    out = SECRET_REDACT_PATTERNS[7].sub(r"\1[REDACTED]", out)
    return out


def _format_netloc(hostname: str, port: int | None) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host}:{port}" if port else host


def sanitize_url_for_log(url: str) -> str:
    """Return scheme + host + path without userinfo, query params, control chars, or fragments."""
    try:
        raw = str(url)
        if re.search(r"[\x00-\x1f\x7f-\x9f\s]", raw):
            return "[INVALID_URL]"
        parsed = urllib.parse.urlsplit(raw)
        if not parsed.scheme or not parsed.hostname:
            return "[INVALID_URL]"
        safe = urllib.parse.urlunsplit((parsed.scheme, _format_netloc(parsed.hostname, parsed.port), parsed.path, "", ""))
        return redact_secrets(safe)
    except Exception:
        return "[INVALID_URL]"


def validate_https_url(url: str) -> str:
    """Require a clean https:// URL without control characters, whitespace, or userinfo."""
    raw = str(url or "")
    if not raw or re.search(r"[\x00-\x1f\x7f-\x9f\s]", raw):
        raise ValueError("Only clean https:// URLs without control characters are allowed")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"Only https:// URLs are allowed, got: {sanitize_url_for_log(raw)!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Refusing URL containing embedded credentials")
    return urllib.parse.urlunsplit(("https", _format_netloc(parsed.hostname, parsed.port), parsed.path, "", ""))


def normalize_score(raw_score: Any) -> float | None:
    """Convert Artificial Analysis score to finite 0..100 scale without rounding."""
    if raw_score is None or isinstance(raw_score, bool):
        return None
    try:
        val = float(raw_score)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    if 0.0 <= val <= 1.0:
        return val * 100.0
    if 0.0 <= val <= 100.0:
        return val
    return None


def normalize_cost(raw_cost: Any) -> float | None:
    """Validate strictly positive, finite USD cost per task."""
    if raw_cost is None or isinstance(raw_cost, bool):
        return None
    try:
        val = float(raw_cost)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None
    return val


def sanitize_terminal_text(text: str) -> str:
    """Strip ANSI/OSC escapes, C0/C1 controls (keeping \\n and \\t), and Unicode BiDi overrides."""
    s = str(text or "")
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", s)
    s = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", s)
    s = re.sub(r"\x1b[@-Z\\-_]", "", s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", s)
    return s


def sanitize_single_line(text: str, max_len: int = 200) -> str:
    """Strip ANSI/OSC escapes, C0/C1 controls, BiDi overrides, newlines, and backticks."""
    s = str(text or "")
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", " ", s)
    s = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", " ", s)
    s = re.sub(r"\x1b[@-Z\\-_]", " ", s)
    s = re.sub(r"[\r\n\t\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069`]+", " ", s)
    cleaned = re.sub(r"\s{2,}", " ", s).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned


def expand_path(raw: str | Path) -> Path:
    """Expand ~ and normalize relative path against cwd WITHOUT dereferencing symlinks."""
    p = Path(os.path.expanduser(str(raw)))
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _open_regular_file_fd_nofollow(path: str | Path) -> int:
    """Walk path component-by-component via dir_fd + O_NOFOLLOW and open final regular file."""
    nofollow = require_nofollow()
    expanded = Path(os.path.expanduser(str(path)))
    if expanded.is_symlink():
        raise ValueError(f"Refusing symlinked file: {path}")

    open_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
    dir_flags_base = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dir_flags_nofollow = dir_flags_base | nofollow

    is_abs = expanded.is_absolute()
    raw_parts = expanded.parts[1:] if is_abs else expanded.parts
    parts = [part for part in raw_parts if part not in ("", ".")]
    if not parts or parts[-1] == "..":
        raise ValueError(f"Invalid file path: {path}")

    dfd = os.open(expanded.anchor if is_abs else ".", dir_flags_base)
    try:
        start_idx = 0
        if is_abs and len(parts) > 1 and parts[0] in SYSTEM_ROOT_SYMLINK_NAMES:
            try:
                next_dfd = os.open(parts[0], dir_flags_base, dir_fd=dfd)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"File not found: {path}") from exc
            except OSError as exc:
                raise ValueError(
                    f"Refusing unreadable root directory component: {path} ({exc.strerror or exc})"
                ) from exc
            os.close(dfd)
            dfd = next_dfd
            start_idx = 1

        for part in parts[start_idx:-1]:
            if part == "..":
                raise ValueError(f"Refusing '..' traversal component in path: {path}")
            try:
                next_dfd = os.open(part, dir_flags_nofollow, dir_fd=dfd)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"File not found: {path}") from exc
            except OSError as exc:
                raise ValueError(
                    f"Refusing unreadable or symlinked path component: {path} ({exc.strerror or exc})"
                ) from exc
            os.close(dfd)
            dfd = next_dfd

        try:
            return os.open(parts[-1], open_flags, dir_fd=dfd)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"File not found: {path}") from exc
        except OSError as exc:
            raise ValueError(
                f"Refusing unreadable or symlinked file: {path} ({exc.strerror or exc})"
            ) from exc
    finally:
        os.close(dfd)


def read_regular_file_bounded(path: str | Path, max_bytes: int) -> bytes:
    """Open regular non-symlink file via dir_fd + O_NOFOLLOW and verify type/size via fstat."""
    fd = _open_regular_file_fd_nofollow(path)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"Refusing non-regular file (device/FIFO/dir): {path}")
        if st.st_size > max_bytes:
            raise ValueError(
                f"File exceeds {max_bytes} bytes limit: {path} ({st.st_size} bytes)"
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # ownership transferred to file object
            data = fh.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)

    if len(data) > max_bytes:
        raise ValueError(f"File grew past {max_bytes} bytes limit while reading: {path}")
    return data


def ensure_private_dir(path: Path) -> Path:
    """Walk/create a private 0700 directory via dir_fd + O_NOFOLLOW + fchmod + UID verification."""
    nofollow = require_nofollow()
    raw = expand_path(path)
    parts = [part for part in raw.parts[1:] if part not in ("", ".")]
    if not parts:
        raise OSError(f"Refusing root path as private directory: {path}")
    for part in parts:
        if part == "..":
            raise OSError(f"Refusing parent-traversal component in private directory: {path}")

    dir_flags_base = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dir_flags_nofollow = dir_flags_base | nofollow
    allowed_uids = {os.geteuid(), 0} if hasattr(os, "geteuid") else None

    dfd = os.open(raw.anchor, dir_flags_base)
    try:
        start_idx = 0
        if len(parts) > 1 and parts[0] in SYSTEM_ROOT_SYMLINK_NAMES:
            next_dfd = os.open(parts[0], dir_flags_base, dir_fd=dfd)
            os.close(dfd)
            dfd = next_dfd
            start_idx = 1

        for idx in range(start_idx, len(parts)):
            part = parts[idx]
            try:
                next_dfd = os.open(part, dir_flags_nofollow, dir_fd=dfd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=dfd)
                next_dfd = os.open(part, dir_flags_nofollow, dir_fd=dfd)
                os.fchmod(next_dfd, 0o700)
            except OSError as exc:
                raise OSError(
                    f"Refusing symlinked or non-directory component {part!r} in private directory {path}: {exc.strerror or exc}"
                ) from exc
            os.close(dfd)
            dfd = next_dfd
            st = os.fstat(dfd)
            if not stat.S_ISDIR(st.st_mode):
                raise OSError(f"Refusing non-directory component {part!r} in private directory {path}")
            if allowed_uids is not None and st.st_uid not in allowed_uids:
                raise OSError(f"Refusing directory component {part!r} not owned by current user or root: {path}")

        os.fchmod(dfd, 0o700)
        final_st = os.fstat(dfd)
        if not stat.S_ISDIR(final_st.st_mode):
            raise OSError(f"Private directory is not a regular directory: {raw}")
        if hasattr(os, "geteuid") and final_st.st_uid != os.geteuid():
            raise OSError(f"Private directory is not owned by current user: {raw}")
        if (final_st.st_mode & 0o077) != 0:
            raise OSError(f"Private directory permissions too open ({oct(final_st.st_mode)}): {raw}")
    finally:
        os.close(dfd)
    return raw


def _fsync_dir(dir_path: Path) -> None:
    try:
        dfd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def write_private_file(path: Path, content: str) -> None:
    """Atomically write text file with unpredictable O_EXCL|O_NOFOLLOW temp file and 0600 mode."""
    nofollow = require_nofollow()
    ensure_private_dir(path.parent)
    if path.is_symlink():
        raise OSError(f"Refusing to overwrite symlink: {path}")
    tmp_path = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    fd = os.open(tmp_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1  # ownership transferred to file object
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def state_lock(state_dir: str | Path) -> Iterator[None]:
    """Exclusive process lock on <state_dir>/.lock to prevent concurrent state races."""
    nofollow = require_nofollow()
    base = ensure_private_dir(Path(state_dir))
    lock_path = base / ".lock"
    if lock_path.is_symlink():
        raise OSError(f"Refusing symlinked lock file: {lock_path}")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def validate_model_id(model_id: str) -> str:
    if not isinstance(model_id, str) or not SAFE_TOKEN_RE.match(model_id) or ".." in model_id:
        raise ValueError(f"Invalid model_id: {model_id!r}")
    return model_id


def validate_effort(effort: str) -> str:
    norm = str(effort).strip().lower()
    if not SAFE_EFFORT_RE.match(norm) or norm not in ALLOWED_EFFORTS:
        raise ValueError(f"Invalid effort level: {effort!r}")
    return norm


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not SAFE_RUN_ID_RE.match(run_id) or ".." in run_id:
        raise ValueError(f"Invalid run_id: {run_id!r}")
    return run_id


def resolve_secret(
    env_var: str | None = None,
    secret_command: list[str] | None = None,
) -> str | None:
    """Resolve a secret from environment variable or an explicit argv secret command.

    Never executes a shell string. Never logs the resolved value.
    """
    if env_var:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    if secret_command:
        if not isinstance(secret_command, list) or not all(
            isinstance(part, str) and part for part in secret_command
        ):
            raise ValueError("secret_command must be a non-empty list of strings")
        try:
            proc = subprocess.Popen(
                secret_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        try:
            stdout_text, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.communicate(timeout=3)
            except Exception:
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            return None
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        if proc.returncode == 0:
            val = (stdout_text or "").strip()
            if val:
                return val
    return None


def default_config() -> dict[str, Any]:
    """Generic safe defaults with zero personal data or credentials."""
    return {
        "schema_version": "1.0",
        "state_dir": str(DEFAULT_STATE_DIR),
        "language": "en",
        "selection": {
            "k_cost_doubling": 5.0,
            "quality_buffer": 3.0,
            "min_context_window": 128000,
            "required_capabilities": ["coding", "reasoning"],
            "allow_routers": False,
        },
        "source": {
            "url": DEFAULT_SOURCE_URL,
            "firecrawl_mode": "api",  # "api" | "bridge_command"
            "firecrawl_api_url": "https://api.firecrawl.dev/v2/scrape",
            "firecrawl_api_key_env": "FIRECRAWL_API_KEY",
            "firecrawl_api_key_command": None,
            "firecrawl_bridge_command": None,
            "timeout_seconds": 90,
        },
        "schedule": {
            "timezone": "UTC",
            "daily_hour": 6,
            "daily_minute": 15,
        },
        "notifications": {
            "language": "en",
            "local_log": True,
            "webhook_url_env": "PEER_REVIEW_WEBHOOK_URL",
            "webhook_url_command": None,
            "webhook_format": "slack",  # "slack" | "generic"
        },
        "review": {
            "timeout_seconds": 600,
            "max_clarification_rounds": 1,
            "read_only": True,
        },
        "moderator": {
            "mode": "provider",  # "provider" | "caller"
            "provider": "anthropic",
            "backend": "api",
            "model_id": "claude-fable-5-2",
            "effort": "high",
        },
        "providers": {},
        "catalog": [],
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load configuration file via dir_fd + O_NOFOLLOW with ownership and permission checks."""
    env_path = os.environ.get("PEER_REVIEW_CONFIG")
    path = expand_path(config_path or env_path or DEFAULT_CONFIG_PATH)
    cfg = default_config()
    try:
        fd = _open_regular_file_fd_nofollow(path)
    except FileNotFoundError:
        fd = -1
    if fd >= 0:
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"Config path must be a regular file: {path}")
            if hasattr(os, "geteuid") and st.st_uid not in (os.geteuid(), 0):
                raise ValueError(f"Refusing config file not owned by current user or root: {path}")
            if (st.st_mode & 0o022) != 0:
                raise ValueError(
                    f"Refusing group/world-writable config file: {path} (run `chmod 600 {path}`)"
                )
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                fd = -1
                raw = json.loads(fh.read())
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(raw, dict):
            raise ValueError(f"Config file must contain a JSON object: {path}")
        cfg = _deep_merge(cfg, raw)
    cfg["_config_path"] = str(path)
    cfg["state_dir"] = str(expand_path(cfg["state_dir"]))
    sel = cfg["selection"]
    k_val = float(sel["k_cost_doubling"])
    buf_val = float(sel["quality_buffer"])
    if not math.isfinite(k_val) or k_val <= 0:
        raise ValueError("selection.k_cost_doubling must be a finite number > 0")
    if not math.isfinite(buf_val) or buf_val < 0:
        raise ValueError("selection.quality_buffer must be a finite number >= 0")
    sel["k_cost_doubling"] = k_val
    sel["quality_buffer"] = buf_val
    if int(cfg["review"].get("max_clarification_rounds", 1)) > 1:
        cfg["review"]["max_clarification_rounds"] = 1
    return cfg
