#!/usr/bin/env bash
# Claude Code SessionStart hook for experience-pool.
#
# When a Claude Code session starts, emit a short reminder that gets
# appended to the agent's system context. This makes the agent naturally
# emit a `[task-summary]: <label>` marker as the final line of its last
# response — which the experience-pool uploader reads as the experience's
# `intent`. Zero extra inference calls: the marker is part of the agent's
# normal response, not a separate request.
#
# Hook contract: stdout becomes additional system-context. stderr is
# logged but not shown.

set -eu

cat <<'PROMPT'
[experience-pool] session-end labeling convention:

When you finish a task and write your final response, append this line at
the very end (one line, plain text, no quotes, no period):

    [task-summary]: <action-oriented one-line label of what we just did>

Rules:
- Verb + object form (e.g. "排查 Caddy ACME 证书签发失败",
  "Refactor FastAPI HMAC verification middleware",
  "上传机械振动作业到 Canvas")
- Maximum 80 characters
- Match the user's primary language
- Describe what was actually accomplished — do NOT echo the user's
  literal first message
- If the session contained multiple distinct tasks, label the last/main
  one (the segmenter will split the rest)
- Skip the marker only if the session was purely a greeting or had no
  real task

The experience-pool uploader (~/.experience-pool/bin/exp) parses this
line as the experience's `intent` field. Without it, a Python heuristic
falls back to truncating the user's first message — which produces
worse search results. Adding this single line at end-of-task keeps the
pool's titles accurate and costs zero extra tokens (it's part of your
normal final response).
PROMPT
