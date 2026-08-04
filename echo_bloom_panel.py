#!/usr/bin/env python3
"""Echo Bloom desktop control panel."""

import tkinter as tk
from tkinter import scrolledtext
import subprocess
import threading
import webbrowser
import time
import os
import sys

SERVICE  = "echo_bloom"
PORT     = 8090
URL      = f"http://localhost:{PORT}"
REFRESH  = 3000  # ms between status polls

# Import license module directly — avoids HTTP auth complexity
_APP_DIR = os.path.expanduser("~/echo_bloom")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
try:
    import license as _lic
    _HAS_LIC = True
except ImportError:
    _HAS_LIC = False

# ── Colors (match Echo Bloom dark theme) ──────────────────────────────────────
BG       = "#0d1117"
BG2      = "#161b22"
BG3      = "#21262d"
GREEN    = "#3fb950"
RED      = "#f85149"
AMBER    = "#d29922"
TEXT     = "#e6edf3"
DIM      = "#8b949e"
CYAN     = "#58a6ff"
BORDER   = "#30363d"

def _run(cmd, capture=True):
    return subprocess.run(
        cmd, shell=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True
    )

def service_status():
    r = _run(f"systemctl --user is-active {SERVICE}")
    return r.stdout.strip()  # "active" | "inactive" | "failed" | "activating"

def service_control(action):
    _run(f"systemctl --user {action} {SERVICE}")

def open_browser():
    webbrowser.open(URL)

# ── Main window ───────────────────────────────────────────────────────────────
class Panel:
    def __init__(self, root):
        self.root = root
        root.title("Echo Bloom")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.geometry("320x560")

        self._build()
        self._poll()

    def _build(self):
        root = self.root

        # Header
        hdr = tk.Frame(root, bg=BG2, pady=16)
        hdr.pack(fill="x")
        tk.Label(hdr, text="ECHO BLOOM", font=("Courier", 15, "bold"),
                 bg=BG2, fg=TEXT).pack()
        tk.Label(hdr, text="local AI lifecycle manager",
                 font=("Courier", 9), bg=BG2, fg=DIM).pack()

        # Status row
        sf = tk.Frame(root, bg=BG, pady=14)
        sf.pack(fill="x", padx=24)
        tk.Label(sf, text="STATUS", font=("Courier", 9, "bold"),
                 bg=BG, fg=DIM).pack(side="left")
        self.dot   = tk.Label(sf, text="●", font=("Courier", 14),
                              bg=BG, fg=DIM)
        self.dot.pack(side="right", padx=(0, 4))
        self.status_lbl = tk.Label(sf, text="checking…",
                                   font=("Courier", 10), bg=BG, fg=DIM)
        self.status_lbl.pack(side="right", padx=8)

        # Divider
        tk.Frame(root, bg=BORDER, height=1).pack(fill="x", padx=16)

        # Buttons
        bf = tk.Frame(root, bg=BG, pady=10)
        bf.pack(fill="x", padx=20)

        def btn(parent, label, color, cmd, **kw):
            b = tk.Button(parent, text=label,
                          font=("Courier", 10, "bold"),
                          bg=BG3, fg=color,
                          activebackground=BG2, activeforeground=color,
                          relief="flat", cursor="hand2",
                          bd=0, pady=8, padx=0,
                          highlightthickness=1,
                          highlightbackground=BORDER,
                          command=cmd, **kw)
            b.pack(fill="x", pady=3)
            return b

        self.btn_start   = btn(bf, "▶  START",   GREEN, self._start)
        self.btn_stop    = btn(bf, "■  STOP",    RED,   self._stop)
        self.btn_restart = btn(bf, "↺  RESTART", AMBER, self._restart)

        tk.Frame(root, bg=BORDER, height=1).pack(fill="x", padx=16, pady=4)

        bf2 = tk.Frame(root, bg=BG, pady=4)
        bf2.pack(fill="x", padx=20)

        btn(bf2, "⎘  OPEN IN BROWSER", CYAN,  open_browser)
        btn(bf2, "≡  VIEW LOGS",       TEXT,  self._show_logs)

        # License section
        tk.Frame(root, bg=BORDER, height=1).pack(fill="x", padx=16, pady=4)

        lf = tk.Frame(root, bg=BG, pady=6)
        lf.pack(fill="x", padx=20)

        tk.Label(lf, text="LICENSE", font=("Courier", 9, "bold"),
                 bg=BG, fg=DIM).pack(anchor="w")

        self.lic_status = tk.Label(lf, text="—", font=("Courier", 9),
                                   bg=BG, fg=DIM)
        self.lic_status.pack(anchor="w", pady=(2, 6))

        self.lic_entry = tk.Entry(lf, font=("Courier", 9),
                                  bg=BG3, fg=TEXT, insertbackground=TEXT,
                                  relief="flat", bd=4)
        self.lic_entry.pack(fill="x", pady=(0, 4))
        self.lic_entry.insert(0, "EB1-…")
        self.lic_entry.bind("<FocusIn>",  lambda e: self._lic_clear_hint())
        self.lic_entry.bind("<FocusOut>", lambda e: self._lic_restore_hint())

        self.lic_msg = tk.Label(lf, text="", font=("Courier", 8),
                                bg=BG, fg=DIM, wraplength=280, justify="left")
        self.lic_msg.pack(anchor="w")

        tk.Button(lf, text="ACTIVATE KEY",
                  font=("Courier", 9, "bold"),
                  bg=BG3, fg=GREEN,
                  activebackground=BG2, activeforeground=GREEN,
                  relief="flat", cursor="hand2", bd=0, pady=6,
                  highlightthickness=1, highlightbackground=BORDER,
                  command=self._activate_key).pack(fill="x", pady=(4, 0))

        # Footer
        tk.Frame(root, bg=BORDER, height=1).pack(fill="x", padx=16, pady=4)
        tk.Label(root, text=f"localhost:{PORT}",
                 font=("Courier", 9), bg=BG, fg=DIM).pack(pady=(0, 10))

        # Load license status
        self._refresh_license()

    def _set_status(self, s):
        color_map = {
            "active":     (GREEN, "running"),
            "activating": (AMBER, "starting…"),
            "failed":     (RED,   "failed"),
            "inactive":   (DIM,   "stopped"),
        }
        color, label = color_map.get(s, (DIM, s))
        self.dot.config(fg=color)
        self.status_lbl.config(text=label, fg=color)

        running = (s == "active")
        self.btn_start.config(state="disabled" if running else "normal",
                              fg=DIM if running else GREEN)
        self.btn_stop.config(state="disabled" if not running else "normal",
                             fg=DIM if not running else RED)

    def _poll(self):
        def _check():
            s = service_status()
            self.root.after(0, self._set_status, s)
        threading.Thread(target=_check, daemon=True).start()
        self.root.after(REFRESH, self._poll)

    def _start(self):
        self._set_status("activating")
        threading.Thread(target=lambda: service_control("start"), daemon=True).start()

    def _stop(self):
        threading.Thread(target=lambda: service_control("stop"), daemon=True).start()

    def _restart(self):
        self._set_status("activating")
        threading.Thread(target=lambda: service_control("restart"), daemon=True).start()

    def _refresh_license(self):
        if not _HAS_LIC:
            self.lic_status.config(text="license module not found", fg=DIM)
            return
        def _fetch():
            try:
                d = _lic.get_status()
                self.root.after(0, self._set_lic_status, d)
            except Exception as e:
                self.root.after(0, self.lic_status.config,
                                {"text": str(e)[:60], "fg": RED})
        threading.Thread(target=_fetch, daemon=True).start()

    def _set_lic_status(self, d):
        state = d.get("state", "unknown")
        if state == "licensed":
            email = d.get("email", "")
            txt = f"✓ licensed{' — ' + email if email else ''}"
            self.lic_status.config(text=txt, fg=GREEN)
        elif state == "trial":
            days = d.get("days_left", "?")
            self.lic_status.config(
                text=f"◉ trial — {days} day{'s' if days != 1 else ''} remaining",
                fg=AMBER)
        elif state == "expired":
            self.lic_status.config(text="◎ trial expired", fg=RED)
        elif state == "denied":
            self.lic_status.config(text="✗ trial unavailable", fg=RED)
        else:
            self.lic_status.config(text=f"— {state}", fg=DIM)

    def _lic_clear_hint(self):
        if self.lic_entry.get() == "EB1-…":
            self.lic_entry.delete(0, "end")
            self.lic_entry.config(fg=TEXT)

    def _lic_restore_hint(self):
        if not self.lic_entry.get():
            self.lic_entry.insert(0, "EB1-…")
            self.lic_entry.config(fg=DIM)

    def _activate_key(self):
        if not _HAS_LIC:
            return
        key = self.lic_entry.get().strip()
        if not key or key == "EB1-…":
            self.lic_msg.config(text="Paste your key first.", fg=RED)
            return
        self.lic_msg.config(text="Verifying…", fg=DIM)
        def _do():
            try:
                result = _lic.verify_key(key)
                if result["valid"]:
                    _lic.save_key(key)
                    ktype = result.get("type", "permanent")
                    if ktype == "permanent":
                        msg = "Licensed forever. Welcome home."
                    else:
                        msg = f"Trial key accepted — {result.get('days_left','?')} days."
                    self.root.after(0, self.lic_msg.config, {"text": msg, "fg": GREEN})
                    self.root.after(0, self._refresh_license)
                else:
                    reason = result.get("reason", "Invalid key.")
                    self.root.after(0, self.lic_msg.config, {"text": reason, "fg": RED})
            except Exception as e:
                self.root.after(0, self.lic_msg.config,
                                {"text": str(e)[:80], "fg": RED})
        threading.Thread(target=_do, daemon=True).start()

    def _show_logs(self):
        win = tk.Toplevel(self.root)
        win.title("Echo Bloom — Logs")
        win.configure(bg=BG)
        win.geometry("700x420")

        tk.Label(win, text="LIVE LOGS", font=("Courier", 10, "bold"),
                 bg=BG, fg=DIM).pack(anchor="w", padx=12, pady=(10, 4))

        box = scrolledtext.ScrolledText(
            win, font=("Courier", 9), bg=BG2, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            borderwidth=0, wrap="word"
        )
        box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        box.config(state="disabled")

        bf = tk.Frame(win, bg=BG)
        bf.pack(fill="x", padx=10, pady=(0, 8))
        tk.Button(bf, text="REFRESH", font=("Courier", 9),
                  bg=BG3, fg=TEXT, relief="flat", bd=0,
                  activebackground=BG2, cursor="hand2",
                  command=lambda: _load_logs(box)).pack(side="left")
        tk.Button(bf, text="CLOSE", font=("Courier", 9),
                  bg=BG3, fg=DIM, relief="flat", bd=0,
                  activebackground=BG2, cursor="hand2",
                  command=win.destroy).pack(side="right")

        _load_logs(box)


def _load_logs(box):
    r = subprocess.run(
        ["journalctl", "--user", "-u", SERVICE, "-n", "120", "--no-pager"],
        capture_output=True, text=True
    )
    box.config(state="normal")
    box.delete("1.0", "end")
    box.insert("end", r.stdout or "(no log entries)")
    box.see("end")
    box.config(state="disabled")


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()


    Panel(root)
    root.mainloop()
