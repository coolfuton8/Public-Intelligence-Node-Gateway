"""MCPing Connect: add another computer's AI tool to this device, and manage who has access.

Press "Create invite": it shows a link and a short code that work once, for 60 seconds. Open the link (or
go to /join and type the code) on the other computer and its setup scripts arrive with that computer's
own access token inside. Each invited computer appears in "Computers with access" and can be revoked
on its own. The older clipboard buttons, which use the shared master token, are kept under "Other ways".

Run on the device (it reads /etc/mcping/mcping.env and the CA, so it needs the device's user).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import clients, invites, tls

ENV_FILE = Path(os.environ.get("MCPING_ENV_FILE", "/etc/mcping/mcping.env"))
PORT = os.environ.get("MCPING_PORT", "8765")


def read_token(path: Path = ENV_FILE) -> str:
    for line in path.read_text().splitlines():
        m = re.match(r"\s*MCPING_TOKEN\s*=\s*(\S+)", line)
        if m:
            return m.group(1).strip("'\"")
    raise ValueError(f"no MCPING_TOKEN in {path}")


def read_ca(d: Path | None = None) -> str:
    return tls.paths(d)["ca_crt"].read_text().strip() + "\n"


def candidate_addresses() -> list[str]:
    """Names and addresses another computer could use, most convenient first."""
    return ["mcping.local"] + tls.local_ips()


def invite_link(address: str, code: str) -> str:
    return f"https://{address}/join/{invites.display(code)}"


def windows_command(address: str, token: str, ca_pem: str, name: str = "mcping") -> str:
    """For a PowerShell window on Windows."""
    url = f"https://{address}:{PORT}/mcp"
    return (
        "$d = Join-Path $env:USERPROFILE '.mcping'; New-Item -ItemType Directory -Force $d | Out-Null\n"
        "$ca = Join-Path $d 'mcping-ca.crt'\n"
        "@'\n" + ca_pem.rstrip("\n") + "\n'@ | Set-Content -Encoding ascii $ca\n"
        "[Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $ca, 'User'); $env:NODE_EXTRA_CA_CERTS = $ca\n"
        f"cmd /c claude mcp remove --scope user {name} 2>$null\n"
        f'cmd /c claude mcp add --transport http --scope user {name} {url} --header "Authorization: Bearer {token}"\n'
        "cmd /c claude mcp list\n"
        "'Done. If Claude Code was already open, restart it.'\n"
    )


def unix_command(address: str, token: str, ca_pem: str, name: str = "mcping") -> str:
    """For a terminal on macOS or Linux."""
    url = f"https://{address}:{PORT}/mcp"
    return (
        "mkdir -p \"$HOME/.mcping\" && cat > \"$HOME/.mcping/mcping-ca.crt\" <<'MCPING_CA'\n"
        + ca_pem.rstrip("\n") + "\nMCPING_CA\n"
        "export NODE_EXTRA_CA_CERTS=\"$HOME/.mcping/mcping-ca.crt\"\n"
        "for rc in \"$HOME/.bashrc\" \"$HOME/.zshrc\"; do [ -f \"$rc\" ] && ! grep -q NODE_EXTRA_CA_CERTS \"$rc\" "
        "&& echo 'export NODE_EXTRA_CA_CERTS=\"$HOME/.mcping/mcping-ca.crt\"' >> \"$rc\"; done\n"
        f"claude mcp remove --scope user {name} >/dev/null 2>&1\n"
        f'claude mcp add --transport http --scope user {name} {url} --header "Authorization: Bearer {token}"\n'
        "claude mcp list\n"
        "echo 'Done. If Claude Code was already open, restart it.'\n"
    )


def main() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("MCPing Connect")
    root.configure(padx=18, pady=14)
    style = ttk.Style(root)
    style.configure("Go.TButton", font=("TkDefaultFont", 11, "bold"), padding=8)
    ttk.Label(root, text="Add a computer to this device",
              font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(root, text="1. Press Create invite.\n"
                         "2. On the other computer, open the link below in a browser (or go to /join and type\n"
                         "    the code), then copy and paste the command it shows.\n"
                         f"An invite works once and expires after {invites.DEFAULT_TTL} seconds.",
              justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 10))

    ttk.Label(root, text="Name (optional):").grid(row=2, column=0, sticky="w")
    name_var = tk.StringVar()
    ttk.Entry(root, textvariable=name_var, width=30).grid(row=2, column=1, sticky="w", padx=8)
    ttk.Label(root, text="Address to use:").grid(row=3, column=0, sticky="w", pady=(6, 0))
    addr = ttk.Combobox(root, values=candidate_addresses(), width=27)
    addr.set("mcping.local")
    addr.grid(row=3, column=1, sticky="w", padx=8, pady=(6, 0))
    ttk.Label(root, text="mcping.local works on the same network; pick an IP address (or a VPN's) otherwise.",
              foreground="#667085").grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 8))

    state = {"expires": 0.0, "link": "", "ip_link": ""}
    link_var = tk.StringVar()
    status = ttk.Label(root, text="", wraplength=500, justify="left")

    def tick() -> None:
        left = state["expires"] - time.time()
        if state["link"] and left > 0:
            extra = f"  Same code by IP address: {state['ip_link']}" if state["ip_link"] else ""
            status.configure(text=f"Valid for {int(left // 60)}:{int(left % 60):02d}, works once.{extra}",
                             foreground="#1b7f3b")
            root.after(1000, tick)
        elif state["link"]:
            status.configure(text="Expired. Press Create invite for a new one.", foreground="#b00020")
            link_var.set("")
            state["link"] = state["ip_link"] = ""

    def create() -> None:
        try:
            code = invites.create(label=name_var.get())
        except OSError as exc:
            status.configure(text=f"Cannot create an invite: {exc}", foreground="#b00020")
            return
        a = addr.get().strip() or "mcping.local"
        state["link"] = invite_link(a, code)
        ips = tls.local_ips()
        state["ip_link"] = invite_link(ips[0], code) if ips and a != ips[0] else ""
        state["expires"] = time.time() + invites.DEFAULT_TTL
        link_var.set(state["link"])
        tick()

    def copy_text(text: str, what: str) -> None:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        status.configure(text=f"Copied {what}.", foreground="#1b7f3b")

    ttk.Button(root, text="Create invite", style="Go.TButton", command=create).grid(
        row=5, column=0, columnspan=2, sticky="ew", pady=3)
    ttk.Entry(root, textvariable=link_var, state="readonly", font=("TkFixedFont", 11), width=54).grid(
        row=6, column=0, columnspan=2, sticky="ew", pady=(6, 2))
    ttk.Button(root, text="Copy link",
               command=lambda: copy_text(state["link"], "the invite link") if state["link"] else None).grid(
        row=7, column=0, sticky="w")
    ttk.Button(root, text="Copy link with IP address (if the name does not resolve there)",
               command=lambda: copy_text(state["ip_link"], "the invite link with the IP address")
               if state["ip_link"] else None).grid(row=7, column=1, sticky="w")
    status.grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 4))

    # --- who has access --------------------------------------------------------------------
    ttk.Separator(root).grid(row=9, column=0, columnspan=2, sticky="ew", pady=8)
    ttk.Label(root, text="Computers with access", font=("TkDefaultFont", 10, "bold")).grid(
        row=10, column=0, sticky="w")
    tree = ttk.Treeview(root, columns=("name", "added", "seen", "state"), show="headings", height=5,
                        selectmode="browse")
    for col, text, width in (("name", "Name", 180), ("added", "Added", 90), ("seen", "Last seen", 110),
                             ("state", "State", 70)):
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="w")
    tree.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(4, 2))
    ttk.Label(root, text="The master token (the shared key in /etc/mcping/mcping.env) also works and is not listed.",
              foreground="#667085").grid(row=12, column=0, columnspan=2, sticky="w")

    refresh_id: list = [None]

    def refresh() -> None:
        sel = tree.selection()
        keep = sel[0] if sel else None
        for item in tree.get_children():
            tree.delete(item)
        for c in sorted(clients.list_clients(), key=lambda c: (c.get("revoked", False), -c["created"])):
            seen = clients.ago(c.get("last_seen", 0.0))
            if c.get("last_ip"):
                seen += f" ({c['last_ip']})"
            tree.insert("", "end", iid=c["id"], values=(
                c["label"], clients.ago(c["created"]), seen, "revoked" if c.get("revoked") else "active"))
        if keep and tree.exists(keep):
            tree.selection_set(keep)
        refresh_id[0] = root.after(5000, refresh)

    def revoke() -> None:
        sel = tree.selection()
        if not sel:
            status.configure(text="Select a computer in the list first.", foreground="#b00020")
            return
        label = tree.item(sel[0], "values")[0]
        if messagebox.askyesno("Revoke access", f"Revoke access for \"{label}\"?\n\nIt will stop working "
                               "immediately. Other computers are not affected."):
            n = clients.revoke(sel[0])
            status.configure(text=f"Revoked {label}." if n else "Nothing to revoke.",
                             foreground="#1b7f3b" if n else "#b00020")
            refresh_now()

    def refresh_now() -> None:
        if refresh_id[0]:
            root.after_cancel(refresh_id[0])
        refresh()

    ttk.Button(root, text="Revoke selected", command=revoke).grid(row=13, column=0, sticky="w", pady=(4, 0))

    # --- the older ways --------------------------------------------------------------------------
    ttk.Separator(root).grid(row=14, column=0, columnspan=2, sticky="ew", pady=10)
    ttk.Label(root, text="Other ways (these use the shared master token)",
              font=("TkDefaultFont", 10, "bold")).grid(row=15, column=0, columnspan=2, sticky="w")

    def copy_clip(kind: str) -> None:
        try:
            token, ca = read_token(), read_ca()
        except (OSError, ValueError) as exc:
            status.configure(text=f"Cannot read the token or certificate: {exc}", foreground="#b00020")
            return
        a = addr.get().strip() or "mcping.local"
        text = (windows_command(a, token, ca) if kind == "windows" else
                unix_command(a, token, ca) if kind == "unix" else token)
        what = {"windows": "the Windows command", "unix": "the Mac / Linux command", "token": "the master token"}[kind]
        copy_text(text, what + (" (it contains the master token: paste it only on a computer you trust)"
                                if kind != "token" else ""))

    ttk.Button(root, text="Copy the setup command for Windows (PowerShell)",
               command=lambda: copy_clip("windows")).grid(row=16, column=0, columnspan=2, sticky="ew", pady=2)
    ttk.Button(root, text="Copy the setup command for Mac / Linux",
               command=lambda: copy_clip("unix")).grid(row=17, column=0, columnspan=2, sticky="ew", pady=2)
    ttk.Button(root, text="Copy the master token only",
               command=lambda: copy_clip("token")).grid(row=18, column=0, columnspan=2, sticky="ew", pady=2)
    ttk.Button(root, text="Close", command=root.destroy).grid(row=19, column=1, sticky="e", pady=(10, 0))
    root.columnconfigure(1, weight=1)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
