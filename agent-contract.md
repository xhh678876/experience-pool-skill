# experience-pool agent contract

You have access to your team's shared **experience pool** through the
`exp` CLI at `~/.experience-pool/bin/exp` (or `EXP_BIN` env var). The pool
stores past task trajectories from every agent on the intranet, ranked by
quality, and lets you search before starting work + automatically capture
your own work after finishing.

This file is your contract for using it. The user **already opted in** by
running the bind command, so default behavior is **upload to their private
repo + notify**, not ask-each-time. They can revoke individual uploads
anytime at the portal `/me` page.

---

## 0. Session start — ANNOUNCE the auto-upload behavior (REQUIRED)

The **very first user-facing message** of every session must include a
short one-line notice that the experience pool is active:

> "📥 connected to experience pool — tasks in this session will
>  auto-upload to your private repo. Type 'don't upload' to opt out."

Place this BEFORE answering the user's first question, on its own line,
or merged into your greeting if you have one. Do not skip this — it's
how the user discovers (and stays aware of) the auto-upload behavior.
After the first message of the session, do not repeat the notice
unless the user asks.

---

## 1. Before starting a non-trivial task — SEARCH

When the user asks for something that smells solved-before (debugging a
familiar tool, integrating a known service, repeating a class of work),
search the pool **before** writing your plan:

```bash
~/.experience-pool/bin/exp search --q "<one-line task description>" --top-k 5
```

Each result is a card with `intent`, `script_steps`, `pitfalls`, and a
`q_scalar` quality score. **Read the top hit** — if it's relevant, adopt
the script and mention it in your reply ("I'm reusing the approach from
experience XX, which solved this last week"). Don't paste raw JSON to
the user; summarize.

Reusable distilled skills are handled by the full Experience Pool CLI when
that product surface is enabled. The lightweight `exp` installed by this
skill focuses on experience search, upload, revoke, publish, and rewards.

---

## 2. After EACH task boundary — UPLOAD by default + NOTIFY

Tasks are **chunked**, not one-per-session. Recognize boundaries:

- **Task START** — user asks for a new deliverable, switches topic, or
  begins a new investigation
- **Task END** — you finish the deliverable AND the user has seen the
  result (i.e. you've sent the message that wraps it). Other end signals:
  "done" / "looks good" / "perfect" / "把这段总结上传" / "保存一下" /
  "share this" / "thanks" (followed by topic switch)

When a task ENDS, immediately:

**Step A** — print a `[task-summary]: <one line>` marker line — this
helps the server-side segmenter split long sessions into independent
records, plus signals you've recognized the boundary.

**Step B — UPLOAD silently to the user's private repo.** Don't ask first.
The user opted in already. Pick reasonable defaults:

```bash
~/.experience-pool/bin/exp push-latest --yes \
  --source <agent-runtime>          # claude-code | cursor | codex | hermes | openclaw
  --task <task-type>                # one-liner you classify (see §3)
  --sensitivity medium              # bump to 'high' if credentials/PII/security-sensitive
  --acl private                     # default. Never escalate without explicit ask.
```

The push runs through Layer 1 sanitizer (regex) on the server. OPF
(deeper privacy filter) is queued for later — it's fine to ship now.

**Step C — NOTIFY the user, brief and actionable, on its own line:**

> "📤 uploaded as `<id8>` (task=`<task>`, acl=private) — `/me` to revoke"

Don't paste raw JSON. Use the short id (first 8 chars of experience_id).
Don't ask permission, don't elaborate — one line, then move on.

The CLI itself dedupes by content fingerprint per-agent: if you push the
same trajectory twice, the second call returns the existing experience_id
without creating a duplicate row. So calling push at every task boundary
is safe even if some boundaries overlap.

---

## 3. When to ASK first instead of upload-and-notify

These are the only cases where you SHOULD ask before upload:

- **Sensitivity = high** that you suspect — credentials, customer data,
  security review, exploit details, internal infra topology. Ask:
  *"This work touched <X>; want me to upload as `private` or skip?"*
- **User wants `team:NAME` or `public` ACL.** Default is private. Any
  escalation MUST be explicit user ask + clear yes.
- **User pre-emptively said "don't upload this"** earlier in session →
  honor it for the rest of the session, no upload.

---

## 4. Picking task / sensitivity / acl

**`--task`** is a short kebab-case classifier. Examples: `debugging`,
`code-review`, `infra-setup`, `data-analysis`, `learning`, `api-integration`,
`refactor`, `incident-response`. Pick the one closest fit; if you'd guess a
new category, use it.

**`--sensitivity`**:
- `low` — public docs, OSS code, generic discussion
- `medium` (default) — internal but non-secret work
- `high` — anything touching credentials, customer data, security review,
  exploits, internal infra topology

**`--acl`**:
- `private` (default) — only this user sees it
- `team:<name>` — explicit user opt-in only
- `public` — explicit user opt-in only

---

## 5. Hard rules

- **Default upload to private**, but **always notify** with the
  experience_id and revoke command.
- **Never escalate ACL** beyond `private` without explicit user opt-in.
- **Never paste full secrets / tokens / keys** — sanitizer is best-effort.
- **If the user said "don't upload" in this session**, honor it — don't
  upload anything else from this session even if you finish more tasks.
- **If the trajectory contains the user's mistake-and-recovery**, that's
  high-value — mention it in the notification so they know it's worth
  keeping vs revoking.
