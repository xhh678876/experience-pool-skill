#!/usr/bin/env bash
# session-extractor — wrapper that downloads + runs the extractor in one shot.
#
# Usage (paste into any terminal where Claude/Codex sessions exist):
#   curl -fsSL <BASE>/session-extractor/run.sh | bash
#
# Required env (the /me page bakes them into the copy-paste command):
#   EXP_AGENT_NAME    your agent name (e.g., user-xhh666)
#   EXP_AGENT_SECRET  your HMAC secret
#   EXP_BASE_URL      experience-pool API base (e.g., http://10.244.66.195:3080)
#
# Optional:
#   EXP_EXTRACTOR_DIR  where to write the extractor (default: ~/.experience-pool/extractor)
#   EXP_EXTRACTOR_FLAGS extra flags for extract_and_upload.py (e.g., "--limit 50 --dry-run")

set -eu

: "${EXP_AGENT_NAME:?set EXP_AGENT_NAME — see /me on the portal}"
: "${EXP_AGENT_SECRET:?set EXP_AGENT_SECRET — see /me on the portal}"
: "${EXP_BASE_URL:?set EXP_BASE_URL — see /me on the portal}"

DIR="${EXP_EXTRACTOR_DIR:-$HOME/.experience-pool/extractor}"
mkdir -p "$DIR" && cd "$DIR"

echo "[extractor] downloading extract_and_upload.py from $EXP_BASE_URL"
curl -fsSL "$EXP_BASE_URL/session-extractor/extract_and_upload.py" -o extract_and_upload.py
chmod +x extract_and_upload.py

# We use only stdlib — no pip needed.
python3 -V >/dev/null 2>&1 || { echo "python3 not found"; exit 1; }

echo "[extractor] running (acl=private hardcoded — uploads visible to nobody but you)"
exec python3 extract_and_upload.py ${EXP_EXTRACTOR_FLAGS:-}
