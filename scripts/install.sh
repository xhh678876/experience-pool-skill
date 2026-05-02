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
#     EXP_SKIP_HOOK      "1" to skip Stop hook patch into ~/.claude/settings.json

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

# ---------- 1. install uploader + annotator ----------
# If this installer is sitting next to the python files (i.e. you git-cloned
# the skill repo into ~/.claude/skills/experience-pool/), use those directly.
# Otherwise download from the gateway.
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd -P || echo "")"
ANNOTATOR="$BIN_DIR/exp_annotator.py"

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/exp_uploader.py" ]; then
    note "[1/5] copying local exp_uploader.py from $SCRIPT_DIR"
    cp "$SCRIPT_DIR/exp_uploader.py" "$UPLOADER"
else
    note "[1/5] downloading exp_uploader.py from $BASE"
    TMP_UP="$(mktemp)"
    trap 'rm -f "$TMP_UP"' EXIT
    curl -fsSL --max-time 30 "$BASE/exp_uploader.py" -o "$TMP_UP" \
        || fail "failed to download $BASE/exp_uploader.py"
    head -1 "$TMP_UP" | grep -q '^#!/usr/bin/env python3' \
        || fail "downloaded uploader doesn't look right"
    mv "$TMP_UP" "$UPLOADER"
    trap - EXIT
fi
chmod 755 "$UPLOADER"

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/exp_annotator.py" ]; then
    cp "$SCRIPT_DIR/exp_annotator.py" "$ANNOTATOR"
    chmod 755 "$ANNOTATOR"
    note "      annotator installed from local copy"
else
    TMP_ANN="$(mktemp)"
    if curl -fsSL --max-time 30 "$BASE/exp_annotator.py" -o "$TMP_ANN" 2>/dev/null \
       && head -1 "$TMP_ANN" | grep -q '^#!/usr/bin/env python3'; then
        mv "$TMP_ANN" "$ANNOTATOR"
        chmod 755 "$ANNOTATOR"
        note "      annotator installed (use: $WRAPPER push --annotate)"
    else
        rm -f "$TMP_ANN"
        warn "      annotator not available; --annotate flag will be a no-op"
    fi
fi

# ---------- 2. shell wrapper ----------
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
exec python3 "$UPLOADER" --base "\${EXP_BASE_URL:-$BASE}" "\$@"
EOF
chmod 755 "$WRAPPER"

# ---------- 3. register agent (idempotent) ----------
CRED_DIR="$HOME/.experience-pool/credentials"
mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR"
CRED_FILE="$CRED_DIR/$NAME.json"
if [ -f "$CRED_FILE" ]; then
    note "[2/4] credential already exists at $CRED_FILE (skipping register)"
else
    note "[2/4] registering agent $NAME on team $TEAM"
    "$WRAPPER" register --name "$NAME" --team "$TEAM" >/dev/null \
        || fail "register failed; check $BASE is reachable"
    note "      credential saved to $CRED_FILE"
fi

# ---------- 4. detect agents on this host & wire hooks ----------
note "[3/4] detecting local agent installations"
DETECTED=()
[ -d "$HOME/.claude/projects" ] && DETECTED+=("claude-code")
[ -d "$HOME/Library/Application Support/Cursor/User" ] && DETECTED+=("cursor")
[ -d "$HOME/.config/Cursor/User" ] && DETECTED+=("cursor")
[ -d "$HOME/.codex/sessions" ] && DETECTED+=("codex")

if [ "${#DETECTED[@]}" -eq 0 ]; then
    warn "      no local agent sessions found; you can still 'exp push-file --file traj.json'"
else
    note "      found: ${DETECTED[*]}"
fi

# Auto-upload helper script (Stop hook for Claude Code)
cat > "$HOOK_SCRIPT" <<EOF
#!/usr/bin/env bash
# Claude Code Stop hook — uploads the just-finished session.
# CLAUDE_SESSION_PATH is set by the harness; falls back to the latest session.
set -eu
"$WRAPPER" push-latest \\
    --source claude-code \\
    --task "\${EXP_TASK:-misc}" \\
    --sensitivity "\${EXP_SENSITIVITY:-medium}" \\
    --acl "\${EXP_ACL:-private}" >/dev/null 2>&1 || true
EOF
chmod 755 "$HOOK_SCRIPT"

# SessionStart hook script — copy/install the system-context injection that
# tells the agent to self-label with [task-summary]: at session end.
SESSION_START_SCRIPT="$BIN_DIR/session_start.sh"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/session_start.sh" ]; then
    cp "$SCRIPT_DIR/session_start.sh" "$SESSION_START_SCRIPT"
else
    # Fetch from gateway if not bundled locally.
    curl -fsSL --max-time 30 "$BASE/session_start.sh" -o "$SESSION_START_SCRIPT" 2>/dev/null \
        || cat > "$SESSION_START_SCRIPT" <<'INLINE'
#!/usr/bin/env bash
echo "[experience-pool] When you finish a task, append this line to your final response: [task-summary]: <one-line action-oriented label>. The experience-pool uploader uses it as the intent field. Costs zero extra inference."
INLINE
fi
chmod 755 "$SESSION_START_SCRIPT"

# Patch ~/.claude/settings.json (only if Claude Code is detected and not skipped).
if [ "${EXP_SKIP_HOOK:-0}" != "1" ] && [ -d "$HOME/.claude" ]; then
    SETTINGS="$HOME/.claude/settings.json"
    note "[4/5] patching Stop + SessionStart hooks into $SETTINGS"
    python3 - "$SETTINGS" "$HOOK_SCRIPT" "$SESSION_START_SCRIPT" <<'PY'
import json, sys, pathlib, os
settings_path = pathlib.Path(sys.argv[1])
stop_cmd = sys.argv[2]
start_cmd = sys.argv[3]
if settings_path.exists():
    try:
        data = json.loads(settings_path.read_text())
    except Exception:
        backup = settings_path.with_suffix(".json.bak")
        backup.write_text(settings_path.read_text())
        print(f"[exp] existing settings.json was unreadable; backed up to {backup}", file=sys.stderr)
        data = {}
else:
    data = {}
hooks = data.setdefault("hooks", {})

# Stop hook — auto-upload finished session
stops = hooks.setdefault("Stop", [])
if not any(isinstance(e, dict) and e.get("command") == stop_cmd for e in stops):
    stops.append({"command": stop_cmd, "description": "experience-pool auto upload"})
    print(f"[exp] Stop hook installed", file=sys.stderr)

# SessionStart hook — inject [task-summary] convention into agent's context
starts = hooks.setdefault("SessionStart", [])
if not any(isinstance(e, dict) and e.get("command") == start_cmd for e in starts):
    starts.append({"command": start_cmd, "description": "experience-pool task-summary convention"})
    print(f"[exp] SessionStart hook installed (zero-cost intent labeling)", file=sys.stderr)

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(data, indent=2))
PY
else
    note "[4/5] skipping Claude Code hooks (EXP_SKIP_HOOK=1 or ~/.claude not present)"
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

# Run one tick immediately so the user sees output and any backlog gets uploaded.
note "running first daemon tick now..."
"$WRAPPER" daemon-tick --max-per-source 3 --acl "$AUTO_ACL" 2>&1 | sed 's/^/      /' || true

cat <<EOF

✓ installed — auto-upload is now ON for: $AUTO_SOURCES
  every session you finish will be uploaded within ${TICK_INTERVAL}s
  (Claude Code uploads instantly via Stop hook).

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
