# experience-pool-skill

A Claude Code (and friends) **Skill** that auto-uploads every agent session
to a shared Experience Pool. One-line install, no manual `push` needed.

完整中文说明见 [docs/UPLOAD_LOGIC_AND_MANUAL.md](docs/UPLOAD_LOGIC_AND_MANUAL.md)，覆盖 HMAC 签名、LiteCard 字段、自动上传、批量回填、ACL、检索和常见问题。

```bash
curl -sSL https://expool.clawsii.com/install | bash
```

## What this is

A 5-piece system:

1. **Skill** — `SKILL.md` + helpers; lives at `~/.claude/skills/experience-pool/`
   so any Claude Code agent on the box knows it can `exp search` /
   `exp push` / `exp get-rewards`.
2. **Universal uploader** — `scripts/exp_uploader.py` (stdlib only). Adapters
   for Claude Code, Hermes, agents-chat, Cursor, Aider, Codex, Continue.dev,
   Open Interpreter, plus a robust generic JSON ingester (auto-detects
   OpenAI / Anthropic / LangChain / AutoGen / CrewAI / LangSmith shapes).
3. **Consent + portal bind** — `scripts/exp_consent.py` and `exp bind` let
   the intranet portal issue a per-user HMAC credential, then store it locally
   under `~/.experience-pool/credentials/`.
4. **Session extractor** — `session-extractor/` bulk-backfills Claude Code /
   Codex / Hermes / OpenClaw history into the user's private repo without
   requiring the full CLI to be pre-installed.
5. **Reward annotator** — `scripts/exp_annotator.py`. Implements Synergy's
   per-turn reward schema (5 dims × `{-1,0,+1}` + confidence + reason),
   slicing `<user>`, `<assistant>`, and the next K turns as delayed feedback
   for the judge. Backends: `claude` CLI · Anthropic API · OpenAI-compat.

## Architecture

```
local agent session                     ┌─────────────────────────┐
   │ ~/.claude/projects/*.jsonl         │ Stop hook (Claude Code) │
   │ ~/.hermes/sessions/*.json[l]       │   → instant upload      │
   │ ~/agents-chat/messages.db          ├─────────────────────────┤
   │ ~/.continue/sessions/*.json        │ launchd / systemd timer │
   │ ~/.codex/sessions/*                │   every 2 min           │
   │ ...                                │   exp daemon-tick       │
   ▼                                    └────────────┬────────────┘
adapter normalize → client sanitize                  │
   │                                                 ▼
   └──── HMAC-signed POST /v1/lite/push ─►  expool.clawsii.com
                                                    │
   optional: exp_annotator slices each              ▼
   (user, assistant) + next K turns,           SQLite + vectors
   posts 5-dim rewards to                      + turn_rewards table
   POST /v1/lite/rewards
```

## Auto-sync coverage

Default sources sync automatically once installed:

| Source | Storage | Auto |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | Stop hook (instant) + daemon backstop |
| OpenClaw / OpenClaw-sjtu | shares `~/.claude/projects/` (+ `~/.openclaw/`) | same as above |
| Hermes | `~/.hermes/sessions/*.json[l]` | daemon (2 min) |
| agents-chat | `~/agents-chat/messages.db` (SQLite by `thread_id`) | daemon (2 min) |
| Continue.dev | `~/.continue/sessions/*.json` | daemon (2 min) |
| Codex CLI | `~/.codex/sessions/**` | daemon (2 min) |
| Cursor | `~/Library/.../Cursor/User/**/state.vscdb` | manual (`exp push`) |
| Aider | `<cwd>/.aider.chat.history.md` | manual |
| Open Interpreter | `~/Library/.../Open Interpreter/**` | manual |
| Generic | any `{messages|trajectory|history|chat_history|runs}` | `exp push-file` |

Override with `EXP_AUTO_SOURCES=claude-code,hermes,...` at install time.

## Install variants

```bash
# default (auto-sync ON, ACL=private, daemon every 120s)
curl -sSL https://expool.clawsii.com/install | bash

# named identity (recommended for shared servers)
curl -sSL https://expool.clawsii.com/install \
  | EXP_AGENT_NAME=alice EXP_TEAM=platform bash

# disable auto-sync entirely (manual push only)
curl -sSL https://expool.clawsii.com/install \
  | EXP_SKIP_HOOK=1 EXP_SKIP_DAEMON=1 bash

# pin which agents auto-sync
curl -sSL https://expool.clawsii.com/install \
  | EXP_AUTO_SOURCES=claude-code,hermes bash

# different default ACL
curl -sSL https://expool.clawsii.com/install \
  | EXP_AUTO_ACL=team:platform bash
```

## After install

```bash
exp whoami
exp daemon-state                         # see what's been synced
exp daemon-tick --dry-run -v             # preview next sync
exp list-sessions --source claude-code   # list local sessions
exp push-latest --acl team:platform      # one-off
exp search --q "FastAPI HMAC signature mismatch" --top-k 5
exp push --session <id> --annotate       # extract + reward + push
exp get-rewards --experience-id <eid>    # pull stored 5-dim rewards
exp annotate-existing                    # re-judge with different model
```

## Bulk backfill

The portal `/me` page can provide a one-line command with the user's
agent name and HMAC secret. Use placeholders in docs and scripts; do not
commit real secrets.

```bash
curl -fsSL http://10.244.66.195:3080/session-extractor/run.sh | \
  EXP_AGENT_NAME='user-xxx' \
  EXP_AGENT_SECRET='<portal-issued-secret>' \
  EXP_BASE_URL='http://10.244.66.195:3080' \
  bash
```

`session-extractor` hardcodes `acl=private` and server-side fingerprinting
makes repeated runs idempotent.

## Reward schema (Synergy-compatible)

For each evaluated `(user, assistant)` pair the annotator sends three sections
to the judge model:

```
<user>        the user request
<assistant>   the assistant response (text + [Tool: name] formatted calls)
<subsequent>  next K user/assistant turns — primary delayed-feedback signal
```

Output (strict JSON):

```json
{
  "outcome": 1, "intent": 0, "execution": 1,
  "orchestration": 0, "expression": 1,
  "confidence": 0.7,
  "reason": "user built on the result without correction"
}
```

`{-1, 0, +1}` per dimension; `confidence ∈ [0,1]`. Stored at
`/v1/lite/rewards` with `(experience_id, turn_index, judge_model)` as the
composite primary key, so multiple judges can co-exist on the same trace.

## Privacy

- Client-side regex sweep before upload: Anthropic / OpenAI / Stripe / GitHub /
  AWS keys, URL credentials, email, IPv4
- Server runs the full 3-layer sanitizer again (rules → heuristic PII →
  optional LLM business sensitivity)
- `~/.experience-pool/credentials/*.json` (your HMAC secret) is mode 0600 and
  never leaves your machine
- Daemon-tick caps each session at 4 MB upload size by default

## Files

```
SKILL.md                # the Claude Code skill manifest
scripts/install.sh      # one-shot installer (also served at expool.clawsii.com/install)
scripts/exp_uploader.py # universal multi-agent uploader (stdlib only)
scripts/exp_consent.py  # local consent / pending / revoke helpers
scripts/exp_annotator.py# Synergy-style 5-dim reward annotator
session-extractor/      # standalone private backfill tool
agent-contract.md       # behavior contract copied into agent runtime dirs
docs/UPLOAD_LOGIC_AND_MANUAL.md
LICENSE                 # MIT
```

## Server

The reference Experience Pool gateway is hosted at
`https://expool.clawsii.com` (run from the open-source
[experience-pool](https://github.com/) project — FastAPI + SQLite, single
binary, behind Caddy + Let's Encrypt). To run your own gateway, point
`EXP_BASE_URL` at it during install.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.experience-pool.daemon   # macOS
systemctl --user disable --now expool-daemon.timer          # Linux
rm -rf ~/.experience-pool
# manually remove the experience-pool entry from ~/.claude/settings.json hooks.Stop
```

## License

MIT — see [LICENSE](LICENSE).
