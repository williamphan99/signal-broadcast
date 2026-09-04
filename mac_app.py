"""Mac interface for the local security service. No inactivity timer."""
from __future__ import annotations

import queue
import resource
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from mac_security import SecurityError
from mac_service import Client


class App(tk.Tk):
    def __init__(self, client=None):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        super().__init__()
        self.client = client or Client()
        self.title("Signal Broadcast")
        self.geometry("760x780")
        self.minsize(620, 650)
        self.responses = queue.Queue()
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

    def request(self, operation, callback=None, **values):
        generation, token = self.generation, self.client.token
        def work():
            client = Client(self.client.root)
            client.token = token
            try:
                value, error = client.call(operation, **values), None
            except SecurityError as exc:
                value, error = None, exc
            self.responses.put((generation, callback, value, error))
        threading.Thread(target=work, daemon=True).start()

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
        self.generation += 1
        for child in self.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
        for child in self.container.winfo_children():
            child.destroy()
        self.data, self.images, self.previews = {}, [], []
        self.qr_image = None
        self.notes_signature = self.group_signature = None
        self.polling = False
        self.notice = tk.StringVar()

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
        self.notice.set("Opening protected storage…")
        self.request("setup" if self.setup_required else "unlock", self.authenticated, password=password)

    def authenticated(self, result):
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
        ttk.Button(header, text="Log out and erase", command=self.erase).pack(side="right", padx=8)

    def build_tabs(self):
        self.tabs = ttk.Notebook(self.container)
        self.tabs.pack(fill="both", expand=True, pady=12)
        frames = {}
        for name in ("Send", "Notes", "Groups", "Schedule", "Security"):
            frame = ttk.Frame(self.tabs, padding=12)
            self.tabs.add(frame, text=name)
            frames[name] = frame
        send = frames["Send"]
        self.message = tk.Text(send, height=8, wrap="word")
        self.message.pack(fill="both", expand=True)
        self.message.insert("1.0", self.data["message"])
        self.images = list(self.data["attachments"])
        self.image_list = tk.Listbox(send, height=4, exportselection=False)
        self.image_list.pack(fill="x", pady=8)
        self.refresh_images()
        row = ttk.Frame(send)
        row.pack(fill="x")
        for label, command in (("Move images into vault…", self.import_images), ("Earlier", lambda: self.reorder(-1)),
                               ("Later", lambda: self.reorder(1)), ("Remove", self.remove_image)):
            ttk.Button(row, text=label, command=command).pack(side="left", padx=2)
        row = ttk.Frame(send)
        row.pack(fill="x", pady=10)
        for index, (label, command) in enumerate((("Save for schedule", self.save), ("Send now", self.send),
                               ("Stop broadcast", lambda: self.request("stop")),
                               ("Resume remaining", lambda: self.job("resume")),
                               ("Discard interrupted", lambda: self.request("discard")))):
            ttk.Button(row, text=label, command=command).grid(row=index // 3, column=index % 3, sticky="w", padx=2, pady=3)
        self.activity = tk.Text(send, height=7, state="disabled", wrap="word")
        self.activity.pack(fill="both", expand=True)

        notes = frames["Notes"]
        ttk.Button(notes, text="Check for new notes", command=lambda: self.job("notes")).pack(anchor="w")
        self.note_list = tk.Listbox(notes, exportselection=False)
        self.note_list.pack(fill="both", expand=True, pady=10)
        row = ttk.Frame(notes)
        row.pack(fill="x")
        ttk.Button(row, text="Use as message", command=self.use_note).pack(side="left")
        ttk.Button(row, text="Delete local note", command=self.delete_note).pack(side="left", padx=8)
        self.notes_signature = None

        groups = frames["Groups"]
        ttk.Button(groups, text="Update list from phone", command=lambda: self.job("sync")).pack(anchor="w")
        self.group_list = ttk.Treeview(groups, columns=("enabled", "name"), show="headings")
        self.group_list.heading("enabled", text="Send")
        self.group_list.column("enabled", width=65, stretch=False)
        self.group_list.heading("name", text="Group")
        self.group_list.pack(fill="both", expand=True, pady=10)
        self.group_list.bind("<ButtonRelease-1>", self.toggle_group)
        self.group_signature = None
        ttk.Button(groups, text="Save selection", command=self.save_groups).pack(anchor="w")

        schedule = frames["Schedule"]
        ttk.Label(schedule, text="Daily times, separated by commas. Background sends continue while locked.", wraplength=600).pack(anchor="w", pady=10)
        self.times = ttk.Entry(schedule, width=45)
        self.times.pack(anchor="w")
        self.times.insert(0, ", ".join(self.data["schedule"]["times"]))
        self.schedule_enabled = tk.BooleanVar(value=self.data["schedule"]["enabled"])
        ttk.Checkbutton(schedule, text="Enable saved schedule", variable=self.schedule_enabled).pack(anchor="w", pady=12)
        ttk.Button(schedule, text="Save schedule", command=self.save_schedule).pack(anchor="w")
        ttk.Label(schedule, text="The Mac must be awake and the service unlocked once after a restart. "
            "Missed jobs are not replayed after restarting the service.", wraplength=600).pack(anchor="w", pady=20)

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
        if self.screen == "link" and data["linked"]:
            self.initial_snapshot(data)
            self.job("sync")
            return
        self.data = data
        self.busy = bool(data["job"])
        if self.screen == "main":
            signature = [(g["group_id"], g["name"], g["enabled"]) for g in data["groups"]]
            if signature != self.group_signature:
                self.group_list.delete(*self.group_list.get_children())
                for group_id, name, enabled in signature:
                    self.group_list.insert("", "end", iid=group_id, values=("✓" if enabled else "", name))
                self.group_signature = signature
            notes_sig = [(n["ts"], n.get("text", ""), len(n.get("photos", []))) for n in data["notes"]]
            if notes_sig != self.notes_signature:
                self.note_list.delete(0, "end")
                for timestamp, text, count in notes_sig:
                    self.note_list.insert("end", f"{datetime.fromtimestamp(timestamp / 1000):%d %b %H:%M}  {text[:80]}  ({count} photos)")
                self.notes_signature = notes_sig
        for event in data["events"]:
            if event["id"] <= self.sequence:
                continue
            kind, value = event["kind"], event["value"]
            if kind == "qr" and self.screen == "link":
                self.qr_image = tk.PhotoImage(data=value)
                self.qr.configure(image=self.qr_image)
            elif kind in ("error", "log", "finished", "progress"):
                text = (f"{value['done']}/{value['total']} {value['status']}" if kind == "progress" else str(value))
                self.notice.set(text)
                if self.screen == "main":
                    self.activity.configure(state="normal")
                    self.activity.insert("end", text + "\n")
                    self.activity.see("end")
                    self.activity.configure(state="disabled")
        self.sequence = data["sequence"]

    def job(self, kind):
        self.request("job", lambda _: self.notice.set("Started. You can lock or close the window."), kind=kind)

    def save(self, callback=None):
        self.request("save", callback or (lambda _: self.notice.set("Saved.")),
                     message=self.message.get("1.0", "end-1c"), attachments=list(self.images))

    def send(self):
        count = sum(group["enabled"] for group in self.data["groups"])
        text = self.message.get("1.0", "end-1c")
        if not text.strip() and not self.images:
            self.notice.set("Type a message or attach an image first.")
            return
        if messagebox.askyesno("Send broadcast?", f"Send to {count} selected groups with {len(self.images)} images?\n\n"
                              f"{text[:120]}\n\nSending continues if you lock or close this window."):
            self.save(lambda _: self.job("send"))

    def refresh_images(self):
        self.image_list.delete(0, "end")
        for path in self.images:
            self.image_list.insert("end", Path(path).name)

    def import_images(self):
        paths = list(filedialog.askopenfilenames(title="Move images into the encrypted vault",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.webp *.heic"), ("All files", "*.*")]))
        if not paths or not messagebox.askyesno("Move original images?", "After each copy is verified, its original file "
                "will be deleted from its current location. The protected copy remains in this app. Continue?"):
            return
        def next_image(_=None):
            if paths:
                self.request("import", added, path=paths.pop(0))
        def added(result):
            self.images.append(result["path"])
            self.refresh_images()
            next_image()
        next_image()

    def reorder(self, offset):
        selection = self.image_list.curselection()
        if selection and 0 <= selection[0] + offset < len(self.images):
            position = selection[0]
            self.images[position], self.images[position + offset] = self.images[position + offset], self.images[position]
            self.refresh_images()
            self.image_list.selection_set(position + offset)

    def remove_image(self):
        selection = self.image_list.curselection()
        if selection:
            self.images.pop(selection[0])
            self.refresh_images()

    def use_note(self):
        selected = self.note_list.curselection()
        if not selected:
            return
        note = self.data["notes"][selected[0]]
        paths = [photo["path"] for photo in note.get("photos", []) if photo.get("path")]
        if note.get("missing_photos") or note.get("missing_body") or len(paths) != len(note.get("photos", [])):
            self.notice.set("Some note attachments are missing. Check for new notes before using this message.")
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
            self.group_list.item(item, values=("" if enabled else "✓", name))

    def save_groups(self):
        enabled = [item for item in self.group_list.get_children() if self.group_list.item(item, "values")[0]]
        self.request("groups", enabled=enabled)

    def save_schedule(self):
        self.request("schedule", enabled=self.schedule_enabled.get(),
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
        if self.client.token:
            client = Client(self.client.root, timeout=5)
            client.token = self.client.token
            try:
                client.call("lock")
            except SecurityError:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
