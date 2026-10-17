# 4. Device setup

> **Non-production, testing only, no warranty.** Read [DISCLAIMER.md](../DISCLAIMER.md) first.
> **Not yet verified end to end on a fresh machine.** Commands are written from a working setup but
> have not been replayed from this page. Treat every step as untested until you have proved it.

The device is the Linux computer that runs the server. These steps assume Linux Mint 22.x or Ubuntu
24.04. They use a user called `mcping` and the folder `/home/mcping/mcping`, because the service
files expect those exact paths. If you want different ones, edit the files under `device/`.

## Installer scripts (optional, UNTESTED)
> **The installer scripts have not been tested on a clean machine.** Treat them as drafts. They make
> system changes (packages, a user account, services), so read them before you run them, run `--check`
> and `--dry-run` first, and be ready to fix problems or finish by hand with the steps below.

| Script | Where it runs | What was actually tested |
|---|---|---|
| `install/install-device.sh` | The Linux device | Syntax only. It has **never been run** end to end: no real package install, no service start. |
| `install/install-windows.ps1` | A Windows PC (firmware tools and client setup; it cannot install the device itself) | Check-only and dry-run modes on one PC. The tool installs, the firmware build and flash, the certificate download and the settings change have **not been run**. |

`install/install-device.sh` does the steps below for you, and checks that every dependency is present
before it touches the project. It asks before each group of changes and shows its defaults. Run it from a
terminal, as a normal user who can use `sudo`:

```bash
bash install/install-device.sh --check     # only checks this computer; changes nothing
bash install/install-device.sh --dry-run   # shows what it would do; changes nothing
bash install/install-device.sh             # does the installation
```

Options: `--yes` (accept every default), `--accept-disclaimer`, `--user NAME`, `--dir PATH`. You must
accept [DISCLAIMER.md](../DISCLAIMER.md) when asked, or pass `--accept-disclaimer`. The script writes a log
file (its path is printed first) and never puts the token in it.

**It creates one account.** If the service user (default `mcping`) does not exist, it is created with no
password, so nobody can log in as it. The user is added to the `dialout` and `video` groups. No sudo entry
or SSH key is created. The Windows script creates no accounts.

The steps below are the same work done by hand, which is also the way to understand or repair an
installation. They are also untested (see the note at the top of this page).

## 1. Install Linux
- Name the computer **`mcping`**, so it is reachable as `mcping.local` on networks that pass mDNS.
- Create the user **`mcping`**.
- After the first boot, update it, and install SSH if you want to manage it remotely:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y openssh-server
ip -br a                       # note the device's address
```

Give the device a **fixed address** in your router (a DHCP reservation). Many networks do not resolve
`mcping.local`; see [Troubleshooting](troubleshooting.md).

## 2. Packages and permissions
```bash
sudo apt install -y git ffmpeg v4l-utils python3-venv python3-pip python3-dev build-essential \
  picocom usbutils avahi-daemon tesseract-ocr tesseract-ocr-eng libtesseract-dev libleptonica-dev \
  pkg-config python3-tk
sudo usermod -aG dialout,video mcping       # serial port and capture card; log out and in afterwards
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

In the computer's BIOS or firmware, set it to power on after a power loss if you want it to restart
by itself.

## 3. Get the code and install it
Put this repository at `/home/mcping/mcping`, then, as the `mcping` user:

```bash
cd ~/mcping
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[vision,flash]"
```

`vision` adds OpenCV, NumPy and Tesseract (the screen description). `flash` adds the tool that
programs the ESP32 (the version in the Linux package repositories is too old for the S3 chip).

## 4. Keep the device awake
```bash
sudo install -D -m 644 device/etc/systemd/logind.conf.d/mcping.conf /etc/systemd/logind.conf.d/mcping.conf
```

This stops the power button and sleep from shutting the device down. It takes effect after a reboot.

## 5. The token
The token controls access. Make it long and random, and keep it private.

```bash
sudo mkdir -p /etc/mcping
echo "MCPING_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" | sudo tee /etc/mcping/mcping.env >/dev/null
sudo chown root:mcping /etc/mcping/mcping.env && sudo chmod 640 /etc/mcping/mcping.env
```

Optional settings go in the same file, one `NAME=value` per line. The useful ones:

| Setting | Meaning | Default |
|---|---|---|
| `MCPING_HID_PORT` | the ESP32 serial port | found automatically |
| `MCPING_VIDEO_DEVICE` | the capture device | found by its USB identity |
| `MCPING_CAPTURE_CONTRAST`, `MCPING_CAPTURE_BRIGHTNESS` | capture card tuning | tuned for the tested card |
| `MCPING_SINGLE_CLIENT` | one client at a time (`0` turns it off) | `1` |
| `MCPING_SESSION_IDLE` | seconds of silence before another client may connect | `300` |
| `MCPING_MONITOR_LOCAL_OPEN` | `1` skips the monitor sign-in for a browser on the device itself | `0` |
| `MCPING_IDLE_REFRESH` | seconds between background frame grabs while a monitor page is open (`0` is off) | `10` |

## 6. Certificate folder and services
The server creates a small certificate authority (CA) and a server certificate on first start.

```bash
sudo mkdir -p /var/lib/mcping/tls
sudo chown -R mcping:mcping /var/lib/mcping && sudo chmod 750 /var/lib/mcping/tls
sudo install -m 644 ~/mcping/device/systemd/mcping.service /etc/systemd/system/
sudo install -m 644 ~/mcping/device/systemd/mcping-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mcping mcping-portal
systemctl status mcping --no-pager
curl -sk https://localhost:8765/health            # {"ok":true,"service":"mcping"}
~/mcping/.venv/bin/mcping-cert status             # names, addresses, expiry, CA fingerprint
```

The services listen on all network interfaces: port **8765** (MCP) and **443** (setup pages).
**Do not expose either to the internet.** See [SECURITY.md](../SECURITY.md).

## 7. Desktop launchers (optional, for a device with a screen)
```bash
install -D -m 755 device/share/mcping-certificate.desktop ~/.local/share/applications/mcping-certificate.desktop
install -m 755 device/share/mcping-connect.desktop ~/.local/share/applications/mcping-connect.desktop
install -m 755 device/share/mcping-monitor.desktop ~/.local/share/applications/mcping-monitor.desktop
install -m 755 device/share/mcping-setup-pages.desktop ~/.local/share/applications/mcping-setup-pages.desktop
```

They give you **MCPing Certificate** (renew and export the CA), **MCPing Connect** (invites and the
list of computers with access), and the live monitor.

## 8. Back up the secrets, away from the device and never in git
- the token line from `/etc/mcping/mcping.env`
- `/var/lib/mcping/tls/ca.key` and `ca.crt`
- `/var/lib/mcping/clients.json` and `invites.json`, if you use per-computer tokens

If the device is lost without a backup you must create a new token and CA and set up every client again.

## 9. Updating
Replace the code, then `sudo systemctl restart mcping mcping-portal`. Reinstall only if
`pyproject.toml` changed.

## Next
[Firmware](05-firmware.md)
