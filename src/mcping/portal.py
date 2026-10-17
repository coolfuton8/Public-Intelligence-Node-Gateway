"""MCPing setup portal: web pages on port 443 that explain how to connect different AI tools to
this device, with the setup scripts downloadable from those pages.

    https://mcping.local/

The ordinary pages hold no secrets. Every script is generated for the address it was requested
through and carries the (public) device CA with its fingerprint; the user types the device token
when running it. The one exception is /join/<code>: a valid one-time invite (see invites.py) is
answered with the setup scripts already containing the token, once.
Uses the same certificate as the MCP endpoint and restarts when that certificate changes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import clients, invites, tls

FILES = Path(__file__).parent / "portal_files"
MCP_PORT = os.environ.get("MCPING_MCP_PORT", "8765")
_HOST_OK = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,61}[A-Za-z0-9])?|\d{1,3}(?:\.\d{1,3}){3})$")

# name -> (content type, templated?)
DOWNLOADS = {
    "mcping-ca.crt": "application/x-x509-ca-cert",
    "setup-claude-code.ps1": "text/plain; charset=utf-8",
    "setup-claude-code.sh": "text/plain; charset=utf-8",
    "setup-opencode.ps1": "text/plain; charset=utf-8",
    "setup-opencode.sh": "text/plain; charset=utf-8",
    "Modelfile.mcping-qwen3": "text/plain; charset=utf-8",
    "mcping.mcp.json": "application/json",
    "opencode.mcping.json": "application/json",
}


def ca_pem() -> str:
    return tls.paths()["ca_crt"].read_text().strip()


def ca_fingerprint(pem: str | None = None) -> str:
    cert = x509.load_pem_x509_certificate((pem or ca_pem()).encode())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def address_for(request: Request) -> str:
    """The name or IP the visitor used (never trusted blindly: it ends up inside scripts)."""
    host = request.headers.get("host", "").rsplit(":", 1)[0] if ":" in request.headers.get("host", "") \
        else request.headers.get("host", "")
    if not host or host in ("localhost",) or host.startswith("127.") or not _HOST_OK.match(host):
        return "mcping.local"
    return host


def render_file(name: str, address: str, token: str = "") -> str:
    """A script or config with the device details filled in. The token is filled in only for a
    visitor holding a valid one-time invite; otherwise the script asks for it."""
    text = (FILES / name).read_text(encoding="utf-8")
    for marker, fname in (("@@BRIDGE_B64@@", "mcping-bridge.py"), ("@@BRIDGE_PS_B64@@", "mcping-bridge.ps1")):
        if marker in text:             # the stdio bridge travels inside the script
            blob = base64.b64encode((FILES / fname).read_bytes()).decode("ascii")
            text = text.replace(marker, blob)
    pem = ca_pem()
    return (text.replace("@@ADDRESS@@", address).replace("@@PORT@@", MCP_PORT)
            .replace("@@CA_PEM@@", pem).replace("@@FINGERPRINT@@", ca_fingerprint(pem))
            .replace("@@TOKEN@@", token))


# --- pages -------------------------------------------------------------------------------------
CSS = """
:root { --bg:#f6f7f9; --panel:#fff; --ink:#1b1f24; --mute:#5f6b7a; --line:#d8dde4; --accent:#1f6feb; --code:#eef1f5; --warn:#8a5a00; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151a; --panel:#1b2027; --ink:#e6e9ee; --mute:#9aa6b6;
        --line:#323a45; --accent:#58a6ff; --code:#252c35; --warn:#e3b341; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.55 system-ui,"Segoe UI",sans-serif; }
header { border-bottom:1px solid var(--line); background:var(--panel); }
.wrap { max-width:860px; margin:0 auto; padding:0 16px; }
header .wrap { display:flex; flex-wrap:wrap; gap:6px 20px; align-items:baseline; padding:12px 16px; }
header a { color:var(--ink); text-decoration:none; font-weight:700; }
header span { color:var(--mute); font-size:14px; }
main { padding:20px 16px 60px; }
h1 { font-size:26px; margin:6px 0 4px; } h2 { font-size:19px; margin:30px 0 6px; } p { margin:8px 0; }
.lead { color:var(--mute); }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; margin-top:18px; }
.card { display:block; background:var(--panel); border:1px solid var(--line); border-radius:10px;
        padding:16px; color:var(--ink); text-decoration:none; }
.card:hover { border-color:var(--accent); }
.card b { display:block; font-size:18px; margin-bottom:4px; } .card span { color:var(--mute); font-size:14.5px; }
a { color:var(--accent); }
.step { display:flex; gap:12px; margin:16px 0; } .num { flex:none; width:28px; height:28px; border-radius:50%;
        background:var(--accent); color:#fff; font-weight:700; text-align:center; line-height:28px; }
pre { background:var(--code); border:1px solid var(--line); border-radius:6px; padding:10px 12px; overflow-x:auto;
      font:13.5px/1.5 ui-monospace,Consolas,monospace; margin:8px 0; }
code { background:var(--code); border-radius:4px; padding:1px 5px; font:13.5px ui-monospace,Consolas,monospace; }
pre code { background:none; padding:0; }
.dl { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; }
.dl a { background:var(--accent); color:#fff; text-decoration:none; padding:7px 13px; border-radius:6px; font-weight:600;
        font-size:15px; }
.dl a.alt { background:var(--panel); color:var(--accent); border:1px solid var(--accent); }
.note { border-left:4px solid var(--warn); background:var(--panel); padding:8px 12px; margin:14px 0; border-radius:0 6px 6px 0; }
table { border-collapse:collapse; width:100%; } td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line);
        vertical-align:top; font-size:15px; } th { color:var(--mute); font-weight:600; }
.fp { word-break:break-all; font:13px ui-monospace,Consolas,monospace; }
"""


def _layout(title: str, body: str, address: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>{CSS}</style></head><body>
<header><div class="wrap"><a href="/">MCP Intelligence Node Gateway (MCPing)</a><span>device: {html.escape(address)}</span></div></header>
<main><div class="wrap">{body}</div></main></body></html>"""


def _dl(name: str, label: str | None = None, alt: bool = False) -> str:
    return f'<a class="{"alt" if alt else ""}" href="/downloads/{name}" download>{html.escape(label or name)}</a>'


def _facts(address: str) -> str:
    return f"""<table>
<tr><th>MCP server URL</th><td><code>https://{html.escape(address)}:{MCP_PORT}/mcp</code></td></tr>
<tr><th>Transport</th><td>streamable HTTP</td></tr>
<tr><th>Authentication</th><td>header <code>Authorization: Bearer &lt;token&gt;</code></td></tr>
<tr><th>Token</th><td>On the device: open <b>MCPing Connect</b> and press <b>Copy token only</b></td></tr>
<tr><th>Certificate</th><td>Signed by the device's own CA (the scripts install it for you)</td></tr>
</table>"""


def index_page(address: str) -> str:
    body = """
<h1>MCP Intelligence Node Gateway (MCPing)</h1>
<p class="lead">Connect an AI to this computer-control device. MCPing lets an AI see and operate the computer attached to this device. Pick the AI tool
you want to use. Each page has a script to download and the exact commands to run.</p>
<div class="cards">
  <a class="card" href="/claude-code"><b>Claude Code</b><span>One script registers the device as an MCP
  server. Windows, macOS and Linux.</span></a>
  <a class="card" href="/opencode-ollama"><b>OpenCode + Ollama</b><span>Run a local model on your own machine and
  let it use the device. Windows, macOS and Linux.</span></a>
  <a class="card" href="/other"><b>Any other MCP client</b><span>The raw connection details: URL, header and
  certificate.</span></a>
  <a class="card" href="/manual"><b>Scripts blocked?</b><span>The same setup as plain commands, or with no
  commands at all.</span></a>
</div>
<h2>Before you start</h2>
<p><b>Have a one-time invite?</b> On the device, open <b>MCPing Connect</b> and press <b>Create invite</b>,
then <a href="/join">enter the code here</a>: the setup scripts you get already contain the token, so
nothing needs to be typed or pasted.</p>
<p>Otherwise you need the device's <b>access token</b>. On the device itself, open <b>MCPing Connect</b>
and press <b>Copy token only</b>. The scripts ask you to paste it; it is not stored on this web page.</p>
<div class="note">This page uses the device's own certificate, so a browser that has not installed the device CA
shows a security warning the first time. That is expected. Download <a href="/downloads/mcping-ca.crt">the CA</a>
and compare its fingerprint with the one in the <b>MCPing Certificate</b> app on the device.</div>
<p class="fp">CA SHA-256 fingerprint: <b>""" + ca_fingerprint() + """</b></p>
<p><a href="https://""" + html.escape(address) + ":" + MCP_PORT + """/monitor">Live monitor</a> (what the AI is doing,
signs in with the token).</p>"""
    return _layout("MCP Intelligence Node Gateway (MCPing)", body, address)


def claude_page(address: str) -> str:
    body = f"""
<h1>Claude Code</h1>
<p class="lead">Registers the device as an MCP server for Claude Code on this computer.</p>
{_facts(address)}
<div class="step"><div class="num">1</div><div><b>Download the script for your computer</b>
<div class="dl">{_dl("setup-claude-code.ps1", "Windows (PowerShell)")}{_dl("setup-claude-code.sh", "macOS / Linux")}
{_dl("mcping-ca.crt", "Certificate only", alt=True)}</div>
<p>Claude Code must already be installed (the <code>claude</code> command). Scripts blocked on this computer?
See <a href="/manual">the same setup as plain commands</a>.</p></div></div>
<div class="step"><div class="num">2</div><div><b>Run it</b> and paste the token when asked.
<p>Windows, in the folder you downloaded it to:</p>
<pre><code>powershell -ExecutionPolicy Bypass -File .\\setup-claude-code.ps1</code></pre>
<p class="fp">This command lets this one run past the PowerShell script policy (it changes no setting). If your
organisation blocks scripts on purpose, do not work around it: use <a href="/manual">the commands without a
script</a>, or ask an administrator.</p>
<p>macOS / Linux:</p>
<pre><code>bash setup-claude-code.sh</code></pre>
<p>It saves the device certificate, tells Claude Code to trust it and registers the server named
<code>mcping</code>. Run it again any time; it replaces the old entry.</p></div></div>
<div class="step"><div class="num">3</div><div><b>Check it</b>
<pre><code>claude mcp list</code></pre>
<p>The <code>mcping</code> line should end with <code>Connected</code>. Then start <code>claude</code> and ask:
<i>"Use the mcping tools to take a screenshot of the screen."</i> Models that cannot read images
should use <code>describe_screen</code>.</p></div></div>
<h2>If something is wrong</h2>
<table>
<tr><th><code>UNABLE_TO_VERIFY_LEAF_SIGNATURE</code></th><td>Claude Code was started before the script ran. Close
all Claude Code windows and open a new one.</td></tr>
<tr><th>Cannot resolve <code>{html.escape(address)}</code></th><td>The name only works on the device's own network. Re-run the
script with the device's IP address: <code>-Address 192.0.2.x</code> (Windows) or
<code>MCPING_ADDRESS=192.0.2.x bash setup-claude-code.sh</code>.</td></tr>
<tr><th>401 / unauthorized</th><td>Wrong token. Copy it again on the device and re-run.</td></tr>
<tr><th>Screen is black</th><td>The target computer is not connected to the capture card's HDMI input, or it is asleep.</td></tr>
</table>
<h2>Doing it by hand</h2>
<pre><code>claude mcp add --transport http --scope user mcping https://{html.escape(address)}:{MCP_PORT}/mcp --header "Authorization: Bearer &lt;token&gt;"</code></pre>
<p>…after pointing <code>NODE_EXTRA_CA_CERTS</code> at the downloaded certificate file.</p>"""
    return _layout("Claude Code - MCP Intelligence Node Gateway (MCPing)", body, address)


def opencode_page(address: str) -> str:
    body = f"""
<h1>OpenCode + Ollama</h1>
<p class="lead">OpenCode is the coding agent, Ollama runs the model on your own machine, and the device
provides the screen, keyboard and mouse tools.</p>
<div class="note">The Claude Code path has been tested end to end. These OpenCode and Ollama scripts follow
the documented OpenCode configuration format but have not yet been run against a real OpenCode install; if
something does not connect, the manual section below shows exactly what they set.</div>
{_facts(address)}
<div class="step"><div class="num">1</div><div><b>Install Ollama and get a model</b>
<p>Install Ollama from <a href="https://ollama.com/download">ollama.com/download</a>, then pull a model that
supports tool calling and give it a larger context window (the default is too small for tool use):</p>
<div class="dl">{_dl("Modelfile.mcping-qwen3", "Modelfile (Qwen3 8B, 16k context)")}</div>
<pre><code>ollama pull qwen3:8b
ollama create qwen3-mcping -f Modelfile.mcping-qwen3</code></pre>
<p>Any other tool-capable model works too; pass its name to the script in step 3.</p></div></div>
<div class="step"><div class="num">2</div><div><b>Install OpenCode</b>
<p>See <a href="https://opencode.ai">opencode.ai</a> for the installer for your system.</p></div></div>
<div class="step"><div class="num">3</div><div><b>Download and run the setup script</b>
<div class="dl">{_dl("setup-opencode.ps1", "Windows (PowerShell)")}{_dl("setup-opencode.sh", "macOS / Linux")}
{_dl("mcping-ca.crt", "Certificate only", alt=True)}</div>
<p>Windows:</p>
<pre><code>powershell -ExecutionPolicy Bypass -File .\\setup-opencode.ps1 -Model qwen3-mcping</code></pre>
<p class="fp">This command lets this one run past the PowerShell script policy (it changes no setting). If your
organisation blocks scripts on purpose, do not work around it: use <a href="/manual">the commands without a
script</a>, or ask an administrator.</p>
<p>macOS / Linux:</p>
<pre><code>bash setup-opencode.sh --model qwen3-mcping</code></pre>
<p>It asks for the token, saves and trusts the device certificate, stores the token in the
<code>MCPING_TOKEN</code> environment variable, and merges the device and an Ollama provider into OpenCode's
config (an existing config is backed up as <code>opencode.json.bak</code>). Use <code>-NoOllama</code> /
<code>--no-ollama</code> to add only the device. Scripts blocked? See <a href="/manual">the manual steps</a>.</p></div></div>
<div class="step"><div class="num">4</div><div><b>Use it</b>
<p>Open a <b>new</b> terminal (so the environment variables are picked up), make sure Ollama is running, then:</p>
<pre><code>opencode mcp list      # mcping should be listed
opencode</code></pre>
<p>Ask it to use the <code>mcping</code> tools. A local model without vision should use
<code>describe_screen</code> and <code>click_element</code>; they work from text only.</p></div></div>
<h2>What the script sets (by hand)</h2>
<p>In <code>~/.config/opencode/opencode.json</code> (on Windows <code>%USERPROFILE%\\.config\\opencode\\opencode.json</code>):</p>
<pre><code>{{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {{
    "mcping": {{
      "type": "remote",
      "url": "https://{html.escape(address)}:{MCP_PORT}/mcp",
      "enabled": true,
      "headers": {{ "Authorization": "Bearer {{env:MCPING_TOKEN}}" }}
    }}
  }},
  "provider": {{
    "ollama": {{
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (local)",
      "options": {{ "baseURL": "http://localhost:11434/v1" }},
      "models": {{ "qwen3-mcping": {{ "name": "qwen3-mcping" }} }}
    }}
  }},
  "model": "ollama/qwen3-mcping"
}}</code></pre>
<p>…plus the environment variables <code>MCPING_TOKEN</code> (the device token) and
<code>NODE_EXTRA_CA_CERTS</code> (path to the downloaded <code>mcping-ca.crt</code>).</p>
<h2>If something is wrong</h2>
<table>
<tr><th>Certificate errors</th><td>OpenCode does not trust the device CA. Make sure the terminal is new and
<code>NODE_EXTRA_CA_CERTS</code> is set. If it still fails, install <code>mcping-ca.crt</code> in your operating
system's trusted certificates.</td></tr>
<tr><th>The model ignores the tools or stops midway</th><td>Check the context window (<code>ollama show</code>) and try a
larger model; small models struggle with long tool lists.</td></tr>
<tr><th>Cannot resolve <code>{html.escape(address)}</code></th><td>Re-run the script with the device's IP address
(<code>-Address</code> / <code>MCPING_ADDRESS=</code>).</td></tr>
</table>"""
    return _layout("OpenCode + Ollama - MCP Intelligence Node Gateway (MCPing)", body, address)


def manual_page(address: str) -> str:
    a = html.escape(address)
    url = f"https://{a}:{MCP_PORT}/mcp"
    body = f"""
<h1>No scripts? Do it by hand</h1>
<p class="lead">If a policy or security tool blocks the setup scripts, the same result takes three things:
the certificate file, two environment settings, and one MCP registration. Nothing below needs a script
file to run. Use <code>&lt;token&gt;</code> for the device token (on the device: <b>MCPing Connect &gt;
Copy token only</b>).</p>
<div class="note">If even typed commands or environment changes are restricted, send this page to whoever
administers the computer. The only things that change are one certificate file, two user environment
variables and one MCP server entry for the AI tool.</div>

<h2>Step 1 (everyone): get the certificate file</h2>
<div class="dl">{_dl("mcping-ca.crt", "Download mcping-ca.crt")}</div>
<p>Save it somewhere permanent, for example <code>C:\\Users\\&lt;you&gt;\\.mcping\\mcping-ca.crt</code> or
<code>~/.mcping/mcping-ca.crt</code>. Its SHA-256 fingerprint should be
<span class="fp">{ca_fingerprint()}</span>.</p>

<h2>Claude Code, with typed commands</h2>
<p><b>Windows</b> (a Command Prompt window, not PowerShell; adjust the path to where you saved the file):</p>
<pre><code>setx NODE_EXTRA_CA_CERTS "C:\\Users\\&lt;you&gt;\\.mcping\\mcping-ca.crt"</code></pre>
<p>Close the window and open a new Command Prompt, then:</p>
<pre><code>claude mcp add --transport http --scope user mcping {url} --header "Authorization: Bearer &lt;token&gt;"
claude mcp list</code></pre>
<p><b>macOS / Linux</b>:</p>
<pre><code>export NODE_EXTRA_CA_CERTS="$HOME/.mcping/mcping-ca.crt"
claude mcp add --transport http --scope user mcping {url} --header "Authorization: Bearer &lt;token&gt;"
claude mcp list</code></pre>
<p>To keep the <code>export</code> line, add it to <code>~/.zshrc</code> or <code>~/.bashrc</code>.</p>

<h2>Claude Code, with no commands (settings dialog and a file)</h2>
<ol>
<li><b>Environment variables.</b> Windows: Start menu, type <i>Edit environment variables for your account</i>,
then <b>New...</b> twice: <code>NODE_EXTRA_CA_CERTS</code> = the path of <code>mcping-ca.crt</code>, and
<code>MCPING_TOKEN</code> = the device token. macOS: add the same two <code>export</code> lines to your shell
profile.</li>
<li><b>The server file.</b> Download this, rename it to exactly <code>.mcp.json</code> and put it in the folder
you start Claude Code from:
<div class="dl">{_dl("mcping.mcp.json", "mcping.mcp.json")}</div>
It reads the token from <code>MCPING_TOKEN</code>, so the token is not in the file.</li>
<li><b>Restart</b> Claude Code from that folder and approve the <code>mcping</code> server when it asks.
<code>/mcp</code> inside Claude Code should show it as connected.</li>
</ol>

<h2>OpenCode and Ollama, by hand</h2>
<ol>
<li><b>Environment variables</b> as above: <code>NODE_EXTRA_CA_CERTS</code> and <code>MCPING_TOKEN</code>. Typed:
<pre><code>setx NODE_EXTRA_CA_CERTS "C:\\Users\\&lt;you&gt;\\.mcping\\mcping-ca.crt"
setx MCPING_TOKEN "&lt;token&gt;"</code></pre></li>
<li><b>Config file.</b> If you have no OpenCode config yet, download this and save it as
<code>opencode.json</code> in <code>%USERPROFILE%\\.config\\opencode\\</code> (Windows) or
<code>~/.config/opencode/</code> (macOS / Linux):
<div class="dl">{_dl("opencode.mcping.json", "opencode.mcping.json")}</div>
If you already have one, copy only its <code>mcp</code> block (and the <code>provider</code> and
<code>model</code> entries if you want Ollama) into yours.</li>
<li><b>Ollama model.</b> Download the
<a href="/downloads/Modelfile.mcping-qwen3">Modelfile</a>, then <code>ollama pull qwen3:8b</code> and
<code>ollama create qwen3-mcping -f Modelfile.mcping-qwen3</code>.</li>
<li>Open a <b>new</b> terminal and run <code>opencode</code>.</li>
</ol>

<h2>Other MCP clients</h2>
<p>Give the client the URL <code>{url}</code>, the header <code>Authorization: Bearer &lt;token&gt;</code>, and
the certificate file (or install <code>mcping-ca.crt</code> in the operating system's trusted certificates).</p>
<h2>Still blocked?</h2>
<table>
<tr><th>Cannot set environment variables</th><td>Ask for <code>NODE_EXTRA_CA_CERTS</code> to be set for your user, or for
the device certificate to be added to the computer's trusted certificates.</td></tr>
<tr><th>Cannot reach <code>{a}</code></th><td>Use the device's IP address in place of the name, or ask for access to
port {MCP_PORT} and 443 on that address.</td></tr>
</table>"""
    return _layout("No scripts? - MCP Intelligence Node Gateway (MCPing)", body, address)


def other_page(address: str) -> str:
    body = f"""
<h1>Any other MCP client</h1>
<p class="lead">Anything that speaks MCP over streamable HTTP can use the device.</p>
{_facts(address)}
<div class="dl">{_dl("mcping-ca.crt", "Download the certificate")}</div>
<p class="fp">SHA-256 fingerprint: {ca_fingerprint()}</p>
<h2>Tools</h2>
<table>
<tr><th>See</th><td><code>screenshot</code>, <code>zoom</code>, <code>describe_screen</code> (text description,
no image needed), <code>screen_changes</code>, <code>read_element</code>, <code>find_elements</code>,
<code>wait_for_text</code></td></tr>
<tr><th>Act</th><td><code>click_element</code>, <code>type_into</code>, <code>type_text</code>,
<code>key_combo</code>, <code>mouse_move</code>, <code>mouse_click</code>, <code>mouse_drag</code>,
<code>mouse_scroll</code></td></tr>
<tr><th>Control</th><td><code>wait</code>, <code>abort</code> (releases every held key), <code>status</code></td></tr>
</table>
<p>Only one client should drive the device at a time.</p>
<h2>Trusting the certificate</h2>
<p>Point your client at the downloaded file (Node-based tools: <code>NODE_EXTRA_CA_CERTS</code>), or install it
in the operating system's trusted certificates. The CA is limited to <code>.local</code>, <code>localhost</code>
and <code>mcping</code> names, so use <code>{html.escape(address)}</code> or an IP address, not other host names.</p>"""
    return _layout("Other clients - MCP Intelligence Node Gateway (MCPing)", body, address)


# --- handlers ----------------------------------------------------------------------------------
async def home(request: Request) -> Response:
    return HTMLResponse(index_page(address_for(request)))


async def claude_code(request: Request) -> Response:
    return HTMLResponse(claude_page(address_for(request)))


async def opencode_ollama(request: Request) -> Response:
    return HTMLResponse(opencode_page(address_for(request)))


async def other(request: Request) -> Response:
    return HTMLResponse(other_page(address_for(request)))


async def manual(request: Request) -> Response:
    return HTMLResponse(manual_page(address_for(request)))


async def download(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in DOWNLOADS:
        return PlainTextResponse("not found", status_code=404)
    if name == "mcping-ca.crt":
        body = ca_pem() + "\n"
    else:
        body = render_file(name, address_for(request))
    return Response(body, media_type=DOWNLOADS[name],
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def health(request: Request) -> Response:
    return PlainTextResponse("ok")


# --- one-time invites ----------------------------------------------------------------------------
_PRIVATE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow"}

_SCRIPTS = (("Claude Code", "Windows (PowerShell)", "setup-claude-code.ps1",
             "powershell -ExecutionPolicy Bypass -File .\\setup-claude-code.ps1"),
            ("Claude Code", "macOS / Linux", "setup-claude-code.sh", "bash setup-claude-code.sh"),
            ("OpenCode + Ollama", "Windows (PowerShell)", "setup-opencode.ps1",
             "powershell -ExecutionPolicy Bypass -File .\\setup-opencode.ps1"),
            ("OpenCode + Ollama", "macOS / Linux", "setup-opencode.sh", "bash setup-opencode.sh"))


def join_form_page(address: str, message: str = "") -> str:
    note = f'<div class="note">{html.escape(message)}</div>' if message else ""
    body = f"""
<h1>Join with a one-time invite</h1>
<p class="lead">On the device, open <b>MCPing Connect</b> and press <b>Create invite</b>. It shows a code that
works once, for {invites.DEFAULT_TTL} seconds. Type it here.</p>
{note}
<form onsubmit="location.href='/join/' + encodeURIComponent(this.code.value.trim()); return false;">
  <p><input name="code" autocomplete="off" autocapitalize="characters" spellcheck="false"
     placeholder="ABCDE" style="font:20px ui-monospace,Consolas,monospace; padding:8px 10px; width:16em;
     border:1px solid var(--line); border-radius:6px; background:var(--panel); color:var(--ink)">
  <button style="font-size:16px; padding:9px 18px; border:0; border-radius:6px; background:var(--accent);
     color:#fff; font-weight:600; cursor:pointer">Join</button></p>
</form>"""
    return _layout("Join - MCP Intelligence Node Gateway (MCPing)", body, address)


def _copy_box(box_id: str, text: str, rows: int = 2) -> str:
    return (f'<p><textarea id="{box_id}" readonly rows="{rows}" onclick="this.select()" '
            f'style="width:100%; font:12px ui-monospace,Consolas,monospace">{html.escape(text)}</textarea><br>'
            f'<button onclick="var t=document.getElementById(\'{box_id}\'); t.select(); '
            f'navigator.clipboard.writeText(t.value); this.textContent=\'Copied\'">Copy</button></p>')


def _blocked_section(address: str, token: str) -> str:
    """The setup with no script at all, for computers where PowerShell scripts are blocked (application control,
    AppLocker, policy): a certificate file, two typed commands, and files to drop in place."""
    url = f"https://{address}:{MCP_PORT}/mcp"
    ca_url = "data:application/x-x509-ca-cert;base64," + base64.b64encode(ca_pem().encode()).decode("ascii")
    entry = {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}
    mcp_json = json.dumps({"mcpServers": {"mcping": entry}}, indent=2)
    mcp_url = "data:application/json;base64," + base64.b64encode(mcp_json.encode()).decode("ascii")
    setx = 'setx NODE_EXTRA_CA_CERTS "%USERPROFILE%\\Downloads\\mcping-ca.crt"'
    add = f'claude mcp add --transport http --scope user mcping {url} --header "Authorization: Bearer {token}"'
    return f"""
<h2>Scripts blocked (application control, AppLocker, company policy)?</h2>
<p>Nothing here runs a script: one file to download, one setting, and either one typed command or one file.</p>
<ol>
<li><b>Download the certificate:</b> <a href="{ca_url}" download="mcping-ca.crt">mcping-ca.crt</a> (it goes to
your Downloads folder; it only lets this computer trust the MCPing device).</li>
<li><b>Tell Claude to trust it:</b> open a normal <b>Command Prompt</b> (Start, type <code>cmd</code>) and type
this one line. (No command prompt? Start, type <code>environment variables</code>, <b>Edit environment
variables for your account</b>, New: name <code>NODE_EXTRA_CA_CERTS</code>, value
<code>%USERPROFILE%\\Downloads\\mcping-ca.crt</code>.)
{_copy_box("blk-setx", setx, 1)}</li>
<li><b>Add the device</b>, either way:
<ul>
<li><i>Claude Code command line</i> (the <code>claude</code> command exists): type this in the same window.
{_copy_box("blk-add", add, 3)}</li>
<li><i>Claude desktop app, no command line</i>: download
<a href="{mcp_url}" download=".mcp.json">.mcp.json</a> and put it in the folder you open in the app's Code
tab (rename it to <code>.mcp.json</code> if the browser dropped the leading dot). Claude asks once to
approve the server.</li>
</ul></li>
<li><b>Quit Claude completely</b> (also its icon near the clock) and open it again.</li>
</ol>
<p class="fp">The file and the command contain this computer's access token: do not share them.</p>"""


def join_ok_page(address: str, token: str, label: str = "") -> str:
    def data_url(name: str) -> str:
        raw = render_file(name, address, token).encode("utf-8")
        return "data:text/plain;charset=utf-8;base64," + base64.b64encode(raw).decode("ascii")

    rows = []
    for tool, system, name, command in _SCRIPTS:
        rows.append(f'<tr><td>{html.escape(tool)}</td><td><div class="dl"><a href="{data_url(name)}" '
                    f'download="{name}">{html.escape(system)}</a></div></td>'
                    f'<td><code>{html.escape(command)}</code></td></tr>')
    easy = []
    for system, name, kind in (("Windows (PowerShell)", "setup-claude-code.ps1", "ps"),
                               ("macOS / Linux terminal", "setup-claude-code.sh", "sh")):
        raw = base64.b64encode(render_file(name, address, token).encode("utf-8")).decode("ascii")
        cmd = (f"& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{raw}'))))"
               if kind == "ps" else f"echo '{raw}' | base64 -d | bash")
        easy.append(f'<p><b>{html.escape(system)}</b></p>'
                    f'<p><textarea id="cmd-{kind}" readonly rows="2" onclick="this.select()" '
                    f'style="width:100%; font:12px ui-monospace,Consolas,monospace">{cmd}</textarea><br>'
                    f'<button onclick="var t=document.getElementById(\'cmd-{kind}\'); t.select(); '
                    f'navigator.clipboard.writeText(t.value); this.textContent=\'Copied: now paste it\'">'
                    f'Copy command</button></p>')
    blocked = _blocked_section(address, token)
    body = f"""
<h1>Invite accepted</h1>
<p class="lead">This computer is now registered as <b>{html.escape(label or "a new computer")}</b>. It has its own
access token, which the device's owner can revoke at any time without affecting any other computer.</p>
<div class="note"><b>This page contains that token and will not be shown again.</b>
Finish setting up now. If you close it, ask for a new invite. Anyone who has these commands or files can
control the attached computer, so keep them private.</div>
<h2>Easiest: copy one command and paste it</h2>
<p>Press <b>Copy command</b> for your system, open a PowerShell window (Windows) or a terminal, paste and
press Enter. Nothing to download and nothing to make executable. It works for the Claude desktop app as well
as the command-line tool; then quit and reopen Claude.</p>
{''.join(easy)}
{blocked}
<h2>Or download the setup script for your tool</h2>
<table><tr><th>Tool</th><th>Download</th><th>Then run, in the folder you saved it to</th></tr>
{''.join(rows)}</table>
<p>The script saves and trusts the device certificate and registers the device; it does not ask for the
token. OpenCode users also need Ollama and a model: see <a href="/opencode-ollama">the OpenCode page</a>.</p>
<h2>Or use the token yourself</h2>
<p><input id="tok" type="password" readonly value="{html.escape(token)}"
   style="font:14px ui-monospace,Consolas,monospace; padding:6px 8px; width:26em; max-width:100%;
   border:1px solid var(--line); border-radius:6px; background:var(--panel); color:var(--ink)">
<button onclick="var t=document.getElementById('tok'); t.type = t.type==='password' ? 'text' : 'password'">Show</button>
<button onclick="navigator.clipboard.writeText(document.getElementById('tok').value)">Copy</button></p>
<p class="fp">Device certificate SHA-256: {ca_fingerprint()}</p>"""
    return _layout("Invite accepted - MCP Intelligence Node Gateway (MCPing)", body, address)


def join_bad_page(address: str, locked: bool) -> str:
    why = ("Too many wrong codes were tried, so invites are paused for a few minutes. Create a new invite on "
           "the device afterwards." if locked else
           f"That invite is not valid: it may have been used already, may have expired (they last "
           f"{invites.DEFAULT_TTL} seconds), or may have been mistyped. Create a new invite on the device "
           f"and try again.")
    body = f"""
<h1>Invite not accepted</h1>
<div class="note">{html.escape(why)}</div>
<p><a href="/join">Enter a code</a></p>"""
    return _layout("Invite not accepted - MCP Intelligence Node Gateway (MCPing)", body, address)


async def join_form(request: Request) -> Response:
    return HTMLResponse(join_form_page(address_for(request)), headers=_PRIVATE)


def guess_label(user_agent: str, ip: str = "") -> str:
    """A readable name for a computer nobody named, from what its browser says: "Windows computer, Edge
    (192.0.2.50)". Nobody has to know their computer's name."""
    ua = user_agent or ""
    system = next((n for key, n in (("Windows", "Windows computer"), ("Android", "Android device"),
                                    ("iPhone", "iPhone"), ("iPad", "iPad"), ("Macintosh", "Mac"),
                                    ("CrOS", "Chromebook"), ("Linux", "Linux computer")) if key in ua),
                  "Computer")
    browser = next((n for key, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                     ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in ua), "")
    label = system + (f", {browser}" if browser else "")
    return f"{label} ({ip})" if ip else label


async def join_confirm(request: Request) -> Response:
    """Opening an invite link only asks for confirmation. The code is used up by the button (a POST), so a
    link scanner, a chat preview or a browser prefetch cannot burn it."""
    address = address_for(request)
    code = request.path_params["code"]
    body = f"""
<h1>Join this device</h1>
<p class="lead">You were invited to connect this computer to the device. The invite works once.</p>
<form method="post" action="/join/{html.escape(urllib.parse.quote(code, safe=""))}">
  <p><button style="font-size:16px; padding:9px 18px; border:0; border-radius:6px; background:var(--accent);
     color:#fff; font-weight:600; cursor:pointer">Accept the invite</button></p>
</form>"""
    return HTMLResponse(_layout("Join - MCP Intelligence Node Gateway (MCPing)", body, address), headers=_PRIVATE)


async def join_redeem(request: Request) -> Response:
    address = address_for(request)
    code = request.path_params["code"]
    info = invites.redeem_info(code) if len(code) <= 40 else None
    if info is None:
        return HTMLResponse(join_bad_page(address, invites.locked()), status_code=403, headers=_PRIVATE)
    try:
        # this computer's own token: revocable on its own, never the shared master token
        who = info.get("label", "") or guess_label(request.headers.get("user-agent", ""),
                                                   request.client.host if request.client else "")
        cid, token = clients.create(who)
    except OSError:
        return HTMLResponse(join_bad_page(address, False), status_code=500, headers=_PRIVATE)
    label = next((c["label"] for c in clients.list_clients() if c["id"] == cid), cid)
    return HTMLResponse(join_ok_page(address, token, label), headers=_PRIVATE)


def make_app() -> Starlette:
    return Starlette(routes=[
        Route("/", home), Route("/claude-code", claude_code), Route("/opencode-ollama", opencode_ollama),
        Route("/other", other), Route("/manual", manual), Route("/downloads/{name}", download),
        Route("/join", join_form), Route("/join/{code}", join_confirm, methods=["GET"]),
        Route("/join/{code}", join_redeem, methods=["POST"]), Route("/health", health),
    ])


def main() -> None:
    ap = argparse.ArgumentParser(prog="mcping-portal")
    ap.add_argument("--host", default=os.environ.get("MCPING_PORTAL_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCPING_PORTAL_PORT", "443")))
    args = ap.parse_args()
    import uvicorn

    p = tls.paths()
    if not (p["crt"].exists() and p["key"].exists()):
        sys.exit(f"no certificate in {p['dir']}; start mcping.service first (it creates it)")

    def watch() -> None:
        seen = p["crt"].stat().st_mtime
        while True:
            time.sleep(5)
            try:
                if p["crt"].stat().st_mtime != seen:
                    os.kill(os.getpid(), signal.SIGTERM)    # systemd starts us again with the new certificate
                    return
            except OSError:
                pass

    threading.Thread(target=watch, daemon=True, name="cert-watch").start()
    uvicorn.run(make_app(), host=args.host, port=args.port, ssl_certfile=str(p["crt"]),
                ssl_keyfile=str(p["key"]), log_level="warning")


if __name__ == "__main__":
    main()
