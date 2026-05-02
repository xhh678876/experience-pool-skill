#!/usr/bin/env python3
"""
exp_uploader — universal Experience Pool uploader.

Single file, stdlib only. Detects the agent's session storage on the local
machine, normalizes to a canonical {trajectory, model, ...} shape, runs a
high-confidence client-side sanitizer, and HMAC-signs an upload to
`/v1/lite/push`. The full normalized trajectory plus optional raw bytes
travel together so the server can reconstruct the complete session.

Supported sources:
    claude-code     ~/.claude/projects/**/*.jsonl  (also openclaw-sjtu)
    hermes          ~/.hermes/sessions/*.json[l]   (skips request_dump_*)
    agents-chat     ~/agents-chat/messages.db      (groups by thread_id)
    cursor          ~/Library/Application Support/Cursor/User/**/state.vscdb
    aider           <cwd>/.aider.chat.history.md
    codex           ~/.codex/sessions/**.json[l]
    generic         any JSON containing {"messages":[...]} or {"trajectory":[...]}

Usage:
    exp_uploader register --name <agent> --team <team>
    exp_uploader list-sessions [--source auto|claude-code|hermes|agents-chat|...]
    exp_uploader push --session <id-or-path> [--source X] [--task ...] [--acl ...]
    exp_uploader push-latest [--source auto] [--acl private]
    exp_uploader push-all --source X [--since 2026-04-01] [--limit 50]
    exp_uploader push-file --file traj.json [--acl public]
    exp_uploader whoami

Env:
    EXP_BASE_URL       gateway URL (default https://expool.clawsii.com)
    EXP_AGENT_NAME     agent identifier (override register)
    EXP_AGENT_SECRET   HMAC secret (override register)
    EXP_CRED_DIR       credential storage dir (default ~/.experience-pool/credentials)

Credentials are stored at $EXP_CRED_DIR/<agent>.json (mode 0600).
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_BASE_URL = os.environ.get("EXP_BASE_URL", "https://expool.clawsii.com")
DEFAULT_CRED_DIR = Path(
    os.environ.get("EXP_CRED_DIR", str(Path.home() / ".experience-pool" / "credentials"))
)
USER_AGENT = "exp_uploader/0.2 (python-stdlib)"


# ---------------------------------------------------------------------------
# Sanitizer (client-side, high-confidence rules; server runs full set again).
# ---------------------------------------------------------------------------

_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # ----- High-severity: keys / tokens / credentials -----
    # PEM blocks must run BEFORE other key matchers — they span multiple lines.
    ("pem_private_key",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
                r"[\s\S]*?"
                r"-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
     "<PRIVATE_KEY>"),
    ("ssh_public_key",
     # ed25519 keys are ~68 chars; rsa keys 200+. Set min to 50 to catch all
     # real keys but reject random short strings like "ssh-rsa abc".
     re.compile(r"ssh-(?:rsa|ed25519|dss|ecdsa(?:-[a-z0-9\-]+)?)\s+[A-Za-z0-9+/=]{50,}"),
     "<SSH_PUBLIC_KEY>"),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "<SECRET>"),
    ("openai_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{20,}\b"), "<SECRET>"),
    ("stripe_secret", re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}\b"), "<SECRET>"),
    ("stripe_test", re.compile(r"\bsk_test_[0-9a-zA-Z]{16,}\b"), "<SECRET>"),
    ("github_token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[0-9a-zA-Z]{20,}\b"), "<SECRET>"),
    ("slack_token", re.compile(r"\bxox[abprso]-[0-9A-Za-z\-]{10,}\b"), "<SECRET>"),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_KEY>"),
    ("aws_secret_key",
     re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})[\"']?"),
     "aws_secret_access_key=<SECRET>"),
    ("gcp_service_account",
     re.compile(r"\"private_key\"\s*:\s*\"-----BEGIN[\s\S]+?-----END[^\"]+\""),
     '"private_key": "<SECRET>"'),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
     "<JWT>"),
    ("bearer_token",
     re.compile(r"(?i)(Authorization:\s*Bearer\s+|^Bearer\s+|\bBearer\s+)([A-Za-z0-9._\-]{16,})"),
     r"\1<TOKEN>"),
    ("generic_api_key",
     re.compile(r"(?i)(api[_-]?key|access[_-]?token|secret[_-]?key|"
                r"password|api_secret|client_secret)\s*[:=]\s*"
                r"['\"]?([A-Za-z0-9_\-]{12,})['\"]?"),
     r"\1=<SECRET>"),
    ("url_credentials",
     re.compile(r"https?://([^\s/:@]+):([^\s/@]+)@"),
     "https://<USER>:<PASS>@"),

    # ----- PII: contact + identity -----
    ("email",
     re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "<EMAIL>"),
    # Chinese mobile numbers: 11 digits, 1[3-9]xxxxxxxxx
    ("cn_phone",
     re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
     "<PHONE>"),
    # Generic international phone (e.g. +1 415 555 1212, +44 ...)
    ("intl_phone",
     re.compile(r"(?<!\d)\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}(?!\d)"),
     "<PHONE>"),
    # Chinese national ID: 18 digits, last can be X
    ("cn_id_card",
     re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}"
                r"(?:0[1-9]|1[012])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
     "<CN_ID>"),
    # Credit card-shaped 13-19 digit run (no Luhn check on client; server does).
    ("credit_card_shape",
     re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
     "<CARD>"),
    # IP addresses
    ("ipv4",
     re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"),
     "<IP>"),
    ("ipv6",
     # Cover both full 8-group and `::` compressed notation. Anchor on
     # word/colon boundaries to avoid eating ordinary text.
     re.compile(
         r"(?<![A-Za-z0-9:])"
         r"(?:"
           r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"        # full
           r"|(?:[0-9a-fA-F]{1,4}:){1,7}:"                     # 1::
           r"|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}"     # 1:2::3
           r"|(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}"
           r"|(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}"
           r"|(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}"
           r"|(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}"
           r"|[0-9a-fA-F]{1,4}:(?::[0-9a-fA-F]{1,4}){1,6}"
           r"|::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}"     # ::1
         r")"
         r"(?![A-Za-z0-9:])"),
     "<IP>"),

    # ----- Internal identifiers (configurable via env vars) -----
    # EXP_INTERNAL_HOST_SUFFIXES=corp.example.com,internal.example.com
    # EXP_EMP_ID_PREFIXES=EMP,STAFF,SJTU
]


def _build_dynamic_rules() -> list[tuple[str, re.Pattern[str], str]]:
    """Allow operators to add domain-specific patterns at runtime via env."""
    extra: list[tuple[str, re.Pattern[str], str]] = []
    suffixes = [s.strip() for s in os.environ.get("EXP_INTERNAL_HOST_SUFFIXES", "").split(",")
                if s.strip()]
    if suffixes:
        # Build a single regex that matches any internal host suffix.
        # \b<sub.>?<suffix>\b
        alt = "|".join(re.escape(s) for s in suffixes)
        extra.append((
            "internal_host",
            re.compile(rf"\b(?:[A-Za-z0-9_\-]+\.)*(?:{alt})\b"),
            "<INTERNAL_HOST>",
        ))
    prefixes = [p.strip() for p in os.environ.get("EXP_EMP_ID_PREFIXES", "").split(",")
                if p.strip()]
    if prefixes:
        alt = "|".join(re.escape(p) for p in prefixes)
        extra.append((
            "employee_id",
            re.compile(rf"(?i)\b(?:{alt})\d{{4,10}}\b"),
            "<EMP_ID>",
        ))
    return extra


_DYNAMIC_RULES = _build_dynamic_rules()


def sanitize(text: str) -> tuple[str, dict[str, int]]:
    if not isinstance(text, str) or not text:
        return text or "", {}
    counts: dict[str, int] = {}
    out = text
    for name, pat, placeholder in _RULES + _DYNAMIC_RULES:
        new, n = pat.subn(placeholder, out)
        if n:
            counts[name] = counts.get(name, 0) + n
            out = new
    return out, counts


# ---------------------------------------------------------------------------
# Canonical session shape produced by every adapter.
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    role: str  # "user" | "assistant" | "system" | "tool"
    content: str  # already string-coerced for upload
    ts: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_result_for: str = ""


@dataclass
class Session:
    agent_type: str
    session_id: str
    started_at: str
    ended_at: str
    model: str
    cwd: str
    agent_version: str
    trajectory: list[Turn]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "model": self.model,
            "cwd": self.cwd,
            "agent_version": self.agent_version,
            "trajectory": [asdict(t) for t in self.trajectory],
            "extra": self.extra,
        }


# ---------------------------------------------------------------------------
# Adapter: Claude Code (~/.claude/projects/<encoded-cwd>/<uuid>.jsonl)
# ---------------------------------------------------------------------------

class ClaudeCodeAdapter:
    """Claude Code + any compatible fork (OpenClaw, hermes-claude bridges)
    that stores transcript JSONL files in <root>/<encoded-cwd>/<uuid>.jsonl.

    Roots are configurable via $EXP_CLAUDE_ROOTS (comma-separated). Defaults
    cover ~/.claude/projects, ~/.openclaw/projects, ~/.openclaw-sjtu/projects.
    """

    name = "claude-code"

    @staticmethod
    def roots() -> list[Path]:
        env = os.environ.get("EXP_CLAUDE_ROOTS")
        if env:
            return [Path(p).expanduser() for p in env.split(",") if p.strip()]
        candidates = [
            Path.home() / ".claude" / "projects",
            Path.home() / ".openclaw" / "projects",
            Path.home() / ".openclaw-sjtu" / "projects",
            Path.home() / "openclaw-sjtu" / "data" / "projects",
        ]
        return [p for p in candidates if p.is_dir()]

    @classmethod
    def available(cls) -> bool:
        return any(r.is_dir() for r in cls.roots())

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        files: list[Path] = []
        for root in cls.roots():
            files.extend(root.glob("*/*.jsonl"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out: list[dict[str, Any]] = []
        for f in files[:limit]:
            head: dict[str, Any] = {}
            # cwd/version may not appear on the FIRST line; scan up to 5.
            try:
                with f.open(encoding="utf-8") as fp:
                    for i, line in enumerate(fp):
                        if i >= 5:
                            break
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("cwd") and not head.get("cwd"):
                            head["cwd"] = obj["cwd"]
                        if obj.get("version") and not head.get("version"):
                            head["version"] = obj["version"]
                        if head.get("cwd") and head.get("version"):
                            break
            except OSError:
                pass
            out.append({
                "id": f.stem,
                "path": str(f),
                "mtime": _dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
                "size_bytes": f.stat().st_size,
                "cwd": head.get("cwd", ""),
                "version": head.get("version", ""),
                "root": str(f.parent.parent),
            })
        return out

    @classmethod
    def resolve_session(cls, ident: str) -> Path:
        p = Path(ident).expanduser()
        if p.is_file():
            return p
        for root in cls.roots():
            for f in root.glob(f"*/{ident}.jsonl"):
                return f
            for f in root.glob("*/*.jsonl"):
                if f.stem.startswith(ident):
                    return f
        raise FileNotFoundError(f"claude-code session not found: {ident}")

    @classmethod
    def latest_session(cls) -> Path:
        files: list[Path] = []
        for root in cls.roots():
            files.extend(root.glob("*/*.jsonl"))
        if not files:
            raise FileNotFoundError(
                "no claude-code sessions; checked: "
                + ", ".join(str(r) for r in cls.roots())
            )
        return max(files, key=lambda p: p.stat().st_mtime)

    @classmethod
    def parse(cls, path: Path) -> Session:
        """Order-based extraction (avoids parentUuid breakage). Mirrors the
        single-turn approach used in claude_sft_delivery/extractor/scanner.py
        and builder.py — emit user/assistant/tool turns in file order.
        """
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        latest_version = ""
        latest_model = ""
        latest_cwd = ""
        started_at = ""
        ended_at = ""
        turns: list[Turn] = []

        for raw in lines:
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue

            v = obj.get("version", "")
            if v:
                latest_version = v
            cwd = obj.get("cwd", "")
            if cwd:
                latest_cwd = cwd
            ts = obj.get("timestamp", "")
            if ts:
                if not started_at:
                    started_at = ts
                ended_at = ts

            t = obj.get("type")
            if t == "assistant":
                msg = obj.get("message", {}) or {}
                m = msg.get("model", "")
                if m and m != "<synthetic>":
                    latest_model = m
                content = msg.get("content", [])
                if isinstance(content, list):
                    text_parts: list[str] = []
                    tool_calls: list[dict[str, Any]] = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                            })
                    text = "\n".join(p for p in text_parts if p.strip())
                    if text or tool_calls:
                        turns.append(Turn(
                            role="assistant",
                            content=text,
                            ts=ts,
                            tool_calls=tool_calls,
                        ))
            elif t == "user":
                msg = obj.get("message", {}) or {}
                if msg.get("model") == "<synthetic>":
                    continue
                content = msg.get("content", [])
                if isinstance(content, str):
                    if content.strip():
                        turns.append(Turn(role="user", content=content, ts=ts))
                    continue
                if not isinstance(content, list):
                    continue
                tool_results: list[Turn] = []
                text_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "tool_result":
                        result_text = block.get("content", "")
                        if isinstance(result_text, list):
                            result_text = "\n".join(
                                str(b.get("text", "")) for b in result_text
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        elif not isinstance(result_text, str):
                            result_text = json.dumps(result_text, ensure_ascii=False)
                        tool_results.append(Turn(
                            role="tool",
                            content=result_text,
                            ts=ts,
                            tool_result_for=block.get("tool_use_id", ""),
                        ))
                    elif bt == "text":
                        text_parts.append(block.get("text", ""))
                user_text = "\n".join(p for p in text_parts if p.strip())
                if user_text.strip() and not user_text.startswith("[Request interrupted"):
                    turns.append(Turn(role="user", content=user_text, ts=ts))
                turns.extend(tool_results)

        return Session(
            agent_type=cls.name,
            session_id=path.stem,
            started_at=started_at,
            ended_at=ended_at,
            model=latest_model or "unknown",
            cwd=latest_cwd,
            agent_version=latest_version,
            trajectory=turns,
            extra={"source_path": str(path)},
        )


# ---------------------------------------------------------------------------
# Adapter: Cursor (state.vscdb SQLite + protobuf blobs).
#
# This wraps the proven cursor_sft_delivery extractor when present locally
# (preferred — it handles the protobuf wire format and v13 schema). Falls
# back to a minimal "list workspaces" probe otherwise so the user gets a
# clear "install the official extractor" message rather than a silent miss.
# ---------------------------------------------------------------------------

class CursorAdapter:
    name = "cursor"

    @staticmethod
    def user_dirs() -> list[Path]:
        candidates = [
            Path.home() / "Library" / "Application Support" / "Cursor" / "User",
            Path.home() / ".config" / "Cursor" / "User",
            Path(os.environ.get("APPDATA", "")) / "Cursor" / "User"
            if os.environ.get("APPDATA") else None,
        ]
        return [p for p in candidates if p and p.is_dir()]

    @classmethod
    def available(cls) -> bool:
        return any(d.exists() for d in cls.user_dirs())

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for user_dir in cls.user_dirs():
            for state_db in user_dir.glob("workspaceStorage/*/state.vscdb"):
                if not state_db.is_file():
                    continue
                try:
                    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
                    try:
                        rows = list(conn.execute(
                            "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%' LIMIT ?",
                            (limit,),
                        ))
                    finally:
                        conn.close()
                except sqlite3.Error:
                    continue
                for (key,) in rows:
                    composer_id = key.replace("composerData:", "")
                    out.append({
                        "id": composer_id,
                        "path": str(state_db),
                        "mtime": _dt.datetime.fromtimestamp(state_db.stat().st_mtime).isoformat(timespec="seconds"),
                        "workspace": state_db.parent.parent.name,
                    })
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        return out[:limit]

    @classmethod
    def parse(cls, ident: str) -> Session:
        """Cursor extraction needs the v13_data_convert pipeline (protobuf parse,
        ~1000 LoC). We prefer to delegate to a sibling cursor_sft_delivery
        checkout when present, then re-read its enriched JSONL."""
        helper_root = _find_cursor_extractor()
        if helper_root is None:
            raise SystemExit(
                "[cursor] Cursor session extraction needs the cursor_sft_delivery\n"
                "extractor (it parses the protobuf-encoded v13 schema). Either:\n"
                "  1. clone https://expool.clawsii.com/cursor_sft_delivery (or your\n"
                "     internal mirror) under ~/cursor_sft_delivery, or\n"
                "  2. run that pipeline yourself and pass --source generic with\n"
                "     the resulting v13_training_data_enriched.jsonl"
            )
        # Resolve the requested session id within the latest enriched JSONL,
        # or run the pipeline first if it hasn't been run.
        cache_dir = Path.home() / ".experience-pool" / "cache" / "cursor"
        cache_dir.mkdir(parents=True, exist_ok=True)
        out_jsonl = cache_dir / "v13_training_data_enriched.jsonl"
        if not out_jsonl.exists() or _stale(out_jsonl, hours=6):
            _run_cursor_extractor(helper_root, cache_dir)
        return _read_cursor_enriched(out_jsonl, ident)


def _find_cursor_extractor() -> Path | None:
    candidates = [
        Path.home() / "cursor_sft_delivery",
        Path.home() / "Downloads" / "cursor_sft_delivery",
        Path(os.environ.get("CURSOR_SFT_DIR", "")) if os.environ.get("CURSOR_SFT_DIR") else None,
    ]
    for c in candidates:
        if c and (c / "scripts" / "full_pipeline.py").exists():
            return c
    return None


def _stale(p: Path, hours: int) -> bool:
    age_h = (_dt.datetime.now().timestamp() - p.stat().st_mtime) / 3600.0
    return age_h > hours


def _run_cursor_extractor(root: Path, cache: Path) -> None:
    import subprocess
    rc = subprocess.call(
        [sys.executable, str(root / "collector" / "v13_data_convert.py"),
         "--output", str(cache / "v13_training_data.jsonl"),
         "--meta", str(cache / "v13_data_convert_meta.jsonl")],
        cwd=str(root),
    )
    if rc != 0:
        raise SystemExit(f"[cursor] extractor exit {rc}")
    rc = subprocess.call(
        [sys.executable, str(root / "collector" / "v13_enrich_turns.py"),
         str(cache / "v13_training_data.jsonl"),
         "-o", str(cache / "v13_training_data_enriched.jsonl")],
        cwd=str(root),
    )
    if rc != 0:
        raise SystemExit(f"[cursor] enricher exit {rc}")


def _read_cursor_enriched(jsonl_path: Path, ident: str) -> Session:
    target = ident.lower()
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = rec.get("session_id") or rec.get("composer_id") or rec.get("id") or ""
        if target == "latest" or sid.lower().startswith(target):
            messages = rec.get("messages") or rec.get("trajectory") or []
            turns: list[Turn] = []
            model = ""
            for m in messages:
                role = m.get("role", "")
                content = m.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                turns.append(Turn(role=role or "user", content=content))
                if not model and m.get("model"):
                    model = m["model"]
            return Session(
                agent_type=CursorAdapter.name,
                session_id=sid,
                started_at=rec.get("started_at", ""),
                ended_at=rec.get("ended_at", ""),
                model=model or rec.get("model", "unknown"),
                cwd=rec.get("cwd", ""),
                agent_version=rec.get("cursor_version", ""),
                trajectory=turns,
                extra={"mode": rec.get("mode", "")},
            )
    raise FileNotFoundError(f"cursor session id not found in enriched output: {ident}")


# ---------------------------------------------------------------------------
# Adapter: Hermes Agent (~/.hermes/sessions/*.json[l])
#
# File patterns:
#   request_dump_*.json         debug request dumps — skipped
#   session_*.json              JSON object with request.body.messages
#   <yyyymmdd>_<hash>.jsonl     line-per-turn {role, content[, ...]}
# ---------------------------------------------------------------------------

class HermesAdapter:
    name = "hermes"

    @staticmethod
    def roots() -> list[Path]:
        env = os.environ.get("EXP_HERMES_ROOT")
        if env:
            return [Path(env).expanduser()]
        seen: set[str] = set()
        out: list[Path] = []
        for p in (Path.home() / ".hermes" / "sessions",
                  Path.home() / ".Hermes" / "sessions"):
            if p.is_dir():
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    out.append(p)
        return out

    @classmethod
    def available(cls) -> bool:
        return bool(cls.roots())

    @staticmethod
    def _is_session_file(p: Path) -> bool:
        if p.name.startswith("request_dump_"):
            return False
        return p.suffix in {".json", ".jsonl"}

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        files: list[Path] = []
        for root in cls.roots():
            for f in root.iterdir():
                if f.is_file() and cls._is_session_file(f):
                    files.append(f)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for f in files[:limit]:
            kind = "json-bundle" if f.suffix == ".json" else "jsonl"
            out.append({
                "id": f.stem,
                "path": str(f),
                "mtime": _dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
                "size_bytes": f.stat().st_size,
                "format": kind,
            })
        return out

    @classmethod
    def resolve_session(cls, ident: str) -> Path:
        p = Path(ident).expanduser()
        if p.is_file():
            return p
        for root in cls.roots():
            for cand in root.iterdir():
                if not cand.is_file() or not cls._is_session_file(cand):
                    continue
                if cand.stem == ident or cand.stem.startswith(ident):
                    return cand
        raise FileNotFoundError(f"hermes session not found: {ident}")

    @classmethod
    def parse(cls, ident: str | Path) -> Session:
        path = Path(ident).expanduser() if isinstance(ident, (str, Path)) and Path(ident).expanduser().is_file() \
               else cls.resolve_session(str(ident))
        if path.suffix == ".jsonl":
            return cls._parse_jsonl(path)
        return cls._parse_json_bundle(path)

    @classmethod
    def _parse_jsonl(cls, path: Path) -> Session:
        turns: list[Turn] = []
        model = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("role") or obj.get("type") or "user"
            content = obj.get("content", "")
            tool_calls: list[dict[str, Any]] = []
            tool_result_for = ""
            if isinstance(content, list):
                # Anthropic-style content blocks
                text_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text":
                        text_parts.append(block.get("text", ""))
                    elif bt == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        })
                    elif bt == "tool_result":
                        tool_result_for = block.get("tool_use_id", "")
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rc = "\n".join(b.get("text", "") for b in rc
                                           if isinstance(b, dict) and b.get("type") == "text")
                        text_parts.append(rc if isinstance(rc, str) else json.dumps(rc, ensure_ascii=False))
                content = "\n".join(p for p in text_parts if p)
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if not content.strip() and not tool_calls:
                continue
            turns.append(Turn(
                role=role, content=content,
                ts=obj.get("ts", obj.get("timestamp", "")),
                tool_calls=tool_calls,
                tool_result_for=tool_result_for,
            ))
            if not model:
                model = obj.get("model", "") or obj.get("source_model", "")
        return Session(
            agent_type=cls.name,
            session_id=path.stem,
            started_at="",
            ended_at=_dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            model=model or "hermes-unknown",
            cwd="",
            agent_version="",
            trajectory=turns,
            extra={"source_path": str(path), "format": "jsonl"},
        )

    @classmethod
    def _parse_json_bundle(cls, path: Path) -> Session:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"[hermes] {path.name} is not valid JSON: {e}")
        if not isinstance(data, dict):
            raise SystemExit(f"[hermes] {path.name} top-level is not an object")
        # Two layouts seen in the wild:
        #   A) {"messages": [...], "model": "...", "session_id": "..."}
        #   B) {"request": {"body": {"messages": [...], "model": "..."}}, ...}
        body = data.get("request", {}).get("body", {}) if isinstance(data.get("request"), dict) else {}
        messages = data.get("messages") or data.get("history") or data.get("turns") \
            or body.get("messages") or []
        model = data.get("model") or body.get("model") or ""
        system_msgs: list[dict[str, Any]] = []
        turns: list[Turn] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text_parts.append(b.get("text", ""))
                content = "\n".join(text_parts)
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role == "system":
                system_msgs.append({"role": "system", "content": content})
                continue
            if not content.strip():
                continue
            turns.append(Turn(role=role, content=content))
        return Session(
            agent_type=cls.name,
            session_id=data.get("session_id") or path.stem,
            started_at=data.get("timestamp", ""),
            ended_at=_dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            model=model or "hermes-unknown",
            cwd=data.get("cwd", ""),
            agent_version=data.get("hermes_version", data.get("version", "")),
            trajectory=turns,
            extra={
                "source_path": str(path),
                "format": "json-bundle",
                "system": system_msgs,
                "tools": body.get("tools", []),
                "reason": data.get("reason", ""),
            },
        )


# ---------------------------------------------------------------------------
# Adapter: agents-chat (~/agents-chat/messages.db, SQLite, multi-agent peer)
# Each thread_id is one "session". Trace is the chronological message list.
# ---------------------------------------------------------------------------

class AgentsChatAdapter:
    name = "agents-chat"

    @staticmethod
    def db_paths() -> list[Path]:
        env = os.environ.get("EXP_AGENTS_CHAT_DB")
        if env:
            return [Path(env).expanduser()]
        return [p for p in [
            Path.home() / "agents-chat" / "messages.db",
        ] if p.is_file()]

    @classmethod
    def available(cls) -> bool:
        return bool(cls.db_paths())

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for db in cls.db_paths():
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                rows = conn.execute(
                    "SELECT thread_id, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
                    "GROUP_CONCAT(DISTINCT sender) AS senders "
                    "FROM messages GROUP BY thread_id ORDER BY last_ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                conn.close()
            except sqlite3.Error as e:
                continue
            for thread_id, n, first_ts, last_ts, senders in rows:
                out.append({
                    "id": thread_id,
                    "path": str(db),
                    "msg_count": n,
                    "started_at": first_ts,
                    "ended_at": last_ts,
                    "participants": (senders or "").split(","),
                })
        return out

    @classmethod
    def parse(cls, ident: str) -> Session:
        db = cls.db_paths()[0] if cls.db_paths() else None
        if db is None:
            raise SystemExit("[agents-chat] no messages.db found")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT id, sender, recipient, content, attachments, workflow, stage, ts, reply_to "
            "FROM messages WHERE thread_id = ? ORDER BY ts ASC", (ident,),
        ).fetchall()
        conn.close()
        if not rows:
            # Allow prefix match
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT id, sender, recipient, content, attachments, workflow, stage, ts, reply_to, thread_id "
                "FROM messages WHERE thread_id LIKE ? ORDER BY ts ASC LIMIT 500", (f"{ident}%",),
            ).fetchall()
            conn.close()
            if not rows:
                raise FileNotFoundError(f"agents-chat thread not found: {ident}")
        turns: list[Turn] = []
        senders = set()
        first_ts = rows[0][7]
        last_ts = rows[-1][7]
        for row in rows:
            mid, sender, recipient, content, attachments, workflow, stage, ts = row[:8]
            senders.add(sender)
            # Map sender→role: anything that's the human user is "user"; agents "assistant"
            role = "assistant" if sender not in {"xiehaohui", "xiaohui", "user", "human"} else "user"
            tag = f"[{sender}→{recipient}{f' /{workflow}/{stage}' if workflow else ''}] "
            turns.append(Turn(role=role, content=tag + (content or ""), ts=ts or ""))
        return Session(
            agent_type=cls.name,
            session_id=ident,
            started_at=first_ts or "",
            ended_at=last_ts or "",
            model="multi-agent",
            cwd=str(db.parent),
            agent_version="",
            trajectory=turns,
            extra={"participants": sorted(senders), "db": str(db)},
        )


# ---------------------------------------------------------------------------
# Adapter: Aider (project-local .aider.chat.history.md)
# ---------------------------------------------------------------------------

_AIDER_TURN_RE = re.compile(r"^####\s+(.*)$", re.MULTILINE)


class AiderAdapter:
    name = "aider"

    @staticmethod
    def history_paths(cwd: Path | None = None) -> list[Path]:
        cwd = cwd or Path.cwd()
        return [p for p in [cwd / ".aider.chat.history.md"] if p.exists()]

    @classmethod
    def available(cls) -> bool:
        return bool(cls.history_paths())

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for h in cls.history_paths():
            out.append({
                "id": h.parent.name,
                "path": str(h),
                "mtime": _dt.datetime.fromtimestamp(h.stat().st_mtime).isoformat(timespec="seconds"),
                "size_bytes": h.stat().st_size,
            })
        return out[:limit]

    @classmethod
    def parse(cls, path: Path | str) -> Session:
        p = Path(path).expanduser()
        if p.is_dir():
            p = p / ".aider.chat.history.md"
        text = p.read_text(encoding="utf-8")
        # Aider history uses `#### user message` then `<assistant text>` blocks.
        # Split on user-turn markers.
        chunks = re.split(r"^####\s+", text, flags=re.MULTILINE)
        turns: list[Turn] = []
        # First chunk is preamble — skip.
        for chunk in chunks[1:]:
            head, _, rest = chunk.partition("\n")
            user_msg = head.strip()
            assistant_msg = rest.strip()
            if user_msg:
                turns.append(Turn(role="user", content=user_msg))
            if assistant_msg:
                turns.append(Turn(role="assistant", content=assistant_msg))
        return Session(
            agent_type=cls.name,
            session_id=p.parent.name,
            started_at="",
            ended_at=_dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            model="aider-unknown",
            cwd=str(p.parent),
            agent_version="",
            trajectory=turns,
            extra={"source_path": str(p)},
        )


# ---------------------------------------------------------------------------
# Adapter: Continue.dev (VSCode extension; sessions under ~/.continue/sessions)
# ---------------------------------------------------------------------------

class ContinueDevAdapter:
    name = "continue-dev"

    @staticmethod
    def root() -> Path:
        return Path.home() / ".continue" / "sessions"

    @classmethod
    def available(cls) -> bool:
        return cls.root().is_dir()

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        root = cls.root()
        if not root.is_dir():
            return []
        files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{
            "id": p.stem,
            "path": str(p),
            "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "size_bytes": p.stat().st_size,
        } for p in files[:limit]]

    @classmethod
    def parse(cls, ident: str) -> Session:
        p = Path(ident).expanduser()
        if not p.is_file():
            for f in cls.root().glob(f"{ident}*.json"):
                p = f
                break
        if not p.is_file():
            raise FileNotFoundError(f"continue.dev session not found: {ident}")
        data = json.loads(p.read_text(encoding="utf-8"))
        # Continue.dev shape: {"history":[{"message":{"role,content},"contextItems":[...]}]}
        history = data.get("history") or data.get("messages") or []
        turns: list[Turn] = []
        model = data.get("model") or ""
        for item in history:
            msg = item.get("message", item) if isinstance(item, dict) else {}
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if not content.strip():
                continue
            turns.append(Turn(role=role, content=content))
        return Session(
            agent_type=cls.name,
            session_id=p.stem,
            started_at="",
            ended_at=_dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            model=model or "continue-unknown",
            cwd="",
            agent_version="",
            trajectory=turns,
            extra={"source_path": str(p)},
        )


# ---------------------------------------------------------------------------
# Adapter: Open Interpreter (~/Library/Application Support/Open Interpreter/...)
# Files: <profile>/conversations/<id>.json — OpenAI-shaped messages.
# ---------------------------------------------------------------------------

class OpenInterpreterAdapter:
    name = "open-interpreter"

    @staticmethod
    def roots() -> list[Path]:
        candidates = [
            Path.home() / "Library" / "Application Support" / "Open Interpreter" / "profiles",
            Path.home() / ".config" / "Open Interpreter" / "profiles",
            Path.home() / ".cache" / "open-interpreter" / "conversations",
        ]
        return [p for p in candidates if p.is_dir()]

    @classmethod
    def available(cls) -> bool:
        return bool(cls.roots())

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        files: list[Path] = []
        for r in cls.roots():
            files.extend(r.rglob("*.json"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [{
            "id": p.stem,
            "path": str(p),
            "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "size_bytes": p.stat().st_size,
        } for p in files[:limit]]

    @classmethod
    def parse(cls, ident: str) -> Session:
        p = Path(ident).expanduser()
        if not p.is_file():
            for r in cls.roots():
                for f in r.rglob(f"{ident}*"):
                    if f.is_file():
                        p = f
                        break
        if not p.is_file():
            raise FileNotFoundError(f"open-interpreter session not found: {ident}")
        return GenericAdapter.parse(str(p))


# ---------------------------------------------------------------------------
# Adapter: Codex CLI (~/.codex/sessions/*.json[l])
# ---------------------------------------------------------------------------

class CodexAdapter:
    name = "codex"

    @staticmethod
    def root() -> Path:
        return Path.home() / ".codex" / "sessions"

    @classmethod
    def available(cls) -> bool:
        return cls.root().is_dir()

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        root = cls.root()
        if not root.is_dir():
            return []
        files = sorted(
            list(root.rglob("*.json")) + list(root.rglob("*.jsonl")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [{
            "id": p.stem,
            "path": str(p),
            "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "size_bytes": p.stat().st_size,
        } for p in files[:limit]]

    @classmethod
    def parse(cls, ident: str) -> Session:
        p = Path(ident).expanduser()
        if not p.is_file():
            for f in cls.root().rglob(f"{ident}*"):
                p = f
                break
        if not p.is_file():
            raise FileNotFoundError(f"codex session not found: {ident}")
        turns: list[Turn] = []
        text = p.read_text(encoding="utf-8")
        # Try JSONL first, fall back to single JSON object.
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except json.JSONDecodeError:
                records = []
                break
        if not records:
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    records = obj.get("messages") or obj.get("turns") or [obj]
            except json.JSONDecodeError:
                pass
        model = ""
        for rec in records:
            role = rec.get("role") or rec.get("type") or ""
            content = rec.get("content") or rec.get("text") or rec.get("message", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(b.get("text", "")) for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role and content.strip():
                turns.append(Turn(role=role, content=content))
            if not model and rec.get("model"):
                model = rec["model"]
        return Session(
            agent_type=cls.name,
            session_id=p.stem,
            started_at="",
            ended_at=_dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            model=model or "codex-unknown",
            cwd=str(p.parent),
            agent_version="",
            trajectory=turns,
            extra={"source_path": str(p)},
        )


# ---------------------------------------------------------------------------
# Adapter: Generic JSON ({"messages":[...]} or {"trajectory":[...]} or [...])
# ---------------------------------------------------------------------------

class GenericAdapter:
    """Universal fallback. Auto-detects common shapes:

    1. JSON array of messages — `[{role, content}, ...]` (OpenAI chat-completion,
       LangChain `chat_history`, AutoGen logs, CrewAI traces).
    2. JSON object with one of: `trajectory`, `messages`, `turns`, `history`,
       `conversation`, `chat_history`, `request.body.messages`.
    3. JSONL — one JSON object per line, each with `role`/`content` (plus
       optional `tool_calls`, `tool_call_id`, `name`, `timestamp`).
    4. LangSmith-style runs: `{"runs":[{"inputs":{"messages":[...]},"outputs":...}]}`.
    5. AutoGen group-chat: `[{"name":"agent_x","content":"..."}]`.
    6. Plain text — read as a single user turn.
    """

    name = "generic"

    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def list_sessions(cls, limit: int = 50) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def _extract_message_list(raw: Any) -> tuple[list[Any], dict[str, Any]]:
        if isinstance(raw, list):
            return raw, {}
        if isinstance(raw, dict):
            for key in ("trajectory", "messages", "turns", "history",
                        "conversation", "chat_history", "events"):
                if isinstance(raw.get(key), list) and raw[key]:
                    meta = {k: v for k, v in raw.items() if k != key}
                    return raw[key], meta
            # request.body.messages (OpenAI request dump)
            body = raw.get("request", {}).get("body", {}) if isinstance(raw.get("request"), dict) else {}
            if isinstance(body.get("messages"), list):
                meta = {k: v for k, v in raw.items() if k != "request"}
                meta["request_meta"] = {k: v for k, v in body.items() if k != "messages"}
                return body["messages"], meta
            # LangSmith-style
            runs = raw.get("runs")
            if isinstance(runs, list) and runs:
                msgs: list[Any] = []
                for r in runs:
                    inp = r.get("inputs", {}).get("messages") if isinstance(r, dict) else None
                    if inp:
                        msgs.extend(inp)
                    out = r.get("outputs") if isinstance(r, dict) else None
                    if isinstance(out, dict):
                        msgs.append({"role": "assistant", "content": json.dumps(out, ensure_ascii=False)})
                if msgs:
                    return msgs, {k: v for k, v in raw.items() if k != "runs"}
        return [], {}

    @staticmethod
    def _coerce_role(name: str, role: str) -> str:
        """AutoGen / CrewAI use `name` for agent identity; map to assistant/user."""
        r = (role or "").lower().strip()
        if r in {"user", "human"}:
            return "user"
        if r in {"assistant", "ai", "bot", "agent"}:
            return "assistant"
        if r in {"system"}:
            return "system"
        if r in {"tool", "function"}:
            return "tool"
        # If only `name` is given, treat as assistant turn from that agent.
        return "assistant" if name else "user"

    @classmethod
    def _normalize_message(cls, m: Any) -> Turn | None:
        if not isinstance(m, dict):
            return None
        role = cls._coerce_role(m.get("name", ""), m.get("role", m.get("type", "")))
        content = m.get("content")
        if content is None:
            content = m.get("text", m.get("message", ""))
        # Anthropic content blocks
        tool_calls: list[dict[str, Any]] = []
        tool_result_for = m.get("tool_call_id", "") or m.get("tool_use_id", "")
        if isinstance(content, list):
            text_parts: list[str] = []
            for b in content:
                if not isinstance(b, dict):
                    text_parts.append(str(b))
                    continue
                bt = b.get("type", "text")
                if bt == "text":
                    text_parts.append(b.get("text", ""))
                elif bt in ("tool_use", "function_call"):
                    tool_calls.append({
                        "id": b.get("id", ""),
                        "name": b.get("name", ""),
                        "input": b.get("input", b.get("arguments", {})),
                    })
                elif bt == "tool_result":
                    tool_result_for = b.get("tool_use_id", tool_result_for)
                    rc = b.get("content", "")
                    if isinstance(rc, list):
                        rc = "\n".join(b2.get("text", "") for b2 in rc
                                       if isinstance(b2, dict) and b2.get("type") == "text")
                    text_parts.append(rc if isinstance(rc, str) else json.dumps(rc, ensure_ascii=False))
                else:
                    text_parts.append(json.dumps(b, ensure_ascii=False))
            content = "\n".join(p for p in text_parts if p)
        elif not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        # OpenAI tool_calls field on message
        for tc in m.get("tool_calls", []) or []:
            if isinstance(tc, dict):
                fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                tool_calls.append({
                    "id": tc.get("id", ""),
                    "name": fn.get("name", tc.get("name", "")),
                    "input": fn.get("arguments", tc.get("arguments", {})),
                })
        if not content.strip() and not tool_calls:
            return None
        return Turn(
            role=role,
            content=content,
            ts=str(m.get("timestamp", m.get("ts", m.get("created_at", "")))),
            tool_calls=tool_calls,
            tool_result_for=tool_result_for,
        )

    @classmethod
    def parse(cls, path_str: str) -> Session:
        p = Path(path_str).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"file not found: {p}")
        text = p.read_text(encoding="utf-8")
        # Try JSONL first.
        records: list[Any] = []
        meta: dict[str, Any] = {}
        try_jsonl = True
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                records.append(obj)
            except json.JSONDecodeError:
                try_jsonl = False
                records = []
                break
        if try_jsonl and len(records) > 1 and all(isinstance(r, dict) for r in records):
            messages = records
        else:
            try:
                raw = json.loads(text)
                messages, meta = cls._extract_message_list(raw)
                if not messages and isinstance(raw, dict) and ("role" in raw or "content" in raw):
                    messages = [raw]
            except json.JSONDecodeError:
                # Plain text — treat as single user turn.
                messages = [{"role": "user", "content": text.strip()}]
                meta = {}
        turns = [t for t in (cls._normalize_message(m) for m in messages) if t is not None]
        return Session(
            agent_type=str(meta.get("agent_type", "generic")),
            session_id=str(meta.get("session_id", p.stem)),
            started_at=str(meta.get("started_at", "")),
            ended_at=str(meta.get("ended_at",
                _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"))),
            model=str(meta.get("model", meta.get("source_model", "unknown"))),
            cwd=str(meta.get("cwd", "")),
            agent_version=str(meta.get("agent_version", meta.get("version", ""))),
            trajectory=turns,
            extra={**meta, "source_path": str(p)},
        )


ADAPTERS: dict[str, Any] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    HermesAdapter.name: HermesAdapter,
    AgentsChatAdapter.name: AgentsChatAdapter,
    ContinueDevAdapter.name: ContinueDevAdapter,
    OpenInterpreterAdapter.name: OpenInterpreterAdapter,
    CursorAdapter.name: CursorAdapter,
    AiderAdapter.name: AiderAdapter,
    CodexAdapter.name: CodexAdapter,
    GenericAdapter.name: GenericAdapter,
}


def detect_source(explicit: str | None = None) -> str:
    if explicit and explicit != "auto":
        return explicit
    # Hook env wins (Claude Code Stop hook sets CLAUDE_SESSION_PATH).
    if os.environ.get("CLAUDE_SESSION_PATH"):
        return ClaudeCodeAdapter.name
    if os.environ.get("HERMES_SESSION_PATH"):
        return HermesAdapter.name
    # Probe each adapter in priority order (most specific first).
    for adapter in (
        HermesAdapter, ClaudeCodeAdapter, AgentsChatAdapter,
        ContinueDevAdapter, OpenInterpreterAdapter,
        CursorAdapter, AiderAdapter, CodexAdapter,
    ):
        if adapter.available():
            return adapter.name
    return GenericAdapter.name


def _adapter_parse(src: str, ident: str) -> Session:
    """Single dispatch for `parse` across all adapters."""
    if src == ClaudeCodeAdapter.name:
        return ClaudeCodeAdapter.parse(ClaudeCodeAdapter.resolve_session(ident))
    if src == HermesAdapter.name:
        return HermesAdapter.parse(ident)
    if src == AgentsChatAdapter.name:
        return AgentsChatAdapter.parse(ident)
    if src == ContinueDevAdapter.name:
        return ContinueDevAdapter.parse(ident)
    if src == OpenInterpreterAdapter.name:
        return OpenInterpreterAdapter.parse(ident)
    if src == AiderAdapter.name:
        return AiderAdapter.parse(ident)
    if src == CodexAdapter.name:
        return CodexAdapter.parse(ident)
    if src == CursorAdapter.name:
        return CursorAdapter.parse(ident)
    return GenericAdapter.parse(ident)


def _adapter_latest_path_or_id(src: str) -> str:
    """Return an identifier the adapter's parse() can consume for the most
    recent session of that source."""
    if src == ClaudeCodeAdapter.name:
        return str(
            Path(os.environ["CLAUDE_SESSION_PATH"]) if os.environ.get("CLAUDE_SESSION_PATH")
            else ClaudeCodeAdapter.latest_session()
        )
    if src == HermesAdapter.name:
        rows = HermesAdapter.list_sessions(limit=1)
        if not rows:
            raise SystemExit("no hermes sessions found")
        return rows[0]["path"]
    if src == AgentsChatAdapter.name:
        rows = AgentsChatAdapter.list_sessions(limit=1)
        if not rows:
            raise SystemExit("no agents-chat threads found")
        return rows[0]["id"]
    if src == ContinueDevAdapter.name:
        rows = ContinueDevAdapter.list_sessions(limit=1)
        if not rows:
            raise SystemExit("no continue.dev sessions found")
        return rows[0]["path"]
    if src == OpenInterpreterAdapter.name:
        rows = OpenInterpreterAdapter.list_sessions(limit=1)
        if not rows:
            raise SystemExit("no open-interpreter sessions found")
        return rows[0]["path"]
    if src == AiderAdapter.name:
        paths = AiderAdapter.history_paths()
        if not paths:
            raise SystemExit("no aider history under cwd")
        return str(paths[0])
    if src == CodexAdapter.name:
        rows = CodexAdapter.list_sessions(limit=1)
        if not rows:
            raise SystemExit("no codex sessions found")
        return rows[0]["path"]
    if src == CursorAdapter.name:
        return "latest"
    raise SystemExit(f"--source {src!r} doesn't support push-latest; use push --session <path>")


# ---------------------------------------------------------------------------
# Trajectory segmentation — split a multi-task session into single-task slices.
#
# A single live session often contains several distinct tasks ("debug X" then
# "now write Y" then "translate Z"). Uploading the whole thing as one
# experience means: query/intent only reflects the first task, embeddings are
# diluted, and SFT samples mix concerns. Segmentation cuts the trajectory
# into spans on three signals:
#
#   1. Time gap   — adjacent user turn arrives > N minutes after prior
#                   assistant turn (you walked away → came back fresh)
#   2. Keyword    — user turn opens with explicit topic-shift markers
#                   ("换个话题", "next task", "另一个问题", ...)
#   3. Length cap — any segment > MAX_TURNS forces a soft split at the next
#                   user-turn boundary (avoids one runaway segment swallowing
#                   later tasks in pathological sessions)
#
# Each segment must contain at least one user-turn AND at least one
# assistant-turn — degenerate spans are dropped.
# ---------------------------------------------------------------------------

_TOPIC_SHIFT_TRIGGERS = (
    # zh
    "换个话题", "另一个问题", "另一个事", "另一件事",
    "接下来", "好的接下来", "ok接下来", "现在我",
    "下一个任务", "下一题", "下一件",
    "新任务", "另外问个", "另外一个",
    # en
    "next task", "new task", "different question", "switch topic",
    "now i need", "moving on", "ok so next",
)
_MAX_TURNS_PER_SEGMENT = 60
_DEFAULT_TIME_GAP_MIN = 30


def _parse_ts(s: str) -> _dt.datetime | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(s.split("+")[0], fmt)
        except ValueError:
            continue
    return None


def _is_topic_shift(content: str) -> bool:
    head = (content or "").strip()[:120].lower()
    if not head:
        return False
    for trig in _TOPIC_SHIFT_TRIGGERS:
        if trig in head or trig.lower() in head:
            return True
    return False


def segment_trajectory(
    trajectory: list[Turn],
    *,
    time_gap_minutes: int = _DEFAULT_TIME_GAP_MIN,
    max_turns: int = _MAX_TURNS_PER_SEGMENT,
    enable_keyword: bool = True,
) -> list[list[Turn]]:
    """Return a list of segments. Each segment is a slice of `trajectory`."""
    if not trajectory:
        return []
    segments: list[list[Turn]] = [[]]
    last_assistant_ts: _dt.datetime | None = None
    turns_in_current = 0
    has_user_in_current = False
    has_assistant_in_current = False

    def _start_new() -> None:
        nonlocal turns_in_current, has_user_in_current, has_assistant_in_current
        segments.append([])
        turns_in_current = 0
        has_user_in_current = False
        has_assistant_in_current = False

    for turn in trajectory:
        # Decide whether this turn opens a new segment.
        if turn.role == "user" and segments[-1]:
            should_split = False
            ts = _parse_ts(turn.ts)
            if last_assistant_ts and ts:
                gap_min = (ts - last_assistant_ts).total_seconds() / 60.0
                if gap_min >= time_gap_minutes:
                    should_split = True
            if not should_split and enable_keyword and _is_topic_shift(turn.content):
                should_split = True
            if not should_split and turns_in_current >= max_turns:
                should_split = True
            if should_split and has_user_in_current and has_assistant_in_current:
                _start_new()
        segments[-1].append(turn)
        turns_in_current += 1
        if turn.role == "user":
            has_user_in_current = True
        elif turn.role == "assistant":
            has_assistant_in_current = True
            ts = _parse_ts(turn.ts)
            if ts:
                last_assistant_ts = ts

    # Drop degenerate segments (need at least one user + one assistant).
    valid: list[list[Turn]] = []
    for seg in segments:
        roles = {t.role for t in seg}
        if "user" in roles and "assistant" in roles:
            valid.append(seg)
    return valid or [trajectory]  # if filtering nuked everything, return whole


def session_to_segments(session: Session, **kwargs: Any) -> list[Session]:
    """Yield a list of mini-Sessions, one per segment, with proper meta linkage."""
    segs = segment_trajectory(session.trajectory, **kwargs)
    if len(segs) <= 1:
        return [session]
    out: list[Session] = []
    base_extra = {k: v for k, v in session.extra.items()
                  if k != "raw_b64"}  # raw bytes only stay on seg 0
    for i, seg_turns in enumerate(segs):
        extra = dict(base_extra)
        extra["segment"] = {
            "parent_session_id": session.session_id,
            "seg_index": i,
            "total_segments": len(segs),
            "turn_count": len(seg_turns),
        }
        # Only segment 0 carries the (potentially heavy) raw bytes.
        if i == 0 and "raw_b64" in session.extra:
            extra["raw_b64"] = session.extra["raw_b64"]
            extra["raw_size_bytes"] = session.extra.get("raw_size_bytes")
            extra["raw_sha256"] = session.extra.get("raw_sha256")
        seg_started = seg_turns[0].ts or session.started_at
        seg_ended = seg_turns[-1].ts or session.ended_at
        out.append(Session(
            agent_type=session.agent_type,
            session_id=f"{session.session_id}#seg{i}",
            started_at=seg_started,
            ended_at=seg_ended,
            model=session.model,
            cwd=session.cwd,
            agent_version=session.agent_version,
            trajectory=seg_turns,
            extra=extra,
        ))
    return out


# ---------------------------------------------------------------------------
# Lite-card builder (matches cli/src/lite.ts shape).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Zero-cost intent extraction — runs BEFORE any LLM call.
# Two strategies, applied in order:
#   1. Look for an explicit `[task-summary]: ...` line written by the agent
#      itself during the session (SKILL.md instructs agents to do this).
#   2. Heuristic: parse the first user turn — strip greetings, extract the
#      action phrase, truncate.
# ---------------------------------------------------------------------------

_AGENT_SUMMARY_RE = re.compile(
    r"\[task[-_]summary\]\s*[:：]\s*(.+?)(?:\n|$)",
    re.IGNORECASE,
)

# Throwaway openings (zh + en) we strip from the first user turn.
_GREETING_PREFIXES = (
    "你好", "您好", "hi", "hello", "hey", "嗨",
    "麻烦", "请", "能否", "可以", "你能", "您能", "可不可以", "能不能",
    "帮我", "帮个忙", "help me", "could you", "can you", "would you", "please",
    "我想", "我要", "我需要", "我得", "i want", "i need", "i'd like",
    "现在", "现在我", "now", "now i",
)


def _extract_agent_summary(trajectory: list[Turn]) -> str | None:
    """Look for a `[task-summary]: ...` marker the agent emitted itself.

    SKILL.md instructs agents to add this as a final line in their last
    response. If found, that's the source of truth — zero token cost.
    """
    for t in reversed(trajectory):
        if t.role != "assistant" or not t.content:
            continue
        m = _AGENT_SUMMARY_RE.search(t.content)
        if m:
            label = m.group(1).strip().strip('"').strip("「」").rstrip(".。 ")
            if 4 <= len(label) <= 200:
                return label[:120]
    return None


def _heuristic_summary(turns: list[Turn]) -> str | None:
    """Pure-Python intent guess from the first user turn. No LLM."""
    user = next((t for t in turns if t.role == "user" and (t.content or "").strip()), None)
    if user is None:
        return None
    text = user.content.strip().replace("\n", " ")
    # Strip leading greeting layers, repeatedly.
    lowered = text.lower()
    for _ in range(4):
        stripped = False
        for prefix in _GREETING_PREFIXES:
            if lowered.startswith(prefix):
                text = text[len(prefix):].lstrip(" ,，。.:：!！?？")
                lowered = text.lower()
                stripped = True
                break
        if not stripped:
            break
    # If we stripped to nothing useful, just use the original text.
    if len(text) < 4:
        text = user.content.strip().replace("\n", " ")
    # Take first natural sentence.
    for sep in ("。", "！", "？", ".", "!", "?", "\n"):
        idx = text.find(sep)
        if 0 < idx < 100:
            text = text[:idx]
            break
    text = text.strip().rstrip(".。 ")
    if not text:
        return None
    if len(text) > 80:
        text = text[:77].rstrip() + "…"
    return text


_INTENT_SYSTEM = """You are a task-labeling tool. You receive a TRANSCRIPT of a past
conversation between a user and an AI agent, and you output a one-line label
that names the task the user was working on.

CRITICAL — you are NOT continuing the conversation. You are NOT a participant.
You are a labeler. You read the transcript from the outside.

Output rules (strict):
- Exactly ONE line, no preamble, no quotes, no trailing punctuation
- Maximum 80 characters
- Action-oriented: a verb phrase or noun-of-action (e.g. "Debug FastAPI HMAC
  signature mismatch", "上传 PDF 作业并生成解题大纲", "Plan Lobster Square
  building takeover")
- Match the language the user predominantly used in the transcript
- Do NOT echo the user's literal first message
- Do NOT include role labels, emojis, "the user wants to", "task:", etc.
- If the transcript is purely a greeting / acknowledgment / has no real task,
  output exactly: (no task)

Examples:

TRANSCRIPT:
[user] csv revenue 按区域汇总然后按 quarter 排序
[assistant] 我用 pandas...
LABEL: 按区域汇总 CSV 营收并按季度排序

TRANSCRIPT:
[user] hi there
[assistant] hello! how can i help?
LABEL: (no task)

TRANSCRIPT:
[user] 我的 Caddy 拿不到 cert，报 acme challenge 失败
[assistant] 检查 80 端口是否开放...
LABEL: 排查 Caddy Let's Encrypt ACME 证书签发失败

Now do the same for the transcript that follows."""


def _format_for_intent(turns: list[Turn], max_chars: int = 4000) -> str:
    """Compact representation for the labeler. Use bracketed role tags so the
    model treats it as data, not as a live conversation it should respond to."""
    if not turns:
        return ""
    user_turns = [t for t in turns if t.role == "user"]
    asst_turns = [t for t in turns if t.role == "assistant"]
    picks: list[tuple[str, str]] = []
    if user_turns:
        picks.append(("user", user_turns[0].content or ""))
    if asst_turns:
        picks.append(("assistant", asst_turns[0].content or ""))
    if len(asst_turns) > 1 and asst_turns[-1] is not asst_turns[0]:
        picks.append(("assistant", asst_turns[-1].content or ""))
    chunks: list[str] = []
    budget_per = max(200, max_chars // max(1, len(picks)))
    for role, content in picks:
        chunks.append(f"[{role}] {content[:budget_per]}")
    return "TRANSCRIPT:\n" + "\n".join(chunks) + "\nLABEL:"


def summarize_intent(turns: list[Turn], *,
                     backend_kind: str = "auto",
                     model: str | None = None,
                     verbose: bool = False) -> str | None:
    """One-shot LLM call → concise task summary. Returns None if no backend."""
    try:
        here = Path(__file__).parent
        sys.path.insert(0, str(here))
        from exp_annotator import pick_backend  # type: ignore
    except ImportError:
        return None
    try:
        backend = pick_backend(backend_kind, model)
    except SystemExit:
        return None
    user_block = _format_for_intent(turns)
    if not user_block.strip():
        return None
    try:
        raw = backend.call(_INTENT_SYSTEM, user_block)
    except Exception as e:
        if verbose:
            print(f"[intent] summarizer failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if not raw:
        return None
    # Take the FIRST non-empty line that looks like a label, strip artifacts.
    for line in raw.splitlines():
        cand = line.strip().strip('"').strip("'").strip("「」")
        # Drop common preamble forms ("Label:", "标签:", "Task:")
        for prefix in ("LABEL:", "Label:", "label:", "TASK:", "Task:", "task:",
                       "标签:", "标签：", "任务:", "任务："):
            if cand.startswith(prefix):
                cand = cand[len(prefix):].strip()
        if not cand:
            continue
        cand = cand.rstrip(".。 ")
        # Skip if it's the model continuing the conversation (look for
        # red-flag phrases that mean "model is responding, not labeling")
        lowered = cand.lower()
        if any(bad in lowered for bad in ("我在的", "i'm here", "hello", "hi there",
                                          "抱歉", "不行。", "ok, i'll", "好的，我")):
            return None
        if cand == "(no task)" or cand.lower() == "(no task)":
            return None
        if len(cand) > 120:
            cand = cand[:117].rstrip() + "…"
        return cand
    return None


def build_lite_card(
    session: Session,
    *,
    task_type: str,
    sensitivity: str,
    acl: str,
    tags: list[str],
    summarize: bool = False,
    summarizer_backend: str = "auto",
    summarizer_model: str | None = None,
    summarize_mode: str = "auto",  # auto | heuristic | llm
    verbose: bool = False,
) -> dict[str, Any]:
    query = ""
    steps: list[str] = []
    outcome = ""
    totals: dict[str, int] = {}
    sanitized_traj: list[dict[str, Any]] = []

    for t in session.trajectory:
        body, counts = sanitize(t.content)
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        sanitized_traj.append({
            "role": t.role,
            "content": body,
            "ts": t.ts,
            "tool_calls": t.tool_calls,
            "tool_result_for": t.tool_result_for,
        })
        if t.role == "user" and not query and body.strip():
            query = body
        elif t.role == "assistant" and body.strip():
            steps.append(body[:280])
            outcome = body

    # Intent priority (cheapest to most expensive):
    #   1. agent-emitted [task-summary]: line (0 tokens, found in trajectory)
    #   2. heuristic from first user turn (0 tokens, pure Python)
    #   3. LLM call (only when mode=auto AND heuristic is degenerate, OR
    #      mode=llm explicitly)
    #   4. literal first-user-turn truncation (fallback)
    summary_intent: str | None = None
    if summarize:
        summary_intent = _extract_agent_summary(session.trajectory)
        if summary_intent and verbose:
            print("[intent] used agent self-emitted [task-summary] marker", file=sys.stderr)

        if summary_intent is None and summarize_mode in ("auto", "heuristic"):
            summary_intent = _heuristic_summary(session.trajectory)
            if summary_intent and verbose:
                print(f"[intent] heuristic → {summary_intent!r}", file=sys.stderr)

        # Only call the LLM when explicitly requested, OR when mode=auto and
        # heuristic produced nothing useful (rare).
        should_call_llm = summarize_mode == "llm" or (
            summarize_mode == "auto" and summary_intent is None
        )
        if should_call_llm:
            llm_intent = summarize_intent(
                session.trajectory,
                backend_kind=summarizer_backend,
                model=summarizer_model,
                verbose=verbose,
            )
            if llm_intent:
                summary_intent = llm_intent
                if verbose:
                    print(f"[intent] LLM → {llm_intent!r}", file=sys.stderr)

    intent = summary_intent or (query[:120] or "unspecified task").strip()
    return {
        "card": {
            "query": query or "(no user turn)",
            "intent": intent,
            "steps": steps,
            "outcome": outcome[:500] or "(no assistant turn)",
            "task_type": task_type,
            "source_model": session.model,
            "sensitivity": sensitivity,
            "acl": acl,
            "tags": tags,
            "redactions": totals,
        },
        "trajectory": sanitized_traj,
    }


# ---------------------------------------------------------------------------
# HMAC signing + HTTP client.
# ---------------------------------------------------------------------------

def sign(secret: str, method: str, path: str, body: bytes) -> str:
    canonical = method.upper().encode() + b"\n" + path.encode() + b"\n" + body
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def http_request(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    cred: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    body_str = "" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    body_bytes = body_str.encode("utf-8")
    url = base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if cred:
        headers["X-Agent-Name"] = cred["agent_name"]
        headers["X-Signature"] = sign(cred["secret"], method, path, body_bytes)
    req = urllib.request.Request(
        url,
        data=body_bytes if method != "GET" else None,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"[gateway] {method} {path} -> {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise SystemExit(f"[gateway] {method} {path} -> network error: {e.reason}")
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise SystemExit(f"[gateway] non-JSON response from {path}: {payload[:200]}")


# ---------------------------------------------------------------------------
# Credential storage.
# ---------------------------------------------------------------------------

def cred_path(name: str | None = None) -> Path:
    DEFAULT_CRED_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if name:
        return DEFAULT_CRED_DIR / f"{name}.json"
    # Prefer named file via env, otherwise pick newest.
    env_name = os.environ.get("EXP_AGENT_NAME")
    if env_name:
        return DEFAULT_CRED_DIR / f"{env_name}.json"
    files = sorted(DEFAULT_CRED_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else DEFAULT_CRED_DIR / "default.json"


def load_credential() -> dict[str, str] | None:
    env_name = os.environ.get("EXP_AGENT_NAME")
    env_secret = os.environ.get("EXP_AGENT_SECRET")
    if env_name and env_secret:
        return {"agent_name": env_name, "secret": env_secret}
    p = cred_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {"agent_name": data["agent_name"], "secret": data["secret"]}
    except Exception:
        return None


def save_credential(cred: dict[str, Any]) -> Path:
    p = cred_path(cred.get("agent_name"))
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.write_text(json.dumps(cred, indent=2), encoding="utf-8")
    p.chmod(0o600)
    return p


# ---------------------------------------------------------------------------
# CLI commands.
# ---------------------------------------------------------------------------

def cmd_register(args: argparse.Namespace) -> int:
    body = {"name": args.name, "team": args.team}
    res = http_request(args.base, "POST", "/v1/agents/register", body)
    save_path = save_credential(res)
    res["credentials_path"] = str(save_path)
    res_safe = {**res, "secret": "***"}
    print(json.dumps(res_safe, indent=2, ensure_ascii=False))
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    cred = load_credential()
    if cred is None:
        print("(no credential found — run `exp_uploader register` first)")
        return 1
    print(json.dumps({"agent_name": cred["agent_name"], "secret": "***"}, indent=2))
    return 0


def cmd_list_sessions(args: argparse.Namespace) -> int:
    src = detect_source(args.source)
    adapter = ADAPTERS[src]
    rows = adapter.list_sessions(limit=args.limit)
    print(json.dumps({"source": src, "sessions": rows}, indent=2, ensure_ascii=False))
    return 0


def _maybe_attach_raw(session: Session, source_path: str | Path) -> None:
    """Embed base64 of the raw source file under session.extra.raw_b64
    so the server can preserve byte-exact recovery (capped at 8 MiB)."""
    p = Path(source_path) if source_path else None
    if not p or not p.is_file():
        return
    size = p.stat().st_size
    cap = int(os.environ.get("EXP_RAW_CAP_BYTES", str(8 * 1024 * 1024)))
    if size > cap:
        session.extra["raw_truncated"] = True
        session.extra["raw_size_bytes"] = size
        return
    session.extra["raw_b64"] = base64.b64encode(p.read_bytes()).decode("ascii")
    session.extra["raw_size_bytes"] = size
    session.extra["raw_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()


def _maybe_annotate(session: Session, args: argparse.Namespace) -> dict[str, Any] | None:
    """Optionally annotate per-turn rewards using the synergy schema."""
    if not getattr(args, "annotate", False):
        return None
    try:
        here = Path(__file__).parent
        sys.path.insert(0, str(here))
        from exp_annotator import annotate_session, pick_backend  # type: ignore
    except ImportError as e:
        print(f"[annotate] exp_annotator.py not found alongside uploader: {e}", file=sys.stderr)
        return None
    try:
        backend = pick_backend(getattr(args, "annotate_backend", "auto"),
                               getattr(args, "annotate_model", None))
    except SystemExit as e:
        print(f"[annotate] no backend available: {e}", file=sys.stderr)
        return None
    payload = session.to_payload()
    return annotate_session(
        payload, backend,
        subsequent_k=getattr(args, "annotate_subsequent_k", 4),
        max_turns=getattr(args, "annotate_max_turns", 8),
        strategy=getattr(args, "annotate_pick", "even"),
        verbose=getattr(args, "verbose", False),
    )


def _post_rewards(base: str, cred: dict[str, str], experience_id: str,
                  rewards: dict[str, Any]) -> None:
    """POST per-turn rewards to /v1/lite/rewards (separate from the trace push)."""
    body: dict[str, Any] = {
        "experience_id": experience_id,
        "rewards": [
            {
                "turn_index": r["turn_index"],
                "user_turn_index": r.get("user_turn_index"),
                "outcome": r["outcome"],
                "intent": r["intent"],
                "execution": r["execution"],
                "orchestration": r["orchestration"],
                "expression": r["expression"],
                "confidence": r["confidence"],
                "reason": r.get("reason", ""),
            }
            for r in rewards.get("rewards", [])
        ],
        "summary": rewards.get("summary", {}),
        "judge_model": rewards.get("model_used", "unknown"),
        "judge_backend": rewards.get("backend", "unknown"),
        "annotated_at": rewards.get("annotated_at", ""),
        "replace": True,
    }
    if not body["rewards"]:
        return
    res = http_request(base, "POST", "/v1/lite/rewards", body, cred=cred)
    print(json.dumps({"rewards_posted": res.get("rewards_stored"),
                      "judge_model": res.get("judge_model"),
                      "experience_id": res.get("experience_id")}, ensure_ascii=False),
          file=sys.stderr)


def _push_one(session: Session, args: argparse.Namespace,
              cred: dict[str, str], parent_eid: str | None = None) -> str | None:
    """Push a single (already-segmented or whole) session. Returns experience_id."""
    if getattr(args, "full_trace", False):
        src = session.extra.get("source_path") or session.extra.get("db") or ""
        if src and "raw_b64" not in session.extra:
            _maybe_attach_raw(session, src)
    rewards = _maybe_annotate(session, args)
    if rewards is not None:
        session.extra["rewards"] = rewards
    summarize_flag = not getattr(args, "no_summarize_intent", False)
    parts = build_lite_card(
        session,
        task_type=args.task,
        sensitivity=args.sensitivity,
        acl=args.acl,
        tags=args.tag or [],
        summarize=summarize_flag,
        summarize_mode=getattr(args, "summarize_mode", "auto"),
        summarizer_backend=getattr(args, "summarizer_backend", "auto"),
        summarizer_model=getattr(args, "summarizer_model", None),
        verbose=getattr(args, "verbose", False),
    )
    body: dict[str, Any] = {
        **parts["card"],
        "trajectory": None if args.no_trace else parts["trajectory"],
        "system": session.extra.get("system"),
        "tools": session.extra.get("tools"),
        "meta": {
            "agent_type": session.agent_type,
            "session_id": session.session_id,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "cwd": session.cwd,
            "agent_version": session.agent_version,
            "extra": session.extra,
            "uploader_version": "0.4",
        },
    }
    if parent_eid:
        # Server treats this as the segment chain backlink.
        body["meta"]["parent_experience_ids"] = [parent_eid]
    if body["trajectory"] is None:
        body.pop("trajectory")
    if body["system"] is None:
        body.pop("system")
    if body["tools"] is None:
        body.pop("tools")
    res = http_request(args.base, "POST", "/v1/lite/push", body, cred=cred)
    eid = res.get("experience_id")
    seg_info = session.extra.get("segment", {})
    out_line = {
        "session": session.session_id,
        "agent_type": session.agent_type,
        "experience_id": eid,
        "review_status": res.get("review_status"),
    }
    if seg_info:
        out_line["seg"] = f"{seg_info.get('seg_index', 0) + 1}/{seg_info.get('total_segments', 1)}"
    print(json.dumps(out_line, ensure_ascii=False))
    if rewards is not None and eid:
        try:
            _post_rewards(args.base, cred, eid, rewards)
        except SystemExit as e:
            print(f"[rewards] post failed: {e}", file=sys.stderr)
    return eid


def _push(session: Session, args: argparse.Namespace) -> int:
    cred = load_credential()
    if cred is None:
        raise SystemExit("no credential found. run `exp_uploader register` first.")
    # Decide whether to segment. Default: ON; off when --no-segment or
    # the trajectory is already short enough to be a single task.
    do_segment = not getattr(args, "no_segment", False)
    segments: list[Session]
    if do_segment:
        segments = session_to_segments(
            session,
            time_gap_minutes=getattr(args, "segment_time_gap_min", _DEFAULT_TIME_GAP_MIN),
            max_turns=getattr(args, "segment_max_turns", _MAX_TURNS_PER_SEGMENT),
            enable_keyword=not getattr(args, "no_segment_keyword", False),
        )
    else:
        segments = [session]
    if len(segments) > 1 and getattr(args, "verbose", False):
        print(f"[segment] split {session.session_id} into {len(segments)} segments",
              file=sys.stderr)
    parent_eid: str | None = None
    for seg in segments:
        eid = _push_one(seg, args, cred, parent_eid=parent_eid)
        # Chain: each segment's parent is the previous segment in the same session.
        if eid:
            parent_eid = eid
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    src = detect_source(args.source)
    session = _adapter_parse(src, args.session)
    return _push(session, args)


def cmd_push_latest(args: argparse.Namespace) -> int:
    src = detect_source(args.source)
    ident = _adapter_latest_path_or_id(src)
    session = _adapter_parse(src, ident)
    return _push(session, args)


def cmd_push_file(args: argparse.Namespace) -> int:
    session = GenericAdapter.parse(args.file)
    return _push(session, args)


def cmd_export(args: argparse.Namespace) -> int:
    """Dump normalized sessions to a JSONL file. No server required.

    Useful for offline workflows: hand the JSONL to any downstream pipeline
    (training data prep, custom analytics, your own ingestion service, etc.).
    Each line is a full Session payload (the same shape POST /v1/lite/push
    accepts in its optional `trajectory` + `meta` fields).
    """
    enabled_input = args.sources or ",".join(DEFAULT_AUTO_SOURCES)
    enabled = [s.strip() for s in enabled_input.split(",")
               if s.strip() and s.strip() in ADAPTERS]
    out_path = Path(args.output).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0
    failures: list[dict[str, Any]] = []
    with out_path.open("w", encoding="utf-8") as fp:
        for src in enabled:
            adapter = ADAPTERS[src]
            if not adapter.available():
                continue
            rows = adapter.list_sessions(limit=args.limit)
            for row in rows:
                if args.since:
                    when = row.get("mtime") or row.get("ended_at") or ""
                    if when and when < args.since:
                        continue
                ident = row.get("path") or row.get("id")
                try:
                    session = _adapter_parse(src, ident)
                    if not session.trajectory:
                        continue
                    fp.write(json.dumps(session.to_payload(), ensure_ascii=False) + "\n")
                    n_written += 1
                except Exception as e:
                    failures.append({"source": src, "id": row.get("id"),
                                     "error": f"{type(e).__name__}: {e}"})
    print(json.dumps({"output": str(out_path), "written": n_written,
                      "sources": enabled, "failed": len(failures)},
                     ensure_ascii=False))
    if args.verbose and failures:
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
    return 0 if not failures else 2


def cmd_annotate_existing(args: argparse.Namespace) -> int:
    """Re-annotate an already-uploaded trace by running the local annotator
    against the local source session, then POSTing rewards to /v1/lite/rewards.

    Useful when you want to add (or replace) rewards on an experience that was
    pushed earlier without --annotate, or to compare two judge models on the
    same trace.
    """
    cred = load_credential()
    if cred is None:
        raise SystemExit("no credential found. run `exp_uploader register` first.")
    src = detect_source(args.source)
    session = _adapter_parse(src, args.session)
    # Force annotation on
    args.annotate = True
    rewards = _maybe_annotate(session, args)
    if rewards is None:
        raise SystemExit("annotation failed (no backend available)")
    _post_rewards(args.base, cred, args.experience_id, rewards)
    print(json.dumps({"experience_id": args.experience_id,
                      "n_rewards": len(rewards.get("rewards", [])),
                      "judge_model": rewards.get("model_used")},
                     ensure_ascii=False))
    return 0


def cmd_get_rewards(args: argparse.Namespace) -> int:
    cred = load_credential()
    path = f"/v1/lite/rewards/{args.experience_id}"
    if args.judge_model:
        path += f"?judge_model={urllib.parse.quote(args.judge_model)}"
    res = http_request(args.base, "GET", path, body=None, cred=cred)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Background auto-sync daemon (one-shot tick; scheduled by launchd/systemd).
# State at $EXP_INSTALL_DIR/state.json:
#   {"by_source": {"claude-code": {"uploaded_ids": [...], "last_seen_mtime": "2026..."}}}
# ---------------------------------------------------------------------------

DEFAULT_AUTO_SOURCES = ["claude-code", "hermes", "continue-dev", "codex"]


def _state_path() -> Path:
    p = Path(os.environ.get("EXP_STATE_PATH",
                            str(Path.home() / ".experience-pool" / "state.json")))
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {"version": 1, "by_source": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "by_source": {}}


def _save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    p.chmod(0o600)


def cmd_daemon_tick(args: argparse.Namespace) -> int:
    """One-shot incremental sync. Designed to be run by launchd / systemd
    every few minutes. Idempotent: tracks uploaded session ids per source
    and an mtime watermark so the same session is never uploaded twice.
    """
    cred = load_credential()
    if cred is None:
        print(json.dumps({"status": "no_credential", "uploaded": 0}))
        return 0
    enabled = (args.sources or os.environ.get("EXP_AUTO_SOURCES")
               or ",".join(DEFAULT_AUTO_SOURCES)).split(",")
    enabled = [s.strip() for s in enabled if s.strip() and s.strip() in ADAPTERS]
    state = _load_state()
    by_source = state.setdefault("by_source", {})
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    summary: dict[str, Any] = {
        "started_at": started_at,
        "sources": {},
        "total_uploaded": 0,
        "total_skipped": 0,
        "total_failed": 0,
    }
    cap_per_source = max(1, args.max_per_source)
    cap_per_session_kb = max(64, args.max_session_kb)
    default_acl = args.acl or os.environ.get("EXP_AUTO_ACL", "private")
    default_task = args.task or os.environ.get("EXP_AUTO_TASK", "auto-sync")

    # Synthesize an args namespace for _push to consume.
    push_ns = argparse.Namespace(
        base=args.base,
        task=default_task,
        sensitivity="medium",
        acl=default_acl,
        tag=["auto-sync"],
        no_trace=False,
        full_trace=False,
        annotate=False,
        no_segment=os.environ.get("EXP_AUTO_SEGMENT", "1").lower() in {"0", "false", "no"},
        no_segment_keyword=False,
        segment_time_gap_min=int(os.environ.get("EXP_SEGMENT_TIME_GAP_MIN", "30")),
        segment_max_turns=int(os.environ.get("EXP_SEGMENT_MAX_TURNS", "60")),
        no_summarize_intent=os.environ.get("EXP_AUTO_SUMMARIZE_INTENT", "1").lower() in {"0", "false", "no"},
        # Daemon defaults to heuristic-only (zero LLM calls). Set
        # EXP_SUMMARIZE_MODE=llm to force LLM, or =auto to fall back
        # to LLM only when the heuristic is degenerate.
        summarize_mode=os.environ.get("EXP_SUMMARIZE_MODE", "heuristic"),
        summarizer_backend="auto",
        summarizer_model=None,
        verbose=args.verbose,
    )
    for src in enabled:
        adapter = ADAPTERS[src]
        if not adapter.available():
            summary["sources"][src] = {"status": "unavailable"}
            continue
        bookkeeping = by_source.setdefault(src, {"uploaded_ids": [], "last_mtime": ""})
        already = set(bookkeeping.get("uploaded_ids", []))
        try:
            rows = adapter.list_sessions(limit=200)
        except Exception as e:
            summary["sources"][src] = {"status": "list_failed", "error": str(e)}
            summary["total_failed"] += 1
            continue
        # Sort oldest-first so we upload chronologically.
        rows.sort(key=lambda r: (r.get("mtime") or r.get("ended_at") or ""))
        uploaded, skipped, failed = 0, 0, 0
        new_ids: list[str] = []
        last_mtime_seen = bookkeeping.get("last_mtime", "")
        for row in rows:
            sid = str(row.get("id") or row.get("path") or "")
            mtime = row.get("mtime") or row.get("ended_at") or ""
            if sid in already:
                skipped += 1
                continue
            size_kb = (row.get("size_bytes") or 0) / 1024
            if size_kb and size_kb > cap_per_session_kb:
                skipped += 1
                if args.verbose:
                    print(f"[daemon] {src} {sid[:24]}: skipped (size {size_kb:.0f}KB > cap {cap_per_session_kb}KB)",
                          file=sys.stderr)
                continue
            if uploaded >= cap_per_source:
                break
            ident = row.get("path") or row.get("id")
            try:
                session = _adapter_parse(src, ident)
                if not session.trajectory:
                    skipped += 1
                    continue
                if args.dry_run:
                    print(f"[dry-run] would upload {src}/{sid} ({len(session.trajectory)} turns)")
                else:
                    _push(session, push_ns)
                uploaded += 1
                new_ids.append(sid)
                if mtime > last_mtime_seen:
                    last_mtime_seen = mtime
            except SystemExit as e:
                failed += 1
                if args.verbose:
                    print(f"[daemon] {src} {sid[:24]}: failed ({e})", file=sys.stderr)
            except Exception as e:
                failed += 1
                if args.verbose:
                    print(f"[daemon] {src} {sid[:24]}: failed ({type(e).__name__}: {e})",
                          file=sys.stderr)
        # Persist bookkeeping (cap remembered ids at 5000 to keep state small).
        all_ids = list(already) + new_ids
        if len(all_ids) > 5000:
            all_ids = all_ids[-5000:]
        bookkeeping["uploaded_ids"] = all_ids
        bookkeeping["last_mtime"] = last_mtime_seen
        bookkeeping["last_tick_uploaded"] = uploaded
        bookkeeping["last_tick_at"] = started_at
        summary["sources"][src] = {
            "uploaded": uploaded, "skipped": skipped, "failed": failed,
            "available_now": len(rows),
        }
        summary["total_uploaded"] += uploaded
        summary["total_skipped"] += skipped
        summary["total_failed"] += failed
    state["last_tick_at"] = started_at
    if not args.dry_run:
        _save_state(state)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_daemon_state(args: argparse.Namespace) -> int:
    state = _load_state()
    # Don't dump full uploaded_ids list — only counts.
    out = {"last_tick_at": state.get("last_tick_at"), "by_source": {}}
    for src, info in state.get("by_source", {}).items():
        out["by_source"][src] = {
            "uploaded_count": len(info.get("uploaded_ids", [])),
            "last_mtime": info.get("last_mtime", ""),
            "last_tick_uploaded": info.get("last_tick_uploaded", 0),
            "last_tick_at": info.get("last_tick_at", ""),
        }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_daemon_reset(args: argparse.Namespace) -> int:
    """Forget what we've uploaded so the next tick will re-scan from scratch."""
    state = _load_state()
    if args.source:
        state.get("by_source", {}).pop(args.source, None)
    else:
        state["by_source"] = {}
    _save_state(state)
    print(json.dumps({"reset": args.source or "all"}, ensure_ascii=False))
    return 0


def cmd_push_all(args: argparse.Namespace) -> int:
    src = detect_source(args.source)
    adapter = ADAPTERS[src]
    rows = adapter.list_sessions(limit=args.limit)
    if not rows:
        print(json.dumps({"source": src, "uploaded": 0, "reason": "no sessions found"}))
        return 1
    since_ts = args.since
    uploaded = 0
    failed: list[dict[str, Any]] = []
    for row in rows:
        if since_ts:
            row_ts = row.get("mtime") or row.get("ended_at") or ""
            if row_ts and row_ts < since_ts:
                continue
        ident = row.get("path") or row.get("id")
        try:
            session = _adapter_parse(src, ident)
            _push(session, args)
            uploaded += 1
        except SystemExit as e:
            failed.append({"id": row.get("id"), "error": str(e)})
        except Exception as e:
            failed.append({"id": row.get("id"), "error": f"{type(e).__name__}: {e}"})
    print(json.dumps({"source": src, "uploaded": uploaded, "failed": failed}, ensure_ascii=False))
    return 0 if not failed else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="exp_uploader", description=__doc__.split("\n")[1])
    p.add_argument("--base", default=DEFAULT_BASE_URL, help=f"gateway URL (default {DEFAULT_BASE_URL})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("register", help="register agent and save HMAC credential")
    sp.add_argument("--name", required=True)
    sp.add_argument("--team", required=True)
    sp.set_defaults(func=cmd_register)

    sp = sub.add_parser("whoami")
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("list-sessions", help="list recent local sessions for a source")
    sp.add_argument("--source", default="auto", choices=["auto"] + list(ADAPTERS))
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_list_sessions)

    push_args = argparse.ArgumentParser(add_help=False)
    push_args.add_argument("--task", default="misc")
    push_args.add_argument("--sensitivity", default="medium", choices=["low", "medium", "high"])
    push_args.add_argument("--acl", default="private")
    push_args.add_argument("--tag", action="append", default=[])
    push_args.add_argument("--no-trace", action="store_true",
                           help="upload only the LiteCard, drop the trajectory")
    push_args.add_argument("--full-trace", action="store_true",
                           help="also embed the raw source file (base64, capped 8MiB) "
                                "for byte-exact recovery")
    push_args.add_argument("--annotate", action="store_true",
                           help="run synergy-style 5-dim per-turn reward annotation "
                                "before upload (calls claude/anthropic/openai)")
    push_args.add_argument("--annotate-backend", default="auto",
                           choices=["auto", "claude", "anthropic", "openai"])
    push_args.add_argument("--annotate-model", default=None,
                           help="model id for annotation (default haiku)")
    push_args.add_argument("--annotate-subsequent-k", type=int, default=4,
                           help="subsequent turns fed as delayed feedback per evaluation")
    push_args.add_argument("--annotate-max-turns", type=int, default=8,
                           help="cap evaluated turns per session (cost control)")
    push_args.add_argument("--annotate-pick", default="even",
                           choices=["first", "even", "important"])
    push_args.add_argument("--no-summarize-intent", action="store_true",
                           help="skip task summary entirely; use literal first "
                                "user turn (degraded but zero work)")
    push_args.add_argument("--summarize-mode", default="heuristic",
                           choices=["auto", "heuristic", "llm"],
                           help="heuristic (default): zero LLM calls — uses "
                                "agent's [task-summary] marker if present, "
                                "else strips greetings from first user turn. "
                                "auto: heuristic, then LLM only if degenerate. "
                                "llm: always call LLM (forces extra inference).")
    push_args.add_argument("--summarizer-backend", default="auto",
                           choices=["auto", "claude", "anthropic", "openai"])
    push_args.add_argument("--summarizer-model", default=None,
                           help="model id for LLM fallback (default haiku)")
    push_args.add_argument("--no-segment", action="store_true",
                           help="upload the whole session as one experience "
                                "(default is to segment by topic shifts / time gaps)")
    push_args.add_argument("--no-segment-keyword", action="store_true",
                           help="disable keyword-based topic-shift detection "
                                "(time gap + length cap still active)")
    push_args.add_argument("--segment-time-gap-min", type=int, default=30,
                           help="minutes of inactivity between turns that triggers a split")
    push_args.add_argument("--segment-max-turns", type=int, default=60,
                           help="hard cap: any segment > this many turns force-splits "
                                "at the next user-turn boundary")
    push_args.add_argument("--verbose", "-v", action="store_true")

    sp = sub.add_parser("push", parents=[push_args])
    sp.add_argument("--session", required=True, help="session id, prefix, or path")
    sp.add_argument("--source", default="auto", choices=["auto"] + list(ADAPTERS))
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("push-latest", parents=[push_args])
    sp.add_argument("--source", default="auto", choices=["auto"] + list(ADAPTERS))
    sp.set_defaults(func=cmd_push_latest)

    sp = sub.add_parser("push-all", parents=[push_args],
                        help="bulk upload every recent session for a source")
    sp.add_argument("--source", default="auto", choices=["auto"] + list(ADAPTERS))
    sp.add_argument("--since", default="",
                    help="ISO date prefix; only sessions with mtime >= since are uploaded "
                         "(e.g. 2026-04-01)")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_push_all)

    sp = sub.add_parser("push-file", parents=[push_args])
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_push_file)

    sp = sub.add_parser(
        "export",
        help="dump normalized sessions to JSONL (no server required) — "
             "the universal use case when you don't run the reference gateway")
    sp.add_argument("--output", default="./sessions.jsonl",
                    help="output JSONL path (default ./sessions.jsonl)")
    sp.add_argument("--sources", default="",
                    help="comma-sep adapter names (default: all auto-sync sources)")
    sp.add_argument("--limit", type=int, default=200,
                    help="max sessions per source (default 200)")
    sp.add_argument("--since", default="",
                    help="ISO date prefix; only newer sessions exported")
    sp.add_argument("--verbose", "-v", action="store_true")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("annotate-existing", parents=[push_args],
                        help="re-annotate an already-uploaded trace and POST rewards")
    sp.add_argument("--experience-id", required=True,
                    help="server-side experience id returned by an earlier push")
    sp.add_argument("--session", required=True,
                    help="local session id/path that the experience was created from")
    sp.add_argument("--source", default="auto", choices=["auto"] + list(ADAPTERS))
    sp.set_defaults(func=cmd_annotate_existing)

    sp = sub.add_parser("get-rewards", help="fetch stored rewards for an experience")
    sp.add_argument("--experience-id", required=True)
    sp.add_argument("--judge-model", default=None)
    sp.set_defaults(func=cmd_get_rewards)

    sp = sub.add_parser("daemon-tick",
                        help="one-shot incremental sync of new sessions across all enabled sources")
    sp.add_argument("--sources", default="",
                    help="comma-sep adapter names; empty = $EXP_AUTO_SOURCES or default set")
    sp.add_argument("--max-per-source", type=int, default=10,
                    help="cap uploads per source per tick (default 10)")
    sp.add_argument("--max-session-kb", type=int, default=4096,
                    help="skip sessions larger than this (default 4MB)")
    sp.add_argument("--acl", default="", help="default ACL (default $EXP_AUTO_ACL or private)")
    sp.add_argument("--task", default="", help="default task type (default auto-sync)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verbose", "-v", action="store_true")
    sp.set_defaults(func=cmd_daemon_tick)

    sp = sub.add_parser("daemon-state", help="show last-seen state per source")
    sp.set_defaults(func=cmd_daemon_state)

    sp = sub.add_parser("daemon-reset",
                        help="forget what we've uploaded; next tick re-scans from scratch")
    sp.add_argument("--source", default="", help="reset only this source (default: all)")
    sp.set_defaults(func=cmd_daemon_reset)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
