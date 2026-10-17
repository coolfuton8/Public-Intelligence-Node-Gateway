#!/usr/bin/env bash
# MCPing device installer for Linux (Ubuntu 24.04, Linux Mint 22 and close relatives).
#
# NON-PRODUCTION. FOR TESTING ONLY. NO WARRANTY. AI-generated and not yet verified on a clean machine.
# By running this script you accept the terms in DISCLAIMER.md.
#
# What it does, in order (it asks before each group of changes and shows every default):
#   1. checks the computer (OS, memory, disk, network, ports, systemd, the source tree)
#   2. installs every dependency, then CHECKS that every one is present before touching the project
#   3. creates the service user, copies the project, builds its Python environment
#   4. creates the access token, the certificate folder and the services, and starts them
#
# Usage:  bash install/install-device.sh [options]
#   --check               only check that the computer is ready; change nothing
#   --dry-run             show what would be done; change nothing
#   --yes                 accept every default without asking (still needs --accept-disclaimer)
#   --accept-disclaimer   state that you have read and accept DISCLAIMER.md
#   --user NAME           service user (default: mcping)
#   --dir PATH            install folder (default: /home/<user>/mcping)
#   --help                this text
#
# Everything is logged to a file whose path is printed at the start. The token is never written to the log.

set -Eeuo pipefail

# ----------------------------------------------------------------------------------------------- setup
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG="${TMPDIR:-/tmp}/mcping-install-$(date +%Y%m%d-%H%M%S).log"
: > "$LOG"; chmod 600 "$LOG"

CHECK_ONLY=0; DRY=0; ASSUME_YES=0; ACCEPTED=0
SVC_USER="mcping"; DEST=""
FAILS=0; WARNS=0

if [ -t 1 ]; then B=$'\e[1m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; N=$'\e[0m'; else B=""; R=""; G=""; Y=""; N=""; fi

say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
step() { printf '\n%s== %s ==%s\n' "$B" "$*" "$N" | tee -a "$LOG"; }
ok()   { printf '  %s[ OK ]%s %s\n' "$G" "$N" "$*" | tee -a "$LOG"; }
warn() { WARNS=$((WARNS+1)); printf '  %s[WARN]%s %s\n' "$Y" "$N" "$*" | tee -a "$LOG"; }
bad()  { FAILS=$((FAILS+1)); printf '  %s[FAIL]%s %s\n' "$R" "$N" "$*" | tee -a "$LOG"; }

# die "what went wrong" "what to do about it"
die() {
  printf '\n%sERROR:%s %s\n' "$R" "$N" "$1" | tee -a "$LOG" >&2
  [ -n "${2:-}" ] && printf '%sWhat to try:%s %s\n' "$B" "$N" "$2" | tee -a "$LOG" >&2
  printf 'Full log: %s\n' "$LOG" >&2
  exit 1
}

on_err() {
  local line="$1" cmd="$2" code="$3"
  printf '\n%sERROR:%s command failed (exit %s) at line %s of install-device.sh:\n    %s\n' "$R" "$N" "$code" "$line" "$cmd" | tee -a "$LOG" >&2
  printf 'Full log: %s\nThe last lines of it:\n' "$LOG" >&2
  tail -n 15 "$LOG" >&2 || true
  printf '\nNothing was rolled back. Fix the cause and run the script again: it is safe to repeat.\n' >&2
}
trap 'on_err "$LINENO" "$BASH_COMMAND" "$?"' ERR

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }

# ------------------------------------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --dry-run) DRY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --accept-disclaimer) ACCEPTED=1 ;;
    --user) shift; SVC_USER="${1:-}" ;;
    --dir) shift; DEST="${1:-}" ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown option: $1" "Run with --help to see the options." ;;
  esac
  shift
done
cd /
[[ "$SVC_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "Invalid user name '$SVC_USER'." "Use lower-case letters, digits, '_' or '-', starting with a letter."
[ -n "$DEST" ] || DEST="/home/$SVC_USER/mcping"

# ------------------------------------------------------------------------------------------ prompting
HAVE_TTY=0; [ -t 0 ] && HAVE_TTY=1

# ask "Question" default  -> REPLY
ask() {
  local q="$1" def="${2:-}"
  if [ "$ASSUME_YES" = 1 ]; then REPLY="$def"; say "  $q [$def] (default used)"; return; fi
  [ "$HAVE_TTY" = 1 ] || die "A question needs an answer but there is no terminal: $q" "Run the script from a terminal, or pass --yes to accept the defaults."
  read -r -p "  $q [$def]: " REPLY || true
  REPLY="${REPLY:-$def}"
  printf '  %s -> %s\n' "$q" "$REPLY" >> "$LOG"
}
# ask_yn "Question" y|n -> returns 0 for yes
ask_yn() {
  local q="$1" def="${2:-n}" hint
  [ "$def" = y ] && hint="Y/n" || hint="y/N"
  if [ "$ASSUME_YES" = 1 ]; then say "  $q [$hint] (default used: $def)"; [ "$def" = y ]; return; fi
  [ "$HAVE_TTY" = 1 ] || die "A question needs an answer but there is no terminal: $q" "Run the script from a terminal, or pass --yes."
  local a=""; read -r -p "  $q [$hint]: " a || true
  a="${a:-$def}"; printf '  %s -> %s\n' "$q" "$a" >> "$LOG"
  [[ "$a" =~ ^[Yy] ]]
}

# run "description" cmd args...   : runs, logs, and on failure explains. In dry-run only prints.
run() {
  local what="$1"; shift
  if [ "$DRY" = 1 ]; then say "  [dry-run] $what: $*"; return 0; fi
  printf '  ... %s\n' "$what" | tee -a "$LOG"
  if ! "$@" >> "$LOG" 2>&1; then
    printf '\n%sERROR:%s %s failed.\n  Command: %s\n  Last output:\n' "$R" "$N" "$what" "$*" | tee -a "$LOG" >&2
    tail -n 20 "$LOG" | sed 's/^/    /' >&2
    printf '  Full log: %s\n' "$LOG" >&2
    return 1
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# asu cmd args...: run as the service user (works from a normal user with sudo, or as root)
asu() {
  if [ "$(id -u)" -eq 0 ]; then runuser -u "$SVC_USER" -- "$@"; else sudo -u "$SVC_USER" "$@"; fi
}

# --------------------------------------------------------------------------------------------- banner
say "MCPing device installer for Linux  (log: $LOG)"
say "Source tree: $SRC_ROOT"
[ "$DRY" = 1 ] && say "DRY RUN: nothing will be changed."
[ "$CHECK_ONLY" = 1 ] && say "CHECK ONLY: nothing will be changed."

step "NON-PRODUCTION, TESTING ONLY"
cat <<'TXT' | tee -a "$LOG"
  This software is AI-generated, not independently reviewed and offered with NO WARRANTY.
  It lets an AI or a person see and operate another computer's keyboard and mouse.
  Use it only on equipment you own or are explicitly allowed to control, entirely at your own risk.
  Full terms: DISCLAIMER.md in this project.
TXT
if [ "$ACCEPTED" != 1 ]; then
  [ "$HAVE_TTY" = 1 ] || die "You must accept DISCLAIMER.md." "Read it, then run again with --accept-disclaimer."
  a=""; read -r -p "  Type  I AGREE  to accept DISCLAIMER.md and continue: " a || true
  [ "$a" = "I AGREE" ] || die "The disclaimer was not accepted." "Read DISCLAIMER.md. Nothing was changed."
fi
ok "Disclaimer accepted"

# ---------------------------------------------------------------------------------------------- sudo
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
  if [ "$CHECK_ONLY" != 1 ] && [ "$DRY" != 1 ]; then
    have sudo || die "sudo is not installed and you are not root." "Install sudo (as root: apt install sudo), or run this script as root."
    say "  sudo is needed to install packages and services. You may be asked for your password."
    sudo -v || die "sudo did not accept your password, or you are not allowed to use it." "Ask an administrator to add you to the sudo group."
  fi
fi

# ------------------------------------------------------------------------------------------- preflight
step "1. Checking this computer"

[ -r /etc/os-release ] || die "Cannot read /etc/os-release, so the Linux version is unknown." "Run this on Ubuntu 24.04 or Linux Mint 22."
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${ID_LIKE:-}" in
  ubuntu:*|linuxmint:*|*:*ubuntu*|debian:*|*:*debian*) ok "OS: ${PRETTY_NAME:-$ID} (apt based)" ;;
  *) bad "OS '${PRETTY_NAME:-unknown}' is not Debian/Ubuntu based. This script uses apt. Install the packages from docs/04-device-setup.md by hand." ;;
esac
case "${VERSION_ID:-}" in
  22.*|24.04|12|13) ;;
  *) warn "Only Linux Mint 22 and Ubuntu 24.04 were used. Version ${VERSION_ID:-?} is untested." ;;
esac
have apt-get || bad "apt-get was not found."

ARCH="$(uname -m)"
case "$ARCH" in x86_64|aarch64|arm64) ok "CPU: $ARCH" ;; *) warn "CPU '$ARCH' is untested (x86-64 and ARM64 were intended)." ;; esac

if [ -d /run/systemd/system ]; then ok "systemd is running"; else bad "systemd is not running (a container or WSL without systemd?). The services need it."; fi

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -ge 3500 ]; then ok "Memory: ${MEM_MB} MB"; elif [ "$MEM_MB" -ge 1900 ]; then warn "Memory: ${MEM_MB} MB. 4 GB or more is recommended; reading the screen may be slow."; else bad "Memory: ${MEM_MB} MB is too little (about 4 GB recommended)."; fi

free_gb() { df -Pk "$1" 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}'; }
CHK_DIR="$(dirname "$DEST")"; [ -d "$CHK_DIR" ] || CHK_DIR="/"
FREE=$(free_gb "$CHK_DIR"); FREE="${FREE:-0}"
if [ "$FREE" -ge 6 ]; then ok "Free disk space near $CHK_DIR: ${FREE} GB"; else bad "Only ${FREE} GB free near $CHK_DIR. About 6 GB is needed (packages, OpenCV, the Python environment)."; fi

PY=python3
if have python3; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then ok "Python $(python3 -c 'import platform;print(platform.python_version())')"
  else bad "Python 3.10 or newer is required (found $(python3 -c 'import platform;print(platform.python_version())'))."; fi
else warn "python3 is not installed yet; it will be installed in step 2."; fi

net_ok() { curl -fsS --max-time 10 -o /dev/null "$1" 2>>"$LOG"; }
if have curl; then
  if net_ok https://pypi.org/simple/pip/; then ok "Network: can reach pypi.org (Python packages)"
  else bad "Cannot reach https://pypi.org. Check the internet connection, DNS, or the proxy settings (http_proxy / https_proxy)."; fi
else warn "curl is not installed yet; the network check will be repeated after step 2."; fi

for sp in "$SRC_ROOT/pyproject.toml" "$SRC_ROOT/src/mcping/server.py" "$SRC_ROOT/firmware/mcping_hid/mcping_hid.ino" "$SRC_ROOT/device/systemd/mcping.service" "$SRC_ROOT/device/systemd/mcping-portal.service"; do
  [ -f "$sp" ] && ok "Found ${sp#$SRC_ROOT/}" || bad "Missing ${sp#$SRC_ROOT/}. Run this script from a complete copy of the project (install/install-device.sh)."
done

port_busy() { have ss && ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"; }
for p in 8765 443; do
  if port_busy "$p"; then
    if [ "$p" = 8765 ] && systemctl is-active --quiet mcping 2>/dev/null; then ok "Port $p is used by the MCPing service (fine for a re-run)"
    elif [ "$p" = 443 ] && systemctl is-active --quiet mcping-portal 2>/dev/null; then ok "Port $p is used by the MCPing portal (fine for a re-run)"
    else warn "Port $p is already in use by something else. Find it with: sudo ss -ltnp | grep :$p"; fi
  else ok "Port $p is free"; fi
done

HOSTN="$(hostname)"
if [ "$HOSTN" = "mcping" ]; then ok "Computer name is 'mcping' (reachable as mcping.local where the network passes mDNS)"
else warn "Computer name is '$HOSTN', so it will not be reachable as mcping.local. You can use its IP address instead (see docs/troubleshooting.md)."; fi

# dependency table: command | apt package | what it is for
DEPS=(
  "python3|python3|the language the server is written in"
  "pip3|python3-pip|Python package installer"
  "ffmpeg|ffmpeg|captures frames from the capture card"
  "v4l2-ctl|v4l-utils|finds and tunes the capture card"
  "tesseract|tesseract-ocr|reads text on the screen"
  "pkg-config|pkg-config|needed to build the OCR binding"
  "gcc|build-essential|compiler for the OCR binding"
  "lsusb|usbutils|lists the USB devices (diagnostics)"
  "curl|curl|health checks and downloads"
  "openssl|openssl|certificate checks"
  "git|git|optional: updating the project"
)
APT_PKGS=(python3 python3-pip python3-venv python3-dev python3-tk ffmpeg v4l-utils tesseract-ocr tesseract-ocr-eng libtesseract-dev libleptonica-dev pkg-config build-essential usbutils curl openssl avahi-daemon git picocom)

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed'; }

# Returns the number of missing items and prints each one.
check_deps() {
  local missing=0 line cmd pkg why
  for line in "${DEPS[@]}"; do
    IFS='|' read -r cmd pkg why <<< "$line"
    if have "$cmd"; then ok "$cmd ($why)"; else bad "$cmd is missing ($why). Package: $pkg"; missing=$((missing+1)); fi
  done
  for pkg in python3-venv python3-dev python3-tk tesseract-ocr-eng libtesseract-dev libleptonica-dev avahi-daemon; do
    if pkg_installed "$pkg"; then ok "package $pkg"; else bad "package $pkg is missing"; missing=$((missing+1)); fi
  done
  if have python3; then
    python3 -c 'import venv, ensurepip' 2>/dev/null && ok "Python venv + ensurepip" || { bad "Python cannot create virtual environments (python3-venv)."; missing=$((missing+1)); }
    local inc; inc="$(python3 -c 'import sysconfig;print(sysconfig.get_paths()["include"])' 2>/dev/null || true)"
    [ -f "$inc/Python.h" ] && ok "Python headers (Python.h)" || { bad "Python.h not found (python3-dev)."; missing=$((missing+1)); }
    python3 -c 'import tkinter' 2>/dev/null && ok "tkinter (for the desktop apps)" || { warn "tkinter is missing; the desktop apps will not open (python3-tk)."; }
  fi
  if have pkg-config; then
    pkg-config --exists tesseract lept 2>/dev/null && ok "Tesseract and Leptonica development files" || { bad "pkg-config cannot find tesseract/lept (libtesseract-dev, libleptonica-dev)."; missing=$((missing+1)); }
  fi
  ls /usr/share/tesseract-ocr/*/tessdata/eng.traineddata >/dev/null 2>&1 && ok "Tesseract English data" || { bad "Tesseract English data (eng.traineddata) not found (tesseract-ocr-eng)."; missing=$((missing+1)); }
  return "$missing"
}

if [ "$CHECK_ONLY" = 1 ]; then
  step "Dependency check (nothing is installed in this mode)"
  set +e; check_deps; MISSING=$?; set -e
  step "Result"
  say "  $FAILS problem(s), $WARNS warning(s). Missing dependencies: $MISSING."
  [ "$FAILS" -eq 0 ] && say "  This computer looks ready. Run the script again without --check to install." || say "  Fix the FAIL lines above, or run the installer: it installs the missing packages."
  [ "$FAILS" -eq 0 ] && exit 0 || exit 2
fi

# problems found by the checks above stop us here, before anything is changed
if [ "$FAILS" -gt 0 ]; then
  say ""
  say "  $FAILS problem(s) were found in step 1 (the FAIL lines above)."
  { [ -d /run/systemd/system ] && have apt-get; } || die "This computer cannot be used as it is (it needs apt and a running systemd)." "Use Ubuntu 24.04 or Linux Mint 22 on real or virtual hardware, or install by hand from docs/04-device-setup.md."
  ask_yn "Continue anyway? (not recommended)" n || die "Stopped because of the problems found in step 1. Nothing was changed." "Fix them and run the script again, or use --check to re-test."
fi

# --------------------------------------------------------------------------------------- questions
step "2. Your choices (press Enter to accept a default)"
ask "Service user (the account the server runs as)" "$SVC_USER"; SVC_USER="$REPLY"
[[ "$SVC_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "Invalid user name '$SVC_USER'." "Use lower-case letters, digits, '_' or '-'."
case "$DEST" in /home/*/mcping) DEST="/home/$SVC_USER/mcping" ;; esac
ask "Install folder" "$DEST"; DEST="$REPLY"
[[ "$DEST" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "The install folder '$DEST' is not acceptable." "Use an absolute path (starting with /) made of letters, digits, . _ - and /. For example /home/$SVC_USER/mcping."

DO_HOSTNAME=0
if [ "$HOSTN" != "mcping" ]; then
  ask_yn "Set the computer name to 'mcping' so it can be found as mcping.local? (changes the system name)" n && DO_HOSTNAME=1
fi
DO_SLEEP=0;   ask_yn "Stop the computer sleeping or shutting down from the power button? (recommended for an always-on device)" y && DO_SLEEP=1
DO_UFW=0
if have ufw && [ "$DRY" != 1 ] && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
  ask_yn "A firewall (ufw) is active. Allow ports 8765 and 443 from your local network only?" y && DO_UFW=1
fi
DO_DESKTOP=0
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then ask_yn "Install the desktop launchers (certificate, invites, monitor)?" y && DO_DESKTOP=1; fi
START_NOW=0;  ask_yn "Start the services when the install finishes?" y && START_NOW=1

TOKEN_MODE="generate"
ENVF=/etc/mcping/mcping.env
if [ -f "$ENVF" ]; then
  if ask_yn "A token file already exists at $ENVF. Keep it?" y; then TOKEN_MODE="keep"; fi
fi
if [ "$TOKEN_MODE" = "generate" ]; then
  if ask_yn "Choose your own token instead of a random one? (random is safer)" n; then TOKEN_MODE="own"; fi
fi
OWN_TOKEN=""
if [ "$TOKEN_MODE" = "own" ]; then
  if [ "$ASSUME_YES" = 1 ]; then die "Cannot ask for a token with --yes." "Run without --yes, or let the installer generate one."; fi
  read -r -s -p "  Token (at least 24 characters of letters, digits, - _ . ~): " OWN_TOKEN; echo
  [[ "$OWN_TOKEN" =~ ^[A-Za-z0-9._~-]{24,}$ ]] || die "That token is not acceptable." "Use 24 or more of: letters, digits, - _ . ~ (no spaces or quotes)."
fi

# ---------------------------------------------------------------------------------- 3. dependencies
step "3. Installing dependencies"
if [ "$DO_HOSTNAME" = 1 ]; then run "set the computer name" $SUDO hostnamectl set-hostname mcping || die "Could not set the computer name." "Run: sudo hostnamectl set-hostname mcping"; fi
run "update the package lists" $SUDO apt-get update || die "apt-get update failed." "Check the network and the apt sources (sudo apt-get update). Another package manager may be running: wait and retry."
run "install the packages" $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_PKGS[@]}" || die "Installing packages failed." "Read the output above. Common causes: no network, a held or broken package (sudo apt --fix-broken install), or not enough disk space."

step "3b. Checking every dependency before installing the project"
if [ "$DRY" = 1 ]; then say "  [dry-run] would verify all dependencies here"; else
  BEFORE=$FAILS
  set +e; check_deps; MISSING=$?; set -e
  if [ "$MISSING" -gt 0 ] || [ "$FAILS" -gt "$BEFORE" ]; then
    die "$MISSING dependency/dependencies are still missing after installation (see the FAIL lines above)." "Install them by hand (sudo apt install <package>), then run this script again. The project was not touched."
  fi
  have curl && { net_ok https://pypi.org/simple/pip/ && ok "Network: pypi.org reachable" || die "pypi.org is not reachable, but the Python packages must be downloaded." "Check the internet connection, DNS, or the proxy settings."; }
fi

# --------------------------------------------------------------------------------------- 4. user
step "4. Service user and folders"
if id "$SVC_USER" >/dev/null 2>&1; then ok "User $SVC_USER exists"
else
  run "create the user $SVC_USER" $SUDO adduser --disabled-password --gecos "MCPing service" "$SVC_USER" || die "Could not create the user." "Create it yourself: sudo adduser $SVC_USER"
fi
run "add $SVC_USER to the dialout and video groups (serial port and capture card)" $SUDO usermod -aG dialout,video "$SVC_USER" || die "Could not add the groups." "Run: sudo usermod -aG dialout,video $SVC_USER"
run "create $DEST" $SUDO install -d -o "$SVC_USER" -g "$SVC_USER" -m 755 "$DEST" || die "Could not create $DEST." "Check that the parent folder exists and the disk is not full."

# --------------------------------------------------------------------------------- 5. copy + venv
step "5. Installing the project"
if [ "$(cd "$DEST" 2>/dev/null && pwd -P || echo none)" = "$(cd "$SRC_ROOT" && pwd -P)" ]; then
  ok "The project is already in $DEST"
else
  if [ "$DRY" = 1 ]; then say "  [dry-run] would copy $SRC_ROOT to $DEST"; else
    say "  ... copying the project to $DEST"
    ( cd "$SRC_ROOT" && tar --exclude=.git --exclude=.venv --exclude=__pycache__ -cf - . ) | asu tar -C "$DEST" -xf - >> "$LOG" 2>&1 \
      || die "Copying the project failed." "Check that $DEST is writable by $SVC_USER and the disk has space."
    ok "Copied"
  fi
fi

if [ -x "$DEST/.venv/bin/python" ]; then ok "Python environment exists"
else run "create the Python environment" asu python3 -m venv "$DEST/.venv" || die "Could not create the Python environment." "Is python3-venv installed? Remove $DEST/.venv and try again."; fi
run "upgrade pip" asu "$DEST/.venv/bin/pip" install --upgrade pip || die "Could not upgrade pip." "Check the network (pypi.org) and retry."
say "  ... installing the Python packages (OpenCV and the OCR binding can take several minutes)"
if [ "$DRY" != 1 ]; then
  if ! ( cd "$DEST" && asu "$DEST/.venv/bin/pip" install -e ".[vision,flash]" ) >> "$LOG" 2>&1; then
    tail -n 25 "$LOG" | sed 's/^/    /' >&2
    die "Installing the Python packages failed." "Look for 'tesserocr' or 'leptonica' in the output above: that means libtesseract-dev, libleptonica-dev or pkg-config is missing or too old. 'No matching distribution' means no network or an unsupported Python version. Then re-run this script."
  fi
  ok "Python packages installed"
  step "5b. Verifying the Python environment"
  BEFORE=$FAILS
  for mod in mcp serial PIL cryptography numpy cv2 tesserocr esptool mcping; do
    if asu "$DEST/.venv/bin/python" -c "import $mod" 2>>"$LOG"; then ok "import $mod"; else bad "cannot import $mod"; fi
  done
  for exe in mcping-server mcping-cli mcping-cert mcping-invite mcping-clients; do
    [ -x "$DEST/.venv/bin/$exe" ] && ok "command $exe" || bad "command $exe was not created"
  done
  [ "$FAILS" -eq "$BEFORE" ] || die "Some Python parts did not install correctly (see the FAIL lines above)." "Read $LOG. Re-run the script after fixing the cause; pip will skip what is already installed."
fi

# -------------------------------------------------------------------------------- 6. system settings
step "6. System settings"
if [ "$DO_SLEEP" = 1 ]; then
  run "mask sleep, suspend and hibernate" $SUDO systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || warn "Could not mask the sleep targets."
  if [ -f "$DEST/device/etc/systemd/logind.conf.d/mcping.conf" ]; then
    run "install the power button setting" $SUDO install -D -m 644 "$DEST/device/etc/systemd/logind.conf.d/mcping.conf" /etc/systemd/logind.conf.d/mcping.conf || warn "Could not install the logind setting."
    say "  (the power button setting takes effect after the next reboot)"
  fi
fi

# --------------------------------------------------------------------------------------- 7. token
step "7. Access token"
run "create /etc/mcping" $SUDO install -d -m 755 /etc/mcping || die "Could not create /etc/mcping." "Check permissions."
case "$TOKEN_MODE" in
  keep) ok "Keeping the existing token file" ;;
  generate|own)
    if [ "$DRY" = 1 ]; then say "  [dry-run] would write $ENVF (mode 640)"; else
      if [ "$TOKEN_MODE" = "own" ]; then TOK="$OWN_TOKEN"; else TOK="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"; fi
      [ "${#TOK}" -ge 24 ] || die "The token is too short." "This should not happen; run the script again."
      printf 'MCPING_TOKEN=%s\n' "$TOK" | $SUDO tee "$ENVF" >/dev/null
      $SUDO chown "root:$SVC_USER" "$ENVF"; $SUDO chmod 640 "$ENVF"
      unset TOK OWN_TOKEN
      ok "Token written to $ENVF (readable by root and $SVC_USER only)"
    fi ;;
esac

# --------------------------------------------------------------------------------- 8. certificates
step "8. Certificate folder"
run "create /var/lib/mcping/tls" $SUDO install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 /var/lib/mcping /var/lib/mcping/tls || die "Could not create /var/lib/mcping/tls." "Check permissions and disk space."
$SUDO chown -R "$SVC_USER:$SVC_USER" /var/lib/mcping 2>>"$LOG" || true

# ----------------------------------------------------------------------------------- 9. services
step "9. Services"
install_unit() {
  local unit="$1" tmp; tmp="$(mktemp)"
  sed -e "s#/home/mcping/mcping#$DEST#g" -e "s#^User=mcping#User=$SVC_USER#" -e "s#^Group=mcping#Group=$SVC_USER#" \
      "$DEST/device/systemd/$unit" > "$tmp" 2>>"$LOG" || die "Could not read $DEST/device/systemd/$unit." "The project copy is incomplete; re-run the script."
  run "install $unit" $SUDO install -m 644 "$tmp" "/etc/systemd/system/$unit" || die "Could not install $unit." "Check permissions on /etc/systemd/system."
  rm -f "$tmp"
}
if [ "$DRY" = 1 ]; then say "  [dry-run] would install mcping.service and mcping-portal.service"; else
  install_unit mcping.service
  install_unit mcping-portal.service
  run "reload systemd" $SUDO systemctl daemon-reload || die "systemctl daemon-reload failed." "Is systemd running? (systemctl status)"
  run "enable the services at boot" $SUDO systemctl enable mcping mcping-portal || die "Could not enable the services." "Run: systemctl status mcping"
fi

if [ "$START_NOW" = 1 ] && [ "$DRY" != 1 ]; then
  run "start the MCP service" $SUDO systemctl restart mcping || die "The MCP service did not start." "Run: journalctl -u mcping -n 50 --no-pager"
  say "  ... waiting for it to answer"
  UP=0
  for _ in $(seq 1 30); do
    if curl -sk --max-time 3 https://127.0.0.1:8765/health 2>/dev/null | grep -q '"ok":true'; then UP=1; break; fi
    sleep 1
  done
  if [ "$UP" = 1 ]; then ok "MCP service answers on https://127.0.0.1:8765/health"
  else
    $SUDO journalctl -u mcping -n 25 --no-pager 2>&1 | sed 's/^/    /' | tee -a "$LOG" >&2 || true
    die "The MCP service started but did not answer within 30 seconds." "Read the journal lines above. Usual causes: the token file is not readable by $SVC_USER, port 8765 is in use, or the capture card or ESP32 is missing (the service may still run without them; check journalctl -u mcping)."
  fi
  run "start the setup portal" $SUDO systemctl restart mcping-portal || warn "The setup portal did not start (journalctl -u mcping-portal -n 50). The MCP service itself is running."
  sleep 2
  if $SUDO systemctl is-active --quiet mcping-portal; then ok "Setup portal is running on port 443"; else warn "Setup portal is not running."; fi
  if [ -x "$DEST/.venv/bin/mcping-cert" ]; then
    asu env MCPING_TLS_DIR=/var/lib/mcping/tls "$DEST/.venv/bin/mcping-cert" status 2>&1 | sed 's/^/    /' | tee -a "$LOG" || warn "Could not read the certificate status."
  fi
fi

# --------------------------------------------------------------------------------- 10. optional bits
if [ "$DO_UFW" = 1 ]; then
  step "10. Firewall"
  NET="$(ip -o -4 route show scope link 2>/dev/null | awk '!/docker|virbr|br-/ {print $1; exit}')"
  ask "Allow connections from this network" "${NET:-192.0.2.0/24}"; NET="$REPLY"
  [[ "$NET" =~ ^[0-9.]+/[0-9]+$ ]] || warn "'$NET' does not look like a network (a.b.c.d/nn); skipping the firewall rules."
  if [[ "$NET" =~ ^[0-9.]+/[0-9]+$ ]]; then
    run "allow 8765 from $NET" $SUDO ufw allow from "$NET" to any port 8765 proto tcp || warn "Could not add the rule."
    run "allow 443 from $NET" $SUDO ufw allow from "$NET" to any port 443 proto tcp || warn "Could not add the rule."
  fi
fi

if [ "$DO_DESKTOP" = 1 ]; then
  step "11. Desktop launchers"
  if [ "$(id -un)" = "$SVC_USER" ]; then
    APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$APPS"
    for d in mcping-certificate mcping-connect mcping-monitor mcping-setup-pages; do
      if [ -f "$DEST/device/share/$d.desktop" ]; then
        sed "s#/home/mcping/mcping#$DEST#g" "$DEST/device/share/$d.desktop" > "$APPS/$d.desktop" && chmod 755 "$APPS/$d.desktop" && ok "$d launcher"
      fi
    done
  else
    warn "You are logged in as $(id -un), not $SVC_USER, so the launchers were not installed. Log in as $SVC_USER and re-run, or copy $DEST/device/share/*.desktop yourself."
  fi
fi

# ------------------------------------------------------------------------------- 12. hardware report
step "12. What hardware is connected right now (information only)"
if have lsusb; then
  USBLIST="$(lsusb 2>/dev/null | grep -Ei '345f|ms2130|1a86|303a|capture' || true)"
  if [ -n "$USBLIST" ]; then printf '%s
' "$USBLIST" | sed 's/^/    /' | tee -a "$LOG"; else say "    (no capture card or ESP32 seen yet)"; fi
fi
SERIALS="$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true)"
if [ -n "$SERIALS" ]; then printf '%s
' "$SERIALS" | sed 's/^/    serial: /' | tee -a "$LOG"; else say "    no serial port yet: plug the ESP32 UART port into this computer"; fi
if have v4l2-ctl; then v4l2-ctl --list-devices 2>/dev/null | sed 's/^/    /' | tee -a "$LOG" || true; fi
say "  You may need to log out and in (or reboot) before $SVC_USER's new group memberships apply to a desktop session."

# ------------------------------------------------------------------------------------------ summary
step "Done"
say "  Problems: $FAILS   Warnings: $WARNS"
say "  Log: $LOG"
cat <<TXT | tee -a "$LOG"

  Next steps
    1. Flash the ESP32 firmware (docs/05-firmware.md; the Windows script can do it from a PC).
    2. Connect the cables (docs/03-build.md).
    3. Read the token with:   sudo cat $ENVF      (keep it private; it is full control of the target)
    4. Add a client:          open https://<this-computer>/  or use  $DEST/.venv/bin/mcping-invite
    5. Read SECURITY.md. Never expose ports 8765 or 443 to the internet.

  Check status any time:  systemctl status mcping mcping-portal
TXT
exit 0
