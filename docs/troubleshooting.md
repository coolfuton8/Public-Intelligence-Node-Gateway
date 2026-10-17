# Troubleshooting and known issues

> **Non-production, testing only.** Read [DISCLAIMER.md](../DISCLAIMER.md) first. This list is
> incomplete; the system should be treated as flawed until you prove otherwise.

## Known issue 1: the name `mcping.local` does not resolve

The node announces itself as `mcping.local` using multicast DNS (mDNS). Many networks do not
pass it, including guest and corporate Wi-Fi, networks with client isolation, VPNs, and
computers managed by a security policy. When the name does not resolve, clients cannot find
the node, and setup scripts, invite links, the live monitor and the MCP connection all fail.

**What to do**
- Use the node's **IP address** instead of the name. Find it on the node (`ip -br a`) or in
  your router's client list.
- Give the node a **fixed address** (a DHCP reservation in the router) so the address does not
  change.
- Make every client use the same address you used to reach the setup page.
- If you move the node to another network, update the address in each client.
- Do not expose the node to the internet to get around this. Use a VPN instead.

## Known issue 2: the system assumes you may run scripts

Setup is built around scripts (PowerShell on Windows, bash on Linux and macOS) that save a
certificate, set environment variables and register the server. This assumes the person
running them has **permission to run scripts and change user settings** on that computer.
That is often not true on managed computers, where application control, execution policy,
endpoint security or company policy blocks unsigned scripts.

**What to do**
- Do not try to bypass the policy. Ask the computer's owner or administrator, or use a
  computer you control.
- Use the manual route that needs no script: download the certificate, set the
  `NODE_EXTRA_CA_CERTS` environment variable, then register the server by typing the one
  command shown on the setup page, or use a downloadable configuration file. Restart the
  client afterwards.
- The Claude desktop app connectors list uses a small helper script, so it is not available
  on the no-script route.
- The no-script route has had very little testing.

## Known issue 3: the access token sits in plain text in several places

The setup scripts store the device's access token in plain text so that the client can connect:
in the client's settings files (for example the Claude user settings and the desktop app's
configuration), in a ready-made configuration file, and, on Windows, without any restriction on
who can read those files. While a script runs, the token is also passed on a command line, so
**other users on the same computer can see it briefly in the process list.**

**What to do**
- Only run setup on a computer you trust, and not on a shared computer with other users logged in.
- Treat the token like a password. Do not paste it into chat, tickets or screenshots.
- If you think it was exposed, revoke that computer's token on the node (or change the master
  token) and set the computer up again.
- Remove the configuration files from a computer you are finished with.

## Seen together

A demonstration on a managed Windows computer failed because of **both** issues at once: the
network did not resolve `mcping.local`, and the policy on the computer blocked the setup
script. If a first attempt fails, check name resolution and script permissions first.

## Other things to check
- The certificate is trusted only after the client is restarted with the environment variable
  set; otherwise connections fail with a certificate error.
- Only one controller should drive the node at a time.
- Typed text, including passwords, can appear in the live monitor.
