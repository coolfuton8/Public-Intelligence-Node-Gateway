# 1. Overview

> **Non-production, testing only, no warranty.** Read [DISCLAIMER.md](../DISCLAIMER.md) first.

## What it is

The Public Intelligence Node Gateway lets a person, or an AI model, **see and operate another
computer with nothing installed on it**. A small Linux computer, the *node*, sits between you and
the *target*:

- **Seeing:** an HDMI capture card takes the target's video. Its loop-out passes the picture on to a
  monitor, so a person can watch what the AI sees.
- **Typing and clicking:** a microcontroller (an ESP32-S3 board) plugs into the target's USB port and
  appears as an ordinary keyboard and mouse.
- **The node:** runs the software that captures frames, drives the microcontroller, and offers all of
  it as an [MCP](https://modelcontextprotocol.io) server over HTTPS with a token.
- **Text-only models:** a model that cannot read images gets a plain-text description of the screen,
  made without any AI (OpenCV plus Tesseract OCR).

```
 MCP client (an AI app) ──HTTPS + token──> NODE (Linux) ──USB──> capture card <── HDMI ── TARGET
                                             │                                     │
                                             └──USB (serial)──> ESP32-S3 ──USB────┘ (keyboard + mouse)
                                                         monitor <── loop-out ── capture card
```

## What it is good for
Lawful, supervised work on computers you own or are allowed to control: IT support, setting up or
testing machines, accessibility, and research. See the acceptable use rules in
[DISCLAIMER.md](../DISCLAIMER.md).

## What it cannot do
- It only sees what the target sends over HDMI. Content protected by HDCP appears black.
- It has no access to the target's files, network or processes. Everything goes through the screen,
  keyboard and mouse.
- Reading text is imperfect. Small, dark or low-contrast text is often misread. A model should check
  important details with a screenshot or zoom.
- Typing assumes a US keyboard layout on the target.
- Only one client can drive the node at a time.
- The software has been tested on one Linux target and one Windows-style test fixture. Behaviour on
  other systems, and in a BIOS, is unknown.

## Status
Experimental. The software was written with AI assistance, has not been independently reviewed, and
the build instructions in this repository have **not yet been verified end to end** on a fresh
machine. Pages say what is and is not tested.

## Similar projects
If you want a mature product for remote keyboard, video and mouse control by a human, look at
PiKVM, JetKVM and similar network KVMs. This project differs in that the client is an MCP-speaking AI
model and in the text description of the screen.

## Reading order
1. [Parts](02-parts.md) 2. [Build](03-build.md) 3. [Node setup](04-node-setup.md)
4. [Firmware](05-firmware.md) 5. [Connect a client](06-connect.md) 6. [Using it](07-using.md)
7. [Troubleshooting](troubleshooting.md) 8. [SECURITY.md](../SECURITY.md)
