#!/usr/bin/env python3
"""Tkinter front end for Signal Broadcast.

A thin UI over engine.py: a first-run Link screen (renders the QR you scan with
your phone) and a tabbed main screen — Send (type, attach, send, resend), Groups
(pick which to send to), Schedule (daily auto-send), and Security (station-mode
wipe-on-unplug). All sending happens on a worker thread; the engine talks back
through a thread-safe queue that the Tk main loop drains. Colours are chosen
explicitly so the log is readable in both macOS Light and Dark mode.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import engine

IMAGE_TYPES = [("Images", "*.png *.jpg *.jpeg *.gif *.webp *.heic"), ("All files", "*.*")]


def _detect_dark() -> bool:
    try:
        r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                           capture_output=True, text=True)
        return "Dark" in r.stdout
    except Exception:
        return False


DARK = _detect_dark()
# tk.Text widgets don't follow the macOS theme, so set both colours explicitly.
PALETTE = {
    "text_bg": "#1e1f22" if DARK else "#ffffff",
    "text_fg": "#e8e8e8" if DARK else "#1a1a1a",
    "muted": "#9aa0a6",
    "error": "#ff6b6b" if DARK else "#c0392b",
    "ok": "#4ec973" if DARK else "#1a7f37",
    "accent": "#2c6bed",                       # the one primary button (Send)
    "accent_hi": "#1f57c9",                     # hover
    "accent_fg": "#ffffff",
    "disabled": "#3a3b3e" if DARK else "#d7d9dd",
}


class AccentButton(tk.Label):
    """The app's one primary action, rendered as a colour-filled button. Built on
    tk.Label because macOS's native Tk buttons ignore a background colour."""

    def __init__(self, parent, text: str, command) -> None:
        super().__init__(parent, text=text, font=("", 15, "bold"),
                         fg=PALETTE["accent_fg"], bg=PALETTE["accent"],
                         padx=22, pady=11, cursor="hand2")
        self._command = command
        self._enabled = True
        self.bind("<Button-1>", lambda _e: self._command() if self._enabled else None)
        self.bind("<Enter>", lambda _e: self._enabled and self.configure(bg=PALETTE["accent_hi"]))
        self.bind("<Leave>", lambda _e: self._enabled and self.configure(bg=PALETTE["accent"]))

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        self.configure(bg=PALETTE["accent"] if on else PALETTE["disabled"],
                       fg=PALETTE["accent_fg"] if on else PALETTE["muted"],
                       cursor="hand2" if on else "")

    def set_text(self, text: str) -> None:
        self.configure(text=text)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Signal Broadcast")
        self.geometry("700x780")
        self.minsize(600, 660)

        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.selected_images: list[str] = []
        self.failed_results: list[engine.GroupSendResult] = []
        self._qr_img: tk.PhotoImage | None = None
        self._screen = ""
        self._awaiting_power = False  # True only while showing the "Plug in" prompt
        self._refreshing = False      # guard: one "Update list from phone" at a time

        self.container = ttk.Frame(self, padding=16)
        self.container.pack(fill="both", expand=True)

        if os.environ.get("SB_SKIP_LINK") or engine.is_linked():
            self.show_main()
        else:
            self.show_link()

        self.after(80, self._poll)
        self.after(2000, self._health_tick)

    # ----------------------------------------------------------------- utils
    def _clear(self) -> None:
        for child in self.container.winfo_children():
            child.destroy()

    def _text_widget(self, parent, **kw) -> tk.Text:
        return tk.Text(parent, background=PALETTE["text_bg"], foreground=PALETTE["text_fg"],
                       insertbackground=PALETTE["text_fg"], relief="flat", highlightthickness=1,
                       highlightbackground="#888", padx=8, pady=6, **kw)

    def _log(self, msg: str, tag: str = "") -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        engine.append_activity(msg)  # PII-safe; also persisted to logs/activity-*.txt

    def _scrollable(self, parent) -> ttk.Frame:
        """A vertically scrollable frame (Canvas + inner ttk.Frame). Returns the
        inner frame to pack children into."""
        bg = ttk.Style().lookup("TFrame", "background")
        canvas = tk.Canvas(parent, highlightthickness=0, **({"background": bg} if bg else {}))
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        wheel = lambda e: canvas.yview_scroll(int(-1 * e.delta), "units")  # noqa: E731
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    # ------------------------------------------------------------ link screen
    def show_link(self) -> None:
        self._screen = "link"
        self._clear()
        if engine.watcher_enabled() and not engine.on_ac_power():
            self._awaiting_power = True
            self._show_plug_in_prompt()
            return
        self._awaiting_power = False
        ttk.Label(self.container, text="Link this computer to Signal",
                  font=("", 18, "bold")).pack(anchor="w")
        ttk.Label(self.container, wraplength=620, justify="left", text=(
            "On your phone: open Signal → Settings → Linked Devices → tap “+”, "
            "then scan the code below. This does not log your phone out — it adds "
            "this computer as a linked device, exactly like Signal Desktop.")
        ).pack(anchor="w", pady=(6, 14))

        self.qr_label = ttk.Label(self.container,
                                  text="Click “Start linking” below, then scan the code.")
        self.qr_label.pack(pady=10)
        self.link_status = ttk.Label(self.container, text="", foreground=PALETTE["muted"])
        self.link_status.pack(pady=(4, 12))
        # Animated only while linking — a moving bar says "working, not frozen"
        # through the fixed ~12s phone sync. Packed on start (see _start_link).
        self.link_progress = ttk.Progressbar(self.container, mode="indeterminate", length=280)

        btns = ttk.Frame(self.container)
        btns.pack()
        self.link_retry = ttk.Button(btns, text="Start linking", command=self._start_link)
        self.link_retry.pack(side="left", padx=4)
        ttk.Button(btns, text="Quit", command=self.destroy).pack(side="left", padx=4)
        # No auto-start: linking only begins when the button is clicked, so a wipe
        # leaves nothing behind (signal-cli creates files the moment 'link' runs).
        ttk.Label(self.container, wraplength=620, justify="left", foreground=PALETTE["muted"],
                  text=("Settings like pacing and schedule times live in config.toml — "
                        "open it in any text editor to change them.")
        ).pack(anchor="w", pady=(16, 0))

    def _start_link(self) -> None:
        self.link_retry.configure(state="disabled")
        self.link_status.configure(text="Starting…", foreground=PALETTE["muted"])
        self.link_progress.pack(after=self.link_status, pady=(0, 10))
        self.link_progress.start()
        threading.Thread(target=self._link_worker, daemon=True).start()

    def _stop_link_progress(self) -> None:
        if self.link_progress.winfo_exists():
            self.link_progress.stop()
            self.link_progress.pack_forget()

    def _link_worker(self) -> None:
        png = None
        try:
            binary = engine.signal_cli_bin()
            qrencode = shutil.which("qrencode")
            if not qrencode:
                raise engine.BroadcastError("qrencode is not installed. Run Setup first.")
            engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                [binary, "--config", str(engine.DATA_DIR), "link", "-n", "broadcast-laptop"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            uri = ""
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith(("sgnl://linkdevice", "tsdevice:")):
                    uri = line
                    break
            if not uri:
                proc.wait()
                raise engine.BroadcastError("No link code received from signal-cli.")

            png = Path(tempfile.gettempdir()) / "sb-link-qr.png"
            subprocess.run([qrencode, "-o", str(png), "-s", "7", "-m", "2", uri], check=True)
            self.events.put(("qr", str(png)))
            self.events.put(("link_status", "Scan the code with your phone…"))

            if proc.wait() != 0:
                raise engine.BroadcastError("Linking did not complete. Try again.")

            self.events.put(("link_status", "Linked! Setting things up…"))
            number = engine.detect_account()
            if number:
                engine.save_account(number)
                count = engine.sync_groups(number, on_log=lambda m: self.events.put(("link_status", m)))
                self.events.put(("link_status", f"Ready — found {count} groups."))
            self.events.put(("linked_done", None))
        except Exception as exc:
            self.events.put(("link_error", str(exc)))
        finally:
            # The QR encodes a one-time link token — don't leave it in /tmp.
            if png is not None:
                png.unlink(missing_ok=True)

    # ------------------------------------------------------- plug-in gate
    def _show_plug_in_prompt(self) -> None:
        ttk.Label(self.container, text="Plug in to continue",
                  font=("", 18, "bold")).pack(anchor="w")
        ttk.Label(self.container, wraplength=620, justify="left", text=(
            "Station mode is on, so this Mac only runs while it's plugged into power. "
            "Connect the charger to link your phone.")
        ).pack(anchor="w", pady=(6, 14))
        btns = ttk.Frame(self.container)
        btns.pack(anchor="w")
        ttk.Button(btns, text="Quit", command=self.destroy).pack(side="left")
        ttk.Button(btns, text="Disarm station mode",
                   command=self._disarm_from_prompt).pack(side="left", padx=6)
        ttk.Label(self.container, wraplength=620, justify="left", foreground=PALETTE["muted"], text=(
            "No charger handy? Disarming turns off station mode so you can link on "
            "battery. Nothing is stored on this Mac right now.")
        ).pack(anchor="w", pady=(10, 0))
        self.after(1500, self._maybe_resume_link)

    def _disarm_from_prompt(self) -> None:
        """Escape hatch: nothing is linked here, so disarming on battery exposes no
        data and avoids a no-charger lockout. Drops straight to the QR screen."""
        engine.disable_watcher()
        self.show_link()

    def _maybe_resume_link(self) -> None:
        if self._screen != "link" or not self._awaiting_power:
            return
        if engine.on_ac_power() or not engine.watcher_enabled():
            self.show_link()                  # power's back (or disarmed) — show the QR
        else:
            self.after(1500, self._maybe_resume_link)

    # ------------------------------------------------------------ main screen
    def show_main(self) -> None:
        self._screen = "main"
        self._clear()
        header = ttk.Frame(self.container)
        header.pack(fill="x")
        self.status_label = ttk.Label(header, text="", font=("", 13, "bold"),
                                      foreground=PALETTE["ok"])
        self.status_label.pack(side="left")
        self.power_label = ttk.Label(header, text="", font=("", 11))
        self.power_label.pack(side="left", padx=(12, 0))
        ttk.Button(header, text="Unlink…", command=self._unlink).pack(side="right")

        nb = ttk.Notebook(self.container)
        nb.pack(fill="both", expand=True, pady=(12, 0))
        send_tab = ttk.Frame(nb, padding=14)
        groups_tab = ttk.Frame(nb, padding=14)
        sched_tab = ttk.Frame(nb, padding=14)
        security_tab = ttk.Frame(nb, padding=14)
        nb.add(send_tab, text="  Send  ")
        nb.add(groups_tab, text="  Groups  ")
        nb.add(sched_tab, text="  Schedule  ")
        nb.add(security_tab, text="  Security  ")

        self._build_send_tab(send_tab)
        self._build_groups_tab(groups_tab)
        self._build_schedule_tab(sched_tab)
        self._build_security_tab(security_tab)
        self._refresh_status()
        self._refresh_power()

    def _build_send_tab(self, tab) -> None:
        ttk.Label(tab, text="Message", font=("", 12, "bold")).pack(anchor="w")
        self.msg_text = self._text_widget(tab, height=6, wrap="word")
        self.msg_text.pack(fill="x", pady=(2, 0))
        try:
            self.msg_text.insert("1.0", engine.read_message())
        except engine.BroadcastError:
            pass

        img_row = ttk.Frame(tab)
        img_row.pack(fill="x", pady=(12, 2))
        ttk.Button(img_row, text="Add images…", command=self._add_images).pack(side="left")
        ttk.Button(img_row, text="Clear", command=self._clear_images).pack(side="left", padx=6)
        self.img_label = ttk.Label(img_row, text="", foreground=PALETTE["muted"])
        self.img_label.pack(side="left", padx=8)
        try:
            self.selected_images = list(engine.read_attachments())
        except engine.BroadcastError:
            self.selected_images = []
        self._refresh_img_label()

        send_row = ttk.Frame(tab)
        send_row.pack(fill="x", pady=(14, 2))
        self.send_btn = AccentButton(send_row, text="Send", command=self._on_send)
        self.send_btn.pack(side="left")
        self.stop_btn = ttk.Button(send_row, text="Stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(10, 6))
        self.resend_btn = ttk.Button(send_row, text="Resend failed", command=self._on_resend, state="disabled")
        self.resend_btn.pack(side="left", padx=6)
        ttk.Button(send_row, text="Save for auto-send", command=self._on_save).pack(side="right")
        ttk.Label(tab, foreground=PALETTE["muted"], wraplength=600, justify="left", text=(
            "“Send” delivers to every group now. “Save for auto-send” just stores this "
            "message so a scheduled run can send it later — it doesn't send now.")
        ).pack(anchor="w", pady=(4, 8))

        self.progress = ttk.Progressbar(tab, mode="determinate")
        self.progress.pack(fill="x", pady=(6, 2))
        self.counter = ttk.Label(tab, text="", foreground=PALETTE["muted"])
        self.counter.pack(anchor="w")

        ttk.Label(tab, text="Activity", font=("", 12, "bold")).pack(anchor="w", pady=(12, 2))
        log_frame = ttk.Frame(tab)
        log_frame.pack(fill="both", expand=True)
        self.log_box = self._text_widget(log_frame, height=9, wrap="word", state="disabled")
        for name in ("error", "ok", "muted"):
            self.log_box.tag_configure(name, foreground=PALETTE[name])
        scroll = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_schedule_tab(self, tab) -> None:
        ttk.Label(tab, text="Daily auto-send", font=("", 12, "bold")).pack(anchor="w")
        self.sched_status = ttk.Label(tab, text="", font=("", 14, "bold"))
        self.sched_status.pack(anchor="w", pady=(4, 4))
        self.last_send_label = ttk.Label(tab, text="", foreground=PALETTE["muted"])
        self.last_send_label.pack(anchor="w", pady=(0, 10))
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "Automatically send your saved message to every group at set times each "
            "day — you don't need to be at the computer.")
        ).pack(anchor="w", pady=(0, 12))

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Send at:").pack(side="left")
        self.times_entry = ttk.Entry(row)
        self.times_entry.pack(side="left", fill="x", expand=True, padx=8)
        try:
            saved_times = engine.load_config().send_times
        except engine.BroadcastError:
            saved_times = []
        # After a wipe, send_times is empty — fall back to a sensible default.
        self.times_entry.insert(0, ", ".join(saved_times) if saved_times else "12:00, 16:00")
        ttk.Label(tab, foreground=PALETTE["muted"], text=(
            "24-hour time, separated by commas.   e.g. 09:00 (9am),  13:30 (1:30pm),  17:00 (5pm)")
        ).pack(anchor="w", pady=(4, 12))

        btns = ttk.Frame(tab)
        btns.pack(anchor="w")
        ttk.Button(btns, text="Turn on", command=self._enable_schedule).pack(side="left")
        ttk.Button(btns, text="Turn off", command=self._disable_schedule).pack(side="left", padx=6)
        ttk.Button(btns, text="Update times", command=self._save_times).pack(side="left")
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "Set your times, then Turn on. Already on and want to change the times? "
            "Edit them and click Update times.")
        ).pack(anchor="w", pady=(10, 0))

        ttk.Separator(tab).pack(fill="x", pady=14)
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "The Mac must be awake and logged in at each time. Asleep → it sends at the "
            "next wake; powered off → that time is skipped. It sends whatever you last "
            "saved with “Send” or “Save for auto-send”, so set your message first.")
        ).pack(anchor="w", pady=(8, 0))
        self._refresh_schedule_status()
        self._refresh_last_send()

    # ------------------------------------------------------------ small refresh
    def _refresh_status(self) -> None:
        n = engine.count_groups()
        try:
            who = engine.load_config().account
        except engine.BroadcastError:
            who = engine.detect_account() or "not linked"
        self.status_label.configure(text=f"●  Linked: {who}   —   {n} groups")
        self.send_btn.set_text(f"Send to {n} groups" if n else "No groups yet")
        self.send_btn.set_enabled(bool(n))

    def _refresh_img_label(self) -> None:
        if not self.selected_images:
            self.img_label.configure(text="No images (text only)")
        else:
            names = ", ".join(Path(p).name for p in self.selected_images)
            self.img_label.configure(text=f"{len(self.selected_images)}: {names}")

    def _refresh_schedule_status(self) -> None:
        if engine.schedule_enabled():
            try:
                times = ", ".join(engine.load_config().send_times)
            except engine.BroadcastError:
                times = ""
            self.sched_status.configure(text=f"● On — daily at {times}", foreground=PALETTE["ok"])
        else:
            self.sched_status.configure(text="○ Off", foreground=PALETTE["muted"])

    def _refresh_last_send(self) -> None:
        """Show the last completed send (counts only) so a scheduled run's result
        is visible without opening logs. Cleared with everything else on unlink."""
        s = engine.read_run_summary()
        if not s:
            self.last_send_label.configure(text="No sends yet.", foreground=PALETTE["muted"])
            return
        try:
            when = datetime.fromisoformat(s.at).strftime("%d %b %H:%M")
        except ValueError:
            when = s.at
        self.last_send_label.configure(
            text=f"Last send: {when} — sent {s.sent}, failed {s.failed}",
            foreground=PALETTE["error"] if s.failed else PALETTE["muted"])

    # ----------------------------------------------------------------- images
    def _add_images(self) -> None:
        for p in filedialog.askopenfilenames(title="Choose images", filetypes=IMAGE_TYPES):
            if p not in self.selected_images:
                self.selected_images.append(p)
        self._refresh_img_label()

    def _clear_images(self) -> None:
        self.selected_images = []
        self._refresh_img_label()

    # ----------------------------------------------------------------- groups
    def _build_groups_tab(self, tab) -> None:
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Groups", font=("", 12, "bold")).pack(side="left")
        ttk.Button(top, text="Select none", command=lambda: self._set_all_groups(False)).pack(side="right")
        ttk.Button(top, text="Select all", command=lambda: self._set_all_groups(True)).pack(side="right", padx=6)
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "Tick the groups to send to; unticked groups are skipped. Click "
            "“Save selection” to apply — your choices are kept even when you update "
            "the list from your phone.")
        ).pack(anchor="w", pady=(2, 6))
        self.group_count_label = ttk.Label(tab, text="", foreground=PALETTE["muted"])
        self.group_count_label.pack(anchor="w")

        listwrap = ttk.Frame(tab)
        listwrap.pack(fill="both", expand=True, pady=(4, 8))
        self.groups_inner = self._scrollable(listwrap)

        bottom = ttk.Frame(tab)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Save selection", command=self._save_groups).pack(side="left")
        self.refresh_btn = ttk.Button(bottom, text="Update list from phone", command=self._refresh_groups)
        self.refresh_btn.pack(side="right")
        self.groups_sync_label = ttk.Label(tab, text="", foreground=PALETTE["muted"])
        self.groups_sync_label.pack(anchor="w", pady=(6, 0))
        # Animated only while a refresh runs (see _refresh_groups / _finish_refresh).
        self.groups_progress = ttk.Progressbar(tab, mode="indeterminate", length=280)
        self._populate_groups()

    def _populate_groups(self) -> None:
        for child in self.groups_inner.winfo_children():
            child.destroy()
        self.group_vars: dict[str, tk.BooleanVar] = {}
        entries = engine.read_group_entries()
        for e in entries:
            var = tk.BooleanVar(value=e.enabled)
            self.group_vars[e.group_id] = var
            ttk.Checkbutton(self.groups_inner, text=e.name, variable=var,
                            command=self._update_group_count).pack(anchor="w", pady=1)
        if not entries:
            ttk.Label(self.groups_inner, text="No groups yet — link your phone first.",
                      foreground=PALETTE["muted"]).pack(anchor="w")
        self._update_group_count()

    def _set_all_groups(self, value: bool) -> None:
        for var in self.group_vars.values():
            var.set(value)
        self._update_group_count()

    def _update_group_count(self) -> None:
        total = len(self.group_vars)
        selected = sum(1 for v in self.group_vars.values() if v.get())
        self.group_count_label.configure(text=f"{selected} of {total} selected")

    def _save_groups(self) -> None:
        enabled = {gid for gid, var in self.group_vars.items() if var.get()}
        engine.write_group_selection(enabled)
        self._refresh_status()
        messagebox.showinfo("Saved", f"{len(enabled)} of {len(self.group_vars)} "
                            "groups will receive the broadcast.")

    # --------------------------------------------------------------- schedule
    def _read_times(self) -> list[str]:
        return [t.strip() for t in self.times_entry.get().split(",") if t.strip()]

    def _save_times(self) -> None:
        """Persist the schedule times. If the job is already running, reload it so
        the new times take effect now. Does NOT enable a disabled schedule."""
        times = self._read_times()
        try:
            engine.parse_times(times)
            engine.save_send_times(times)
            running = engine.schedule_enabled()
            if running:
                engine.enable_schedule(times)
        except engine.BroadcastError as exc:
            messagebox.showerror("Can't save times", str(exc))
            return
        verb = "updated — schedule reloaded" if running else "saved (schedule is off)"
        messagebox.showinfo("Times saved", f"Times {verb}: {', '.join(times)}.")
        self._refresh_schedule_status()

    def _has_saved_message(self) -> bool:
        try:
            engine.read_message()
            return True
        except engine.BroadcastError:
            return False

    def _enable_schedule(self) -> None:
        times = self._read_times()
        try:
            engine.parse_times(times)  # validate before warning or enabling
        except engine.BroadcastError as exc:
            messagebox.showerror("Can't turn on schedule", str(exc))
            return
        if not self._has_saved_message() and not messagebox.askyesno(
                "No message saved",
                "You haven't saved a message yet, so a scheduled run will have "
                "nothing to send. Save one on the Send tab first.\n\n"
                "Turn the schedule on anyway?"):
            return
        try:
            engine.save_send_times(times)
            engine.enable_schedule(times)
        except engine.BroadcastError as exc:
            messagebox.showerror("Can't turn on schedule", str(exc))
            return
        messagebox.showinfo("Schedule on", "Will send automatically every day at "
                            + ", ".join(times) + ".")
        self._refresh_schedule_status()

    def _disable_schedule(self) -> None:
        engine.disable_schedule()
        self._refresh_schedule_status()

    # --------------------------------------------------------------- station mode
    def _build_security_tab(self, tab) -> None:
        ttk.Label(tab, text="Station mode", font=("", 12, "bold")).pack(anchor="w")
        self.station_status = ttk.Label(tab, text="", font=("", 14, "bold"))
        self.station_status.pack(anchor="w", pady=(4, 10))
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "For a Mac that stays plugged in at one spot. When armed, unplugging the "
            "power automatically ERASES all of this app's data after a 10-second grace "
            "— the Signal link, your groups, the message, the schedule, and logs. It "
            "does not touch anything else on the Mac. Plug back in within those 10 "
            "seconds to cancel. After a wipe you scan the QR to link again.")
        ).pack(anchor="w", pady=(0, 12))

        btns = ttk.Frame(tab)
        btns.pack(anchor="w")
        ttk.Button(btns, text="Arm station mode", command=self._arm_station).pack(side="left")
        ttk.Button(btns, text="Disarm", command=self._disarm_station).pack(side="left", padx=6)

        ttk.Separator(tab).pack(fill="x", pady=14)
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "This is a deterrent, not full security — someone with time can still image "
            "the disk. For real protection turn on FileVault disk encryption in System "
            "Settings → Privacy & Security.")
        ).pack(anchor="w")
        self._refresh_station_status()

    def _arm_station(self) -> None:
        if not engine.on_ac_power():
            messagebox.showwarning("Plug in first",
                                   "Plug into power before arming station mode.")
            return
        if not messagebox.askyesno("Arm station mode?",
                "From now on, unplugging this Mac will ERASE all of this app's data "
                "(the Signal link, groups, message, schedule, and logs) after a "
                "10-second grace, and you'll have to link again. Nothing else on the "
                "Mac is touched.\n\nArm it now?",
                icon="warning"):
            return
        try:
            engine.enable_watcher()
        except engine.BroadcastError as exc:
            messagebox.showerror("Couldn't arm", str(exc))
            return
        self._refresh_station_status()

    def _disarm_station(self) -> None:
        engine.disable_watcher()
        self._refresh_station_status()

    def _refresh_station_status(self) -> None:
        if engine.watcher_enabled():
            self.station_status.configure(text="● Armed — unplugging erases the app's data",
                                          foreground=PALETTE["error"])
        else:
            self.station_status.configure(text="○ Off", foreground=PALETTE["muted"])

    def _refresh_power(self) -> None:
        if engine.on_ac_power():
            self.power_label.configure(text="·  AC power", foreground=PALETTE["ok"])
        else:
            self.power_label.configure(text="·  on battery", foreground=PALETTE["error"])

    def _health_tick(self) -> None:
        if self._screen == "main":
            self._refresh_power()
            # If station mode wiped us while the window was open, fall back to linking.
            if engine.watcher_enabled() and not engine.is_linked():
                self.show_link()
        self.after(2000, self._health_tick)

    # --------------------------------------------------------------- sending
    def _on_save(self) -> None:
        """Persist the message + images without sending, so the scheduled run
        picks up the new text on its next fire."""
        text = self.msg_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Empty message", "Type a message before saving.")
            return
        engine.write_message(text)
        engine.write_attachments(self.selected_images)
        self._log("Saved. The schedule will send this next time it runs.", "ok")

    def _on_send(self) -> None:
        text = self.msg_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Empty message", "Type a message before sending.")
            return
        engine.write_message(text)
        engine.write_attachments(self.selected_images)
        try:
            cfg = engine.load_config()
            message = engine.read_message()
            attachments = engine.read_attachments()
            groups = engine.read_groups()
        except engine.BroadcastError as exc:
            messagebox.showerror("Can't send", str(exc))
            return
        if not self._confirm_send(cfg, groups, message, attachments):
            return
        blocked = engine.cooldown_blocks_run(cfg.cooldown_hours)
        if blocked and not messagebox.askyesno("Cooldown", f"{blocked}\n\nSend anyway?"):
            return
        self._begin_send(cfg, groups, message, attachments)

    def _confirm_send(self, cfg, groups, message, attachments) -> bool:
        """Last check before a real blast: show count, preview, and rough duration."""
        preview = next((ln for ln in message.splitlines() if ln.strip()), "")[:80]
        imgs = len(attachments)
        img_note = f"{imgs} image(s) attached." if imgs else "No images (text only)."
        mins = max(1, round(len(groups) * max(engine.MIN_DELAY_S, cfg.base_delay_seconds) / 60))
        return messagebox.askyesno("Send now?",
            f"Send to {len(groups)} groups?\n\n“{preview}”\n{img_note}\n\n"
            f"This takes about {mins} min (longer if Signal throttles). "
            "Keep the Mac awake and this app open.")

    def _on_resend(self) -> None:
        if not self.failed_results:
            return
        try:
            cfg = engine.load_config()
            message = engine.read_message()
            attachments = engine.read_attachments()
        except engine.BroadcastError as exc:
            messagebox.showerror("Can't resend", str(exc))
            return
        groups = [(r.group_id, r.name) for r in self.failed_results]
        self._begin_send(cfg, groups, message, attachments)

    def _on_stop(self) -> None:
        """A stop can only take effect between groups (after the in-flight send
        returns), so acknowledge the click immediately: disable the button and say
        we're stopping. Prevents confused re-clicks during the brief wait."""
        self.stop_event.set()
        self.stop_btn.configure(state="disabled", text="Stopping…")
        self._log("Stopping — finishing the current group first…", "muted")

    def _begin_send(self, cfg, groups, message, attachments) -> None:
        self.stop_event.clear()
        self.failed_results = []
        self._sending_groups = list(groups)  # so a stop can resume the un-sent tail
        self.send_btn.set_enabled(False)
        self.resend_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal", text="Stop")
        self.progress.configure(maximum=max(1, len(groups)), value=0)
        self.counter.configure(text=f"0 / {len(groups)}")
        threading.Thread(target=self._send_worker,
                         args=(cfg, groups, message, attachments), daemon=True).start()

    def _send_worker(self, cfg, groups, message, attachments) -> None:
        try:
            results = engine.broadcast(
                config=cfg, groups=groups, message=message, attachments=attachments,
                on_log=lambda m: self.events.put(("log", m)),
                on_progress=lambda d, t, n, ok: self.events.put(("progress", (d, t, n, ok))),
                should_stop=self.stop_event.is_set)
            if not self.stop_event.is_set():  # a stopped run is incomplete — don't arm the cooldown
                engine.stamp_run()
                engine.write_run_summary(results)
            self.events.put(("send_done", results))
        except engine.BroadcastError as exc:
            self.events.put(("log", f"Error: {exc}"))
            self.events.put(("send_done", []))

    # ----------------------------------------------------------- misc actions
    def _refresh_groups(self) -> None:
        if self._refreshing:                 # one sync at a time — re-clicks are ignored
            return
        self._refreshing = True
        self.refresh_btn.configure(state="disabled")
        self.groups_progress.pack(anchor="w", pady=(4, 0))
        self.groups_progress.start()
        self.groups_sync_label.configure(text="Syncing…", foreground=PALETTE["muted"])

        def work():
            try:
                number = engine.detect_account() or engine.load_config().account
                count = engine.sync_groups(number, on_log=lambda m: self.events.put(("refresh_status", m)))
                self.events.put(("refresh_done", count))
            except engine.BroadcastError as exc:
                self.events.put(("refresh_done", f"Error: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _finish_refresh(self, result) -> None:
        self._refreshing = False
        self.refresh_btn.configure(state="normal")
        self.groups_progress.stop()
        self.groups_progress.pack_forget()
        if isinstance(result, int):
            self.groups_sync_label.configure(text=f"Updated — {result} groups.",
                                             foreground=PALETTE["muted"])
            self._populate_groups()
            self._refresh_status()
        else:
            self.groups_sync_label.configure(text=result, foreground=PALETTE["error"])

    def _unlink(self) -> None:
        if not messagebox.askyesno("Unlink and erase the app's data?",
                "This signs this Mac out of Signal and deletes all the data this app "
                "stored here — the link keys, your groups, the message, the schedule, "
                "and logs. Nothing else on the Mac is touched, and nothing personal is "
                "left behind.\n\nUse this before handing the Mac to someone else. Your "
                "phone is not affected.\n\nContinue?", icon="warning"):
            return
        try:
            engine.unlink()
            engine.disable_watcher()
        except Exception as exc:
            messagebox.showerror("Couldn't fully erase", str(exc))
            return
        messagebox.showinfo("Erased", "Signed out and erased. To also remove this Mac "
                            "from your account, open Signal on your phone → Settings → "
                            "Linked Devices and delete it there.")
        self.show_link()

    # --------------------------------------------------------------- event loop
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _handle(self, kind: str, payload) -> None:
        if kind == "qr":
            self._qr_img = tk.PhotoImage(file=payload)
            self.qr_label.configure(image=self._qr_img, text="")
        elif kind == "link_status":
            self.link_status.configure(text=payload)
        elif kind == "link_error":
            self._stop_link_progress()
            self.link_status.configure(text=payload, foreground=PALETTE["error"])
            self.link_retry.configure(state="normal", text="Try again")
        elif kind == "linked_done":
            self._stop_link_progress()
            self.show_main()
        elif kind == "log":
            m = payload
            low = m.lower()
            # Final failures are red; retries ("backing off", "retrying") stay neutral.
            if "gave up" in low or "failed after retries" in low or low.startswith("error"):
                tag = "error"
            elif m.startswith("Done"):
                tag = "ok"
            else:
                tag = "muted"
            self._log(m, tag)
        elif kind == "progress":
            done, total, _name, ok = payload
            self.progress.configure(value=done)
            self.counter.configure(text=f"{done} / {total}")
            self._log(f"[{done}/{total}] sent" if ok else f"[{done}/{total}] failed",
                      "ok" if ok else "error")
        elif kind == "send_done":
            self._finish_send(payload)
        elif kind == "refresh_status":
            if hasattr(self, "groups_sync_label"):
                self.groups_sync_label.configure(text=payload)
        elif kind == "refresh_done":
            self._finish_refresh(payload)

    def _finish_send(self, results: list[engine.GroupSendResult]) -> None:
        self.stop_btn.configure(state="disabled", text="Stop")
        self.send_btn.set_enabled(True)
        stopped = self.stop_event.is_set()
        failed = [r for r in results if not r.ok]
        sent = len(results) - len(failed)
        # On a stop, the groups never reached are resumable too — fold them in so
        # “Resend failed” finishes the run.
        pending: list[engine.GroupSendResult] = []
        if stopped:
            done_ids = {r.group_id for r in results}
            pending = [engine.GroupSendResult(gid, name, False)
                       for gid, name in getattr(self, "_sending_groups", [])
                       if gid not in done_ids]
        self.failed_results = failed + pending
        if stopped:
            self._log(f"Stopped. Sent {sent}; {len(self.failed_results)} not sent.", "muted")
        else:
            self._log(f"Done. Sent {sent}, failed {len(failed)}.",
                      "error" if failed else "ok")
        if self.failed_results:
            out = engine.write_failures(self.failed_results)
            label = "Unsent + failed" if stopped else "Failed"
            verb = "finish." if stopped else "retry them."
            self._log(f"{label} groups saved to {out}. Use “Resend failed” to {verb}", "muted")
            self.resend_btn.configure(state="normal")
        self._refresh_status()
        if hasattr(self, "last_send_label"):
            self._refresh_last_send()


if __name__ == "__main__":
    App().mainloop()
