# MCPing troubleshooting reference

Use this after the main skill. Always start with the two known issues, then match the symptom. Run only
read-only checks without asking. Replace `<device>` with the device's IP address or name.

## First, the two common causes
| Cause | How to recognise it | Check | Fix |
|---|---|---|---|
| `mcping.local` does not resolve | "name does not resolve", "No such host", "ENOTFOUND", browsers cannot open the setup page | `ping mcping.local`; on Windows `[Net.Dns]::GetHostAddresses('mcping.local')`; on the device `ip -br a` | Use the device's **IP address** everywhere; give the device a fixed address (DHCP reservation); use the same address in every client; do not expose the device to the internet to work around it |
| Scripts are not allowed to run | PowerShell says scripts are disabled or blocked; a security tool blocks the script; "restricted language mode" | `Get-ExecutionPolicy -List`; `$ExecutionContext.SessionState.LanguageMode` | Do **not** bypass it. Ask the administrator, or use the no-script route in `docs/06-connect.md` (certificate download, `NODE_EXTRA_CA_CERTS`, typed `claude mcp add`, or a `.mcp.json`) |
They can occur together; check both before anything else.

## Installation problems (Linux device)
| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `--check` says not apt based / no systemd | Wrong OS, a container, or WSL without systemd | `cat /etc/os-release`; `ls /run/systemd/system` | Use Ubuntu 24.04 or Mint 22 on real or virtual hardware with systemd |
| `apt-get update` or install fails | No network, another package manager running, broken package | `sudo apt-get update`; `sudo apt --fix-broken install` | Fix the network or wait for the other process |
| pip fails building `tesserocr` ("leptonica", "tesseract", "pkg-config") | Missing development packages | `pkg-config --exists tesseract lept && echo ok` | `sudo apt install libtesseract-dev libleptonica-dev pkg-config build-essential python3-dev tesseract-ocr-eng` |
| "No matching distribution" / pip cannot reach pypi.org | No network, proxy, or Python older than 3.10 | `python3 --version`; `curl -I https://pypi.org` | Fix the proxy (`https_proxy`), upgrade Python |
| `mcping` service fails to start | Token file unreadable, port in use, wrong paths | `journalctl -u mcping -n 50 --no-pager`; `ls -l /etc/mcping/mcping.env`; `sudo ss -ltnp \| grep -E ':8765\|:443'` | File must be `root:<service user>` mode 640 and contain `MCPING_TOKEN=` with 24+ characters; free the port; service files assume `/home/<user>/mcping` |
| Health check fails | Service still starting, or TLS problem | `curl -sk https://127.0.0.1:8765/health`; `mcping-cert status` | Wait 30 s; read the journal |
| Setup portal (443) not running | Another web server on 443 | `systemctl status mcping-portal`; `sudo ss -ltnp \| grep :443` | Stop the other service or free the port |
| Desktop launchers missing | Installed only when logged in as the service user | `whoami` | Set a password for the service user and log in as it, or copy `device/share/*.desktop` by hand |
| Group changes not applied | `dialout`/`video` need a new login | `id` | Log out and in, or reboot |

## Hardware problems
| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| ESP32 not seen | Charge-only cable, or the wrong USB-C port | `lsusb`; `ls /dev/ttyACM*`; `dmesg \| tail` | Use a data cable in the **UART** port (next to the USB-serial chip) |
| Target sees no keyboard or mouse | The UART port is plugged into the target, or the firmware is missing | LED colour; `mcping-cli ping` (service stopped) | The native **USB** port goes to the target; flash the firmware (`docs/05-firmware.md`) |
| LED steady red | No target connected, powered off, or suspended | Watch the LED while plugging in | Check the cable and that the target is on |
| LED steady green but nothing types | Wrong keyboard layout, or a BIOS that ignores this device | Try typing in a text box | Typing assumes a US layout; BIOS support is untested |
| Flashing cannot connect | Board not in download mode, wrong port | `ls /dev/ttyACM*` or the COM list | Hold BOOT, tap RST, release BOOT; stop the service first; use the **UART** port |
| Wrong LED colours / no LED | LED on a different pin | Check the board | Build with `-DLED_PIN=38` for v1.1 boards |
| Screen is black or the picture is wrong | No HDMI signal, protected content, or the card not found | `v4l2-ctl --list-devices`; the live monitor; `status` tool | Check HDMI into the card's **input**; HDCP content shows black; set `MCPING_VIDEO_DEVICE` if the wrong camera is picked |
| Washed-out or clipped greys, controls hard to see | Card brightness/contrast not suited to this chip | A grey ramp on the target | Tune `MCPING_CAPTURE_CONTRAST` and `MCPING_CAPTURE_BRIGHTNESS` in `/etc/mcping/mcping.env` |
| Wi-Fi drops or the device freezes | Failing Wi-Fi hardware | `dmesg \| grep -i firmware` | Use wired Ethernet or another adapter; do not tune around a hardware fault |

## Connection problems (clients)
| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `UNABLE_TO_VERIFY_LEAF_SIGNATURE` / certificate errors | CA not trusted, or the program started before `NODE_EXTRA_CA_CERTS` was set | `echo $NODE_EXTRA_CA_CERTS` (Windows: `$env:NODE_EXTRA_CA_CERTS`) | Point it at `mcping-ca.crt`, then **fully restart** the client |
| Certificate name mismatch | Connecting by a name or IP the certificate does not list | `mcping-cert status` on the device | Use a listed name or IP; the certificate renews itself when the device's addresses change |
| Browser warning on the setup page | The CA is not yet trusted | Compare the fingerprint with the device's | Expected on first visit; verify the fingerprint **on the device** before trusting |
| HTTP 401 | Wrong token, or that computer's token was revoked | `mcping-clients list` on the device | Create a new invite; tokens are shown once |
| HTTP 409 "busy" | Another client holds the device (one at a time) | Who is connected? | Wait up to 5 minutes idle, or the other client disconnects; `MCPING_SESSION_IDLE` sets the time |
| Invite link says invalid | Expired (60 s), used, or locked after wrong tries | `mcping-invite` | Create a new invite; two wrong tries lock invites for 5 minutes |
| Claude app does not list `mcping` | App not restarted, or the helper script not allowed | Check the app's config for an `mcping` entry | Quit the app completely and reopen; the connectors-list bridge needs a script, so on the no-script route use the Code tab (`/mcp`) |
| Claude Code says the server failed | Name, port, token or certificate | `claude mcp list` | Re-register with the IP address; check the four causes above |
| Monitor asks for a sign-in on the device itself | Local no-sign-in shortcut is off by default | n/a | Sign in with a token, or set `MCPING_MONITOR_LOCAL_OPEN=1` knowing the risk (`SECURITY.md`) |

## Reading the screen
| Symptom | Fix |
|---|---|
| Numbers or small dark text misread | Use `zoom` or `screenshot`; use `text_zoom` to enlarge text first; check the `TEXT QUALITY` line |
| Controls not recognised | Custom-drawn or dark themes are weak spots (`docs/07-using.md`) |
| `describe_screen` slow | `MCPING_OCR_PASSES=raw` is about twice as fast |

## When to stop
Stop and summarise if: the same step fails twice with the same error, a fix would need to bypass a policy, an
action could lose data, or the user is unsure whether they are authorised to control the target.
