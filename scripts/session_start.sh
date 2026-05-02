#!/usr/bin/env bash
# Claude Code SessionStart hook for experience-pool.
#
# Injects the [task-summary] convention into the agent's system context so
# the running agent self-labels each completed task in-flight, with zero
# extra inference calls.

set -eu

cat <<'PROMPT'
[experience-pool] task-labeling convention for this session:

A single Claude Code session often contains multiple distinct tasks
(debug X, then write Y, then translate Z). The experience-pool uploader
splits the trajectory into segments by topic-shift, so EACH task needs
its own label — a single end-of-session marker only covers the last
segment.

When to emit a marker:
- WHEN YOU FINISH A COHESIVE SUB-TASK and the user is about to move on
  (you just delivered the answer / code / fix / explanation that
  satisfies the current request)
- BEFORE the user's next message switches topic, if you can predict it
- AT SESSION END (as a final safeguard)

How to emit a marker — append this single line at the end of the wrapping-
up assistant response:

    [task-summary]: <action-oriented one-line label>

Rules:
- Verb + object form: "排查 Caddy ACME 证书签发失败",
  "Refactor FastAPI HMAC verification", "上传机械振动作业到 Canvas"
- Maximum 80 characters
- Match the user's primary language
- Describe what was actually accomplished, NOT the user's literal first
  message
- One line, no quotes, no period at the end
- Skip the marker only if the sub-task was a pure greeting / clarification
  with no real outcome

Examples in a multi-task session:

    User: 帮我排一下 Caddy 拿不到证书的原因
    Assistant: <diagnostic steps...>
    用户的 80 端口被防火墙挡了，开放后 ACME challenge 通过。
    [task-summary]: 排查并修复 Caddy ACME 80 端口防火墙阻塞

    User: 顺便把 README 翻译成英文
    Assistant: <translation...>
    Done — README.md is now translated.
    [task-summary]: Translate project README from Chinese to English

    User: 谢了
    Assistant: 不客气！

Each [task-summary] line is parsed by exp_uploader._extract_agent_summary()
and used as that segment's `intent` field. Without per-task markers,
earlier segments fall back to a Python heuristic that strips greetings
from the first user turn — which produces inferior search titles.

This convention costs ZERO extra inference: the marker is part of the
response you were already going to produce, not a separate call.
PROMPT
