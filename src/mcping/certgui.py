"""MCPing Certificate: a small window on the device to see the HTTPS certificate and renew it."""

from __future__ import annotations

import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import tls


def _service_state() -> str:
    try:
        return subprocess.run(["systemctl", "is-active", "mcping"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MCPing Certificate")
        self.minsize(560, 330)
        self.configure(padx=18, pady=14)
        style = ttk.Style(self)
        style.configure("Big.TLabel", font=("TkDefaultFont", 13, "bold"))
        style.configure("Go.TButton", font=("TkDefaultFont", 11, "bold"), padding=8)

        self.status = ttk.Label(self, style="Big.TLabel")
        self.status.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.rows: dict[str, ttk.Label] = {}
        for i, (key, label) in enumerate([("expires", "Valid until"), ("names", "Names"),
                                          ("addresses", "Addresses"), ("url", "Connect to"),
                                          ("ca", "CA fingerprint"), ("service", "Service")], start=1):
            ttk.Label(self, text=label + ":").grid(row=i, column=0, sticky="nw", padx=(0, 12), pady=2)
            v = ttk.Label(self, wraplength=400, justify="left")
            v.grid(row=i, column=1, sticky="w", pady=2)
            self.rows[key] = v

        bar = ttk.Frame(self)
        bar.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(bar, text="Renew certificate", style="Go.TButton", command=self.renew).pack(side="left")
        ttk.Button(bar, text="Export CA certificate...", command=self.export).pack(side="left", padx=10)
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self.columnconfigure(1, weight=1)
        self.refresh()
        self.after(15000, self._tick)

    def _tick(self) -> None:
        self.refresh()
        self.after(15000, self._tick)

    def refresh(self) -> None:
        try:
            tls.ensure_ca()
            i = tls.info()
        except Exception as exc:  # noqa: BLE001
            self.status.configure(text=f"Cannot read certificate: {exc}", foreground="#b00020")
            return
        if not i.get("ok") and i.get("problem"):
            self.status.configure(text="No certificate yet. Press Renew certificate.", foreground="#b00020")
            for v in self.rows.values():
                v.configure(text="")
            return
        d = i["days_left"]
        if d < 0:
            self.status.configure(text="Certificate EXPIRED", foreground="#b00020")
        elif d < tls.RENEW_BEFORE_DAYS:
            self.status.configure(text=f"Expires soon: {d} days left", foreground="#b36b00")
        else:
            self.status.configure(text=f"Certificate is valid ({d} days left)", foreground="#1b7f3b")
        self.rows["expires"].configure(text=i["expires"])
        self.rows["names"].configure(text=", ".join(i["names"]))
        self.rows["addresses"].configure(text=", ".join(i["addresses"]))
        self.rows["url"].configure(text="https://mcping.local:8765/mcp")
        fp = i["ca_fingerprint"]
        self.rows["ca"].configure(text=":".join(fp[n:n + 2] for n in range(0, 32, 2)).upper() + "...")
        self.rows["service"].configure(text=_service_state())

    def renew(self) -> None:
        try:
            msg = tls.ensure(force=True)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Renew failed", str(exc))
            return
        self.refresh()
        messagebox.showinfo("Certificate renewed",
                            msg + "\n\nThe service restarts itself in a few seconds. Clients that already "
                            "trust the device CA keep working without any change.")

    def export(self) -> None:
        path = filedialog.asksaveasfilename(title="Save the device CA certificate", defaultextension=".crt",
                                            initialfile="mcping-ca.crt", filetypes=[("Certificate", "*.crt")])
        if not path:
            return
        try:
            out = tls.export_ca(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("CA certificate saved", f"Saved to {out}\n\nInstall it on each client that "
                            "connects to this device (once).")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
