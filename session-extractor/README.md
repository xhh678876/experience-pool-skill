# session-extractor

Standalone tool: scan local Claude Code / Codex / Hermes / openclaw
session files and bulk-upload them into your **private** experience-pool
repo. Modeled on `claude_sft_delivery`'s single-entry pattern.

## Privacy guarantee

`acl` is **hardcoded to `private`** in `extract_and_upload.py`. There is
no flag to make these uploads public. Each row only you (the owner of
the `EXP_AGENT_NAME`) can see, until you explicitly publish it through
the portal `/me` page.

## How to run

The portal `/me` page gives you a one-liner with your name + secret
embedded. It looks like:

```bash
curl -fsSL http://10.244.66.195:3080/session-extractor/run.sh | \
  EXP_AGENT_NAME='user-xxx' \
  EXP_AGENT_SECRET='<hex>' \
  EXP_BASE_URL='http://10.244.66.195:3080' \
  bash
```

That command:
1. Downloads `extract_and_upload.py` from the API
2. Auto-detects `~/.claude/projects/`, `~/.codex/sessions/`, etc.
3. POSTs each session to `/v1/lite/push` with HMAC + `acl=private`
4. Uses `[task-summary]` as title when present, otherwise falls back to
   the first real user question
5. Server fingerprint-dedups so re-runs are safe

## Manual run

```bash
EXP_AGENT_NAME='user-xxx' \
EXP_AGENT_SECRET='<hex>' \
EXP_BASE_URL='http://10.244.66.195:3080' \
python3 extract_and_upload.py [options]
```

## Options

| flag | default | what |
|---|---|---|
| `--sources` | auto-detect | comma-list: `claude-code,codex,hermes,openclaw` |
| `--limit N` | unlimited | cap total uploads |
| `--since <iso>` | none | only sessions newer than this date |
| `--dry-run` | off | list, don't post |
| `--verbose`, `-v` | off | per-session output |

## Output

```
[extractor] sources: claude-code, codex
[extractor] target:  http://10.244.66.195:3080  agent=user-xxx
[extractor] acl:     private (never public — by design)

[claude-code] found 21 session file(s)
  ✓ [1] claude-code/1e03ccdb → a3f8b21c  (acl=private)
  ⏎ [2] claude-code/030cf3d4 → b71d2e44 (already in pool)
  ✓ [3] claude-code/63999d18 → c4f0a512  (acl=private)
  ...

[extractor] DONE — uploaded=18  duplicate=2  skipped=1  failed=0
[extractor] visit your portal /me to review or revoke.
```

## Why standalone

- **Zero deps** beyond stdlib — runs anywhere with Python 3.9+
- **Self-contained credentials** — no need to have `exp` CLI installed
- **Idempotent** — server-side fingerprint dedup, run as many times as you want
- **Pre-installed `exp` CLI not required** — useful when bind hasn't been
  run on this machine yet
