#!/usr/bin/env python3
"""experience-pool consent module — local-only opt-in/opt-out controls.

Single source of truth: ~/.experience-pool/consent.json

Goals:
  * No upload happens unless decide() returns 'always' or the user
    answers 'yes' to a prompt.
  * Decisions cascade: per-session > cwd-glob > per-agent > top-level.
  * `never` cannot be overridden by anything more permissive at a
    deeper layer — it's a hard stop.
  * `ask` mode pops a 30s prompt (osascript / zenity / terminal). The
    timeout default is 'skip', NOT 'upload'.
  * Skipped sessions can be saved to ~/.experience-pool/pending/ for
    later review (default ON, capped at 100 entries / 7 day TTL).

Public API used by exp_uploader.py + auto_upload.sh:
    load_consent() / save_consent()
    decide(agent, cwd, session_id) -> Decision
    prompt(agent, cwd, session_id, ttl_seconds) -> 'yes' | 'no' | 'never_cwd'
    record_session_override(session_id, mode, ttl_seconds=86400)
    set_agent(agent, mode)            set_cwd(glob, mode, reason='')
    set_global(mode)                  reset()
    list_pending() / save_pending(payload) / prune_pending()
    revoke(eid, base_url, cred)       — calls server endpoint
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Paths + constants
# --------------------------------------------------------------------------

INSTALL_DIR = Path(os.environ.get("EXP_INSTALL_DIR", str(Path.home() / ".experience-pool")))
CONSENT_PATH = INSTALL_DIR / "consent.json"
PENDING_DIR = INSTALL_DIR / "pending"
AUDIT_LOG = INSTALL_DIR / "audit.log"

VALID_MODES = frozenset({"always", "never", "ask", "prompt-on-start", "dry-run"})
DEFAULT_MODE = "ask"
PROMPT_TIMEOUT_SECONDS = 30
PROMPT_DEFAULT_ON_TIMEOUT = "no"  # never auto-upload on timeout
PENDING_TTL_DAYS = 7
PENDING_MAX_ENTRIES = 100
SESSION_OVERRIDE_TTL_SECONDS = 24 * 3600  # 1 day default; override per-call


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------


@dataclass
class Decision:
    mode: str               # 'always' | 'never' | 'ask' | 'dry-run' | 'prompt-on-start'
    reason: str             # 'session_override' | 'cwd_rule:<glob>' | 'agent:<name>' | 'global' | 'default'
    rule: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Consent store I/O (atomic, fsync, fallback on corrupt)
# --------------------------------------------------------------------------


def _ensure_dirs() -> None:
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(INSTALL_DIR, 0o700)
    except OSError:
        pass


def default_consent() -> dict[str, Any]:
    return {
        "mode": DEFAULT_MODE,
        "default_acl": "private",
        "save_pending_on_skip": True,
        "agents": {},
        "cwd_rules": [],
        "session_overrides": {},
        "version": 1,
    }


def load_consent() -> dict[str, Any]:
    """Load consent.json. Always returns a dict; falls back to default
    on missing/corrupt files (and backs up the corrupt one)."""
    _ensure_dirs()
    if not CONSENT_PATH.exists():
        return default_consent()
    try:
        data = json.loads(CONSENT_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("consent.json root must be an object")
        # Forward-compat: fill in missing keys.
        merged = default_consent()
        merged.update(data)
        merged.setdefault("agents", {})
        merged.setdefault("cwd_rules", [])
        merged.setdefault("session_overrides", {})
        return merged
    except Exception as exc:
        backup = CONSENT_PATH.with_suffix(".json.corrupt")
        try:
            CONSENT_PATH.replace(backup)
            _audit("consent_corrupt_backup", {"backup": str(backup), "error": str(exc)})
        except OSError:
            pass
        return default_consent()


def save_consent(data: dict[str, Any]) -> None:
    """Atomic write with fsync. Refuses to write invalid mode values."""
    _ensure_dirs()
    _validate_consent(data)
    tmp = CONSENT_PATH.with_suffix(".json.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, CONSENT_PATH)


def _validate_consent(data: dict[str, Any]) -> None:
    mode = data.get("mode", DEFAULT_MODE)
    if mode not in VALID_MODES:
        raise ValueError(f"invalid global mode: {mode!r} (valid: {sorted(VALID_MODES)})")
    for agent, rule in (data.get("agents") or {}).items():
        if not isinstance(rule, dict) or rule.get("mode") not in VALID_MODES:
            raise ValueError(f"agent {agent!r} has invalid rule")
    for entry in (data.get("cwd_rules") or []):
        if entry.get("mode") not in VALID_MODES:
            raise ValueError(f"cwd_rules entry has invalid mode: {entry}")


def reset() -> None:
    save_consent(default_consent())
    _audit("consent_reset", {})


# --------------------------------------------------------------------------
# Setters
# --------------------------------------------------------------------------


def set_global(mode: str) -> None:
    data = load_consent()
    data["mode"] = mode
    save_consent(data)
    _audit("consent_set_global", {"mode": mode})


def set_agent(agent: str, mode: str, default_acl: str | None = None,
              comment: str = "") -> None:
    data = load_consent()
    rule: dict[str, Any] = {"mode": mode}
    if default_acl:
        rule["default_acl"] = default_acl
    if comment:
        rule["comment"] = comment
    data["agents"][agent] = rule
    save_consent(data)
    _audit("consent_set_agent", {"agent": agent, "mode": mode})


def set_cwd(glob: str, mode: str, reason: str = "") -> None:
    data = load_consent()
    expanded = os.path.expanduser(glob)
    # Replace existing rule for the same glob (idempotent).
    rules = [r for r in data["cwd_rules"] if r.get("glob") != expanded]
    rules.append({"glob": expanded, "mode": mode, "reason": reason})
    data["cwd_rules"] = rules
    save_consent(data)
    _audit("consent_set_cwd", {"glob": expanded, "mode": mode, "reason": reason})


def record_session_override(session_id: str, mode: str,
                            ttl_seconds: int = SESSION_OVERRIDE_TTL_SECONDS) -> None:
    """Save a per-session decision so we don't re-prompt on the next tick."""
    if not session_id:
        return
    data = load_consent()
    expires = (_dt.datetime.now(_dt.timezone.utc)
               + _dt.timedelta(seconds=ttl_seconds)).isoformat()
    data["session_overrides"][session_id] = {"mode": mode, "expires_at": expires}
    save_consent(data)
    _audit("consent_session_override", {"session_id": session_id, "mode": mode})


def _prune_expired_session_overrides(data: dict[str, Any]) -> None:
    """Drop session_overrides whose expires_at is in the past. Mutates in place."""
    now = _dt.datetime.now(_dt.timezone.utc)
    keep = {}
    for sid, rule in (data.get("session_overrides") or {}).items():
        try:
            exp = _dt.datetime.fromisoformat(rule.get("expires_at", ""))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_dt.timezone.utc)
            if exp > now:
                keep[sid] = rule
        except Exception:
            # Malformed entry → drop.
            pass
    data["session_overrides"] = keep


# --------------------------------------------------------------------------
# decide() — the priority cascade
# --------------------------------------------------------------------------


def decide(agent: str, cwd: str, session_id: str = "",
           consent: dict[str, Any] | None = None) -> Decision:
    """Determine the upload mode for (agent, cwd, session_id).

    Order: session_overrides > cwd_rules (first match wins by list order)
           > agents[<agent>] > top-level mode > default 'ask'.

    Hard stop: any layer returning 'never' wins immediately.
    """
    persist_back = consent is None
    data = consent if consent is not None else load_consent()
    sov_count_before = len(data.get("session_overrides", {}))
    _prune_expired_session_overrides(data)
    sov_count_after = len(data.get("session_overrides", {}))
    if persist_back and sov_count_after != sov_count_before:
        # An expired session override was just dropped — write back to
        # disk so we don't keep re-pruning forever.
        try:
            save_consent(data)
        except Exception:
            pass

    # Layer 1 — session override (highest priority for explicit user input)
    if session_id:
        sov = data.get("session_overrides", {}).get(session_id)
        if sov and sov.get("mode") in VALID_MODES:
            return Decision(mode=sov["mode"], reason="session_override", rule=sov)

    # Hard-stop scan — never anywhere wins.
    candidates: list[tuple[str, str, dict[str, Any]]] = []

    # Layer 2 — cwd glob match. We use expanduser only (NOT realpath) so
    # symlink trees like macOS /tmp -> /private/tmp don't mismatch user-
    # written globs of the form '/tmp/**'.
    expanded_cwd = os.path.expanduser(cwd or "")
    for rule in data.get("cwd_rules", []):
        glob = os.path.expanduser(rule.get("glob", ""))
        if not glob:
            continue
        if _glob_match(expanded_cwd, glob):
            candidates.append((rule["mode"], f"cwd_rule:{glob}", rule))

    # Layer 3 — agent rule
    agent_rule = data.get("agents", {}).get(agent)
    if agent_rule and agent_rule.get("mode") in VALID_MODES:
        candidates.append((agent_rule["mode"], f"agent:{agent}", agent_rule))

    # Layer 4 — top-level
    top = data.get("mode", DEFAULT_MODE)
    candidates.append((top, "global", {"mode": top}))

    # Hard stop wins regardless of order.
    for mode, reason, rule in candidates:
        if mode == "never":
            return Decision(mode="never", reason=reason, rule=rule)

    # Otherwise return the first non-hard-stop candidate (priority order).
    for mode, reason, rule in candidates:
        return Decision(mode=mode, reason=reason, rule=rule)
    return Decision(mode=DEFAULT_MODE, reason="default", rule={"mode": DEFAULT_MODE})


def _glob_match(path: str, glob: str) -> bool:
    """fnmatch with `**` semantics — `~/work/**` matches any depth under ~/work."""
    if "**" in glob:
        prefix = glob.split("**", 1)[0].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, glob)


# --------------------------------------------------------------------------
# Prompt UI — adaptive: osascript on macOS, zenity on Linux GUI, terminal else
# --------------------------------------------------------------------------


def prompt(agent: str, cwd: str, session_id: str = "",
           timeout_seconds: int = PROMPT_TIMEOUT_SECONDS) -> str:
    """Ask the user whether to upload this session. Returns one of:
        'yes'         — upload this session
        'no'          — skip just this one
        'never_cwd'   — add cwd to never list
        'never_agent' — add agent to never list

    Times out at `timeout_seconds` returning PROMPT_DEFAULT_ON_TIMEOUT ('no').
    Honors EXP_NONINTERACTIVE=1 by returning 'no' immediately.
    """
    if os.environ.get("EXP_NONINTERACTIVE") == "1":
        return "no"
    short_cwd = _shorten_path(cwd)
    short_sid = (session_id or "?")[:8]
    msg_title = "experience-pool"
    msg_body = f"Upload session {short_sid} from {agent}?\nCwd: {short_cwd}"

    # macOS path
    if sys.platform == "darwin" and shutil.which("osascript"):
        ans = _osascript_dialog(msg_title, msg_body, timeout_seconds, cwd)
        if ans is not None:
            return ans

    # Linux GUI path
    if sys.platform.startswith("linux") and shutil.which("zenity"):
        ans = _zenity_dialog(msg_title, msg_body, timeout_seconds)
        if ans is not None:
            return ans

    # Fallback — terminal prompt
    return _terminal_prompt(msg_body, timeout_seconds)


def _shorten_path(p: str, maxlen: int = 60) -> str:
    if not p:
        return "(no cwd)"
    home = str(Path.home())
    if p.startswith(home):
        p = "~" + p[len(home):]
    return p if len(p) <= maxlen else "…" + p[-(maxlen - 1):]


def _osascript_dialog(title: str, body: str, timeout: int, cwd: str) -> str | None:
    btn_yes = "Upload"
    btn_no = "Skip"
    btn_never_cwd = "Never for this dir"
    script = (
        f'display dialog "{body}" with title "{title}" '
        f'buttons {{"{btn_never_cwd}", "{btn_no}", "{btn_yes}"}} '
        f'default button "{btn_no}" with timeout of {timeout} seconds'
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout + 5
        )
    except Exception:
        return None
    if out.returncode != 0:
        # User cancelled OR timeout. Default = skip.
        return PROMPT_DEFAULT_ON_TIMEOUT
    text = out.stdout.strip()
    if btn_yes in text:
        return "yes"
    if btn_never_cwd in text:
        return "never_cwd"
    return "no"


def _zenity_dialog(title: str, body: str, timeout: int) -> str | None:
    try:
        out = subprocess.run(
            ["zenity", "--question", "--title", title,
             "--text", body, "--timeout", str(timeout),
             "--ok-label", "Upload", "--cancel-label", "Skip",
             "--extra-button", "Never for this dir"],
            capture_output=True, text=True, timeout=timeout + 5,
        )
    except Exception:
        return None
    if "Never for this dir" in (out.stdout or ""):
        return "never_cwd"
    return "yes" if out.returncode == 0 else "no"


def _terminal_prompt(body: str, timeout: int) -> str:
    if not sys.stdin.isatty():
        return PROMPT_DEFAULT_ON_TIMEOUT
    try:
        import select
    except ImportError:
        return PROMPT_DEFAULT_ON_TIMEOUT
    sys.stderr.write(
        f"\n[exp] {body}\n"
        f"      [Y]es  [N]o  [V]=never for this cwd  [A]=never for this agent\n"
        f"      (default 'No' in {timeout}s) > "
    )
    sys.stderr.flush()
    rl, _, _ = select.select([sys.stdin], [], [], timeout)
    if not rl:
        sys.stderr.write("(timeout → skip)\n")
        return PROMPT_DEFAULT_ON_TIMEOUT
    line = sys.stdin.readline().strip().lower()
    if line in ("y", "yes"):
        return "yes"
    if line in ("v", "never_cwd"):
        return "never_cwd"
    if line in ("a", "never_agent"):
        return "never_agent"
    return "no"


# --------------------------------------------------------------------------
# Pending queue — dry-run / skipped sessions stored locally for later review
# --------------------------------------------------------------------------


def save_pending(payload: dict[str, Any], session_id: str = "") -> Path:
    """Save a payload to pending/ for later upload. Prunes the queue to
    keep at most PENDING_MAX_ENTRIES, dropping oldest first."""
    _ensure_dirs()
    sid = session_id or payload.get("session_id") or str(int(time.time()))
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:60]
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = PENDING_DIR / f"{ts}_{safe}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    prune_pending()
    _audit("pending_saved", {"path": str(path), "session_id": sid})
    return path


def list_pending() -> list[dict[str, Any]]:
    """Return metadata for each pending file, newest first."""
    _ensure_dirs()
    out: list[dict[str, Any]] = []
    for p in sorted(PENDING_DIR.glob("*.json"), reverse=True):
        try:
            stat = p.stat()
            out.append({
                "path": str(p),
                "size_bytes": stat.st_size,
                "mtime": _dt.datetime.fromtimestamp(stat.st_mtime, _dt.timezone.utc).isoformat(),
                "name": p.name,
            })
        except OSError:
            pass
    return out


def prune_pending(*, max_entries: int | None = None,
                  ttl_days: int | None = None) -> int:
    """Drop pending files older than ttl_days, then drop oldest until at
    most max_entries remain. Returns number of files removed.

    Defaults read from the module-level constants at call time (not at
    function-definition time) so monkey-patching them in tests works.
    """
    _ensure_dirs()
    eff_max = PENDING_MAX_ENTRIES if max_entries is None else max_entries
    eff_ttl = PENDING_TTL_DAYS if ttl_days is None else ttl_days
    cutoff = time.time() - eff_ttl * 86400
    files = sorted(PENDING_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    removed = 0
    surviving: list[Path] = []
    for p in files:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
            else:
                surviving.append(p)
        except OSError:
            pass
    while len(surviving) > eff_max:
        oldest = surviving.pop(0)
        try:
            oldest.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        _audit("pending_pruned", {"removed": removed})
    return removed


# --------------------------------------------------------------------------
# Audit log (append-only, never deleted)
# --------------------------------------------------------------------------


def _audit(event: str, payload: dict[str, Any]) -> None:
    _ensure_dirs()
    line = json.dumps({
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event,
        **payload,
    }, ensure_ascii=False)
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            os.chmod(AUDIT_LOG, 0o600)
        except OSError:
            pass
    except OSError:
        pass


# --------------------------------------------------------------------------
# Format helpers (used by `exp consent show`)
# --------------------------------------------------------------------------


def explain(agent: str, cwd: str, session_id: str = "") -> dict[str, Any]:
    """Return a structured explanation of how a hypothetical decision
    would resolve, useful for `exp consent show --simulate`."""
    decision = decide(agent, cwd, session_id)
    return {
        "agent": agent,
        "cwd": cwd,
        "session_id": session_id,
        "decision": {
            "mode": decision.mode,
            "reason": decision.reason,
            "rule": decision.rule,
        },
    }
