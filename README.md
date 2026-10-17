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

## Status
Experimental. The build instructions have not yet been verified end to end on a fresh machine.
The source code is not yet published here.

## License
Software: [MIT](LICENSE).
