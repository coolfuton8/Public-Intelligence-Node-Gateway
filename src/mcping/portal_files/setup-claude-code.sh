#!/usr/bin/env bash
# MCPing: connect Claude Code on this computer to the device at @@ADDRESS@@
#
#   bash setup-claude-code.sh
#
# It saves the device certificate authority (CA), tells Claude Code to trust it and registers
# the MCP server. It asks for the device token (on the device: MCPing Connect > Copy token only).
set -euo pipefail

ADDRESS="${MCPING_ADDRESS:-@@ADDRESS@@}"
NAME="${MCPING_NAME:-mcping}"
URL="https://${ADDRESS}:@@PORT@@/mcp"
EXPECTED="@@FINGERPRINT@@"
TOKEN="${MCPING_TOKEN:-}"
if [ -z "$TOKEN" ]; then TOKEN='@@TOKEN@@'; fi   # filled in only for a script obtained with a one-time invite

CLAUDE="$(command -v claude || true)"
if [ -z "$CLAUDE" ]; then
  for p in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /usr/local/bin/claude "$HOME/.npm-global/bin/claude"; do
    [ -x "$p" ] && { CLAUDE="$p"; break; }
  done
fi
if [ -z "$TOKEN" ]; then
  read -rsp 'Device token (on the device: MCPing Connect > Copy token only): ' TOKEN; echo
fi
[ "${#TOKEN}" -ge 24 ] || { echo 'That does not look like a device token (too short).' >&2; exit 1; }

DIR="$HOME/.mcping"; CA="$DIR/mcping-ca.crt"
mkdir -p "$DIR"
cat > "$CA" <<'MCPING_CA'
@@CA_PEM@@
MCPING_CA

if command -v openssl >/dev/null; then
  GOT="$(openssl x509 -in "$CA" -outform DER | openssl dgst -sha256 | awk '{print $NF}')"
  [ "$GOT" = "$EXPECTED" ] || { echo "Certificate fingerprint mismatch ($GOT). Download the script again." >&2; exit 1; }
  echo "Device CA saved to $CA"
  echo "  SHA-256 fingerprint: $GOT"
  echo "  Compare it with the one shown on the device (MCPing Certificate app) if this network is not yours."
else
  echo "Device CA saved to $CA (openssl not found, fingerprint not checked)"
fi

export NODE_EXTRA_CA_CERTS="$CA"
LINE="export NODE_EXTRA_CA_CERTS=\"$CA\""
for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
  [ -f "$rc" ] && ! grep -q NODE_EXTRA_CA_CERTS "$rc" && echo "$LINE" >> "$rc"
done

# The Claude desktop app, if it is installed here: it must see the certificate setting even when started
# from the menu (which does not read .bashrc), and its connectors list only loads local (stdio) servers,
# so a tiny bridge to the device is installed and added to the app's own config.
if [ "$(uname -s)" = Linux ]; then
  APPCFG="${XDG_CONFIG_HOME:-$HOME/.config}/Claude"
  mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d"
  echo "NODE_EXTRA_CA_CERTS=$CA" > "${XDG_CONFIG_HOME:-$HOME/.config}/environment.d/mcping.conf"
else
  APPCFG="$HOME/Library/Application Support/Claude"
fi
if [ -d "$APPCFG" ] && command -v python3 >/dev/null; then
  echo '@@BRIDGE_B64@@' | base64 -d > "$DIR/mcping-bridge.py" && chmod 755 "$DIR/mcping-bridge.py"
  if python3 - "$APPCFG/claude_desktop_config.json" "$NAME" "$URL" "$TOKEN" "$DIR/mcping-bridge.py" "$CA" <<'PY'
import json, os, shutil, sys
path, name, url, token, bridge, ca = sys.argv[1:7]
cfg = {}
if os.path.exists(path):
    shutil.copy(path, path + ".mcping-backup")
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
cfg.setdefault("mcpServers", {})[name] = {"command": "python3", "args": [bridge],
    "env": {"MCPING_URL": url, "MCPING_TOKEN": token, "MCPING_CA": ca}}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
os.chmod(path, 0o600)
PY
  then
    DESKTOP_DONE=1
  fi
fi
desktop_note() {
  [ "${DESKTOP_DONE:-}" = 1 ] || return 0
  echo
  echo 'The Claude desktop app was set up too (it will list "mcping" under its connectors).'
  echo 'IMPORTANT: quit the Claude app completely and open it again, or it will not see mcping.'
}

if [ -z "$CLAUDE" ]; then
  FILE="$DIR/mcping.mcp.json"
  printf '{"mcpServers":{"%s":{"type":"http","url":"%s","headers":{"Authorization":"Bearer %s"}}}}\n' \
    "$NAME" "$URL" "$TOKEN" > "$FILE"
  chmod 600 "$FILE"
  echo
  # Only the desktop app is installed: add the entry to the user settings it reads (~/.claude.json).
  if command -v python3 >/dev/null && python3 - "$HOME/.claude.json" "$NAME" "$URL" "$TOKEN" <<'PY'
import json, os, shutil, sys
path, name, url, token = sys.argv[1:5]
cfg = {}
if os.path.exists(path):
    shutil.copy(path, path + ".mcping-backup")
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
cfg.setdefault("mcpServers", {})[name] = {"type": "http", "url": url, "headers": {"Authorization": "Bearer " + token}}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
PY
  then
    echo 'SUCCESS: this computer is set up for MCPing.'
    echo
    echo 'What is left: quit the Claude app completely and open it again. Then ask Claude to take a'
    echo 'screenshot with the mcping tools.'
    echo
    echo "(Details: the mcping server was added to $HOME/.claude.json; a backup of the old file is next to it.)"
    desktop_note
  else
    echo 'The "claude" command was not found and the settings file could not be updated.'
    echo 'The device certificate is saved and trusted. To finish, either install the Claude Code CLI'
    echo "(https://docs.claude.com/claude-code) and run this again, or copy $FILE to the folder you open"
    echo "in Claude as '.mcp.json' (it contains your access token)."
  fi
  exit 0
fi
"$CLAUDE" mcp remove --scope user "$NAME" >/dev/null 2>&1 || true
"$CLAUDE" mcp add --transport http --scope user "$NAME" "$URL" --header "Authorization: Bearer $TOKEN"
echo
"$CLAUDE" mcp list
echo
echo 'Done. Open a new terminal (or restart Claude Code), then ask it to take a screenshot with the mcping tools.'
desktop_note
