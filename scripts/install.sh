#!/usr/bin/env bash
# experience-pool universal installer.
#
# Usage:
#     curl -sSL https://expool.clawsii.com/install | bash
#     curl -sSL https://expool.clawsii.com/install | EXP_AGENT_NAME=alice EXP_TEAM=platform bash
#
# Env:
#     EXP_BASE_URL       gateway URL (default https://expool.clawsii.com)
#     EXP_AGENT_NAME     agent identifier (default $USER-$(hostname -s))
#     EXP_TEAM           team for the agent (default "default")
#     EXP_INSTALL_DIR    where to place the uploader (default ~/.experience-pool)
#     EXP_SKIP_HOOK      "1" to skip Claude Code hook patch into ~/.claude/settings.json

set -eu

BASE="${EXP_BASE_URL:-https://expool.clawsii.com}"
NAME="${EXP_AGENT_NAME:-${USER:-agent}-$(hostname -s 2>/dev/null || hostname)}"
TEAM="${EXP_TEAM:-default}"
INSTALL_DIR="${EXP_INSTALL_DIR:-$HOME/.experience-pool}"
BIN_DIR="$INSTALL_DIR/bin"
UPLOADER="$BIN_DIR/exp_uploader.py"
WRAPPER="$BIN_DIR/exp"
HOOK_SCRIPT="$BIN_DIR/auto_upload.sh"

note() { printf '\033[36m[exp]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m[exp]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[31m[exp]\033[0m %s\n' "$*" >&2; exit 1; }

note "installing experience-pool client into $INSTALL_DIR"
note "gateway: $BASE  agent: $NAME  team: $TEAM"

# ---------- prerequisites ----------
command -v python3 >/dev/null 2>&1 || fail "python3 not found (need >=3.9)"
PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3,9)))') || PY_OK=0
[ "$PY_OK" = "1" ] || fail "python3 >= 3.9 required"
command -v curl >/dev/null 2>&1 || fail "curl not found"

mkdir -p "$BIN_DIR"
chmod 700 "$INSTALL_DIR"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd -P || echo "")"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd -P || echo "")"

# ---------- 1. fetch uploader + annotator + consent module ----------
note "[1/4] installing exp_uploader.py + exp_annotator.py + exp_consent.py"
TMP_UP="$(mktemp)"
TMP_ANN="$(mktemp)"
TMP_CON="$(mktemp)"
TMP_SS="$(mktemp)"
trap 'rm -f "$TMP_UP" "$TMP_ANN" "$TMP_CON" "$TMP_SS"' EXIT
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/exp_uploader.py" ]; then
    cp "$SCRIPT_DIR/exp_uploader.py" "$UPLOADER"
else
    curl -fsSL --max-time 30 "$BASE/exp_uploader.py" -o "$TMP_UP" \
        || fail "failed to download $BASE/exp_uploader.py"
    head -1 "$TMP_UP" | grep -q '^#!/usr/bin/env python3' \
        || fail "downloaded uploader doesn't look right"
    mv "$TMP_UP" "$UPLOADER"
fi
chmod 755 "$UPLOADER"

# Consent module — required for the opt-in/opt-out flow. Sits next to
# exp_uploader.py so it gets imported by relative path.
CONSENT="$BIN_DIR/exp_consent.py"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/exp_consent.py" ]; then
    cp "$SCRIPT_DIR/exp_consent.py" "$CONSENT"
    chmod 755 "$CONSENT"
    note "      consent module installed from local copy"
elif curl -fsSL --max-time 30 "$BASE/exp_consent.py" -o "$TMP_CON" 2>/dev/null \
     && head -1 "$TMP_CON" | grep -q '^#!/usr/bin/env python3'; then
    mv "$TMP_CON" "$CONSENT"
    chmod 755 "$CONSENT"
    note "      consent module installed"
else
    warn "      consent module not available; uploads will proceed without prompt"
fi

# Optional SessionStart hook for Claude Code — injects [task-summary]:
# convention and (when enabled) the prompt-on-start gate.
SS_HOOK="$BIN_DIR/session_start.sh"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/session_start.sh" ]; then
    cp "$SCRIPT_DIR/session_start.sh" "$SS_HOOK"
    chmod 755 "$SS_HOOK"
elif curl -fsSL --max-time 30 "$BASE/session_start.sh" -o "$TMP_SS" 2>/dev/null \
     && head -1 "$TMP_SS" | grep -q '^#!/usr/bin/env bash'; then
    mv "$TMP_SS" "$SS_HOOK"
    chmod 755 "$SS_HOOK"
fi

# The agent-contract markdown — single source of truth for "what should
# every agent do with the experience pool". Stashed locally so per-agent
# distribution (next section) can copy from one canonical place.
CONTRACT="$BIN_DIR/agent-contract.md"
TMP_CT="$(mktemp)"
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/agent-contract.md" ]; then
    cp "$REPO_ROOT/agent-contract.md" "$CONTRACT"
    chmod 644 "$CONTRACT"
elif [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/agent-contract.md" ]; then
    cp "$SCRIPT_DIR/agent-contract.md" "$CONTRACT"
    chmod 644 "$CONTRACT"
elif curl -fsSL --max-time 30 "$BASE/agent-contract.md" -o "$TMP_CT" 2>/dev/null \
     && [ -s "$TMP_CT" ]; then
    mv "$TMP_CT" "$CONTRACT"
    chmod 644 "$CONTRACT"
else
    rm -f "$TMP_CT"
    warn "      agent-contract.md not available; skill bundle will not be installed"
fi

# annotator is optional — if download fails, uploader still works without --annotate
ANNOTATOR="$BIN_DIR/exp_annotator.py"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/exp_annotator.py" ]; then
    cp "$SCRIPT_DIR/exp_annotator.py" "$ANNOTATOR"
    chmod 755 "$ANNOTATOR"
    note "      annotator installed from local copy"
elif curl -fsSL --max-time 30 "$BASE/exp_annotator.py" -o "$TMP_ANN" 2>/dev/null \
     && head -1 "$TMP_ANN" | grep -q '^#!/usr/bin/env python3'; then
    mv "$TMP_ANN" "$ANNOTATOR"
    chmod 755 "$ANNOTATOR"
    note "      annotator installed (run with: $WRAPPER push --annotate)"
else
    warn "      annotator not available; --annotate flag will be a no-op"
fi
trap - EXIT

# ---------- 2. shell wrapper ----------
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
exec python3 "$UPLOADER" --base "\${EXP_BASE_URL:-$BASE}" "\$@"
EOF
chmod 755 "$WRAPPER"

# ---------- 3. credential setup ----------
# Three paths, in priority order:
#   (a) pre-baked secret in env (EXP_AGENT_NAME + EXP_AGENT_SECRET) — written
#       to disk verbatim. This is the path the portal's "bind script" uses:
#       the user logged in at the portal, the portal returned the secret
#       inside the curl command, and we just need to drop it into place.
#   (b) credential file already exists — re-use as-is.
#   (c) fall back to interactive register (needs the server reachable).
CRED_DIR="$HOME/.experience-pool/credentials"
mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR"
CRED_FILE="$CRED_DIR/$NAME.json"
if [ -n "${EXP_AGENT_SECRET:-}" ]; then
    note "[2/4] using bind-supplied credential for $NAME (no register call)"
    AGENT_ID="${EXP_AGENT_ID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
    python3 - "$CRED_FILE" "$AGENT_ID" "$NAME" "$TEAM" "$EXP_AGENT_SECRET" <<'PY'
import json, os, stat, sys
path, agent_id, name, team, secret = sys.argv[1:]
data = {"agent_id": agent_id, "agent_name": name, "team": team, "secret": secret}
with open(path, "w") as f:
    json.dump(data, f, indent=2)
os.chmod(path, 0o600)
PY
    note "      credential written to $CRED_FILE"
elif [ -f "$CRED_FILE" ]; then
    note "[2/4] credential already exists at $CRED_FILE (skipping register)"
else
    note "[2/4] registering agent $NAME on team $TEAM"
    "$WRAPPER" register --name "$NAME" --team "$TEAM" >/dev/null \
        || fail "register failed; check $BASE is reachable"
    note "      credential saved to $CRED_FILE"
fi

# ---------- 2.5. optional: install OpenAI Privacy Filter (~3GB) ----------
# When EXP_INSTALL_OPF=1, install the opf package + download model weights.
# This adds context-aware PII redaction (8 categories) on top of the
# regex-only pass that runs by default. Server-side OPF still runs as a
# defense-in-depth layer — installing it client-side just means raw L1
# never even leaves the host.
if [ "${EXP_INSTALL_OPF:-0}" = "1" ]; then
    note "[2.5] installing OpenAI Privacy Filter client-side (~3GB download)"
    if ! command -v pip3 >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
        warn "      pip not available; skipping OPF install"
    else
        OPF_PIP="python3 -m pip install --user --quiet"
        if $OPF_PIP "git+https://github.com/openai/privacy-filter.git" 2>/dev/null; then
            note "      opf package installed"
            note "      downloading model weights to ~/.opf/privacy_filter (first run)"
            python3 - <<'PY' || warn "      model download failed; OPF will retry on first push"
import os, sys
try:
    from opf._api import RedactorAPI
    ckpt = os.path.expanduser("~/.opf/privacy_filter")
    RedactorAPI(checkpoint=ckpt, device="cpu")
    print("      OPF model ready at " + ckpt, file=sys.stderr)
except Exception as e:
    print("      OPF model load deferred: " + str(e), file=sys.stderr)
    sys.exit(1)
PY
        else
            warn "      pip install failed; OPF disabled (regex-only redaction will still apply)"
        fi
    fi
else
    note "[2.5] OPF client-side skipped (set EXP_INSTALL_OPF=1 to enable; server-side OPF still active)"
fi

# ---------- 4. detect agents on this host & wire hooks ----------
note "[3/4] detecting local agent installations"
DETECTED=()
[ -d "$HOME/.claude/projects" ] && DETECTED+=("claude-code")
[ -d "$HOME/Library/Application Support/Cursor/User" ] && DETECTED+=("cursor")
[ -d "$HOME/.config/Cursor/User" ] && DETECTED+=("cursor")
# Cursor 0.42+ also reads global rules from ~/.cursor/rules/ (no app
# install needed). Treat presence of either dir as "cursor seen".
[ -d "$HOME/.cursor" ] && DETECTED+=("cursor")
[ -d "$HOME/.codex/sessions" ] && DETECTED+=("codex")
[ -d "$HOME/.hermes" ]   && DETECTED+=("hermes")
[ -d "$HOME/.openclaw" ] && DETECTED+=("openclaw")
[ -d "$HOME/agents-chat" ] && DETECTED+=("agents-chat")
# de-dup
if [ "${#DETECTED[@]}" -gt 0 ]; then
    DETECTED=($(printf '%s\n' "${DETECTED[@]}" | awk '!seen[$0]++'))
fi

if [ "${#DETECTED[@]}" -eq 0 ]; then
    warn "      no local agent sessions found; you can still 'exp push-file --file traj.json'"
else
    note "      found: ${DETECTED[*]}"
fi

# ---------- 3.4. distribute the agent-contract per detected agent ---------
# Each agent runtime has a different "always-on rules" mechanism. We
# write the SAME canonical contract into each of those locations so the
# agent — whichever it is — picks the rules up automatically without
# needing the user to invoke a slash-command.
if [ -f "$CONTRACT" ]; then
    note "[3.6] installing agent-contract for: ${DETECTED[*]}"
    for agent in "${DETECTED[@]}"; do
        case "$agent" in
            claude-code)
                # Claude Code: drop a SKILL.md into ~/.claude/skills/<name>/.
                # Auto-loads when the agent decides the skill description
                # matches the current task — broad description = always.
                SKILL_DIR="$HOME/.claude/skills/experience-pool"
                mkdir -p "$SKILL_DIR"
                {
                    cat <<'FRONTMATTER'
---
name: experience-pool
description: Search the team's experience pool before starting solved-before tasks; upload finished work to the user's private pool by default and notify. Apply to debugging, coding, infra, learning, or anything that produces reusable steps.
version: 1.0.0
---

FRONTMATTER
                    cat "$CONTRACT"
                } > "$SKILL_DIR/SKILL.md"
                note "      claude-code → $SKILL_DIR/SKILL.md"
                ;;
            cursor)
                # Cursor: project-rules go in .cursor/rules/. We can only
                # touch the user's HOME, so write to ~/.cursor/rules/
                # which Cursor reads as global rules in newer versions.
                CURSOR_RULES_DIR="$HOME/.cursor/rules"
                mkdir -p "$CURSOR_RULES_DIR"
                cp "$CONTRACT" "$CURSOR_RULES_DIR/experience-pool.md"
                note "      cursor      → $CURSOR_RULES_DIR/experience-pool.md"
                ;;
            codex)
                # Codex (OpenAI): AGENTS.md at the project root or HOME is
                # picked up. We append (rather than overwrite) so we don't
                # nuke user customizations. Idempotent: only append if our
                # marker is missing.
                CODEX_AGENTS="$HOME/.codex/AGENTS.md"
                mkdir -p "$(dirname "$CODEX_AGENTS")"
                MARKER="<!-- experience-pool agent contract — managed by install.sh -->"
                if [ ! -f "$CODEX_AGENTS" ] || ! grep -qF "$MARKER" "$CODEX_AGENTS"; then
                    {
                        echo
                        echo "$MARKER"
                        cat "$CONTRACT"
                        echo
                        echo "<!-- end experience-pool -->"
                    } >> "$CODEX_AGENTS"
                    note "      codex       → $CODEX_AGENTS (appended)"
                else
                    note "      codex       → $CODEX_AGENTS (already present)"
                fi
                ;;
            hermes|openclaw)
                # hermes / openclaw: assume Claude-Code-style skill layout
                # at ~/.<runtime>/skills/<name>/SKILL.md (mirrors Claude
                # Code so the same SKILL.md format reuses cleanly). If the
                # actual runtime expects a different path, the operator
                # can `ln -sf` to the right place; the canonical contract
                # also lands at ~/.experience-pool/bin/agent-contract.md
                # for any other consumer to pull.
                SKILL_DIR="$HOME/.${agent}/skills/experience-pool"
                mkdir -p "$SKILL_DIR"
                {
                    cat <<FRONTMATTER_$agent
---
name: experience-pool
description: Search the team's experience pool before starting solved-before tasks; upload finished work to the user's private pool by default and notify. Apply to debugging, coding, infra, learning, or anything that produces reusable steps.
version: 1.0.0
---

FRONTMATTER_$agent
                    cat "$CONTRACT"
                } > "$SKILL_DIR/SKILL.md"
                # Belt-and-suspenders: also drop an AGENTS.md so runtimes
                # that read system-prompt files (rather than skills) still
                # pick the rules up.
                AGENTS_FILE="$HOME/.${agent}/AGENTS.md"
                MARKER="<!-- experience-pool agent contract — managed by install.sh -->"
                if [ ! -f "$AGENTS_FILE" ] || ! grep -qF "$MARKER" "$AGENTS_FILE"; then
                    {
                        echo
                        echo "$MARKER"
                        cat "$CONTRACT"
                        echo
                        echo "<!-- end experience-pool -->"
                    } >> "$AGENTS_FILE"
                fi
                note "      $agent     → $SKILL_DIR/SKILL.md + $AGENTS_FILE"
                ;;
            agents-chat)
                # No standard injection point yet; just drop the contract
                # locally so the operator can wire it.
                cp "$CONTRACT" "$INSTALL_DIR/agent-contract-${agent}.md"
                note "      $agent → $INSTALL_DIR/agent-contract-${agent}.md (manual wire-up)"
                ;;
        esac
    done
fi

# ---------- 3.5. consent wizard — interactive opt-in/opt-out ----------
# Skipped when EXP_NONINTERACTIVE=1 (CI/curl-pipe) or stdin is not a TTY.
# Non-interactive policy (this is the bind-command path): the user already
# opted in by pasting the curl one-liner from the portal, so we default
# mode=always for every detected adapter and acl=private. This means the
# subsequent bulk-upload step (and SessionEnd hook for future sessions)
# both push silently into the user's personal repo. Operators who want
# the older "ask each time" behavior can set EXP_CONSENT_DEFAULT=ask.
if [ "${EXP_NONINTERACTIVE:-0}" = "1" ] || [ ! -t 0 ]; then
    DEFAULT_MODE="${EXP_CONSENT_DEFAULT:-always}"
    note "[3.5] non-interactive bind: setting consent mode=$DEFAULT_MODE for all detected adapters"
    "$WRAPPER" consent reset >/dev/null 2>&1 || true
    "$WRAPPER" consent set --mode "$DEFAULT_MODE" \
        --reason "non-interactive bind" >/dev/null 2>&1 || true
    for agent in "${DETECTED[@]}"; do
        "$WRAPPER" consent set --agent "$agent" --mode "$DEFAULT_MODE" \
            --reason "non-interactive bind" >/dev/null 2>&1 \
            && note "      $agent → $DEFAULT_MODE"
    done
elif [ "${EXP_SKIP_CONSENT:-0}" = "1" ]; then
    note "[3.5] consent wizard skipped (EXP_SKIP_CONSENT=1)"
else
    note "[3.5] consent setup — choose what to share"
    cat <<'BANNER' >&2

experience-pool defaults to ASK before each session upload.
You can pre-set per-agent rules below. Modes:

  [a]lways  upload every session, no prompt
  [n]ever   never upload (sessions stay on this host)
  [k]ask    ask each time (recommended, default)
  [s]kip    inherit the global mode

BANNER
    for agent in "${DETECTED[@]}"; do
        # Default suggestion based on agent type — keep secrets-rich
        # sources (cursor, hermes) opt-in; chat-only (claude-code) ask.
        DEFAULT="k"
        case "$agent" in
            cursor|hermes) DEFAULT="k" ;;
        esac
        printf "  %s → [a/n/k/s] (default %s): " "$agent" "$DEFAULT" >&2
        read -r choice || choice=""
        choice="${choice:-$DEFAULT}"
        case "$choice" in
            a|always)  "$WRAPPER" consent set --agent "$agent" --mode always   >/dev/null && note "      $agent → always" ;;
            n|never)   "$WRAPPER" consent set --agent "$agent" --mode never    >/dev/null && note "      $agent → never" ;;
            k|ask)     "$WRAPPER" consent set --agent "$agent" --mode ask      >/dev/null && note "      $agent → ask" ;;
            s|skip)    note "      $agent → (inherit global)" ;;
            *)         "$WRAPPER" consent set --agent "$agent" --mode ask      >/dev/null && warn "      unknown choice; defaulting to ask" ;;
        esac
    done

    printf "\n  Exclude any directories from upload? (space-separated globs, blank to skip)\n  e.g. ~/work/clients/** ~/.aws/** > " >&2
    read -r EXCLUDES || EXCLUDES=""
    if [ -n "$EXCLUDES" ]; then
        # shellcheck disable=SC2086
        for glob in $EXCLUDES; do
            "$WRAPPER" consent set --cwd "$glob" --mode never \
                --reason "set during install wizard" >/dev/null \
                && note "      $glob → never"
        done
    fi
    printf "\n  [exp] consent saved to ~/.experience-pool/consent.json\n" >&2
fi

# Auto-upload helper script (SessionEnd hook for Claude Code).
# No-op by default under Plan B: hook-driven interactive prompts don't
# work in Claude Code (hook subprocess has no TTY), so silent uploads
# require an explicit opt-in. To enable:
#   1. export EXP_AUTO_UPLOAD=1   (in ~/.claude/settings.json env block)
#   2. exp consent set --mode always
# With both set, SessionEnd will fire once per session and push-latest
# will upload the full trajectory. Errors land in upload.log instead
# of being silently swallowed.
cat > "$HOOK_SCRIPT" <<EOF
#!/usr/bin/env bash
# Claude Code SessionEnd hook — no-op by default under Plan B.
#
# Stop / SessionEnd hooks run non-interactively, so they cannot prompt
# the user for upload consent. Uploads are user-triggered: the agent
# asks at end of session via the SessionStart-injected convention and
# runs \`exp push-latest\` only with explicit consent. This script stays
# in settings.json so the wiring is intact; it exits cleanly unless an
# escape hatch is set.
#
# Escape hatch (for users who genuinely want silent auto-upload):
#   export EXP_AUTO_UPLOAD=1
#   exp consent set --mode always
# Then this hook defers to the consent decision and uploads silently.

set -eu

LOG_DIR="\${HOME}/.experience-pool/logs"
mkdir -p "\$LOG_DIR" 2>/dev/null || true
LOG="\$LOG_DIR/upload.log"

if [ "\${EXP_AUTO_UPLOAD:-0}" != "1" ]; then
    exit 0
fi

SID="\${CLAUDE_SESSION_ID:-}"
CWD="\${CLAUDE_PROJECT_DIR:-\$PWD}"

DECISION=\$("$WRAPPER" consent decide \\
              --agent claude-code --cwd "\$CWD" --session "\$SID" \\
              2>>"\$LOG" || echo skip)

if [ "\$DECISION" = "upload" ]; then
    "$WRAPPER" push-latest --yes \\
        --source claude-code \\
        --task "\${EXP_TASK:-misc}" \\
        --sensitivity "\${EXP_SENSITIVITY:-medium}" \\
        --acl "\${EXP_ACL:-private}" \\
        >>"\$LOG" 2>&1 || true
fi

exit 0
EOF
chmod 755 "$HOOK_SCRIPT"

# Patch ~/.claude/settings.json (only if Claude Code is detected and not skipped).
# Aggressive cleanup: every prior Stop/SessionStart/SessionEnd entry that
# references ANY experience-pool script gets removed before we wire the
# canonical pair. This guarantees no duplicate triggers across re-runs of
# the bind command (a complaint observed when install was re-run on a
# machine that already had the previous flavor of hook installed).
if [ "${EXP_SKIP_HOOK:-0}" != "1" ] && [ -d "$HOME/.claude" ]; then
    SETTINGS="$HOME/.claude/settings.json"
    note "[4/5] rewiring Claude Code hooks in $SETTINGS (auto-upload to user's repo)"
    python3 - "$SETTINGS" "$HOOK_SCRIPT" "${SS_HOOK:-}" "$INSTALL_DIR" <<'PY'
import json, sys, pathlib
settings_path = pathlib.Path(sys.argv[1])
end_cmd = sys.argv[2]
start_cmd = sys.argv[3] or ""
exp_root = sys.argv[4]
if settings_path.exists():
    try:
        data = json.loads(settings_path.read_text())
    except Exception:
        backup = settings_path.with_suffix(".json.bak")
        backup.write_text(settings_path.read_text())
        print(f"[exp] existing settings.json was unreadable; backed up to {backup}",
              file=sys.stderr)
        data = {}
else:
    data = {}

# Make sure SessionEnd hook actually does something: stamp the env block
# so EXP_AUTO_UPLOAD=1 is present when the hook subprocess runs.
env = data.setdefault("env", {})
env["EXP_AUTO_UPLOAD"] = "1"

hooks = data.setdefault("hooks", {})

def references_us(entry) -> bool:
    """Does this hook entry point at any experience-pool script?
    Match by string-containment so paths under different home dirs / old
    layouts all get caught."""
    if not isinstance(entry, dict):
        return False
    candidates = []
    if isinstance(entry.get("command"), str):
        candidates.append(entry["command"])
    for inner in entry.get("hooks", []) or []:
        if isinstance(inner, dict) and isinstance(inner.get("command"), str):
            candidates.append(inner["command"])
    needles = (".experience-pool/", "/experience-pool/bin/",
               "auto_upload.sh", "auto-upload.sh",
               "session_start.sh", exp_root.rstrip("/") + "/")
    return any(any(n in c for n in needles) for c in candidates)

# 1. Strip every prior reference across ALL hook events (Stop, SessionStart,
#    SessionEnd, SubagentStop, ...). Belt-and-suspenders.
for event in list(hooks.keys()):
    cleaned = [e for e in hooks[event] if not references_us(e)]
    if cleaned:
        hooks[event] = cleaned
    else:
        del hooks[event]

# 2. Add canonical SessionEnd hook (single upload per session).
hooks.setdefault("SessionEnd", []).append({
    "matcher": "clear|logout|prompt_input_exit|bypass_permissions_disabled|other",
    "hooks": [{"type": "command", "command": end_cmd}],
})

# 3. Add canonical SessionStart hook (inject [task-summary] convention +
#    bind-manifest hint when present).
if start_cmd:
    hooks.setdefault("SessionStart", []).append({
        "matcher": "",
        "hooks": [{"type": "command", "command": start_cmd}],
    })

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(data, indent=2))
print(f"[exp] hooks rewired in {settings_path}: "
      f"SessionEnd=1, SessionStart={1 if start_cmd else 0}, EXP_AUTO_UPLOAD=1",
      file=sys.stderr)
PY
else
    note "[4/5] skipping Claude Code SessionEnd hook (EXP_SKIP_HOOK=1 or ~/.claude not present)"
fi

# ---------- 5. universal background daemon (covers all OTHER agents) ----------
TICK_INTERVAL="${EXP_TICK_INTERVAL_SECONDS:-120}"
AUTO_SOURCES="${EXP_AUTO_SOURCES:-claude-code,hermes,continue-dev,codex,agents-chat}"
AUTO_ACL="${EXP_AUTO_ACL:-private}"

if [ "${EXP_SKIP_DAEMON:-0}" = "1" ]; then
    note "[5/5] skipping background daemon (EXP_SKIP_DAEMON=1)"
elif [ "$(uname -s)" = "Darwin" ]; then
    note "[5/5] installing launchd LaunchAgent (every ${TICK_INTERVAL}s)"
    PLIST="$HOME/Library/LaunchAgents/com.experience-pool.daemon.plist"
    LOG_DIR="$HOME/Library/Logs/experience-pool"
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.experience-pool.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>$WRAPPER</string>
        <string>daemon-tick</string>
        <string>--max-per-source</string><string>5</string>
        <string>--acl</string><string>$AUTO_ACL</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>EXP_BASE_URL</key><string>$BASE</string>
        <key>EXP_AUTO_SOURCES</key><string>$AUTO_SOURCES</string>
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>StartInterval</key><integer>$TICK_INTERVAL</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/daemon.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/daemon.err</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/com.experience-pool.daemon" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
        || launchctl load -w "$PLIST" 2>/dev/null \
        || warn "      launchctl bootstrap failed; you can load manually with: launchctl load -w $PLIST"
    note "      log: $LOG_DIR/daemon.log"
elif [ "$(uname -s)" = "Linux" ]; then
    note "[5/5] installing systemd user timer (every ${TICK_INTERVAL}s)"
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/expool-daemon.service" <<EOF
[Unit]
Description=Experience Pool background sync

[Service]
Type=oneshot
Environment=EXP_BASE_URL=$BASE
Environment=EXP_AUTO_SOURCES=$AUTO_SOURCES
ExecStart=$WRAPPER daemon-tick --max-per-source 5 --acl $AUTO_ACL
EOF
    cat > "$UNIT_DIR/expool-daemon.timer" <<EOF
[Unit]
Description=Run experience-pool sync every ${TICK_INTERVAL}s

[Timer]
OnBootSec=30
OnUnitActiveSec=${TICK_INTERVAL}
Unit=expool-daemon.service

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload 2>/dev/null \
        && systemctl --user enable --now expool-daemon.timer 2>/dev/null \
        || warn "      systemctl --user not available; enable manually: systemctl --user enable --now expool-daemon.timer"
else
    warn "[5/5] no scheduler for $(uname -s); install a cron entry manually:"
    warn "      */2 * * * * EXP_AUTO_SOURCES=$AUTO_SOURCES $WRAPPER daemon-tick --acl $AUTO_ACL"
fi

# ---------- 6. bulk upload of past sessions ---------------------------------
# By default the bind command means "claim everything you find on this
# machine for me, into my private repo". So we run a full daemon-tick
# (no per-source cap) with acl=private and write a manifest the user
# can revoke from individually if they decide some shouldn't be there.
MANIFEST="$INSTALL_DIR/last-bind-manifest.json"
BACKFILL_LOG="$INSTALL_DIR/logs/backfill.log"
mkdir -p "$INSTALL_DIR/logs"
RUNNER="$INSTALL_DIR/run-backfill.sh"

# Write the backfill runner (used both for opt-in immediate run and for
# manual `bash $RUNNER` later). It does the daemon-reset + tick itself.
cat > "$RUNNER" <<RUNNER_EOF
#!/usr/bin/env bash
# Backfill all past local sessions to the user's private repo.
# Runs in the background so the FastAPI single-thread isn't blocked.
echo "[\$(date -u +%FT%TZ)] backfill started" >>"$BACKFILL_LOG"
"$WRAPPER" daemon-reset >/dev/null 2>&1 || true
"$WRAPPER" daemon-tick \\
    --max-per-source 9999 \\
    --max-session-kb 32768 \\
    --acl private \\
    -v >"$MANIFEST.tmp" 2>>"$BACKFILL_LOG"
mv "$MANIFEST.tmp" "$MANIFEST"
echo "[\$(date -u +%FT%TZ)] backfill done; manifest at $MANIFEST" >>"$BACKFILL_LOG"
RUNNER_EOF
chmod +x "$RUNNER"

if [ "${EXP_BACKFILL:-0}" = "1" ]; then
    note "[6/6] kicking off background bulk-upload of past sessions (EXP_BACKFILL=1)"
    setsid nohup "$RUNNER" </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
    note "      backfill log : $BACKFILL_LOG"
    note "      manifest      : $MANIFEST  (written when backfill finishes)"
else
    note "[6/6] backfill of past sessions: NOT auto-run (EXP_BACKFILL=0)"
    note "      future sessions auto-upload via SessionEnd hook with no friction."
    note "      to backfill past sessions later (offline, won't block UI), run:"
    note "        bash $RUNNER &"
    note "        tail -f $BACKFILL_LOG"
fi
note "      progress check: $WRAPPER daemon-state"
note "      to revoke later: visit portal /me, or $WRAPPER consent revoke --eid <id>"

if [ "${EXP_BACKFILL:-0}" = "1" ]; then
    BACKFILL_LINE="past sessions backfilling in background, future sessions will auto-upload."
else
    BACKFILL_LINE="future sessions will auto-upload via SessionEnd hook (past sessions NOT backfilled)."
fi
cat <<EOF

✓ bind complete — $BACKFILL_LINE
  · auto-upload sources : $AUTO_SOURCES
  · default ACL          : private (your repo only — visible to nobody else)
  · SessionEnd hook      : $HOOK_SCRIPT
  · agent-contract       : $CONTRACT
  · manifest of backfill : $MANIFEST

  binary  : $WRAPPER
  uploader: $UPLOADER
  cred    : $CRED_FILE
  log     : ~/Library/Logs/experience-pool/daemon.log  (macOS)

control commands:
  $WRAPPER daemon-state                    # see what's been synced
  $WRAPPER daemon-reset --source claude-code   # force re-upload of one source
  $WRAPPER daemon-tick --dry-run -v        # preview without uploading

disable:
  launchctl bootout gui/\$(id -u)/com.experience-pool.daemon         # macOS
  systemctl --user disable --now expool-daemon.timer                 # Linux

tweak which agents auto-sync:
  EXP_AUTO_SOURCES=claude-code,hermes  curl -sSL $BASE/install | bash

EOF
