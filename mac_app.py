"""Mac interface for the local security service. No inactivity timer."""
from __future__ import annotations

import queue
import os
import subprocess
import sys
import engine
import resource
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from mac_security import SecurityError
from mac_service import Client
from mac_photos import Thumbnails
from photo_strip import PhotoStrip
from ui_theme import PALETTE


def operation_status(data, pending=None):
    if pending == "stop":
        return "Stopping… Waiting for the background service to confirm."
    kind = data.get("job") or pending
    if kind:
        if kind in ("send", "resume", "retry") and data.get("phase") == "preparing":
            return "Preparing message and photos…"
        return {"send": "Sending…", "resume": "Sending remaining groups…",
                "sync": "Updating groups…", "notes": "Checking for new notes…",
                "link": "Linking…", "health": "Checking the Signal link…",
                "import": "Adding and verifying photos…", "update": "Checking for updates…",
                "retry": "Retrying failed groups…"}.get(kind, "Working…")
    last = data.get("last_operation") or {}
    name = {"send": "Broadcast", "resume": "Broadcast", "retry": "Broadcast", "update": "Update", "sync": "Group update",
            "notes": "Notes check", "link": "Linking", "health": "Link check"}.get(last.get("kind"), "Operation")
    if last.get("outcome") == "stopped":
        suffix = " Messages already dispatched may still arrive." if last.get("kind") in ("send", "resume", "retry") else ""
        return f"{name} stopped.{suffix}"
    if last.get("outcome") == "failed":
        return f"{name} failed. Check the activity below before trying again."
    if last.get("outcome") == "completed":
        summary = data.get("summary")
        if last.get("kind") in ("send", "resume", "retry") and summary:
            return (f"Broadcast finished. {summary.get('sent', 0)} sent, {summary.get('skipped', 0)} skipped, "
                    f"{summary.get('failed', 0)} failed, {summary.get('uncertain', 0)} unconfirmed.")
        return f"{name} finished."
    if data.get("interrupted"):
        return "Broadcast interrupted. Review the remaining groups below."
    return "Ready to send."


class App(tk.Tk):
    def __init__(self, client=None):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        super().__init__()
        self.client = client or Client()
        self.title("Signal Broadcast")
        self.geometry("760x780")
        self.minsize(620, 650)
        self.responses = queue.Queue()
        self.closing = threading.Event()
        self.authentication = None
        self.generation = 0
        self.sequence = 0
        self.polling = False
        self.screen = ""
        self.data = {}
        self.images = []
        self.previews = []
        self.busy = False
        self.container = ttk.Frame(self, padding=16)
        self.container.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.createcommand("tk::mac::Quit", self.close)
        self.show_login()
        self.after(80, self.drain)
        self.after(500, self.poll)

    def request(self, operation, callback=None, on_error=None, **values):
        generation, token = self.generation, self.client.token
        client = Client(self.client.root)
        client.token = token
        authenticating = operation in ("setup", "unlock")
        if authenticating:
            self.authentication = client
        def work():
            try:
                value, error = client.call(operation, **values), None
            except SecurityError as exc:
                value, error = None, exc
            if self.closing.is_set() or generation != self.generation:
                if authenticating and client.token:
                    try:
                        client.call("lock")
                    except SecurityError:
                        pass
                return
            if error and on_error and error.code not in ("locked", "unavailable"):
                self.responses.put((generation, on_error, error, None))
            else:
                self.responses.put((generation, callback, value, error))
        # Finish revoking an in-flight authentication even after the window quits.
        threading.Thread(target=work, daemon=not authenticating).start()

    def drain(self):
        while True:
            try:
                generation, callback, value, error = self.responses.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            if error:
                self.auth_pending = False
                if self.screen != "login" and error.code in ("locked", "unavailable"):
                    self.show_login(str(error))
                else:
                    self.notice.set(str(error))
                self.polling = False
                if hasattr(self, "login_button") and self.screen == "login":
                    self.login_button.configure(state="normal")
            elif callback:
                callback(value)
        self.after(80, self.drain)

    def clear(self):
        if getattr(self, "thumbnailer", None):
            self.thumbnailer.close()
            self.thumbnailer = None
        self.generation += 1
        while True:
            try:
                self.responses.get_nowait()
            except queue.Empty:
                break
        for child in self.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
        for child in self.container.winfo_children():
            child.destroy()
        self.data, self.images, self.previews = {}, [], []
        self.qr_image = None
        self.notes_signature = self.group_signature = None
        self.pending_action = None
        self.activity_kind = None
        self.activity_started = None
        self.last_activity = None
        self.progress_caption = ""
        self.polling = False
        self.notice = tk.StringVar()
        self.group_choices = {}
        self.update_prompted = False
        self.login_state = {}

    def show_login(self, text=""):
        self.clear()
        self.client.token = None
        self.screen = "login"
        self.setup_required = False
        self.auth_pending = False
        ttk.Label(self.container, text="Signal Broadcast", font=("", 22, "bold")).pack(anchor="w", pady=20)
        self.login_description = ttk.Label(self.container, text="Checking local security…", wraplength=640)
        self.login_description.pack(anchor="w", pady=10)
        ttk.Label(self.container, text="Password").pack(anchor="w")
        self.password = ttk.Entry(self.container, show="•", width=40)
        self.password.pack(anchor="w", pady=8)
        self.password.bind("<Return>", lambda _: self.login())
        self.confirm_label = ttk.Label(self.container, text="Confirm password")
        self.confirm = ttk.Entry(self.container, show="•", width=40)
        self.login_button = ttk.Button(self.container, text="Unlock", command=self.login, state="disabled")
        self.login_button.pack(anchor="w", pady=12)
        self.update_button = ttk.Button(self.container, text="Update", command=self.check_update, state="disabled")
        self.update_button.pack(anchor="w")
        self.login_update_text = tk.StringVar()
        ttk.Label(self.container, textvariable=self.login_update_text, wraplength=640).pack(anchor="w", pady=8)
        self.login_update_progress = ttk.Progressbar(self.container, mode="indeterminate")
        ttk.Label(self.container, textvariable=self.notice, wraplength=640).pack(anchor="w", pady=10)
        ttk.Separator(self.container).pack(fill="x", pady=12)
        ttk.Label(self.container, text="Three consecutive incorrect passwords erase this installation.\n"
            "Background sending may continue while this screen is locked.", wraplength=640).pack(anchor="w", pady=10)
        ttk.Button(self.container, text="Log out and erase", command=self.erase).pack(anchor="w")
        self.notice.set(text)
        self.request("status", self.login_status)
        self.password.focus_set()

    def login_status(self, state):
        if self.auth_pending:
            return
        self.login_state = state
        self.refresh_login_update()
        self.setup_required = state["setup_required"]
        if state["state"] == "erasing":
            self.notice.set("Erasure is incomplete. Use Log out and erase to retry cleanup.")
            self.login_button.configure(state="disabled")
            return
        if self.setup_required:
            self.login_description.configure(text="Set a local password of at least 12 characters before linking. "
                "Existing data will be moved into encrypted storage. There is no password recovery. "
                "Previously attached originals will be moved into the vault after verification.")
            self.confirm_label.pack(before=self.login_button, anchor="w")
            self.confirm.pack(before=self.login_button, anchor="w", pady=8)
            self.login_button.configure(text="Set password and protect data", state="normal")
        else:
            self.login_description.configure(text=f"Enter your password. {state['attempts_remaining']} attempts remain.")
            self.login_button.configure(text="Unlock", state="normal")
        if state.get("updating") or self.pending_action == "update":
            self.login_button.configure(state="disabled")

    def refresh_login_update(self):
        state = self.login_state
        active = state.get("updating") or self.pending_action == "update"
        blocked = active or self.auth_pending or state.get("background_running") or state.get("state") == "erasing"
        update = state.get("update") or {}
        if active:
            self.login_update_text.set("Checking and downloading app updates… Your Signal link and saved data are kept.")
            if not self.login_update_progress.winfo_manager():
                self.login_update_progress.pack(fill="x", before=self.update_button, pady=8)
                self.login_update_progress.start(16)
        else:
            self.login_update_progress.stop()
            self.login_update_progress.pack_forget()
            self.login_update_text.set("Wait for the background operation to finish before updating."
                                       if state.get("background_running") else "Updates keep your Signal link and saved data.")
            self.show_update_result(update)
        self.update_button.configure(text="Finish update" if update.get("changed") and not active else "Update",
                                     state="disabled" if blocked else "normal")

    def show_update_result(self, update):
        if update and not self.update_prompted:
            self.update_prompted = True
            self.notice.set("Update downloaded. Click Finish update to install it." if update.get("changed")
                            else ("Could not download the update. Check your connection and try again." if update.get("error")
                                  else "You are on the latest version."))
            self.update_button.configure(text="Finish update" if update.get("changed") else "Update", state="normal")

    def login(self):
        if self.auth_pending or str(self.login_button.cget("state")) == "disabled":
            return
        password = self.password.get()
        confirmation = self.confirm.get()
        if self.setup_required and password != confirmation:
            self.notice.set("The passwords do not match. No attempt was counted.")
            return
        self.password.delete(0, "end")
        self.confirm.delete(0, "end")
        self.login_button.configure(state="disabled")
        self.auth_pending = True
        self.update_button.configure(state="disabled")
        self.notice.set("Opening protected storage…")
        self.request("setup" if self.setup_required else "unlock", self.authenticated, password=password)

    def authenticated(self, result):
        self.auth_pending = False
        self.client.token = result["token"]
        self.request("snapshot", self.initial_snapshot)

    def initial_snapshot(self, data):
        self.clear()
        self.data = data
        self.sequence = 0
        self.screen = "main" if data["linked"] else "link"
        self.header()
        if self.screen == "link":
            ttk.Label(self.container, text="Link this Mac", font=("", 18, "bold")).pack(anchor="w", pady=18)
            ttk.Label(self.container, text="On your phone: Signal → Settings → Linked Devices → Add.\n"
                "Scan the code below. This does not log out your phone.", wraplength=640).pack(anchor="w")
            self.qr = ttk.Label(self.container)
            self.qr.pack(pady=12)
            ttk.Button(self.container, text="Start linking", command=lambda: self.job("link")).pack()
        else:
            self.build_tabs()
        ttk.Label(self.container, textvariable=self.notice, wraplength=680).pack(fill="x", pady=10)
        self.apply_snapshot(data)

    def header(self):
        header = ttk.Frame(self.container)
        header.pack(fill="x")
        ttk.Label(header, text="Signal Broadcast", font=("", 16, "bold")).pack(side="left")
        ttk.Button(header, text="Lock now", command=self.lock).pack(side="right")
        self.update_button = ttk.Button(header, text="Update", command=self.check_update)
        self.update_button.pack(side="right", padx=8)
        ttk.Label(header, text=f"v{self.data.get('version', '')}").pack(side="right", padx=8)
        if self.screen == "main":
            status = ttk.Frame(self.container)
            status.pack(fill="x", pady=(12, 0))
            self.operation_text = tk.StringVar(value="Ready to send.")
            status_row = ttk.Frame(status)
            status_row.pack(fill="x")
            ttk.Label(status_row, textvariable=self.operation_text, font=("", 13, "bold"), wraplength=500).pack(side="left")
            self.stop_button = ttk.Button(status_row, text="Stop broadcast", command=self.stop)
            self.operation_progress = ttk.Progressbar(status, mode="indeterminate")
            self.activity_hint = tk.StringVar()
            ttk.Label(status, textvariable=self.activity_hint, wraplength=650).pack(anchor="w", pady=(4, 0))

    def build_tabs(self):
        self.tabs = ttk.Notebook(self.container)
        self.tabs.pack(fill="both", expand=True, pady=12)
        frames = {}
        for name in ("Send", "Notes", "Groups", "Schedule", "Security"):
            frame = ttk.Frame(self.tabs, padding=12)
            self.tabs.add(frame, text=name)
            frames[name] = frame
        send = frames["Send"]
        self.recipient_text = tk.StringVar()
        ttk.Label(send, textvariable=self.recipient_text).pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(send)
        row.pack(fill="x", pady=10)
        self.send_button = ttk.Button(row, text="Send now", command=self.send)
        self.send_button.pack(side="left")
        self.save_button = ttk.Button(row, text="Save draft", command=self.save)
        self.save_button.pack(side="right")
        self.retry_button = ttk.Button(row, text="Retry failed groups", command=self.retry_failed)
        ttk.Label(send, text="Message").pack(anchor="w")
        self.message = tk.Text(send, height=4, wrap="word", background=PALETTE["text_bg"], foreground=PALETTE["text_fg"], insertbackground=PALETTE["text_fg"])
        self.message.pack(fill="both", expand=True)
        self.message.insert("1.0", self.data["message"])
        style_row = ttk.Frame(send)
        style_row.pack(fill="x", pady=(4, 0))
        ttk.Label(style_row, text="Formatting").pack(side="left")
        self.style = tk.StringVar(value=dict(engine.MESSAGE_STYLE_LABELS)[self.data["config"].get("message_style", "none")])
        self.style_picker = ttk.Combobox(style_row, textvariable=self.style, state="readonly", width=18,
                                         values=[label for _, label in engine.MESSAGE_STYLE_LABELS])
        self.style_picker.pack(side="left", padx=8)
        self.style_picker.bind("<<ComboboxSelected>>", lambda _: self.preview_style())
        self.preview_style()
        self.images = list(self.data["attachments"])
        self.thumbnailer = Thumbnails()
        self.photo_strip = PhotoStrip(send, self.photos_changed, palette=PALETTE,
                                      make_thumbnail=self.thumbnailer.make)
        self.photo_strip.pack(fill="x", pady=8)
        self.refresh_images()
        row = ttk.Frame(send)
        row.pack(fill="x")
        self.add_photos_button = ttk.Button(row, text="Add photos…", command=self.import_images)
        self.add_photos_button.pack(side="left")
        self.clear_photos_button = ttk.Button(row, text="Clear all photos", command=self.clear_photos)
        self.clear_photos_button.pack(side="left", padx=8)
        self.recovery = ttk.LabelFrame(send, text="Interrupted broadcast", padding=8)
        self.recovery_text = ttk.Label(self.recovery, wraplength=620)
        self.recovery_text.pack(anchor="w")
        self.resume_button = ttk.Button(self.recovery, text="Resume remaining", command=lambda: self.job("resume"))
        self.resume_button.pack(side="left", pady=(8, 0))
        self.discard_button = ttk.Button(self.recovery, text="Discard this run…", command=self.discard)
        self.discard_button.pack(side="left", padx=8, pady=(8, 0))
        self.activity_label = ttk.Label(send, text="Recent activity")
        self.activity_label.pack(anchor="w", pady=(8, 0))
        self.activity = tk.Text(send, height=4, state="disabled", wrap="word", relief="flat", background=PALETTE["text_bg"], foreground=PALETTE["text_fg"])
        self.activity.pack(fill="x", pady=(6, 0))

        notes = frames["Notes"]
        self.notes_button = ttk.Button(notes, text="Check for new notes", command=lambda: self.job("notes"))
        self.notes_button.pack(anchor="w")
        self.note_list = tk.Listbox(notes, height=5, exportselection=False)
        self.note_list.pack(fill="both", expand=True, pady=10)
        self.note_list.bind("<<ListboxSelect>>", lambda _: self.preview_note())
        self.note_list.bind("<Double-Button-1>", lambda _: self.use_note())
        self.note_preview = tk.Text(notes, height=10, wrap="word", state="disabled",
                                    background=PALETTE["text_bg"], foreground=PALETTE["text_fg"])
        self.note_preview.pack(fill="both", expand=True)
        self.note_details = tk.StringVar()
        ttk.Label(notes, textvariable=self.note_details, wraplength=600).pack(anchor="w", pady=8)
        row = ttk.Frame(notes)
        row.pack(fill="x")
        ttk.Button(row, text="Use as message", command=self.use_note).pack(side="left")
        ttk.Button(row, text="Copy text", command=self.copy_note).pack(side="left", padx=8)
        ttk.Button(row, text="Delete local note", command=self.delete_note).pack(side="left", padx=8)
        self.notes_signature = None

        groups = frames["Groups"]
        self.sync_button = ttk.Button(groups, text="Update list from phone", command=lambda: self.job("sync"))
        self.sync_button.pack(anchor="w")
        search_row = ttk.Frame(groups)
        search_row.pack(fill="x", pady=8)
        ttk.Label(search_row, text="Search groups").pack(side="left")
        self.group_query = tk.StringVar()
        ttk.Entry(search_row, textvariable=self.group_query).pack(side="left", fill="x", expand=True, padx=8)
        self.group_query.trace_add("write", lambda *_: self.render_groups())
        self.group_list = ttk.Treeview(groups, columns=("enabled", "name"), show="headings")
        self.group_list.heading("enabled", text="Send")
        self.group_list.column("enabled", width=65, stretch=False)
        self.group_list.heading("name", text="Group")
        self.group_list.pack(fill="both", expand=True, pady=10)
        self.group_list.bind("<ButtonRelease-1>", self.toggle_group)
        self.group_signature = None
        group_actions = ttk.Frame(groups)
        group_actions.pack(fill="x")
        ttk.Button(group_actions, text="Select visible", command=lambda: self.select_groups(True)).pack(side="left")
        ttk.Button(group_actions, text="Deselect visible", command=lambda: self.select_groups(False)).pack(side="left", padx=8)
        ttk.Button(group_actions, text="Save selection", command=self.save_groups).pack(side="right")
        self.group_count = tk.StringVar()
        ttk.Label(groups, textvariable=self.group_count).pack(anchor="w", pady=8)

        schedule = frames["Schedule"]
        self.last_send_text = tk.StringVar()
        ttk.Label(schedule, textvariable=self.last_send_text, wraplength=600).pack(anchor="w", pady=8)
        self.schedule_status = tk.StringVar()
        ttk.Label(schedule, textvariable=self.schedule_status, wraplength=600).pack(anchor="w", pady=8)
        ttk.Label(schedule, text="Daily send times on this Mac, in 24-hour HH:MM, separated by commas.", wraplength=600).pack(anchor="w", pady=6)
        self.times = ttk.Entry(schedule, width=45)
        self.times.pack(anchor="w")
        self.times.insert(0, ", ".join(self.data["schedule"]["times"]))
        self.schedule_enabled = tk.BooleanVar(value=self.data["schedule"]["enabled"])
        ttk.Checkbutton(schedule, text="Enable saved schedule", variable=self.schedule_enabled).pack(anchor="w", pady=8)
        ttk.Button(schedule, text="Save schedule", command=self.save_schedule).pack(anchor="w")
        ttk.Label(schedule, text="Uses the saved message, photos and group selection. Save draft changes in Send first. "
            "Only one broadcast runs at a time. Missed times combine into one pending send, which expires after one hour. "
            "Sending pace and cooldown in Security can delay it.", wraplength=600).pack(anchor="w", pady=8)
        ttk.Label(schedule, text="Locking or closing the window keeps scheduling active. Log out and erase stops it. "
            "Keep the Mac awake for on-time sends. Active broadcasts prevent idle sleep. "
            "After a service restart, unlock once; pending sends still within the one-hour limit can run.",
            wraplength=600).pack(anchor="w", pady=8)
        ttk.Label(schedule, text="Schedule activity").pack(anchor="w")
        self.schedule_history = tk.Text(schedule, height=5, state="disabled", wrap="word",
            background=PALETTE["text_bg"], foreground=PALETTE["text_fg"])
        self.schedule_history.pack(fill="both", expand=True)
        self.schedule_history_signature = None

        security = frames["Security"]
        ttk.Label(security, text="Manual locking only. There is no inactivity timer.\n"
            "Locking hides this interface while sending continues.\n"
            "Three wrong passwords or Log out and erase stop work and delete local data.", wraplength=600).pack(anchor="w", pady=10)
        ttk.Button(security, text="Change password", command=self.change_password).pack(anchor="w", pady=8)
        ttk.Button(security, text="Lock now", command=self.lock).pack(anchor="w", pady=8)
        ttk.Button(security, text="Log out and erase", command=self.erase).pack(anchor="w", pady=8)
        ttk.Button(security, text="Clear logs", command=lambda: self.request("clear_logs")).pack(anchor="w", pady=8)
        self.setting_fields = {}
        for key, label in (("base_delay_seconds", "Seconds between sends"), ("jitter_seconds", "Timing variation"),
                           ("cooldown_hours", "Hours between broadcasts"), ("concurrent_sends", "Concurrent sends, 1–5")):
            row = ttk.Frame(security)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=30).pack(side="left")
            entry = ttk.Entry(row, width=12)
            entry.insert(0, str(self.data["config"][key]))
            entry.pack(side="left")
            self.setting_fields[key] = entry
        ttk.Button(security, text="Save sending settings", command=lambda: self.request("settings", values={
            key: field.get() for key, field in self.setting_fields.items()})).pack(anchor="w", pady=8)
        ttk.Label(security, text="All app data stays on this Mac. Keep FileVault enabled. "
            "The background service can access data while running; this is not an operating-system security boundary.", wraplength=600).pack(anchor="w", pady=12)

    def poll(self):
        if self.screen == "main":
            self.refresh_elapsed()
        if self.screen != "login" and not self.polling:
            self.polling = True
            self.request("snapshot", self.apply_snapshot, after=self.sequence)
        elif self.screen == "login" and not self.polling:
            self.polling = True
            def status(value):
                self.polling = False
                self.login_status(value)
            self.request("status", status)
        self.after(1000, self.poll)

    def apply_snapshot(self, data):
        self.polling = False
        if data["sequence"] < self.sequence:
            return
        if self.screen == "link" and data["linked"]:
            self.initial_snapshot(data)
            self.job("sync")
            return
        self.data = data
        self.busy = bool(data["job"])
        if self.screen == "main":
            signature = [(g["group_id"], g["name"], g["enabled"]) for g in data["groups"]]
            if signature != self.group_signature:
                for group_id, name, enabled in signature:
                    self.group_choices.setdefault(group_id, enabled)
                self.group_choices = {gid: self.group_choices[gid] for gid, _, _ in signature}
                self.group_signature = signature
                self.render_groups()
            notes_sig = [(n["ts"], n.get("text", ""), len(n.get("photos", []))) for n in data["notes"]]
            if notes_sig != self.notes_signature:
                selected = self.note_list.curselection()
                selected_ts = self.notes_signature[selected[0]][0] if selected and self.notes_signature else None
                self.note_list.delete(0, "end")
                for timestamp, text, count in notes_sig:
                    self.note_list.insert("end", f"{datetime.fromtimestamp(timestamp / 1000):%d %b %H:%M}  {text[:80]}  ({count} photos)")
                self.notes_signature = notes_sig
                if notes_sig:
                    index = next((i for i, n in enumerate(data["notes"]) if n["ts"] == selected_ts), 0)
                    self.note_list.selection_set(index)
                self.preview_note()
        for event in data["events"]:
            if event["id"] <= self.sequence:
                continue
            kind, value = event["kind"], event["value"]
            if kind == "qr" and self.screen == "link":
                self.qr_image = tk.PhotoImage(data=value)
                self.qr.configure(image=self.qr_image)
            elif self.screen == "main":
                if kind == "receive_status":
                    self.notice.set(str(value))
                    self.add_activity(str(value))
                elif kind == "error":
                    self.notice.set(str(value))
                    self.add_activity(str(value))
                elif kind == "started":
                    self.progress_caption = ""
                    self.add_activity(operation_status({"job": value}))
                elif kind == "progress":
                    result = {"sent": "Message sent", "failed": "Send failed", "skipped": "Group skipped",
                              "uncertain": "Delivery not confirmed"}.get(value["status"], "Group processed")
                    self.progress_caption = f"{value['done']} of {value['total']} groups processed."
                    self.add_activity(f"{result}. {self.progress_caption}")
                elif kind == "phase":
                    text = {"preparing": "Preparing your saved message and photos…",
                            "sending": "Sending your message. Photo uploads can take a while.",
                            "sync": "Getting the latest groups from Signal…",
                            "notes": "Receiving notes and downloading their photos…"}.get(value)
                    if text:
                        self.add_activity(text)
                elif kind in ("stopped", "finished"):
                    self.add_activity(operation_status(data))
        self.sequence = data["sequence"]
        update = data.get("update")
        if update and not data.get("job"):
            self.show_update_result(update)
        if self.screen == "main":
            self.refresh_controls()

    def refresh_controls(self):
        active = self.pending_action or self.data.get("job")
        updating = bool((self.data.get("update") or {}).get("changed"))
        blocked = bool(active) or updating
        sending = active in ("send", "resume", "retry", "stop")
        self.update_button.configure(text="Finish update" if updating else "Update", state="disabled" if active else "normal")
        count = sum(group["enabled"] for group in self.data["groups"])
        self.operation_text.set(operation_status(self.data, self.pending_action))
        if active != self.activity_kind:
            self.activity_kind = active
            self.activity_started = time.monotonic() if active else None
            self.operation_progress.stop()
            if active:
                self.operation_progress.pack(fill="x", pady=(6, 0))
                self.operation_progress.start(16)
            else:
                self.operation_progress.pack_forget()
        self.refresh_elapsed()
        schedule = self.data["schedule"]
        self.refresh_schedule(schedule)
        suffix = " Scheduled sends remain enabled." if schedule["enabled"] else ""
        self.recipient_text.set(f"{count} groups selected · {len(self.images)} photos.{suffix}")
        self.send_button.configure(text=f"Send to {count} groups", state="disabled" if blocked or not count or self.data.get("interrupted") else "normal")
        self.save_button.configure(state="disabled" if blocked else "normal")
        self.add_photos_button.configure(state="disabled" if blocked else "normal")
        self.clear_photos_button.configure(state="disabled" if blocked or not self.images else "normal")
        self.style_picker.configure(state="disabled" if blocked else "readonly")
        count_failed = self.data.get("retry_count", 0)
        if count_failed and not self.data.get("interrupted"):
            self.retry_button.configure(text=f"Retry {count_failed} failed", state="disabled" if blocked else "normal")
            self.retry_button.pack(side="left", padx=8)
        else:
            self.retry_button.pack_forget()
        summary = self.data.get("summary")
        self.last_send_text.set("No completed broadcast yet." if not summary else
            f"Last broadcast: {summary['at']}\n{summary['sent']} sent, {summary['failed']} failed, "
            f"{summary.get('skipped', 0)} skipped, {summary.get('uncertain', 0)} unconfirmed.")
        self.notes_button.configure(state="disabled" if blocked else "normal")
        self.sync_button.configure(state="disabled" if blocked else "normal")
        self.message.configure(state="disabled" if sending else "normal")
        if self.photo_strip._enabled == bool(sending):
            self.photo_strip.set_enabled(not sending)
        if active and self.pending_action != "import" and active != "update":
            self.stop_button.pack(side="right", padx=(8, 0))
            label = "Stop broadcast" if sending else "Cancel"
            self.stop_button.configure(text="Stopping…" if self.pending_action == "stop" else label,
                                       state="disabled" if self.pending_action else "normal")
        else:
            self.stop_button.pack_forget()
        interrupted = self.data.get("interrupted")
        if interrupted and not blocked:
            remaining = len(interrupted.get("remaining", []))
            self.recovery_text.configure(text=f"{remaining} groups remain in the saved broadcast. Resume uses the saved draft.")
            self.recovery.pack(fill="x", before=self.activity_label, pady=(4, 8))
        else:
            self.recovery.pack_forget()

    def refresh_schedule(self, schedule):
        if schedule.get("error"):
            text = schedule["error"]
        elif not schedule["enabled"]:
            text = "Schedule is off. An already-running broadcast continues until stopped."
        elif schedule.get("pending"):
            text = f"Pending send from {schedule['pending']}. "
            if schedule.get("history"):
                text += schedule["history"][-1]["message"]
        elif schedule.get("running"):
            text = f"Running the send scheduled for {schedule['running']}."
        else:
            from datetime import timedelta
            now = datetime.now()
            candidates = [datetime.combine((now + timedelta(days=day)).date(), datetime.min.time()).replace(
                hour=int(value.split(':')[0]), minute=int(value.split(':')[1]))
                for day in (0, 1) for value in schedule["times"]]
            upcoming = [at for at in candidates if at > now]
            text = f"Next scheduled time: {min(upcoming):%d %b %H:%M} (Mac local time)." if upcoming else "Add a daily time."
        self.schedule_status.set(text)
        history = schedule.get("history", [])
        if history != self.schedule_history_signature:
            self.schedule_history_signature = list(history)
            self.schedule_history.configure(state="normal")
            self.schedule_history.delete("1.0", "end")
            for entry in history[-30:]:
                self.schedule_history.insert("end", f"{entry['at']}  {entry['message']}\n")
            self.schedule_history.see("end")
            self.schedule_history.configure(state="disabled")

    def refresh_elapsed(self):
        if self.activity_started is None:
            self.activity_hint.set("")
            return
        elapsed = int(time.monotonic() - self.activity_started)
        detail = "Waiting for the send processes to exit." if self.pending_action == "stop" else self.progress_caption
        status = self.data.get("send_progress")
        if status and self.data.get("job") in ("send", "resume", "retry") and self.pending_action != "stop":
            active = status["active"]
            detail = (f"{active} send{'s' if active != 1 else ''} in progress · "
                      f"{status['completed']} of {status['total']} groups processed.")
            if not active and status["completed"] < status["total"]:
                detail += " Waiting before the next send."
        self.activity_hint.set(f"Working for {elapsed // 60}:{elapsed % 60:02d}. {detail}")

    def add_activity(self, text):
        if text == self.last_activity:
            return
        self.last_activity = text
        self.activity.configure(state="normal")
        self.activity.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        if int(self.activity.index("end-1c").split(".")[0]) > 100:
            self.activity.delete("1.0", "2.0")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def action_failed(self, error):
        stopping = self.pending_action == "stop"
        self.pending_action = None
        self.notice.set(("Stop not confirmed. " if stopping else "") + str(error))
        if self.screen == "main":
            self.add_activity(self.notice.get())
            self.refresh_controls()
        elif self.screen == "login":
            self.refresh_login_update()

    def action_snapshot(self, _=None):
        def apply(data):
            stopped = self.pending_action == "stop" and not data.get("job")
            self.pending_action = None
            self.apply_snapshot(data)
            if stopped:
                self.notice.set(operation_status(data))
        self.request("snapshot", apply, on_error=self.action_failed, after=self.sequence)

    def job(self, kind):
        self.pending_action = kind
        self.notice.set("")
        if self.screen == "main":
            self.refresh_controls()
        self.request("job", self.action_snapshot, on_error=self.action_failed, kind=kind)

    def stop(self):
        if self.pending_action or not self.data.get("job"):
            return
        self.pending_action = "stop"
        self.notice.set("Stopping this broadcast. Messages already dispatched cannot be recalled."
                        if self.data["job"] in ("send", "resume", "retry") else "Cancelling this operation. Waiting for confirmation.")
        self.add_activity("Stop requested. Waiting for the background service to confirm.")
        self.refresh_controls()
        self.request("stop", self.action_snapshot, on_error=self.action_failed)

    def discard(self):
        if messagebox.askyesno("Discard interrupted broadcast?", "Forget the remaining groups in this run? Your draft and photos are kept.", default="no", parent=self):
            self.request("discard", self.action_snapshot, on_error=self.action_failed)

    def save(self, callback=None):
        self.request("save", callback or (lambda _: self.notice.set("Saved.")),
                     on_error=self.action_failed,
                     message=self.message.get("1.0", "end-1c"), attachments=list(self.images),
                     message_style=self.style_key())

    def send(self):
        if self.pending_action or self.data.get("job") or self.data.get("interrupted"):
            return
        count = sum(group["enabled"] for group in self.data["groups"])
        text = self.message.get("1.0", "end-1c")
        if not count:
            self.notice.set("Choose and save at least one group in Groups first.")
            return
        if not text.strip() and not self.images:
            self.notice.set("Type a message or attach an image first.")
            return
        if messagebox.askyesno("Send broadcast?", f"Send to {count} selected groups with {len(self.images)} images?\n\n"
                              f"{text[:120]}\n\nSending continues if you lock or close this window."):
            self.pending_action = "send"
            self.refresh_controls()
            self.save(lambda _: self.job("send"))

    def refresh_images(self):
        self.photo_strip.set_paths(self.images)

    def photos_changed(self, paths):
        self.images = list(paths)
        self.refresh_controls()

    def import_images(self):
        paths = list(filedialog.askopenfilenames(title="Move images into the encrypted vault",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.webp *.heic"), ("All files", "*.*")]))
        if not paths or not messagebox.askyesno("Move original images?", "After each copy is verified, its original file "
                "will be deleted from its current location. The protected copy remains in this app. Continue?"):
            return
        self.pending_action = "import"
        total = len(paths)
        self.progress_caption = f"Adding {total} photos."
        self.refresh_controls()
        def next_image(_=None):
            if paths:
                self.request("import", added, on_error=self.action_failed, path=paths.pop(0))
            else:
                self.pending_action = None
                self.notice.set(f"Added {total} photos. Drag them into send order.")
                self.add_activity(self.notice.get())
                self.refresh_controls()
        def added(result):
            self.images.append(result["path"])
            self.refresh_images()
            self.progress_caption = f"{total - len(paths)} of {total} photos added."
            self.refresh_controls()
            next_image()
        next_image()

    def style_key(self):
        return next((key for key, label in engine.MESSAGE_STYLE_LABELS if label == self.style.get()), "none")

    def preview_style(self):
        style = self.style_key()
        self.message.configure(font=("Menlo" if style == "monospace" else "TkDefaultFont", 13,
            "bold" if "bold" in style else "normal", "italic" if "italic" in style else "roman"),
            )

    def clear_photos(self):
        if self.pending_action or self.data.get("job"):
            return
        self.photos_changed([])
        self.refresh_images()

    def selected_note(self):
        selected = self.note_list.curselection()
        notes = self.data.get("notes", [])
        return notes[selected[0]] if selected and selected[0] < len(notes) else None

    def preview_note(self):
        note = self.selected_note() or {}
        self.note_preview.configure(state="normal")
        self.note_preview.delete("1.0", "end")
        self.note_preview.insert("1.0", note.get("text", ""))
        self.note_preview.configure(state="disabled")
        details = f"{len(note.get('photos', []))} photos"
        if note.get("missing_photos") or note.get("missing_body"):
            details += ". Incomplete download; forward the original note again."
        self.note_details.set(details if note else "")

    def copy_note(self):
        note = self.selected_note()
        if note:
            self.clipboard_clear()
            self.clipboard_append(note.get("text", ""))
            self.notice.set("Note text copied.")

    def render_groups(self):
        query = self.group_query.get().casefold().strip()
        self.group_list.delete(*self.group_list.get_children())
        visible = 0
        for group in self.data.get("groups", []):
            if query and query not in group["name"].casefold():
                continue
            gid = group["group_id"]
            self.group_list.insert("", "end", iid=gid,
                                   values=("✓" if self.group_choices.get(gid, group["enabled"]) else "", group["name"]))
            visible += 1
        self.group_count.set(f"{sum(self.group_choices.values())} selected · {visible} shown")

    def select_groups(self, enabled):
        for gid in self.group_list.get_children():
            self.group_choices[gid] = enabled
        self.render_groups()

    def retry_failed(self):
        if self.pending_action or self.data.get("job") or self.data.get("interrupted"):
            return
        count = self.data.get("retry_count", 0)
        if count and messagebox.askyesno("Retry failed groups?", f"Retry the previously saved message to {count} failed groups?\n\n"
                f"{self.data['message'][:120]}\n\nSuccessful and unconfirmed deliveries will not be retried.", parent=self):
            self.job("retry")

    def check_update(self):
        state = self.login_state if self.screen == "login" else self.data
        if self.pending_action or state.get("job") or state.get("background_running") or self.auth_pending:
            self.notice.set("Wait for the active operation before updating.")
            return
        update = state.get("update") or {}
        if update.get("changed"):
            if update.get("needs_setup"):
                if messagebox.askyesno("Finish installing update?", "Open the installer to update dependencies and restart the app? Your vault and Signal link are kept.", parent=self):
                    subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(Path(__file__).with_name("Setup.command"))])
                    self.close()
            elif messagebox.askyesno("Restart to update?", "Restart the app and its background service now? Your Signal link and saved data are kept. Enter your local password after restarting; no QR code or relinking is needed.", parent=self):
                self.request("restart_update", self.restart_updated_app, on_error=self.action_failed)
            return
        self.update_prompted = False
        self.pending_action = "update"
        if self.screen == "login":
            self.refresh_login_update()
            self.login_button.configure(state="disabled")
            def started(_):
                self.pending_action = None
                self.request("status", self.login_status)
            self.request("update", started, on_error=self.action_failed)
        elif self.screen == "main":
            self.refresh_controls()
            self.save(lambda _: self.job("update"))
        else:
            self.job("update")

    def restart_updated_app(self, _):
        self.show_login("Restarting the updated app…")
        self.after(1500, lambda: os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())]))

    def use_note(self):
        if self.pending_action or self.data.get("job") in ("send", "resume", "retry", "update"):
            self.notice.set("Wait for the broadcast to stop before replacing the draft.")
            return
        selected = self.note_list.curselection()
        if not selected:
            return
        note = self.data["notes"][selected[0]]
        paths = [photo["path"] for photo in note.get("photos", []) if photo.get("path")]
        if note.get("missing_photos") or note.get("missing_body") or len(paths) != len(note.get("photos", [])):
            self.notice.set("Some note attachments are missing. Forward the original note again, then check for new notes.")
            return
        self.message.delete("1.0", "end")
        self.message.insert("1.0", note.get("text", ""))
        self.images = paths
        self.refresh_images()
        self.tabs.select(0)

    def delete_note(self):
        selected = self.note_list.curselection()
        if selected:
            self.request("delete_note", timestamp=self.data["notes"][selected[0]]["ts"])

    def toggle_group(self, event):
        item = self.group_list.identify_row(event.y)
        if item:
            enabled, name = self.group_list.item(item, "values")
            self.group_choices[item] = not bool(enabled)
            self.render_groups()

    def save_groups(self):
        enabled = [gid for gid, chosen in self.group_choices.items() if chosen]
        def saved(result):
            self.notice.set(f"Saved selection: {len(enabled)} groups.")
            self.request("snapshot", self.apply_snapshot, after=self.sequence)
        self.request("groups", saved, enabled=enabled)

    def save_schedule(self):
        enabled = self.schedule_enabled.get()
        def saved(result):
            self.notice.set("Daily schedule saved and enabled." if enabled else "Daily schedule turned off.")
            self.request("snapshot", self.apply_snapshot, after=self.sequence)
        self.request("schedule", saved, enabled=enabled,
                     times=[time.strip() for time in self.times.get().split(",") if time.strip()])

    def change_password(self):
        current = simpledialog.askstring("Change password", "Current password. Wrong entries count toward erasure.", show="•", parent=self)
        if current is None:
            return
        new = simpledialog.askstring("Change password", "New password, at least 12 characters:", show="•", parent=self)
        if new is None:
            return
        confirmation = simpledialog.askstring("Change password", "Confirm new password:", show="•", parent=self)
        if confirmation is None:
            return
        if new != confirmation:
            self.notice.set("New passwords do not match. No attempt counted.")
            return
        self.request("change_password", lambda _: self.notice.set("Password changed."), current=current, new=new)

    def lock(self):
        token = self.client.token
        client = Client(self.client.root)
        client.token = token
        self.show_login()
        def work():
            try:
                client.call("lock")
            except SecurityError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def erase(self):
        if messagebox.askyesno("Log out and erase?", "Stop every broadcast and schedule and erase all data stored by "
                "this app, including imported images?\n\nYou must set a password and link again. "
                "Messages already sent and your phone are unaffected.", default="no", icon="warning"):
            self.show_login("Erasing local data…")
            self.request("erase", lambda _: self.show_login("Local data erased. Remove this device from Signal's "
                "Linked Devices on your phone if it is still listed."), confirmed=True)

    def close(self):
        self.closing.set()
        self.generation += 1
        tokens = {client.token for client in (self.client, self.authentication) if client and client.token}
        for token in tokens:
            client = Client(self.client.root, timeout=5)
            client.token = token
            try:
                client.call("lock")
            except SecurityError:
                pass
        self.destroy()

    def destroy(self):
        if getattr(self, "thumbnailer", None):
            self.thumbnailer.close()
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
