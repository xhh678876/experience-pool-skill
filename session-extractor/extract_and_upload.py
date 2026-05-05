#!/usr/bin/env python3
"""experience-pool session backfill — standalone extractor + uploader.

Style follows claude_sft_delivery: single self-contained Python,
zero non-stdlib deps for the basic flow. Drops local Claude Code /
Codex session JSONLs into your PRIVATE experience-pool repo via
HMAC-signed HTTP POST. ACL is hard-coded to `private` — uploads done
by this tool are NEVER visible to anyone but the owner.

Usage:
    EXP_AGENT_NAME='user-xxx' \\
    EXP_AGENT_SECRET='<hex>' \\
    EXP_BASE_URL='http://10.244.66.195:3080' \\
    python3 extract_and_upload.py [options]

Options:
    --sources <list>    comma-separated; default auto-detect
                        (claude-code, codex, hermes, openclaw)
    --limit N           cap total uploads across all sources
    --since <iso>       only sessions modified after this ISO date
    --dry-run           list what would be uploaded, don't post
    --verbose, -v       per-session detail

Exit codes:
    0  success (some sessions may have been duplicates)
    1  no creds / no API
    2  partial — some uploads failed
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Always private. The /me page advertises this guarantee — never
# publicize what this script uploads.
HARDCODED_ACL = "private"

CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR  = Path.home() / ".codex" / "sessions"
HERMES_DIR = Path.home() / ".hermes"
OPENCLAW_DIR = Path.home() / ".openclaw"


# ---------- adapters: one per agent runtime ---------------------------

def _detect_sources(user_supplied: list[str] | None) -> list[str]:
    if user_supplied:
        return user_supplied
    detected: list[str] = []
    if CLAUDE_DIR.is_dir(): detected.append("claude-code")
    if CODEX_DIR.is_dir():  detected.append("codex")
    if HERMES_DIR.is_dir() and (HERMES_DIR / "sessions").is_dir(): detected.append("hermes")
    if OPENCLAW_DIR.is_dir() and (OPENCLAW_DIR / "sessions").is_dir(): detected.append("openclaw")
    return detected


def _list_sessions(source: str) -> list[Path]:
    """Return paths of session files for this source, newest first."""
    if source == "claude-code":
        if not CLAUDE_DIR.is_dir(): return []
        files = list(CLAUDE_DIR.glob("*/*.jsonl"))
    elif source == "codex":
        if not CODEX_DIR.is_dir(): return []
        files = []
        for ext in ("*.json", "*.jsonl"):
            files.extend(CODEX_DIR.rglob(ext))
    elif source in ("hermes", "openclaw"):
        root = HERMES_DIR if source == "hermes" else OPENCLAW_DIR
        sess = root / "sessions"
        files = list(sess.rglob("*.json")) if sess.is_dir() else []
    else:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _split_anthropic_blocks(role: str, content: Any) -> list[dict[str, Any]]:
    """Split an Anthropic-style content list into ONE turn per block.
    Each block (text / thinking / tool_use / tool_result / image) becomes
    its own {role, content} entry — no truncation, no encrypted opaques.
    """
    if isinstance(content, str):
        c = content.strip()
        return [{"role": role, "content": content}] if c else []
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            t = block.get("text") or ""
            if t.strip():
                out.append({"role": role, "content": t})
        elif bt == "thinking":
            # keep thinking text, drop opaque base64 `signature`
            t = block.get("thinking") or ""
            if t.strip():
                out.append({"role": role, "content": f"💭 思考\n\n{t}"})
        elif bt == "tool_use":
            name = block.get("name", "?")
            inp = block.get("input")
            try:
                inp_str = json.dumps(inp, ensure_ascii=False, indent=2)
            except Exception:
                inp_str = str(inp)
            tool_id = block.get("id", "")
            id_suffix = f"  (id={tool_id[:12]})" if tool_id else ""
            out.append({
                "role": role,  # assistant
                "content": f"🔧 调用工具: {name}{id_suffix}\n\n```json\n{inp_str}\n```",
            })
        elif bt == "tool_result":
            tr_content = block.get("content")
            if isinstance(tr_content, list):
                inner = []
                for c in tr_content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            inner.append(c.get("text", ""))
                        elif c.get("type") == "image":
                            inner.append("[image]")
                tr_text = "\n".join(inner)
            elif isinstance(tr_content, str):
                tr_text = tr_content
            else:
                tr_text = json.dumps(tr_content, ensure_ascii=False)
            tool_id = block.get("tool_use_id", "")
            id_suffix = f"  (id={tool_id[:12]})" if tool_id else ""
            is_error = block.get("is_error")
            err_marker = " ❌" if is_error else ""
            out.append({
                "role": "tool",
                "content": f"📤 工具返回{err_marker}{id_suffix}\n\n{tr_text}",
            })
        elif bt == "image":
            out.append({"role": role, "content": "🖼️ [图片]"})
        elif bt:
            out.append({"role": role, "content": f"[{bt}]"})
    return out


def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a Claude Code session JSONL into a flat trajectory list.

    Each Anthropic content block becomes its OWN turn (so tool_use stays
    visible alongside the assistant text that called it, instead of being
    crammed into a single assistant message). Meta-only lines (last-prompt
    / queue-operation / attachment / file-history-snapshot / permission-
    mode / summary) are dropped.
    """
    out: list[dict[str, Any]] = []
    SKIP_TYPES = {
        "last-prompt", "queue-operation", "attachment",
        "file-history-snapshot", "permission-mode", "summary",
    }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") in SKIP_TYPES:
            continue
        msg = d.get("message") if isinstance(d.get("message"), dict) else d
        role = msg.get("role") or d.get("type") or ""
        if role not in ("user", "assistant", "system", "tool"):
            continue
        raw_content = msg.get("content", d.get("content", ""))
        out.extend(_split_anthropic_blocks(role, raw_content))
    return out


def _parse_codex_json(path: Path) -> list[dict[str, Any]]:
    """Codex rollouts (~/.codex/sessions/.../rollout-*.jsonl) are JSONL
    where each line is `{type, payload}`. We use `response_item` as the
    source of truth (event_msg duplicates the same content) and emit one
    turn per logical block — message text, reasoning, function_call,
    function_call_output. No truncation, no opaque base64 noise.
    """
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out

    role_norm = {"developer": "system", "tool": "tool"}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue

        # Direct {role, content} (legacy fallback)
        if "role" in d and "content" in d:
            role, content = d.get("role"), d.get("content", "")
            role = role_norm.get(role, role)
            if role in ("user", "assistant", "system", "tool"):
                out.extend(_split_anthropic_blocks(role, content))
            continue

        if d.get("type") != "response_item":
            continue
        payload = d.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")

        if ptype == "message":
            role = payload.get("role")
            role = role_norm.get(role, role)
            if role not in ("user", "assistant", "system", "tool"):
                continue
            content = payload.get("content", "")
            if isinstance(content, str):
                if content.strip():
                    out.append({"role": role, "content": content})
            elif isinstance(content, list):
                # codex content blocks: input_text / output_text / image
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct in ("input_text", "output_text", "text"):
                        t = c.get("text") or ""
                        if t.strip():
                            out.append({"role": role, "content": t})
                    elif ct in ("input_image", "image"):
                        out.append({"role": role, "content": "🖼️ [图片]"})

        elif ptype == "reasoning":
            # `summary` is the human-readable thinking; `encrypted_content`
            # is opaque base64 (skip).
            summ = payload.get("summary") or []
            parts = []
            if isinstance(summ, list):
                for s in summ:
                    if isinstance(s, dict):
                        t = s.get("text") or ""
                        if t.strip():
                            parts.append(t)
                    elif isinstance(s, str) and s.strip():
                        parts.append(s)
            inline = payload.get("content")
            if isinstance(inline, str) and inline.strip():
                parts.append(inline)
            if parts:
                out.append({
                    "role": "assistant",
                    "content": "💭 思考\n\n" + "\n\n".join(parts),
                })

        elif ptype == "function_call":
            name = payload.get("name", "?")
            args_raw = payload.get("arguments", "")
            # arguments is a JSON STRING — parse + pretty-print
            try:
                args_obj = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                args_pretty = json.dumps(args_obj, ensure_ascii=False, indent=2)
            except Exception:
                args_pretty = str(args_raw)
            call_id = payload.get("call_id", "")
            id_suffix = f"  (id={call_id[:12]})" if call_id else ""
            out.append({
                "role": "assistant",
                "content": f"🔧 调用工具: {name}{id_suffix}\n\n```json\n{args_pretty}\n```",
            })

        elif ptype == "function_call_output":
            output = payload.get("output", "")
            call_id = payload.get("call_id", "")
            id_suffix = f"  (id={call_id[:12]})" if call_id else ""
            # output is sometimes a JSON-encoded string with a `content`
            # field — try to flatten that to bare text for readability.
            disp = output
            if isinstance(output, str):
                try:
                    parsed = json.loads(output)
                    if isinstance(parsed, dict) and "output" in parsed:
                        disp = parsed["output"]
                    elif isinstance(parsed, dict) and "content" in parsed:
                        disp = parsed["content"]
                except Exception:
                    pass
            if not isinstance(disp, str):
                disp = json.dumps(disp, ensure_ascii=False)
            out.append({
                "role": "tool",
                "content": f"📤 工具返回{id_suffix}\n\n{disp}",
            })

    return out


def _build_trajectory(source: str, path: Path) -> list[dict[str, Any]]:
    if source == "claude-code":
        return _parse_jsonl(path)
    if source == "codex":
        # Codex rollouts are .jsonl with nested {type, payload} entries —
        # always go through the codex-aware parser regardless of suffix.
        return _parse_codex_json(path)
    return _parse_jsonl(path)


# ---------- card derivation -------------------------------------------

_TASK_SUMMARY_RE = re.compile(r"(?im)^\s*\[task-summary\]\s*[:：]\s*(.+?)\s*$")


def _extract_task_summary_title(traj: list[dict[str, Any]]) -> str:
    """Prefer an agent-emitted [task-summary] marker when present."""
    for turn in reversed(traj):
        content = str(turn.get("content") or "")
        matches = _TASK_SUMMARY_RE.findall(content)
        if not matches:
            continue
        title = " ".join(matches[-1].strip().split())
        title = title.strip('"\'`「」『』').strip()
        if title.endswith(("。", ".", "!", "?", "！", "？", ":", "：")):
            title = title[:-1].strip()
        if title:
            return title[:70] + ("…" if len(title) > 70 else "")
    return ""


def _derive_title(first_user: str, traj: list[dict[str, Any]], source: str) -> str:
    """Build a one-line title summarising the session.

    Strategy: prefer the explicit [task-summary] marker, then take the
    first real user message, drop quoted blocks / URLs / code fences,
    take the first sentence, and trim to ~70 chars. Falls back to
    "<source> session" if no usable text is found.
    """
    explicit = _extract_task_summary_title(traj)
    if explicit:
        return explicit
    text = (first_user or "").strip()
    # strip leading code fence / blockquote markers
    while text.startswith(("```", ">", "<")):
        nl = text.find("\n")
        if nl < 0:
            break
        text = text[nl + 1 :].strip()
    # drop blank lines, take first non-empty
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not line:
        return f"{source} session"
    # cut at sentence boundary if there is one within first ~120 chars
    head = line[:120]
    cut_at = -1
    for sep in ("。", "！", "？", ". ", "! ", "? ", "\n"):
        idx = head.find(sep)
        if idx > 0 and (cut_at < 0 or idx < cut_at):
            cut_at = idx + len(sep)
    title = head[:cut_at].strip() if cut_at > 0 else head.strip()
    # tighten whitespace
    title = " ".join(title.split())
    if len(title) > 70:
        title = title[:69].rstrip() + "…"
    return title or f"{source} session"


def _card_from_trajectory(traj: list[dict[str, Any]], source: str, path: Path) -> dict[str, Any]:
    """Generate the LiteCard fields from the trajectory.

    Heuristic: first user message → query, last assistant message → outcome,
    intent guessed from query. Server-side annotator can re-derive these
    later if you want better quality.
    """
    def _is_real_user(t: dict[str, Any]) -> bool:
        if t["role"] != "user":
            return False
        c = (t.get("content") or "").lstrip()
        # skip codex environment_context wrappers and our emoji-marked
        # synthetic turns (none of these are actual user prompts)
        if c.startswith("<environment_context>"):
            return False
        if c.startswith(("🔧", "📤", "💭", "🖼️", "[")):
            return False
        return bool(c)

    def _is_real_assistant(t: dict[str, Any]) -> bool:
        if t["role"] != "assistant":
            return False
        c = (t.get("content") or "").lstrip()
        if c.startswith(("🔧", "💭")):
            return False
        return bool(c)

    first_user = next((t["content"] for t in traj if _is_real_user(t)), "")
    last_assistant = next(
        (t["content"] for t in reversed(traj) if _is_real_assistant(t)),
        "",
    )
    title = _derive_title(first_user, traj, source)
    return {
        "query": (first_user or "(no user message)")[:512],
        "intent": title,
        "steps": [f"replay session {path.name} ({len(traj)} turns)"],
        "outcome": (last_assistant or "(no assistant reply)")[:512],
        "task_type": f"{source}-backfill",
        "source_model": "unknown",  # server may infer better
        "sensitivity": "medium",
        "acl": HARDCODED_ACL,            # ← never public
        "tags": [f"backfill", f"src:{source}"],
        "trajectory": traj,
        "meta": {
            "agent_type": source,
            "session_id": path.stem,
            "source_path": str(path),
            "uploaded_via": "session-extractor",
        },
    }


# ---------- HMAC + HTTP -----------------------------------------------

def _hmac_post(base_url: str, name: str, secret: str, path: str, body: dict) -> dict[str, Any]:
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    canonical = b"\n".join([b"POST", path.encode(), body_bytes])
    sig = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body_bytes,
        headers={
            "content-type": "application/json",
            "x-agent-name": name,
            "x-signature": sig,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


# ---------- main loop -------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--sources", default="",
                   help="comma-separated; default = auto-detect")
    p.add_argument("--limit", type=int, default=0,
                   help="cap total uploads (0 = unlimited)")
    p.add_argument("--since", default="",
                   help="only sessions modified after this ISO date")
    p.add_argument("--max-mb", type=float, default=3.0,
                   help="skip sessions larger than this (MB); default 3, "
                        "0 = no cap. Big sessions can OOM the API.")
    p.add_argument("--sleep", type=float, default=0.5,
                   help="sleep between pushes (seconds), to give server "
                        "breathing room. Default 0.5")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    name = os.environ.get("EXP_AGENT_NAME", "").strip()
    secret = os.environ.get("EXP_AGENT_SECRET", "").strip()
    base = os.environ.get("EXP_BASE_URL", "").strip().rstrip("/")
    if not name or not secret or not base:
        print("ERROR: EXP_AGENT_NAME, EXP_AGENT_SECRET, EXP_BASE_URL all required.",
              file=sys.stderr)
        print("       Get them from your portal /me page (the bind script).",
              file=sys.stderr)
        return 1

    sources = _detect_sources(
        [s.strip() for s in args.sources.split(",") if s.strip()] or None
    )
    if not sources:
        print("no agent runtimes detected on this host.", file=sys.stderr)
        return 1
    print(f"[extractor] sources: {', '.join(sources)}")
    print(f"[extractor] target:  {base}  agent={name}")
    print(f"[extractor] acl:     {HARDCODED_ACL} (never public — by design)")

    since_ts: float = 0.0
    if args.since:
        try:
            since_ts = datetime.fromisoformat(args.since.replace("Z", "+00:00")).timestamp()
        except ValueError:
            print(f"--since not a valid ISO date: {args.since}", file=sys.stderr)
            return 1

    counts = {"uploaded": 0, "duplicate": 0, "skipped": 0, "failed": 0}
    total = 0

    for src in sources:
        sessions = _list_sessions(src)
        print(f"\n[{src}] found {len(sessions)} session file(s)")
        for path in sessions:
            if args.limit and total >= args.limit:
                print(f"[extractor] hit --limit={args.limit}, stopping.")
                return _summary(counts)
            if since_ts and path.stat().st_mtime < since_ts:
                counts["skipped"] += 1
                continue
            # Skip oversized sessions — they reliably OOM the server.
            if args.max_mb > 0:
                size_mb = path.stat().st_size / (1024 * 1024)
                if size_mb > args.max_mb:
                    counts["skipped"] += 1
                    if args.verbose:
                        print(f"  ⊘ {src}/{path.stem[:8]} too big "
                              f"({size_mb:.1f}MB > {args.max_mb}MB); skip "
                              f"(use --max-mb 0 to push anyway)")
                    continue
            traj = _build_trajectory(src, path)
            if not traj:
                counts["skipped"] += 1
                if args.verbose:
                    print(f"  ⊘ {path.name} empty trajectory; skip")
                continue
            card = _card_from_trajectory(traj, src, path)
            total += 1
            short = path.stem[:8]
            if args.dry_run:
                print(f"  [{total}] would upload {src}/{short}  turns={len(traj)}")
                counts["uploaded"] += 1
                continue
            try:
                resp = _hmac_post(base, name, secret, "/v1/lite/push", card)
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", errors="replace")[:200]
                print(f"  ✗ [{total}] {src}/{short}  HTTP {e.code}  {msg}")
                counts["failed"] += 1
                continue
            except Exception as e:
                print(f"  ✗ [{total}] {src}/{short}  {type(e).__name__}: {e}")
                counts["failed"] += 1
                continue
            eid = (resp.get("experience_id") or "?")[:8]
            if resp.get("ingest_path") == "lite-dup":
                counts["duplicate"] += 1
                if args.verbose:
                    print(f"  ⏎ [{total}] {src}/{short} → {eid} (already in pool)")
            else:
                counts["uploaded"] += 1
                print(f"  ✓ [{total}] {src}/{short} → {eid}  (acl=private)")
            # Tiny breathing room between pushes so the single-thread
            # server has time to commit + GC before the next request.
            if args.sleep > 0:
                time.sleep(args.sleep)

    return _summary(counts)


def _summary(counts: dict[str, int]) -> int:
    print()
    print(f"[extractor] DONE — uploaded={counts['uploaded']}  "
          f"duplicate={counts['duplicate']}  "
          f"skipped={counts['skipped']}  "
          f"failed={counts['failed']}")
    print(f"[extractor] visit your portal /me to review or revoke.")
    return 0 if counts["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
