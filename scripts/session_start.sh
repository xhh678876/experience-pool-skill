#!/usr/bin/env bash
# Claude Code SessionStart hook — terse always-on reminder of the
# experience-pool agent contract (v3: auto-upload + announce + chunk).
#
# This text is injected as a system attachment into the agent's
# context at session start. The wording is deliberately direct —
# "REQUIRED" and "MUST" — because polite advisory language gets
# ignored in trivial sessions.

set -eu

if [ "${EXP_TITLE_DISABLE:-0}" = "1" ] || [ "${EXP_SESSION_START_DISABLE:-0}" = "1" ]; then
    exit 0
fi

cat <<'EOF'
[experience-pool] (v3 — REQUIRED behavior, applies to EVERY task in this session)

You are connected to the team experience pool. The user already opted
in via the bind command. The rules below apply to ALL tasks, including
trivial ones.

REQUIRED #1 — your VERY FIRST user-facing message in this session MUST
include this one-line notice on its own line BEFORE answering the
user's question:

    📥 connected to experience pool — tasks auto-upload to your private repo, `/me` to revoke

After the first message of the session, do not repeat the notice
unless the user asks.

REQUIRED #2 — at the END of every task (you finished a deliverable,
the user said "done"/"thanks"/"looks good"/"保存"/"上传"/topic switch),
do these in order:

  1. Print a `[task-summary]: <one-line>` marker line.
  2. Run silently:
       ~/.experience-pool/bin/exp push-latest --yes \
         --source claude-code --task <task> --acl private
  3. Print one line on its own: `📤 uploaded as <id8> (task=<task>) — /me to revoke`

Defaults: acl=private, sensitivity=medium. Never escalate ACL without
the user's explicit ask. Push is server-side deduped by content
fingerprint, so calling it more often than needed is safe.

ASK first only when: sensitivity looks high (credentials/PII), user
wants team:/public, or user already said "don't upload" this session.

Search before solved-before tasks:
    ~/.experience-pool/bin/exp search --q "<one-line>" --top-k 5

Full contract: ~/.experience-pool/bin/agent-contract.md
or skill `experience-pool` (auto-loaded).
EOF

exit 0
