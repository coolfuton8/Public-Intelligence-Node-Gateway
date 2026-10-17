# Security

> **Non-production, testing only, no warranty.** Read [DISCLAIMER.md](DISCLAIMER.md) first. The
> software was written with AI assistance and has not been independently reviewed or audited. Assume
> it contains security flaws.

## What you are protecting
Whoever can reach the node with a valid token can **see the target's screen and type and click on it**.
That includes entering passwords, changing settings and deleting data. The node is a remote control
for another computer, so treat its token like the keys to that computer.

## Reporting a vulnerability
Please report problems **privately**. On this repository use the **Security** tab and
**Report a vulnerability** (GitHub private vulnerability reporting). Do not post details in public
issues until a fix is available. This is a volunteer, best-effort project with no service levels.

If you believe the system is being misused, report that the same way.

## Threat model
**Meant to defend against**
- Other computers on the same network that do not hold a token.
- A person guessing an invite code (see the limits below).
- A revoked computer, which loses access on its next request.

**Not defended against**
- Anyone who has a valid token (master or per-computer).
- Anyone with administrator access to the node, or physical access to the node, the ESP32 or the
  target.
- An attacker on the network path who gets you to trust the wrong certificate authority (see
  "Trust" below).
- A malicious or mistaken AI model that you have connected. It can do anything its tools can do.

## Rules for running it safely
1. **Never expose the node to the internet.** Ports 8765 (MCP) and 443 (setup pages) listen on all
   interfaces. Use a private network or a VPN, and use a firewall.
2. **Use a private network you control.** The first certificate trust step is only as safe as the network
   you do it on.
3. **Keep the token secret.** It is stored in plain text in the node's environment file and, after setup,
   in client settings files. It is also placed on command lines while setup scripts run, where other
   users on the same computer can briefly see it in the process list. Do not run setup on a shared
   computer.
4. **Prefer per-computer tokens** (invites) over the master token, and revoke any you stop using.
5. **Back up the secrets off the node, and never put them in git** (token, CA key, client files).
6. **Do not type secrets you cannot afford to expose.** Typed text, including passwords, appears in
   the live monitor's call list in full until the node restarts.
7. **Supervise AI use.** A model can misread the screen or act on bad instructions. Give it access only
   to what you can afford to lose.

## Details worth knowing
**Tokens and invites**
- Tokens are compared in constant time. Only a hash of each per-computer token and each invite code is
  stored, in files readable only by the service user.
- An invite code is five characters (about 25 bits). It works once, lasts 60 seconds, and two wrong
  tries cancel every pending invite and pause redemption for five minutes. That keeps guessing very
  unlikely, but it also means someone on your network can repeatedly lock out legitimate invites.
  Creating a new invite on the node always clears the lock.
- The master token never expires. Rotate it by editing the environment file and restarting the service.

**Trust**
- The setup pages and scripts carry the CA and its fingerprint from the same download. The fingerprint
  check only protects you if you compare it with the one shown on the node itself. On any network you do
  not control, do that.
- The CA is limited to `.local`, `localhost` and `mcping` names, valid 10 years. The CA key lives on the
  node (`/var/lib/mcping/tls/ca.key`) and must stay there.

**One client at a time**
- The node accepts one client at a time and releases all keys and buttons when control changes. A client
  that keeps making calls holds the node until you revoke it.

**The live monitor**
- Requires the token (a cookie after sign-in). A browser on the node itself can be allowed in without
  signing in by setting `MCPING_MONITOR_LOCAL_OPEN=1`; this is off by default, because any program on
  that computer could then read the screen.
- Failed sign-ins are slowed down but not otherwise rate limited.

**Setup scripts**
- They change your shell start-up files and client settings, save the token in plain text, and may
  start with a PowerShell option that bypasses the script policy for that one run. If your organisation
  blocks scripts, do not work around it. Use the no-script route or ask an administrator.

## Known gaps
- No independent security review.
- No rate limiting beyond the invite lock-out and a short delay on failed monitor sign-ins.
- No physical stop button or hardware kill switch; stop by revoking the token, running `abort`, or
  unplugging the ESP32's native USB cable.
- Tokens in plain text on disk and in process lists during setup.
- HDCP-protected content cannot be seen; the node cannot detect that a target is a different computer
  from the one you meant.

## Supported versions
There is only the latest commit on `main`. No back-ported fixes are planned.
