# 3. Build (cabling and first checks)

> **Non-production, testing only, no warranty.** Read [DISCLAIMER.md](../DISCLAIMER.md) first.
> **Not yet verified end to end.** Follow it carefully and expect to adjust.

Do this after you have the [parts](02-parts.md). Build the hardware first, then
[set up the device](04-device-setup.md) and [flash the firmware](05-firmware.md). Power everything down
while you connect cables. Handle the boards by their edges, on a non-conductive surface.

## Wiring

![Diagram: the MCPing device (a laptop) connects to the capture card (USB 3.0) and the ESP32-S3 board (UART port). The target's HDMI output goes to the capture card's HDMI input, the card's HDMI output goes to an optional monitor, and the ESP32's USB port goes to the target.](images/system-connections.svg)

```
target HDMI out ──> capture card HDMI IN ──loop-out──> monitor (optional)
capture card USB ──> device (use a USB 3 port)
ESP32 "UART" USB-C ──> device          (commands, and power for the board)
ESP32 "USB" USB-C  ──> target        (the keyboard and mouse the target sees)
device Ethernet ──> your network
```

- The ESP32 board has two USB-C ports. The one next to the USB-serial chip is the **UART** port, and
  it goes to the **device**. The other is the chip's native **USB** port, and it goes to the **target**.
  Check the silkscreen labels on your board. If you swap them, the target sees nothing.
- Check that the ESP32's native USB port does not back-feed power into the target. Confirm this from
  your board's documentation or with a meter before first use.
- The capture card passes the monitor's identity (EDID) to the target, so the target uses the
  monitor's native resolution and the card scales it. Without a monitor on the loop-out the target may
  choose its own resolution.

## What the LED means (after the firmware is flashed)
- Steady green: the target's USB link is up.
- Steady red: no target, powered off or suspended.
- A blue flash: a command came from the device.
- A yellow flash: a key or mouse report was sent.

If the LED does not light, your board may use a different LED pin (see [Firmware](05-firmware.md)).

## Capture card settings
Some capture cards stretch a full-range HDMI picture and clip light greys to white, which erases the
borders of many controls. The software sets the card's brightness and contrast on start-up. The
values that gave a near one-to-one picture on the tested MS2130 card are contrast 44 and
brightness 53. **Another card needs its own values.** To find yours, show a grey ramp on the target and
adjust until the greys are distinct, then set `MCPING_CAPTURE_CONTRAST` and
`MCPING_CAPTURE_BRIGHTNESS` in the device's environment file (see [Device setup](04-device-setup.md)).

## First checks (after device setup)
| Check | Expected |
|---|---|
| `lsusb` on the device | the capture card and the ESP32's USB-serial chip appear |
| `v4l2-ctl --list-devices` | the card is listed as a video device |
| `ls /dev/ttyACM*` | the ESP32's serial port appears |
| The target's device list | a new keyboard and mouse appear when the ESP32 is plugged in |
| `mcping-cli ping` (service stopped) | prints a reply from the firmware |

## Safety
- Do not leave the assembled device powered and unattended.
- Keep cables clear of heat and liquids, and do not force connectors.
- Stop and disconnect if anything smells hot or looks damaged.

## Next
[Device setup](04-device-setup.md)
