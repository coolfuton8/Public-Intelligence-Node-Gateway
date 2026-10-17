# 5. Firmware (ESP32-S3 keyboard and mouse)

> **Non-production, testing only, no warranty.** Read [DISCLAIMER.md](../DISCLAIMER.md) first.
> **Not yet re-verified from this page.** No pre-built binaries are provided; you build the sketch
> yourself.

The sketch `firmware/mcping_hid/mcping_hid.ino` turns an ESP32-S3 board into a USB keyboard and an
absolute-position mouse for the target. It takes line-based text commands from the node over the
board's UART port.

## Build
You need [arduino-cli](https://github.com/arduino/arduino-cli) with the ESP32 core. The tested
core version was 3.3.12, and the sketch uses no extra libraries.

```bash
arduino-cli config init
arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32@3.3.12
arduino-cli compile --fqbn "esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=16M" \
  --output-dir build firmware/mcping_hid
```

- `USBMode=default` selects the native USB stack (TinyUSB), which the HID needs.
- Set `FlashSize` to match your board.
- Some boards have the RGB LED on pin 38 instead of 48. Add `-DLED_PIN=38` through the compiler flags,
  or change `LED_PIN` in the sketch.

## Flash from the node
Use the board's **UART** port. Stop the service first, because it holds the serial port.

```bash
sudo systemctl stop mcping
# a board that already runs this firmware (application only):
~/mcping/.venv/bin/esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 --after hard-reset write-flash 0x10000 build/mcping_hid.ino.bin
# a new board (bootloader, partitions and application):
~/mcping/.venv/bin/esptool --chip esp32s3 -p /dev/ttyACM0 -b 460800 --after hard-reset write-flash 0x0 build/mcping_hid.ino.merged.bin
sudo systemctl start mcping
```

If the flasher cannot connect, hold BOOT, tap RST, release BOOT and try again. The Linux package
repository version of esptool is too old for the S3 chip. Use the one installed by `pip`.

## Check it
With the service stopped: `~/mcping/.venv/bin/mcping-cli ping` should answer. Then plug the board's
native USB port into the target and watch the LED (see [Build](03-build.md)).

The node opens the serial port without toggling the DTR and RTS lines. On some USB-serial boards
those lines are wired to reset, so toggling them restarts the board every time the port opens.

## Serial protocol (for developers)
115200 baud, 8N1. The node sends one command per line and the board answers with one line,
`OK [data]` or `ERR <reason>`. On boot it prints `READY mcping-hid <version>` once.

| Command | Meaning |
|---|---|
| `PING`, `VER` | liveness, firmware version |
| `LINK` | whether the target's USB link is up (`linked`, `suspended`, `unplugged`) |
| `ABORT` | release every key and mouse button now |
| `DELAY <ms>` | gap between typed characters |
| `TYPE <text>` | type text; the text is percent-encoded (`%20` space, `%0A` newline, `%25` percent) |
| `KEYS <combo>` | press and release a chord such as `ctrl+alt+del` or `win+r` |
| `MOVE x y`, `CLICK [button] [x y]`, `DOWN`, `UP`, `SCROLL n` | pointer control |

The mouse is absolute: X and Y run from 0 to 32767 across the target's full screen. Typing assumes a
US keyboard layout on the target. Whether a BIOS or boot menu accepts this keyboard is untested.

## Next
[Connect a client](06-connect.md)
