# 6. Connect a client

> **Non-production, testing only, no warranty.** Read [DISCLAIMER.md](../DISCLAIMER.md) first.
> **Lightly tested.** Setup scripts are the least reliable part, and several managed networks and
> computers break them. See [Troubleshooting](troubleshooting.md) first if something fails.

The device is itself an MCP server, so a client needs nothing on the device and no SSH.

| | |
|---|---|
| Transport | MCP streamable HTTP |
| URL | `https://mcping.local:8765/mcp`, or `https://<device-address>:8765/mcp` |
| Authentication | header `Authorization: Bearer <token>` |
| Health check | `GET /health`, no token needed |
| Connections | **one client at a time** (see below) |

Use the device's **IP address** instead of `mcping.local` if the name does not resolve on your network.

## One client at a time
The first computer to connect holds the device. Anyone else gets HTTP 409 ("busy") until the first
one sends a close request or has been quiet for 5 minutes (`MCPING_SESSION_IDLE`). When control
changes hands the device releases every held key and button. A rogue client that keeps making calls
can keep others out: revoke its token (below).

## Trust the device's certificate
The device signs its own certificate with a small CA. Clients must trust that CA once. Renewing the
server certificate afterwards is invisible to them.

1. Get `mcping-ca.crt`: on the device, run **MCPing Certificate** and press *Export*, or use
   `mcping-cert export <file>`, or download it from the setup pages (next section).
2. **Compare the fingerprint** shown by the device with the one on the page or script. On a network you
   do not control, do not skip this: it is the only check that you have the right device.
3. Point your client at the file. For Node-based clients such as Claude Code, set
   `NODE_EXTRA_CA_CERTS` to its path, then **restart the client**. Programs started before the setting
   change do not see it. Other clients can import the file into the operating system's trust store.

## The setup pages
Open `https://<device-address>/` (port 443) in a browser. You will see a certificate warning until the
CA is trusted. The pages offer:
- **Claude Code**, **OpenCode with Ollama**, and **other clients**, each with downloadable setup scripts
  (PowerShell and bash) that save the CA, set `NODE_EXTRA_CA_CERTS` and register the server;
- a **manual** page with the same steps as typed commands, or with no commands at all (certificate
  download, environment variable, and a downloadable `.mcp.json`), for computers that block scripts.

Scripts ask for the token. They also edit your shell start-up files and your client settings, and
they store the token in plain text. Read [SECURITY.md](../SECURITY.md) first.

## Add a computer with a one-time invite (recommended)
1. On the device, open **MCPing Connect**, optionally name the computer, and press **Create invite**
   (or run `mcping-invite --name "Ana's laptop"`).
2. On the other computer open the link it shows (for example `https://mcping.local/join/K7QX2`), or go
   to `/join` and type the code, then press **Accept the invite**.
3. The page gives that computer **its own token**, already filled into the setup scripts.

An invite works once and expires after 60 seconds. Two wrong tries cancel every pending invite and pause
redemption for 5 minutes. Creating a new invite on the device always cancels the old ones.

## Per-computer tokens and revoking access
Each invited computer has its own token, which can be removed without touching the others.
- See who has access: **MCPing Connect** (list of computers), or `mcping-clients list`.
- Revoke one: select it and press *Revoke selected*, or `mcping-clients revoke <id-or-name>`.
- The master token in `/etc/mcping/mcping.env` is separate and still works. Change it and restart the
  service to invalidate everything issued with it.
- Only a hash of each token is stored. A token is shown once and cannot be recovered; issue a new
  invite instead.

## Register by hand (Claude Code)
```
setx NODE_EXTRA_CA_CERTS "C:\path\to\mcping-ca.crt"      (Windows; restart the app afterwards)
claude mcp add --transport http --scope user mcping https://mcping.local:8765/mcp --header "Authorization: Bearer <token>"
```
Run the second line in Command Prompt on Windows; the PowerShell wrapper can mangle the arguments.

## The Claude desktop app
The app's connectors list only loads local (stdio) servers. The Claude setup scripts therefore install a
small bridge (`mcping-bridge.py` on Linux and macOS, `mcping-bridge.ps1` on Windows) that forwards to
the device over HTTPS and trusts only the device's CA. Quit the app completely and reopen it afterwards.
The bridge does not work on the no-script route; use the Code tab (`/mcp`) there.

## The live monitor
`https://<device-address>:8765/monitor` shows the last frame the MCP took, where it clicked, a boxed view
of detected elements, and a list of tool calls and which computer made each. **Typed text appears in
that list in full**, including passwords. Nothing is written to disk and a restart clears it.

## Certificate renewal
The server certificate lasts 365 days and renews automatically 30 days before expiry, or when the
device's addresses change. The CA lasts 10 years and is limited to `.local`, `localhost` and `mcping`
names. Replacing the CA means every client must trust the new one.

## Next
[Using it](07-using.md)
