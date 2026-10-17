#!/usr/bin/env bash
# MCPing: connect OpenCode on this computer to the device at @@ADDRESS@@ (and optionally Ollama)
#
#   bash setup-opencode.sh [--model qwen3-mcping] [--no-ollama]
#
# It saves the device certificate authority (CA), trusts it for OpenCode, stores the token in
# your shell profile as MCPING_TOKEN and merges the MCP server (and, unless --no-ollama, an Ollama
# provider) into OpenCode's config. An existing config is backed up first. Needs python3.
set -euo pipefail

ADDRESS="${MCPING_ADDRESS:-@@ADDRESS@@}"
URL="https://${ADDRESS}:@@PORT@@/mcp"
EXPECTED="@@FINGERPRINT@@"
TOKEN="${MCPING_TOKEN:-}"
if [ -z "$TOKEN" ]; then TOKEN='@@TOKEN@@'; fi   # filled in only for a script obtained with a one-time invite
MODEL="qwen3-mcping"
OLLAMA_URL="http://localhost:11434/v1"
OLLAMA=1
CONFIG="${OPENCODE_CONFIG_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json}"
NO_ENV="${MCPING_NO_ENV:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="$2"; shift 2;;
    --no-ollama) OLLAMA=0; shift;;
    --config) CONFIG="$2"; shift 2;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done

if [ -z "$TOKEN" ]; then
  read -rsp 'Device token (on the device: MCPing Connect > Copy token only): ' TOKEN; echo
fi
[ "${#TOKEN}" -ge 24 ] || { echo 'That does not look like a device token (too short).' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'python3 is needed to merge the OpenCode config.' >&2; exit 1; }

DIR="$HOME/.mcping"; CA="$DIR/mcping-ca.crt"
mkdir -p "$DIR"
cat > "$CA" <<'MCPING_CA'
@@CA_PEM@@
MCPING_CA
if command -v openssl >/dev/null; then
  GOT="$(openssl x509 -in "$CA" -outform DER | openssl dgst -sha256 | awk '{print $NF}')"
  [ "$GOT" = "$EXPECTED" ] || { echo "Certificate fingerprint mismatch ($GOT). Download the script again." >&2; exit 1; }
  echo "Device CA saved to $CA  (SHA-256 $GOT)"
fi

if [ -z "$NO_ENV" ]; then
  for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
    [ -f "$rc" ] || continue
    grep -q NODE_EXTRA_CA_CERTS "$rc" || echo "export NODE_EXTRA_CA_CERTS=\"$CA\"" >> "$rc"
    grep -q MCPING_TOKEN "$rc" || echo "export MCPING_TOKEN=\"$TOKEN\"" >> "$rc"
  done
  echo "Added NODE_EXTRA_CA_CERTS and MCPING_TOKEN to your shell profile (new terminals only)."
fi

mkdir -p "$(dirname "$CONFIG")"
[ -f "$CONFIG" ] && cp "$CONFIG" "$CONFIG.bak"
CONFIG="$CONFIG" URL="$URL" MODEL="$MODEL" OLLAMA="$OLLAMA" OLLAMA_URL="$OLLAMA_URL" python3 - <<'PY'
import json, os
path = os.environ["CONFIG"]
try:
    cfg = json.load(open(path)) or {}
except (OSError, ValueError):
    cfg = {}
cfg.setdefault("$schema", "https://opencode.ai/config.json")
cfg.setdefault("mcp", {})["mcping"] = {
    "type": "remote", "url": os.environ["URL"], "enabled": True,
    "headers": {"Authorization": "Bearer {env:MCPING_TOKEN}"},
}
if os.environ["OLLAMA"] == "1":
    model = os.environ["MODEL"]
    cfg.setdefault("provider", {}).setdefault("ollama", {
        "npm": "@ai-sdk/openai-compatible", "name": "Ollama (local)",
        "options": {"baseURL": os.environ["OLLAMA_URL"]},
        "models": {model: {"name": model}},
    })
    cfg.setdefault("model", f"ollama/{model}")
json.dump(cfg, open(path, "w"), indent=2)
PY
echo "OpenCode config updated: $CONFIG"
echo
echo 'Next: open a NEW terminal, start Ollama, run: opencode'
