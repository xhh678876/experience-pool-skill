#!/usr/bin/env python3
"""
exp_annotator — per-turn reward annotation for normalized session traces.

Implements Synergy's reward schema (SII-Holos/synergy `reward.txt`):
    5 dimensions × {-1, 0, +1}: outcome, intent, execution, orchestration, expression
    confidence ∈ [0, 1]
    reason: one sentence

Key design property (from synergy): per-turn evaluation uses *delayed feedback*
from the conversation that followed. For each evaluated turn we slice three
sections sent to the judge model:
    <user>        the user request that triggered this turn
    <assistant>   the assistant response (text + tool calls)
    <subsequent>  the next K turns of conversation (the success/failure signal)

Backends (auto-fallback in this order):
    1. claude CLI       (zero-config when `claude -p` is on PATH)
    2. Anthropic Messages API   (env: ANTHROPIC_API_KEY, EXP_REWARD_MODEL)
    3. OpenAI-compat /chat/completions
       (env: EXP_REWARD_BASE_URL, EXP_REWARD_API_KEY, EXP_REWARD_MODEL)

Usage:
    exp_annotator file traj.json                     # any uploader-normalized JSON
    exp_annotator session --source claude-code --session <id>
    exp_annotator stdin < normalized.json
        --subsequent-k 4         # how many next turns to feed as delayed feedback
        --max-turns 8            # cap evaluated turns (cost control)
        --pick first|even|important   # turn-selection strategy when capped
        --output rewards.json    # default: stdout
        --backend auto|claude|anthropic|openai
        --model claude-haiku-4-5-20251001

Output schema:
{
  "session_id": "...",
  "agent_type": "...",
  "model_used": "claude-haiku-4-5-20251001",
  "annotated_at": "2026-05-02T...",
  "summary": {
     "n_turns_evaluated": 8,
     "mean": {"outcome": 0.5, "intent": 0.75, ...},
     "trajectory_score": 0.6
  },
  "rewards": [
    {"turn_index": 3, "outcome":1, "intent":1, "execution":0,
     "orchestration":0, "expression":1, "confidence":0.7, "reason":"..."},
    ...
  ]
}
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Synergy reward.txt prompt (verbatim — see SII-Holos/synergy
# packages/synergy/src/agent/prompt/reward.txt). Kept as a single literal so
# we are byte-faithful to the original schema.
# ---------------------------------------------------------------------------

REWARD_PROMPT = """You are a conversation evaluator. You assess the quality of an AI assistant's response by analyzing the interaction itself and the conversation that followed. Score 5 quality dimensions as discrete values, assess your confidence, and explain your reasoning. Output ONLY valid JSON.

<input_format>

You will receive three sections:

<user> — The original user request for the turn being evaluated.

<assistant> — The assistant's response, which may include:
  - Text output (reasoning, explanations, analysis, generated content)
  - Tool calls shown as `[Tool: name] title` with per-field input parameters and output (may be truncated with "[truncated, N chars]")
  - Tool error indicators shown as `[Tool: name] (error)`

<subsequent> — Multiple rounds of conversation that occurred AFTER the evaluated turn. This section contains the next several user–assistant exchanges, formatted as alternating `User:` and `Assistant:` blocks. This is your primary source of delayed feedback — treat it as behavioral evidence of whether the evaluated turn succeeded or failed.

</input_format>

<scoring>

Each dimension is scored as {-1, 0, 1}. Score based on evidence, not assumption.

outcome — Was the task completed correctly?
  +1  Request fulfilled correctly and completely. The user's goal was achieved.
  0   Too trivial to judge, result unclear, or purely conversational (greetings, acknowledgments).
  -1  Failed, produced incorrect results, left work incomplete, or introduced new problems.

intent — Did the agent understand what the user actually needed?
  +1  Correctly grasped the real intent (especially when implicit). Adapted to user's preferences.
  0   Request was unambiguous and literal — no interpretation needed.
  -1  Misunderstood the request. Solved the wrong problem.

execution — Was the problem-solving approach effective?
  +1  Efficient path. Gathered info before acting. Adapted when blocked. Addressed root causes.
  0   Straightforward task with no meaningful methodology to evaluate.
  -1  Got stuck in retry loops. Brute-force when smarter existed. Shallow fixes for deep problems.

orchestration — Were tools and capabilities well-coordinated?
  +1  Right tool selection. Effective parallelism. Clean integration of multi-tool/multi-agent results.
  0   No coordination needed — single-tool or no-tool task.
  -1  Wrong tool. Over/under-delegated. Sequential when parallel was possible.

expression — Was the response well-delivered?
  +1  Clear, concise, well-structured. Right level of detail. Directly addressed user concerns.
  0   Adequate but unremarkable.
  -1  Verbose, disorganized, misleading, or wrong level of detail.

confidence — Your certainty in this evaluation, from 0 to 1.
  0.8–1.0  Strong explicit signals (praise, correction, "perfect", "wrong")
  0.6–0.8  Clear behavioral signals (user builds on work, or redoes the task)
  0.4–0.6  Moderate signal — conversation continues without clear indicators
  0.2–0.4  Weak signal — different topic, limited evidence
  0.0–0.2  Near-guessing — no subsequent conversation, or too ambiguous

reason — One concise sentence explaining the key factor behind your scores.

</scoring>

<guidelines>
- Read the full action sequence carefully. Tool inputs/outputs reveal what the assistant actually did.
- Distinguish between tasks the user explicitly asked for vs. proactive additions.
- Truncated outputs reduce confidence, not scores.
- Subsequent exchanges are your most valuable signal: corrections > praise; rephrasing same request = previous attempt failed; topic switches are NEUTRAL not negative.
- Don't penalize clarifying questions — they often indicate good intent understanding.
- Don't conflate length with quality.
</guidelines>

<format>
Output ONLY a single JSON object. No markdown, no explanation, no text before or after.
{"outcome":0,"intent":0,"execution":0,"orchestration":0,"expression":0,"confidence":0.5,"reason":"..."}
</format>
"""


# ---------------------------------------------------------------------------
# Trajectory slicing — find evaluable (user, assistant) pairs and build the
# three sections (<user>, <assistant>, <subsequent>) for each.
# ---------------------------------------------------------------------------

def _format_assistant(turn: dict[str, Any]) -> str:
    parts: list[str] = []
    if turn.get("content"):
        parts.append(str(turn["content"]))
    for tc in turn.get("tool_calls") or []:
        name = tc.get("name", "?")
        inp = tc.get("input", {})
        if isinstance(inp, dict):
            preview = json.dumps(inp, ensure_ascii=False)[:600]
        else:
            preview = str(inp)[:600]
        parts.append(f"[Tool: {name}] {preview}")
    return "\n".join(parts).strip()


def _format_subsequent(turns: list[dict[str, Any]], max_turns: int = 8,
                      max_chars_per_turn: int = 800) -> str:
    """Render alternating User:/Assistant: blocks from the next-K trajectory."""
    out: list[str] = []
    count = 0
    for t in turns:
        if count >= max_turns:
            break
        role = t.get("role", "")
        if role == "tool":
            continue  # synergy spec: subsequent only includes user/assistant exchanges
        content = t.get("content", "") or ""
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text")
        content = str(content)[:max_chars_per_turn]
        if not content.strip() and not (t.get("tool_calls")):
            continue
        if role == "user":
            out.append(f"User: {content}")
        elif role == "assistant":
            asst = _format_assistant(t)[:max_chars_per_turn]
            out.append(f"Assistant: {asst}")
        else:
            continue
        count += 1
    return "\n\n".join(out)


def find_evaluable_turns(trajectory: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Return (user_idx, assistant_idx) pairs. Skips system / pure-tool turns."""
    pairs: list[tuple[int, int]] = []
    pending_user: int | None = None
    for i, t in enumerate(trajectory):
        role = t.get("role")
        if role == "user" and (t.get("content") or "").strip():
            pending_user = i
        elif role == "assistant" and pending_user is not None:
            pairs.append((pending_user, i))
            pending_user = None
    return pairs


def select_turns(pairs: list[tuple[int, int]], max_turns: int, strategy: str) -> list[tuple[int, int]]:
    if len(pairs) <= max_turns:
        return pairs
    if strategy == "first":
        return pairs[:max_turns]
    if strategy == "even":
        # Evenly spaced sample, always include first + last.
        step = (len(pairs) - 1) / (max_turns - 1)
        idxs = sorted({0, len(pairs) - 1, *(round(i * step) for i in range(max_turns))})
        return [pairs[i] for i in idxs[:max_turns]]
    if strategy == "important":
        # Pick turns with the most tool calls or longest assistant content (rough
        # proxy for "decision points"). Falls back to even if all equal.
        scored: list[tuple[float, tuple[int, int]]] = []
        for u, a in pairs:
            assistant = a if isinstance(a, int) else 0
            return_pairs: tuple[int, int] = (u, a)
            score = 0
            scored.append((score, return_pairs))
        # Without trajectory in scope here we can't actually score; fallback to even.
        return select_turns(pairs, max_turns, "even")
    return pairs[:max_turns]


def build_judge_input(trajectory: list[dict[str, Any]], user_idx: int,
                      assistant_idx: int, subsequent_k: int) -> str:
    user_turn = trajectory[user_idx]
    assistant_turn = trajectory[assistant_idx]
    subsequent = trajectory[assistant_idx + 1:]
    user_block = str(user_turn.get("content", "")).strip()
    assistant_block = _format_assistant(assistant_turn)
    subsequent_block = _format_subsequent(subsequent, max_turns=subsequent_k)
    return (
        f"<user>\n{user_block}\n</user>\n\n"
        f"<assistant>\n{assistant_block}\n</assistant>\n\n"
        f"<subsequent>\n{subsequent_block or '(no follow-up turns)'}\n</subsequent>"
    )


# ---------------------------------------------------------------------------
# Backends.
# ---------------------------------------------------------------------------

class Backend:
    name = "abstract"
    model = ""

    def call(self, system: str, user: str) -> str:
        raise NotImplementedError


class ClaudeCLIBackend(Backend):
    name = "claude-cli"

    def __init__(self, model: str):
        self.model = model

    def call(self, system: str, user: str) -> str:
        proc = subprocess.run(
            ["claude", "-p", "--output-format", "json", "--model", self.model,
             "--append-system-prompt", system],
            input=user, capture_output=True, text=True, timeout=180, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude cli failed: {proc.stderr[:500]}")
        env = json.loads(proc.stdout)
        if env.get("is_error"):
            raise RuntimeError(f"claude cli error: {env}")
        return env.get("result", "")


class AnthropicAPIBackend(Backend):
    name = "anthropic-api"

    def __init__(self, model: str, api_key: str):
        self.model = model
        self.api_key = api_key

    def call(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": self.api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block.get("text", "")
        return ""


class OpenAIChatBackend(Backend):
    name = "openai-chat"

    def __init__(self, model: str, api_key: str, base_url: str):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def call(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""


def pick_backend(explicit: str, model: str | None) -> Backend:
    chosen_model = (
        model
        or os.environ.get("EXP_REWARD_MODEL")
        or "claude-haiku-4-5-20251001"
    )
    order: list[str] = [explicit] if explicit and explicit != "auto" else \
                       ["claude", "anthropic", "openai"]
    last_err: Exception | None = None
    for kind in order:
        try:
            if kind == "claude":
                # detect claude CLI
                rc = subprocess.run(["claude", "--version"], capture_output=True, timeout=5).returncode
                if rc == 0:
                    return ClaudeCLIBackend(chosen_model)
                continue
            if kind == "anthropic":
                key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("EXP_REWARD_API_KEY")
                if key:
                    return AnthropicAPIBackend(chosen_model, key)
                continue
            if kind == "openai":
                base = os.environ.get("EXP_REWARD_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
                key = os.environ.get("EXP_REWARD_API_KEY") or os.environ.get("OPENAI_API_KEY")
                if base and key:
                    return OpenAIChatBackend(chosen_model, key, base)
                continue
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            last_err = e
    raise SystemExit(
        "no usable annotator backend found. options:\n"
        "  - install Claude Code so `claude -p` is available, or\n"
        "  - export ANTHROPIC_API_KEY=sk-ant-...  (Messages API), or\n"
        "  - export EXP_REWARD_BASE_URL=<url>  EXP_REWARD_API_KEY=<key>  (OpenAI-compat)\n"
        f"last error: {last_err}"
    )


# ---------------------------------------------------------------------------
# Annotation driver.
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _validate_reward(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dim in ("outcome", "intent", "execution", "orchestration", "expression"):
        v = obj.get(dim, 0)
        try:
            v_int = int(v)
        except (TypeError, ValueError):
            v_int = 0
        if v_int not in (-1, 0, 1):
            v_int = max(-1, min(1, v_int))
        out[dim] = v_int
    try:
        c = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        c = 0.5
    out["confidence"] = max(0.0, min(1.0, c))
    out["reason"] = str(obj.get("reason", ""))[:500]
    return out


def annotate_session(session: dict[str, Any], backend: Backend, *,
                     subsequent_k: int, max_turns: int, strategy: str,
                     verbose: bool = False) -> dict[str, Any]:
    trajectory = session.get("trajectory") or []
    if not isinstance(trajectory, list) or not trajectory:
        raise SystemExit("session has no trajectory")
    pairs = find_evaluable_turns(trajectory)
    if not pairs:
        raise SystemExit("no evaluable (user, assistant) pairs in trajectory")
    selected = select_turns(pairs, max_turns, strategy)
    rewards: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for u_idx, a_idx in selected:
        user_block = build_judge_input(trajectory, u_idx, a_idx, subsequent_k)
        if verbose:
            print(f"[annotator] turn {a_idx}: calling {backend.name}/{backend.model}", file=sys.stderr)
        try:
            raw = backend.call(REWARD_PROMPT, user_block)
        except Exception as e:
            failures.append({"turn_index": a_idx, "error": f"{type(e).__name__}: {e}"})
            continue
        parsed = _extract_json(raw)
        if not parsed:
            failures.append({"turn_index": a_idx, "error": "non-json response", "raw": raw[:200]})
            continue
        valid = _validate_reward(parsed)
        rewards.append({
            "turn_index": a_idx,
            "user_turn_index": u_idx,
            **valid,
        })
    summary = _summarize(rewards)
    return {
        "session_id": session.get("session_id", ""),
        "agent_type": session.get("agent_type", ""),
        "backend": backend.name,
        "model_used": backend.model,
        "annotated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "subsequent_k": subsequent_k,
        "n_evaluable_pairs": len(pairs),
        "n_evaluated": len(rewards),
        "summary": summary,
        "rewards": rewards,
        "failures": failures,
    }


def _summarize(rewards: list[dict[str, Any]]) -> dict[str, Any]:
    if not rewards:
        return {"trajectory_score": 0.0, "mean": {}}
    dims = ("outcome", "intent", "execution", "orchestration", "expression")
    means = {}
    for dim in dims:
        vals = [r[dim] for r in rewards]
        means[dim] = round(sum(vals) / len(vals), 3)
    weights = {"outcome": 0.35, "intent": 0.20, "execution": 0.20,
               "orchestration": 0.10, "expression": 0.15}
    weighted = sum(means[d] * w for d, w in weights.items())
    confidence_mean = round(sum(r["confidence"] for r in rewards) / len(rewards), 3)
    return {
        "n": len(rewards),
        "mean": means,
        "trajectory_score": round(weighted, 3),
        "confidence_mean": confidence_mean,
    }


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _load_session(args: argparse.Namespace) -> dict[str, Any]:
    if args.cmd == "stdin":
        raw = sys.stdin.read()
    elif args.cmd == "file":
        raw = Path(args.path).expanduser().read_text(encoding="utf-8")
    elif args.cmd == "session":
        # Reuse exp_uploader's adapters in-process.
        try:
            here = Path(__file__).parent
            sys.path.insert(0, str(here))
            from exp_uploader import _adapter_parse, detect_source  # type: ignore
        except ImportError as e:
            raise SystemExit(f"exp_annotator session mode requires exp_uploader.py side-by-side: {e}")
        src = detect_source(args.source)
        sess = _adapter_parse(src, args.session)
        return sess.to_payload()
    else:
        raise SystemExit("unknown subcommand")
    obj = json.loads(raw)
    if "trajectory" not in obj and "messages" in obj:
        obj["trajectory"] = obj.pop("messages")
    return obj


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="exp_annotator", description=__doc__.split("\n")[1])
    p.add_argument("--backend", default="auto", choices=["auto", "claude", "anthropic", "openai"])
    p.add_argument("--model", default=None,
                   help="model id (default claude-haiku-4-5-20251001 or $EXP_REWARD_MODEL)")
    p.add_argument("--subsequent-k", type=int, default=4,
                   help="how many turns of subsequent conversation to feed as delayed feedback")
    p.add_argument("--max-turns", type=int, default=8,
                   help="cap on evaluated turns per session (cost control)")
    p.add_argument("--pick", default="even", choices=["first", "even", "important"],
                   help="turn-selection strategy when capped")
    p.add_argument("--output", default="-", help="output path; '-' for stdout")
    p.add_argument("--verbose", "-v", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("file", help="read a normalized session JSON file")
    sp.add_argument("path")
    sub.add_parser("stdin", help="read normalized session JSON from stdin")
    sp = sub.add_parser("session", help="extract from a local agent session via exp_uploader adapters")
    sp.add_argument("--source", default="auto")
    sp.add_argument("--session", required=True)

    args = p.parse_args(argv)
    session = _load_session(args)
    backend = pick_backend(args.backend, args.model)
    result = annotate_session(
        session, backend,
        subsequent_k=args.subsequent_k,
        max_turns=args.max_turns,
        strategy=args.pick,
        verbose=args.verbose,
    )
    out = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output == "-":
        print(out)
    else:
        Path(args.output).expanduser().write_text(out + "\n", encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
