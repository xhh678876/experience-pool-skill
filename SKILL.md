---
name: experience-pool
description: Auto-upload every agent session (trace + tool calls + per-turn rewards) to a shared Experience Pool so the team can search past playbooks and train on real trajectories. Works for Claude Code, Hermes, Cursor, agents-chat, Continue.dev, Codex, Aider, and any OpenAI-shaped messages file.
version: 0.3.0
license: MIT
homepage: https://github.com/xhh678876/experience-pool-skill
gateway: https://expool.clawsii.com
triggers:
  - share experience
  - learn from past
  - lookup playbook
  - upload trajectory
  - record what worked
  - score this trace
  - annotate rewards
---

# experience-pool

This skill plugs every agent on a host into the team's **Experience Pool**: an
HMAC-authenticated, ACL-aware shared store of past sessions. Once installed,
every finished session uploads automatically — no manual command needed.

The pool is a Lite-mode pipeline by default: client-side sanitize → HMAC
signed upload → SQLite + vector search on the server. Optional per-turn
reward annotation uses the [Synergy reward schema][synergy-reward]
(5 dimensions × `{-1, 0, +1}` + confidence + reason, with the next K turns
fed as delayed feedback).

## What "automatic" means

- **Claude Code** — A `Stop` hook in `~/.claude/settings.json` uploads the
  just-finished session **instantly** when you exit the agent.
- **Hermes / agents-chat / Continue.dev / Codex** — A launchd LaunchAgent
  (macOS) or systemd user timer (Linux) runs `exp daemon-tick` every
  **2 minutes**, finds new session files, and uploads them incrementally.
  Already-uploaded session ids are remembered in
  `~/.experience-pool/state.json` so nothing is double-sent client-side.
- **Cursor / Aider / Open Interpreter / generic JSON** — supported via
  manual `exp push` or by adding their adapter name to `EXP_AUTO_SOURCES`.

## Install — single command

```bash
curl -sSL https://expool.clawsii.com/install | bash
```

With identity:

```bash
curl -sSL https://expool.clawsii.com/install \
  | EXP_AGENT_NAME=alice EXP_TEAM=platform bash
```

Skip the auto-uploader:

```bash
curl -sSL https://expool.clawsii.com/install \
  | EXP_SKIP_HOOK=1 EXP_SKIP_DAEMON=1 bash
```

What lands on disk:

```
~/.experience-pool/
├── bin/
│   ├── exp                # shell wrapper → python3 exp_uploader.py
│   ├── exp_uploader.py    # 73 KB, stdlib only
│   ├── exp_annotator.py   # 22 KB, optional rewards
│   └── auto_upload.sh     # Stop hook target
├── credentials/<name>.json   # HMAC secret (chmod 600)
└── state.json             # daemon's per-source last-seen bookkeeping
```

Plus one of:

- macOS: `~/Library/LaunchAgents/com.experience-pool.daemon.plist`
- Linux: `~/.config/systemd/user/expool-daemon.{service,timer}`

## Once installed — what to do

Almost nothing. The agent's sessions ship themselves. When you want to
*search* prior work, ask the agent to run:

```bash
# Check what was synced
exp daemon-state

# Find prior playbooks before tackling a similar task
exp list-sessions --source claude-code --limit 10

# Push something explicitly with a non-default ACL
exp push-latest --acl team:platform

# Upload an arbitrary OpenAI-shaped messages.json
exp push-file --file traj.json --task csv_analysis --acl public

# Re-annotate an already-uploaded experience with a different judge model
exp annotate-existing --experience-id <eid> --session <local-id> \
    --annotate-backend claude --annotate-model claude-sonnet-4-6
```

## Reward annotation

Optional — disabled by default to keep the upload path free. When you do want
per-turn rewards on a trace:

```bash
exp push --session <id> --annotate \
    --annotate-model claude-haiku-4-5 \
    --annotate-max-turns 8 \
    --annotate-subsequent-k 4
```

Three backends, picked in order:

1. `claude` CLI (zero config — uses your installed Claude Code)
2. Anthropic Messages API (`ANTHROPIC_API_KEY`)
3. OpenAI-compatible `/chat/completions`
   (`EXP_REWARD_BASE_URL` + `EXP_REWARD_API_KEY`)

Result lands at `/v1/lite/rewards`, retrievable by:

```bash
exp get-rewards --experience-id <eid>
```

## Privacy posture

- 8 high-confidence regex rules redact secrets client-side before upload
  (Anthropic / OpenAI / Stripe / GitHub / AWS keys, URL credentials, email,
  IPv4). The server runs the full 3-layer sanitizer again as defense in
  depth.
- ACL is per-experience: `private` | `team:<name>` | `public`. Default
  for the auto-uploader is `private`; override with `EXP_AUTO_ACL`.
- Per-source toggle: `EXP_AUTO_SOURCES=claude-code,hermes,...` — drop any
  source you don't want auto-shipped.

## Output discipline

When this skill is invoked, do not paste the full JSON from any
`exp ...` call into the user-facing reply. Summarize: which prior
experience was reused, why it matched, and which steps were adapted.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.experience-pool.daemon         # macOS
systemctl --user disable --now expool-daemon.timer                # Linux
rm -rf ~/.experience-pool
# then remove the experience-pool entry from ~/.claude/settings.json hooks.Stop
```

[synergy-reward]: https://github.com/SII-Holos/synergy/blob/main/packages/synergy/src/agent/prompt/reward.txt
