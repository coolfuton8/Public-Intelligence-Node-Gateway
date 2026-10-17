# Public Intelligence Node Gateway

> **WARNING: NON-PRODUCTION. FOR TESTING ONLY. NO WARRANTY.**
> The software is AI-generated ("vibe coded"), unreviewed, and may be flawed or broken. It can
> operate another computer's keyboard and mouse and see its screen. Use it only on equipment you
> own or are explicitly authorised to control, entirely at your own risk.
>
> **By using this software and hardware (the "system") you agree to the terms in
> [DISCLAIMER.md](DISCLAIMER.md). Read it first.**

A small Linux computer, an HDMI capture card and an ESP32-S3 board let an AI model **see and operate
another computer with nothing installed on it**, through a network MCP server. Models that cannot read
images get a plain-text description of the screen.

## Documents

| Read | What it covers |
|---|---|
| [DISCLAIMER.md](DISCLAIMER.md) | Intended use, ethics, prohibited use, no warranty. **Read first.** |
| [SECURITY.md](SECURITY.md) | Threat model, safe-use rules, reporting a problem |
| [1. Overview](docs/01-overview.md) | What it is, what it cannot do |
| [2. Parts](docs/02-parts.md) | What to buy |
| [3. Build](docs/03-build.md) | Cabling and first checks |
| [4. Device setup](docs/04-device-setup.md) | Linux, packages, services, token, certificate |
| [5. Firmware](docs/05-firmware.md) | Build and flash the ESP32-S3 |
| [6. Connect a client](docs/06-connect.md) | Certificates, invites, tokens, client setup |
| [7. Using it](docs/07-using.md) | Tools, screen description, accuracy, limits |
| [Troubleshooting](docs/troubleshooting.md) | Known issues |

## Guided setup with Claude Code
This repository includes a Claude Code skill, `mcping-setup-guide`, that walks you through the install and
helps troubleshoot it. Clone the repository, open the folder in Claude Code, and ask for help installing or
fixing MCPing. Claude Code finds the skill by itself in `.claude/skills/`. It asks before changing anything,
never asks you to paste the token into the chat, and does not bypass security policies. The skill is as
untested as the rest of the project.

## Installers
`install/install-device.sh` (Linux) and `install/install-windows.ps1` (Windows PC: firmware tools and
client setup) are described in [Device setup](docs/04-device-setup.md) and [Firmware](docs/05-firmware.md).
They are untested on a clean machine.

## Status
Experimental. The build instructions have not yet been verified end to end on a fresh machine.
The source code, firmware sketch, service files and installers are published here, untested on a clean machine.

## License
Software: [MIT](LICENSE).
