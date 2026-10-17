# 7. Using it

> **Non-production, testing only, no warranty.** Read [DISCLAIMER.md](../DISCLAIMER.md) first.
> Only use it on computers you own or are explicitly allowed to control, and supervise it.

## A sensible way to work
1. Tell the person who owns the target what is about to happen.
2. Put a monitor on the capture card's loop-out, or open the live monitor, so a human can watch.
3. Ask the model for small steps and review what it does, especially anything that changes settings,
   sends something, spends money or deletes data.
4. Keep a way to stop at hand: the `abort` tool releases every key and button, and revoking the
   client's token cuts it off.

## Tools

**Seeing**
| Tool | Purpose |
|---|---|
| `screenshot` | a scaled picture of the target's screen |
| `zoom` | a full-resolution crop of a region |
| `describe_screen` | a **text** description of the windows and controls (no image needed) |
| `screen_changes` | only what changed since the last description |
| `read_element`, `find_elements`, `wait_for_text` | read one control more carefully, search the last description, wait for text to appear |
| `text_zoom` | enlarge (or reset) the text in the focused window so small text reads better |

**Acting**
| Tool | Purpose |
|---|---|
| `click_element`, `type_into` | click a control, or fill a field, by the ID `describe_screen` gave it |
| `type_text`, `key_combo` | type text, press a chord such as `ctrl+alt+del` |
| `mouse_move`, `mouse_click`, `mouse_drag`, `mouse_scroll` | pointer control by pixel |

**Control**
| Tool | Purpose |
|---|---|
| `wait` | pause |
| `abort` | release everything held, immediately |
| `status` | device health: service, ESP32 link, target USB link, capture card, HDMI signal |

Models that can read images can use the image tools. Models that cannot should use `describe_screen`
and the element tools.

## What `describe_screen` returns
A compact text tree, one control per line, with IDs that stay the same between reads for controls that
did not move:

```
SCREEN 1920x1080
WINDOW w1 "Connection Properties" active
  TEXTBOX e3 label="Server name:" value="srv-sql-01"
  CHECKBOX e12 "Trust server certificate" unchecked
  BUTTON e17 "OK" default
  STATUSBAR e19 "Ready"
```

Items in brackets (menu entries, tabs, list rows) have their own IDs. A `?` after a kind means the
classification is uncertain. Dense monospace text such as a terminal is read by matching glyphs and
returned as numbered lines. A `TEXT QUALITY` line says how much of the text was read with
confidence; below 60% the tool tells the model to check with a screenshot or zoom.

## How accurate is it?
These are one author's measurements on small samples, not a benchmark:
- A clean Windows Forms test dialog: all 25 controls found and all values right from the file; 23 of 25
  controls and 22 of 25 values through the real capture card.
- Real Linux Mint screens with small, dark, 12-pixel text: about 59% of words exactly right, and
  digits (IP addresses, sizes) were the weakest.
- Enlarging terminal text with `text_zoom` raised terminal accuracy to about 90% by OCR, and glyph
  matching reached almost all of the tested text.

**Always check numbers that matter** (an address, an amount, a filename) with `zoom` or a screenshot.

## Known limits
- Custom-drawn interfaces (games, canvases, some web and Electron apps) and unlabelled icons come out as
  plain text or not at all.
- The parser is tuned for standard Windows controls in a light theme. Dark themes and other desktops are
  read as text but windows and controls are often not recognised.
- Small text under about 10 pixels and short words on low-contrast buttons are misread.
- Typing assumes a US layout on the target.
- Only one client can drive the device at a time; a second is refused until the first disconnects or has
  been idle for 5 minutes.

## Settings that change reading
`MCPING_OCR_PASSES` (default `raw,gamma,clahe`; `raw` alone is about twice as fast),
`MCPING_CAPTURE_FORMAT` (`yuyv422` default, `mjpeg` is faster), `MCPING_OCR_TILE=0` (turn off tiled
reading). A poor first reading triggers extra captures with other card settings that vote; this costs
a few seconds.

## Next
[SECURITY.md](../SECURITY.md) · [Troubleshooting](troubleshooting.md)
