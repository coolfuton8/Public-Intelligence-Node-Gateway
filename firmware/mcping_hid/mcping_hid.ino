// MCPing HID firmware for ESP32-S3 (dual USB-C DevKitC-1 style boards).
//
//  - Native USB port  -> target computer: keyboard + absolute-position mouse.
//  - UART port        -> node: line-based text commands.
//
// Build with arduino-cli:
//   FQBN esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=16M
// USBMode=default selects the TinyUSB stack; CDCOnBoot=default keeps Serial on UART0,
// which is the UART port wired to the USB-serial chip.

#include "USB.h"
#include "USBHID.h"
#include "USBHIDKeyboard.h"
#include "tusb.h"  // tud_mounted() / tud_suspended() for the status LED

#define FW_NAME "mcping-hid"
#define FW_VERSION "0.2.2"

static const uint8_t ABS_MOUSE_REPORT_ID = 8;
static const uint16_t ABS_MAX = 32767;

// Absolute mouse: 3 buttons, 16-bit X/Y (0..32767), signed 8-bit wheel.
static const uint8_t absMouseDescriptor[] = {
  0x05, 0x01,                 // Usage Page (Generic Desktop)
  0x09, 0x02,                 // Usage (Mouse)
  0xA1, 0x01,                 // Collection (Application)
  0x85, ABS_MOUSE_REPORT_ID,  //   Report ID
  0x09, 0x01,                 //   Usage (Pointer)
  0xA1, 0x00,                 //   Collection (Physical)
  0x05, 0x09,                 //     Usage Page (Buttons)
  0x19, 0x01,                 //     Usage Minimum (1)
  0x29, 0x03,                 //     Usage Maximum (3)
  0x15, 0x00,                 //     Logical Minimum (0)
  0x25, 0x01,                 //     Logical Maximum (1)
  0x95, 0x03,                 //     Report Count (3)
  0x75, 0x01,                 //     Report Size (1)
  0x81, 0x02,                 //     Input (Data,Var,Abs)
  0x95, 0x01,                 //     Report Count (1)
  0x75, 0x05,                 //     Report Size (5)
  0x81, 0x03,                 //     Input (Const,Var,Abs) padding
  0x05, 0x01,                 //     Usage Page (Generic Desktop)
  0x09, 0x30,                 //     Usage (X)
  0x09, 0x31,                 //     Usage (Y)
  0x16, 0x00, 0x00,           //     Logical Minimum (0)
  0x26, 0xFF, 0x7F,           //     Logical Maximum (32767)
  0x75, 0x10,                 //     Report Size (16)
  0x95, 0x02,                 //     Report Count (2)
  0x81, 0x02,                 //     Input (Data,Var,Abs)
  0x09, 0x38,                 //     Usage (Wheel)
  0x15, 0x81,                 //     Logical Minimum (-127)
  0x25, 0x7F,                 //     Logical Maximum (127)
  0x75, 0x08,                 //     Report Size (8)
  0x95, 0x01,                 //     Report Count (1)
  0x81, 0x06,                 //     Input (Data,Var,Rel)
  0xC0,                       //   End Collection
  0xC0                        // End Collection
};

USBHID HID;
USBHIDKeyboard Keyboard;

class AbsMouseDevice : public USBHIDDevice {
 public:
  AbsMouseDevice() {
    static bool registered = false;
    if (!registered) {
      registered = true;
      HID.addDevice(this, sizeof(absMouseDescriptor));
    }
  }
  void begin() { HID.begin(); }
  uint16_t _onGetDescriptor(uint8_t *buffer) {
    memcpy(buffer, absMouseDescriptor, sizeof(absMouseDescriptor));
    return sizeof(absMouseDescriptor);
  }
  void send() {
    uint8_t r[6] = {buttons, (uint8_t)(x & 0xFF), (uint8_t)(x >> 8),
                    (uint8_t)(y & 0xFF), (uint8_t)(y >> 8), (uint8_t)wheel};
    HID.SendReport(ABS_MOUSE_REPORT_ID, r, sizeof(r));
    wheel = 0;  // the wheel value is a one-shot delta
  }
  uint8_t buttons = 0;
  uint16_t x = ABS_MAX / 2;
  uint16_t y = ABS_MAX / 2;
  int8_t wheel = 0;
};

AbsMouseDevice Mouse;

static uint16_t typeDelayMs = 8;   // gap between characters when typing
static uint16_t clickHoldMs = 40;  // how long a click or chord is held

// ---- Status LED (on-board WS2812 RGB) ----
//   steady GREEN : USB link to the target is up
//   steady RED   : target not connected (unplugged, powered off or suspended)
//   BLUE flash   : a command was received from the MCPing device (receive)
//   YELLOW flash : a key or mouse report is being sent to the target (send)
#ifndef LED_PIN
#define LED_PIN 48  // DevKitC-1 v1.0 clones; v1.1 boards put the LED on GPIO 38
#endif
static const uint8_t LED_LEVEL = 24;  // the LED is very bright; keep it dim
static uint32_t ledFlashUntil = 0;

static bool targetLinked() { return tud_mounted() && !tud_suspended(); }

static void ledColor(uint8_t r, uint8_t g, uint8_t b) { neopixelWrite(LED_PIN, r, g, b); }

static void ledBase() {
  if (targetLinked()) ledColor(0, LED_LEVEL, 0);
  else ledColor(LED_LEVEL, 0, 0);
}

static void ledFlash(uint8_t r, uint8_t g, uint8_t b, uint16_t ms) {
  ledColor(r, g, b);
  ledFlashUntil = millis() + ms;
}

static void ledRx() { ledFlash(0, 0, LED_LEVEL * 2, 60); }
static void ledTx() { ledFlash(LED_LEVEL * 2, LED_LEVEL * 2, 0, 80); }  // yellow

// Call from loop(): returns the LED to the state colour once a flash has expired.
static void ledService() {
  static uint32_t lastBase = 0;
  uint32_t now = millis();
  if ((int32_t)(now - ledFlashUntil) >= 0 && now - lastBase > 100) {
    lastBase = now;
    ledBase();
  }
}

static int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Decode %XX escapes. Returns false on a malformed escape.
static bool percentDecode(const String &in, String &out) {
  out = "";
  for (size_t i = 0; i < in.length(); i++) {
    char c = in[i];
    if (c != '%') {
      out += c;
      continue;
    }
    if (i + 2 >= in.length()) return false;  // needs two hex digits after '%'
    int hi = hexNibble(in[i + 1]);
    int lo = hexNibble(in[i + 2]);
    if (hi < 0 || lo < 0) return false;
    out += (char)((hi << 4) | lo);
    i += 2;
  }
  return true;
}

static uint8_t keyByName(String n, bool &ok) {
  n.toLowerCase();
  ok = true;
  if (n == "ctrl" || n == "control") return KEY_LEFT_CTRL;
  if (n == "shift") return KEY_LEFT_SHIFT;
  if (n == "alt") return KEY_LEFT_ALT;
  if (n == "win" || n == "gui" || n == "super" || n == "meta" || n == "cmd") return KEY_LEFT_GUI;
  if (n == "rctrl") return KEY_RIGHT_CTRL;
  if (n == "rshift") return KEY_RIGHT_SHIFT;
  if (n == "ralt" || n == "altgr") return KEY_RIGHT_ALT;
  if (n == "enter" || n == "return") return KEY_RETURN;
  if (n == "esc" || n == "escape") return KEY_ESC;
  if (n == "tab") return KEY_TAB;
  if (n == "backspace") return KEY_BACKSPACE;
  if (n == "delete" || n == "del") return KEY_DELETE;
  if (n == "insert" || n == "ins") return KEY_INSERT;
  if (n == "home") return KEY_HOME;
  if (n == "end") return KEY_END;
  if (n == "pageup" || n == "pgup") return KEY_PAGE_UP;
  if (n == "pagedown" || n == "pgdn") return KEY_PAGE_DOWN;
  if (n == "up") return KEY_UP_ARROW;
  if (n == "down") return KEY_DOWN_ARROW;
  if (n == "left") return KEY_LEFT_ARROW;
  if (n == "right") return KEY_RIGHT_ARROW;
  if (n == "capslock") return KEY_CAPS_LOCK;
  if (n == "space") return ' ';
  if (n.length() >= 2 && n[0] == 'f') {
    int f = n.substring(1).toInt();
    if (f >= 1 && f <= 12) return KEY_F1 + (f - 1);
  }
  if (n.length() == 1) return (uint8_t)n[0];
  ok = false;
  return 0;
}

static void releaseAll() {
  Keyboard.releaseAll();
  Mouse.buttons = 0;
  Mouse.send();
}

static void tapKey(uint8_t k) {
  Keyboard.press(k);
  delay(typeDelayMs);
  Keyboard.release(k);
}

static void typeText(const String &s) {
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '\n' || c == '\r') {
      if (c == '\r' && i + 1 < s.length() && s[i + 1] == '\n') continue;
      tapKey(KEY_RETURN);
    } else if (c == '\t') {
      tapKey(KEY_TAB);
    } else {
      Keyboard.write((uint8_t)c);
    }
    delay(typeDelayMs);
  }
}

// "ctrl+alt+del", "win+r", "f5" ...
static bool pressCombo(const String &combo, String &err) {
  uint8_t keys[8];
  int n = 0;
  int start = 0;
  while (n < 8) {
    int plus = combo.indexOf('+', start);
    String tok = (plus < 0) ? combo.substring(start) : combo.substring(start, plus);
    tok.trim();
    if (tok.length() == 0) { err = "empty key"; return false; }
    bool ok;
    uint8_t k = keyByName(tok, ok);
    if (!ok) { err = "unknown key: " + tok; return false; }
    keys[n++] = k;
    if (plus < 0) break;
    start = plus + 1;
  }
  for (int i = 0; i < n; i++) {
    Keyboard.press(keys[i]);
    delay(typeDelayMs);
  }
  delay(clickHoldMs);
  Keyboard.releaseAll();
  return true;
}

static uint8_t buttonMask(String b, bool &ok) {
  b.toLowerCase();
  ok = true;
  if (b == "left" || b == "l") return 1;
  if (b == "right" || b == "r") return 2;
  if (b == "middle" || b == "m") return 4;
  ok = false;
  return 0;
}

static bool parseCoord(const String &s, uint16_t &out) {
  if (s.length() == 0) return false;
  long v = s.toInt();
  if (v < 0 || v > ABS_MAX) return false;
  out = (uint16_t)v;
  return true;
}

// idx-th space-separated token of line ("" if missing).
static String token(const String &line, int idx) {
  int pos = 0, cur = 0;
  while (true) {
    while (pos < (int)line.length() && line[pos] == ' ') pos++;
    if (pos >= (int)line.length()) return "";
    int e = line.indexOf(' ', pos);
    if (e < 0) e = line.length();
    if (cur == idx) return line.substring(pos, e);
    cur++;
    pos = e;
  }
}

// Everything after the first fromIdx tokens.
static String restOfLine(const String &line, int fromIdx) {
  int pos = 0;
  for (int cur = 0; cur < fromIdx; cur++) {
    while (pos < (int)line.length() && line[pos] == ' ') pos++;
    int e = line.indexOf(' ', pos);
    if (e < 0) return "";
    pos = e;
  }
  while (pos < (int)line.length() && line[pos] == ' ') pos++;
  return line.substring(pos);
}

static void handle(const String &line) {
  String cmd = token(line, 0);
  cmd.toUpperCase();

  if (cmd == "PING") { Serial.println("OK PONG"); return; }
  if (cmd == "VER")  { Serial.println("OK " FW_NAME " " FW_VERSION); return; }
  // Is the target computer's USB link up? Same state the status LED shows (green / red).
  if (cmd == "LINK") {
    Serial.println(!tud_mounted() ? "OK unplugged" : (tud_suspended() ? "OK suspended" : "OK linked"));
    return;
  }

  // Everything below sends reports to the target: show the "send" flash.
  if (cmd == "TYPE" || cmd == "KEYS" || cmd == "MOVE" || cmd == "CLICK" || cmd == "DOWN" ||
      cmd == "UP" || cmd == "SCROLL" || cmd == "ABORT") {
    ledTx();
  }

  if (cmd == "ABORT") { releaseAll(); Serial.println("OK"); return; }

  if (cmd == "DELAY") {
    long v = token(line, 1).toInt();
    if (v < 0 || v > 1000) { Serial.println("ERR delay 0..1000"); return; }
    typeDelayMs = (uint16_t)v;
    Serial.println("OK");
    return;
  }

  if (cmd == "TYPE") {  // TYPE <percent-encoded text>
    String dec;
    if (!percentDecode(restOfLine(line, 1), dec)) { Serial.println("ERR bad escape"); return; }
    typeText(dec);
    Serial.println("OK");
    return;
  }

  if (cmd == "KEYS") {  // KEYS ctrl+alt+del
    String err;
    if (!pressCombo(token(line, 1), err)) { Serial.println("ERR " + err); return; }
    Serial.println("OK");
    return;
  }

  if (cmd == "MOVE") {  // MOVE x y   (0..32767)
    uint16_t x, y;
    if (!parseCoord(token(line, 1), x) || !parseCoord(token(line, 2), y)) {
      Serial.println("ERR coords 0..32767");
      return;
    }
    Mouse.x = x;
    Mouse.y = y;
    Mouse.send();
    Serial.println("OK");
    return;
  }

  if (cmd == "CLICK") {  // CLICK [left|right|middle] [x y]
    String b = token(line, 1);
    uint8_t mask = 1;
    int coordIdx = 1;
    if (b.length() && !isDigit(b[0])) {
      bool ok;
      mask = buttonMask(b, ok);
      if (!ok) { Serial.println("ERR button"); return; }
      coordIdx = 2;
    }
    String xs = token(line, coordIdx), ys = token(line, coordIdx + 1);
    if (xs.length() || ys.length()) {
      uint16_t x, y;
      if (!parseCoord(xs, x) || !parseCoord(ys, y)) { Serial.println("ERR coords 0..32767"); return; }
      Mouse.x = x;
      Mouse.y = y;
      Mouse.send();
      delay(15);
    }
    Mouse.buttons = mask;
    Mouse.send();
    delay(clickHoldMs);
    Mouse.buttons = 0;
    Mouse.send();
    Serial.println("OK");
    return;
  }

  if (cmd == "DOWN" || cmd == "UP") {  // DOWN left / UP left
    bool ok;
    uint8_t mask = buttonMask(token(line, 1), ok);
    if (!ok) { Serial.println("ERR button"); return; }
    if (cmd == "DOWN") Mouse.buttons |= mask;
    else Mouse.buttons &= ~mask;
    Mouse.send();
    Serial.println("OK");
    return;
  }

  if (cmd == "SCROLL") {  // SCROLL n  (-127..127, positive = up)
    long v = token(line, 1).toInt();
    if (v < -127 || v > 127) { Serial.println("ERR scroll -127..127"); return; }
    Mouse.wheel = (int8_t)v;
    Mouse.send();
    Serial.println("OK");
    return;
  }

  Serial.println("ERR unknown command");
}

static String lineBuf;

void setup() {
  Serial.begin(115200);  // UART0 -> USB-serial chip -> MCPing device
  Keyboard.begin();
  Mouse.begin();
  USB.productName("MCPing HID");
  USB.manufacturerName("MCPing");
  USB.begin();
  lineBuf.reserve(1100);
  ledColor(LED_LEVEL, 0, 0);  // red until the target mounts us
  Serial.println("READY " FW_NAME " " FW_VERSION);
}

void loop() {
  ledService();
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (lineBuf.length()) {
        ledRx();
        delay(15);  // make the receive flash visible
        handle(lineBuf);
      }
      lineBuf = "";
    } else if (lineBuf.length() < 1050) {
      lineBuf += c;
    } else {
      lineBuf = "";
      Serial.println("ERR line too long");
    }
  }
}
