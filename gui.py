#!/usr/bin/env python3
"""Tkinter front end for Signal Broadcast.

A thin UI over engine.py: a first-run Link screen (renders the QR you scan with
your phone) and a tabbed main screen — Send (type, attach, order the photos, send,
resend), Notes (what you wrote to yourself on the phone, ready to broadcast),
Groups (pick which to send to), Schedule (daily auto-send), and Security (send
speed, logging, wipe-on-quit, and station-mode wipe-on-unplug). All sending happens on a
worker thread; the engine talks back
through a thread-safe queue that the Tk main loop drains. Colours are chosen
explicitly so the log is readable in both macOS Light and Dark mode.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import engine
import thumbs

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
    "tile_bg": "#2b2d31" if DARK else "#eceef2",     # photo tiles, on the strip's well
    "tile_line": "#3c3e43" if DARK else "#d7d9dd",
    "badge_bg": "#2b2d31",                            # the ✕ corner — dark in both themes
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


class PhotoStrip(ttk.Frame):
    """The attached photos as a wrapping strip of numbered thumbnails, dragged to
    reorder.

    The order is not cosmetic: broadcast() hands the list straight to signal-cli as
    ``-a first second third``, and that is the order the album appears in inside every
    group's chat. Before this strip existed the order was whatever the file dialog
    happened to return, and nothing in the app could change it.

    Dragging is the quick way, but never the only way — a tile can also be selected and
    moved with the buttons underneath, so using this doesn't depend on discovering a
    gesture. Each tile carries its send position as a badge, so the order is readable
    without touching anything.

    The App owns the list; this widget renders it and calls ``on_change`` with the new
    order whenever the user rearranges or removes something.
    """

    IMG = 92        # tile side, in points; the photo is fitted inside it, aspect kept
    GAP = 10
    PAD = 10
    EMPTY_H = 52    # canvas height with nothing attached
    CLOSE_HIT = 26  # top-right corner of a tile that means "remove", not "drag"

    def __init__(self, parent, on_change) -> None:
        super().__init__(parent)
        self._on_change = on_change
        self._paths: list[str] = []
        self._photos: dict[str, tk.PhotoImage | None] = {}  # None → unreadable, show a stub
        self._pending: set[str] = set()
        self._ready: queue.Queue = queue.Queue()
        self._sel: int | None = None
        self._drag: dict | None = None
        self._enabled = True
        self._cols = 1
        self._previews: dict[str, tk.PhotoImage] = {}   # open preview windows → their image
        self._open = False

        # Collapsed, this is one line: the photos in order, by name. Two rows of tiles
        # pushed Send and the Activity log off the bottom of the window, and the tiles
        # are only needed while you're actually arranging them.
        head = ttk.Frame(self)
        head.pack(fill="x")
        self.toggle_btn = ttk.Button(head, text="Reorder photos ▸", width=17,
                                     command=self.toggle)
        self.toggle_btn.pack(side="left")
        self.summary = ttk.Label(head, foreground=PALETTE["muted"], text="",
                                 wraplength=430, justify="left")
        self.summary.pack(side="left", padx=(8, 0))

        self.canvas = tk.Canvas(self, height=self.EMPTY_H, bd=0, highlightthickness=1,
                                highlightbackground="#888", background=PALETTE["text_bg"])
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Left>", lambda _e: self._nudge(-1))
        self.canvas.bind("<Right>", lambda _e: self._nudge(1))
        self.canvas.bind("<BackSpace>", lambda _e: self._remove_selected())
        self.canvas.bind("<Delete>", lambda _e: self._remove_selected())

        self.row = row = ttk.Frame(self)
        self.done_btn = ttk.Button(row, text="Done", command=lambda: self.set_open(False))
        self.done_btn.pack(side="left", padx=(0, 12))
        self.left_btn = ttk.Button(row, text="◀ Earlier", command=lambda: self._nudge(-1))
        self.right_btn = ttk.Button(row, text="Later ▶", command=lambda: self._nudge(1))
        self.rm_btn = ttk.Button(row, text="Remove", command=self._remove_selected)
        self.view_btn = ttk.Button(row, text="Preview", command=self._preview_selected)
        for b in (self.left_btn, self.right_btn, self.rm_btn, self.view_btn):
            b.pack(side="left", padx=(0, 6))
        self.hint = ttk.Label(row, foreground=PALETTE["muted"], text="")
        self.hint.pack(side="left", padx=(6, 0))
        self._sync_layout()

    # ------------------------------------------------------------------ public
    def set_paths(self, paths, expand: bool | None = None) -> None:
        """Show this list, in this order. Does not call back — the caller already knows
        what it just set.

        ``expand`` defaults to opening the tiles only when photos have just been ADDED:
        that's the moment you want to check the order. Reopening the app, or clearing,
        leaves it shut, so the Send button stays where you left it."""
        if not self.winfo_exists():   # the screen was replaced (unlink) mid-flight
            return
        before, self._paths = len(self._paths), list(paths)
        self._sel = None
        self._drag = None
        if expand is None:
            expand = len(self._paths) > max(before, 0) and bool(self._paths)
        self._open = bool(expand) and bool(self._paths)
        self._request_thumbs()
        self._sync_layout()

    def toggle(self) -> None:
        self.set_open(not self._open)

    def set_open(self, on: bool) -> None:
        """Show or hide the tiles. Collapsed keeps the order visible as text, so the
        strip costs one line when you're not rearranging."""
        if not self.winfo_exists():
            return
        self._open = bool(on) and bool(self._paths)
        self._drag = None
        self._sync_layout()

    def _sync_layout(self) -> None:
        """Pack or unpack the tiles for the current state, then repaint."""
        if not self.winfo_exists():
            return
        if self._open:
            self.canvas.pack(fill="x", pady=(6, 0))
            self.row.pack(fill="x", pady=(5, 0))
            self.toggle_btn.configure(text="Hide ▾")
        else:
            self.canvas.pack_forget()
            self.row.pack_forget()
            self.toggle_btn.configure(text="Reorder photos ▸")
        # With nothing attached there is nothing to reorder, so the button would only be
        # a dead control — the summary line says what to do instead.
        if self._paths and self._enabled:
            self.toggle_btn.pack(side="left")
        else:
            self.toggle_btn.pack_forget()
        self.summary.configure(text=self._summary_text())
        if self._open:
            self._redraw()

    def _summary_text(self) -> str:
        """The order, as words — what you see when the tiles are put away."""
        if not self._paths:
            return "No photos yet — “Add images…” puts them here, in send order."
        shown = [f"{i + 1}. {Path(p).name}" for i, p in enumerate(self._paths[:4])]
        rest = len(self._paths) - len(shown)
        return " · ".join(shown) + (f"  +{rest} more" if rest > 0 else "")

    def set_enabled(self, on: bool) -> None:
        """Locked while a send is running: the run's fingerprint (and so its ability to
        resume after a crash) is computed over this exact list, in this exact order."""
        if not self.winfo_exists():
            return
        self._enabled = on
        self._drag = None
        if not on:
            # Locked mid-send: put the tiles away too. They can't be used, and the space
            # is better spent on the progress and log you actually want to watch.
            self._sel = None
            self._open = False
        self._sync_layout()

    # ------------------------------------------------------------- thumbnails
    def _request_thumbs(self) -> None:
        todo = [p for p in self._paths if p not in self._photos and p not in self._pending]
        if not todo:
            return
        self._pending.update(todo)
        # sips costs 100–400ms a photo (HEIC is the slow end), so ten off the file
        # dialog would visibly freeze the window if this ran on the UI thread. The
        # worker only produces PNG paths; PhotoImage objects must be built on the main
        # thread, which is what _drain_thumbs is for.
        threading.Thread(target=self._thumb_worker, args=(todo,), daemon=True).start()
        self.after(120, self._drain_thumbs)

    def _thumb_worker(self, paths: list[str]) -> None:
        for p in paths:
            self._ready.put((p, thumbs.make(p, self.IMG)))

    def _drain_thumbs(self) -> None:
        got = False
        while True:
            try:
                path, png = self._ready.get_nowait()
            except queue.Empty:
                break
            self._pending.discard(path)
            try:
                self._photos[path] = tk.PhotoImage(file=str(png)) if png else None
            except tk.TclError:
                self._photos[path] = None
            got = True
        try:
            if got:
                self._redraw()
            if self._pending:
                self.after(120, self._drain_thumbs)
        except tk.TclError:
            pass  # the screen was torn down (unlink) while photos were still converting

    # ---------------------------------------------------------------- drawing
    def _cell(self) -> int:
        return self.IMG + self.GAP

    def _slot_xy(self, i: int) -> tuple[int, int]:
        return (self.PAD + (i % self._cols) * self._cell(),
                self.PAD + (i // self._cols) * self._cell())

    def _redraw(self) -> None:
        if not self.canvas.winfo_exists():
            return
        # The summary is the collapsed view of the same data, so it tracks every
        # reorder and removal whether or not the tiles are on screen.
        self.summary.configure(text=self._summary_text())
        if not self._open:
            self._sync_buttons()
            return
        width = max(self.canvas.winfo_width(), 1)
        self._cols = max(1, (width - 2 * self.PAD + self.GAP) // self._cell())
        rows = max(1, -(-len(self._paths) // self._cols))
        height = (self.EMPTY_H if not self._paths
                  else 2 * self.PAD + rows * self.IMG + (rows - 1) * self.GAP)
        if self.canvas.winfo_height() != height:
            self.canvas.configure(height=height)   # guarded: this re-fires <Configure>

        self.canvas.delete("all")
        if not self._paths:
            self.canvas.create_text(self.PAD + 2, self.EMPTY_H // 2, anchor="w",
                                    fill=PALETTE["muted"],
                                    text="No photos yet — “Add images…” puts them here, in send order.")
            self._sync_buttons()
            return
        drag = self._drag if (self._drag and self._drag["moved"]) else None
        lifted = drag["i"] if drag else None
        for i, p in enumerate(self._paths):
            if i != lifted:
                x, y = self._slot_xy(i)
                self._draw_tile(i, p, x, y)
        if drag is not None:     # drawn last so it floats above the row it's crossing
            x, y = drag["xy"]
            self._draw_tile(drag["i"], self._paths[drag["i"]], x, y, lifted=True)
        self._sync_buttons()

    def _draw_tile(self, i: int, path: str, x: int, y: int, lifted: bool = False) -> None:
        c, s = self.canvas, self.IMG
        tags = ("lift",) if lifted else ()
        hot = lifted or i == self._sel
        c.create_rectangle(x, y, x + s, y + s, fill=PALETTE["tile_bg"], tags=tags,
                           outline=PALETTE["accent"] if hot else PALETTE["tile_line"],
                           width=2 if hot else 1)
        photo = self._photos.get(path)
        if photo is not None:
            c.create_image(x + s // 2, y + s // 2, image=photo, tags=tags)
        else:
            waiting = path in self._pending
            c.create_text(x + s // 2, y + s // 2, fill=PALETTE["muted"], tags=tags,
                          text="…" if waiting else (Path(path).suffix.upper().lstrip(".") or "?"))
        # Send position. The badge is the whole point of the strip: the order is legible
        # at a glance, whether or not anyone ever drags a tile.
        c.create_oval(x + 4, y + 4, x + 24, y + 24, fill=PALETTE["accent"], outline="", tags=tags)
        c.create_text(x + 14, y + 14, text=str(i + 1), font=("", 11, "bold"),
                      fill=PALETTE["accent_fg"], tags=tags)
        if self._enabled:
            c.create_oval(x + s - 24, y + 4, x + s - 4, y + 24,
                          fill=PALETTE["badge_bg"], outline="", tags=tags)
            c.create_text(x + s - 14, y + 13, text="✕", font=("", 11, "bold"),
                          fill="#ffffff", tags=tags)

    def _sync_buttons(self) -> None:
        live = self._enabled and self._sel is not None
        for b in (self.left_btn, self.right_btn, self.rm_btn, self.view_btn):
            b.configure(state="normal" if live else "disabled")
        if not self._enabled:
            self.hint.configure(text="Locked while sending.")
        elif not self._paths:
            self.hint.configure(text="")
        elif self._sel is None:
            self.hint.configure(text="Drag to reorder.")
        else:
            name = Path(self._paths[self._sel]).name
            if len(name) > 26:                     # keep the row from stretching the window
                name = name[:23] + "…"
            self.hint.configure(text=f"#{self._sel + 1}: {name}")

    # ------------------------------------------------------------ interaction
    def _hit(self, x: int, y: int) -> tuple[int | None, bool]:
        """(index, is_the_✕_corner) for the tile under the pointer. Worked out from the
        grid rather than canvas item ids, which keeps it correct mid-drag."""
        if not self._paths:
            return None, False
        col, row = (x - self.PAD) // self._cell(), (y - self.PAD) // self._cell()
        if x < self.PAD or y < self.PAD or col >= self._cols:
            return None, False
        i = int(row * self._cols + col)
        if i >= len(self._paths):
            return None, False
        tx, ty = self._slot_xy(i)
        if not (tx <= x <= tx + self.IMG and ty <= y <= ty + self.IMG):
            return None, False        # in the gap between tiles
        return i, (x >= tx + self.IMG - self.CLOSE_HIT and y <= ty + self.CLOSE_HIT)

    def _slot_at(self, x: int, y: int) -> int:
        """Nearest drop slot for a pointer position — rounded, so dragging a tile past
        the halfway point of its neighbour swaps them."""
        col = min(max(int(round((x - self.PAD) / self._cell())), 0), self._cols - 1)
        row = max(int((y - self.PAD) // self._cell()), 0)
        return min(max(row * self._cols + col, 0), len(self._paths) - 1)

    def _on_press(self, e) -> None:
        if not self._enabled:
            return
        self.canvas.focus_set()
        i, on_close = self._hit(e.x, e.y)
        if i is None:
            self._sel = None
            self._redraw()
            return
        if on_close:
            self._remove(i)
            return
        tx, ty = self._slot_xy(i)
        self._sel = i
        self._drag = {"i": i, "start": (e.x, e.y), "off": (e.x - tx, e.y - ty),
                      "xy": (tx, ty), "moved": False}
        self._redraw()

    def _on_motion(self, e) -> None:
        d = self._drag
        if not d or not self._enabled:
            return
        if not d["moved"]:
            # A few pixels of wobble during a click shouldn't lift the tile.
            if abs(e.x - d["start"][0]) < 4 and abs(e.y - d["start"][1]) < 4:
                return
            d["moved"] = True
        old_x, old_y = d["xy"]
        d["xy"] = (e.x - d["off"][0], e.y - d["off"][1])
        target = self._slot_at(e.x, e.y)
        if target != d["i"]:
            self._paths.insert(target, self._paths.pop(d["i"]))
            d["i"] = self._sel = target
            self._redraw()          # renumber every badge and open the gap
        else:
            # Same slot: just slide the lifted tile. Redrawing the whole canvas on every
            # motion event flickers.
            self.canvas.move("lift", d["xy"][0] - old_x, d["xy"][1] - old_y)

    def _on_release(self, _e) -> None:
        d, self._drag = self._drag, None
        if d and d["moved"]:
            self._notify()
        self._redraw()

    def _on_double(self, e) -> None:
        if not self._enabled:
            return
        i, on_close = self._hit(e.x, e.y)
        if i is not None and not on_close:
            self._preview(self._paths[i])

    def _nudge(self, delta: int) -> None:
        if not self._enabled or self._sel is None:
            return
        j = self._sel + delta
        if not 0 <= j < len(self._paths):
            return
        self._paths.insert(j, self._paths.pop(self._sel))
        self._sel = j
        self._notify()
        self._redraw()

    def _remove_selected(self) -> None:
        if self._enabled and self._sel is not None:
            self._remove(self._sel)

    def _remove(self, i: int) -> None:
        """Drop one photo from the send. The file itself is untouched — this is the
        list, not the disk."""
        del self._paths[i]
        self._sel = None
        self._notify()
        self._redraw()

    def _notify(self) -> None:
        self._on_change(list(self._paths))

    def _preview_selected(self) -> None:
        if self._sel is not None:
            self._preview(self._paths[self._sel])

    def _preview(self, path: str) -> None:
        """Full look at one photo, so what's being broadcast can be checked before it
        goes to every group. Synchronous: it's a deliberate click, and one sips run."""
        png = thumbs.make(path, 720)
        try:
            img = tk.PhotoImage(file=str(png)) if png else None
        except tk.TclError:
            img = None
        if img is None:
            messagebox.showwarning("Can't preview",
                f"{Path(path).name} couldn't be opened as an image.\n\n"
                "It can still be sent, but check it's the file you meant.")
            return
        win = tk.Toplevel(self)
        win.title(Path(path).name)
        win.transient(self.winfo_toplevel())
        holder = tk.Label(win, image=img, bd=0, background=PALETTE["text_bg"])
        holder.pack()
        ttk.Label(win, text=Path(path).name, foreground=PALETTE["muted"]).pack(pady=(6, 8))
        # Tk drops an image the moment its last Python reference goes, so the window
        # would come up blank without this — held until the window closes.
        self._previews[str(win)] = img

        def close(_e=None) -> None:
            self._previews.pop(str(win), None)
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", close)
        holder.bind("<Button-1>", close)
        win.focus_set()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Signal Broadcast — v{engine.app_version()}")
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

        # Closing the window routes through _quit so an armed "wipe on close" fires.
        # The red close button triggers WM_DELETE_WINDOW; on macOS, Cmd-Q and the
        # Dock/Apple-menu Quit bypass that and need the tk::mac::Quit hook instead.
        self.protocol("WM_DELETE_WINDOW", self._quit)
        try:
            self.createcommand("tk::mac::Quit", self._quit)
        except tk.TclError:
            pass

        if os.environ.get("SB_SKIP_LINK") or engine.is_linked():
            self.show_main()
            # is_linked() only checks that link FILES exist — they outlive a valid
            # link (a link that died mid-provision, or this Mac removed from the
            # phone's Linked Devices). Verify with signal-cli off the UI thread and
            # fall back to the link screen if the account isn't actually registered.
            threading.Thread(target=self._verify_link, daemon=True).start()
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

    def _log(self, msg: str, tag: str = "", disk_msg: str | None = None) -> None:
        # Prefix the live line with a clock time so the gaps between sends are visible
        # at a glance. The on-disk log adds its own timestamp, so we pass the bare
        # message to append_activity (no double stamp). Group NAMES do appear here (so
        # each line says which group) — they already live on disk in groups.txt, and
        # arming wipe-on-close or station mode erases everything, logs included, on
        # close/unplug. Message TEXT is still never logged. disk_msg stays as a safety
        # valve to write a different (e.g. counts-only) version to disk if ever needed.
        stamp = datetime.now().strftime("%H:%M:%S")
        # The on-screen box may already be destroyed (we routed to the link screen).
        # That must never lose the disk line, and must never raise — _log is what the
        # event pump calls to REPORT a failure, so throwing here turns one bad event
        # into an unhandled traceback on every poll.
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{stamp}  ", "muted")
            self.log_box.insert("end", msg + "\n", tag)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except tk.TclError:
            pass
        engine.append_activity(msg if disk_msg is None else disk_msg)

    def _clear_activity(self) -> None:
        """Empty the on-screen Activity log. Only clears the live view — the on-disk
        log (if logging is on) is untouched; use Security → Clear logs for the files."""
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

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
    def _verify_link(self) -> None:
        """Worker: if signal-cli positively reports no registered account behind the
        on-disk link files, tell the UI to route back to the link screen. Without this
        a broken link shows a normal main screen where every sync/send just fails."""
        if engine.link_is_broken():
            self.events.put(("relink_needed", None))

    def show_link(self, notice: str = "") -> None:
        self._screen = "link"
        self._link_notice = notice
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
        self.link_status = ttk.Label(self.container, text="", foreground=PALETTE["muted"],
                                     wraplength=620, justify="center")
        self.link_status.pack(pady=(4, 12))
        if self._link_notice:  # why they landed here (e.g. the previous link broke)
            self.link_status.configure(text=self._link_notice, foreground=PALETTE["error"])
        # Animated only while linking — a moving bar says "working, not frozen"
        # through the fixed ~12s phone sync. Packed on start (see _start_link).
        self.link_progress = ttk.Progressbar(self.container, mode="indeterminate", length=280)

        btns = ttk.Frame(self.container)
        btns.pack()
        self.link_retry = ttk.Button(btns, text="Start linking", command=self._start_link)
        self.link_retry.pack(side="left", padx=4)
        ttk.Button(btns, text="Quit", command=self.destroy).pack(side="left", padx=4)
        update_row = ttk.Frame(self.container)
        update_row.pack(fill="x", pady=(14, 0))
        self.update_btn = ttk.Button(update_row, text="Update", command=self._check_update)
        self.update_btn.pack(side="right")
        ttk.Label(update_row, text=f"v{engine.app_version()}", font=("", 10),
                  foreground=PALETTE["muted"]).pack(side="right", padx=(0, 12))
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
        threading.Thread(target=self._link_worker_serialized, daemon=True).start()

    def _link_worker_serialized(self) -> None:
        try:
            with engine.signal_cli_operation("linking"):
                self._link_worker()
        except engine.BroadcastError as exc:
            self.events.put(("link_error", str(exc)))

    def _stop_link_progress(self) -> None:
        if self.link_progress.winfo_exists():
            self.link_progress.stop()
            self.link_progress.pack_forget()

    def _linklog(self, msg: str) -> None:
        """Append raw link diagnostics to logs/link-debug.txt so a failed Mac link
        leaves evidence (what signal-cli printed, whether the account registered).
        gui.py used to log nothing here, which is why a broken link was a guessing
        game. Best-effort — never raises into the link flow."""
        try:
            p = engine.LOGS_DIR / "link-debug.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > 1_000_000:  # cap so retries can't grow it forever
                p.unlink()
            with open(p, "a", encoding="utf-8") as f:
                f.write(msg.rstrip("\n") + "\n")
        except Exception:
            pass

    def _link_worker(self) -> None:
        png = None
        proc = None
        try:
            qrencode = engine.qrencode_bin()
            engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
            cmd, env = engine.signal_cli_command(
                "--config", str(engine.DATA_DIR), "link", "-n", "broadcast-laptop")
            self._linklog("--- attempt start ---")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                errors="replace", env=env)

            uri = ""
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith(("sgnl://linkdevice", "tsdevice:")):
                    uri = line
                    self._linklog("URI generated")
                    break
            if not uri:
                proc.wait()
                raise engine.BroadcastError("No link code received from signal-cli.")

            png = Path(tempfile.gettempdir()) / "sb-link-qr.png"
            subprocess.run([qrencode, "-o", str(png), "-s", "7", "-m", "2", uri], check=True)
            self.events.put(("qr", str(png)))
            self.events.put(("link_status", "Scan the code with your phone…"))

            # Keep DRAINING stdout until signal-cli exits. Stopping at the URI (as
            # this used to) let the post-scan provisioning/sync output fill the pipe
            # (stderr is merged into it) — once full, signal-cli blocks mid-write and
            # the link deadlocks forever: the phone says "linked" but this side never
            # finishes, leaving a half-provisioned account and an empty group list.
            # signal-cli is silent between printing the URI and the phone scanning it,
            # so the first line after the URI doubles as a "scanned" signal.
            scanned = False
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                self._linklog("out: " + line[:200])
                if not scanned:
                    scanned = True
                    self.events.put(("link_status", "Code scanned — finishing the link…"))
            rc = proc.wait()
            self._linklog(f"attempt ended rc={rc}")
            if rc != 0:
                raise engine.BroadcastError("Linking did not complete. Try again.")

            self.events.put(("link_status", "Linked! Setting things up…"))
            # Retry briefly: the just-exited link JVM can still hold the account lock
            # for a moment on macOS, so an immediate detect would miss a good link.
            number = engine.wait_for_account()
            self._linklog(f"detect_account -> {number!r}")
            # signal-cli can exit 0 yet leave the account half-provisioned (device
            # associated, registration not finished) — detect_account then returns
            # None. Don't drop the user onto a dead main screen; treat it as a link
            # failure so they can simply scan again.
            if not number:
                raise engine.BroadcastError(
                    "The link didn't finish registering. On your phone, remove any "
                    "'broadcast-laptop' entry under Signal → Linked Devices, then scan again.")
            engine.save_account(number)
            self.events.put(("linked_done", None))
        except Exception as exc:
            self._linklog(f"link_error: {exc}")
            self.events.put(("link_error", str(exc)))
        finally:
            # Never leave a live `signal-cli link` behind (e.g. qrencode failed): it
            # would sit on the account lock and wedge every later signal-cli call.
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
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
        self.update_btn = ttk.Button(header, text="Update", command=self._check_update)
        self.update_btn.pack(side="right", padx=(0, 8))
        ttk.Label(header, text=f"v{engine.app_version()}", font=("", 10),
                  foreground=PALETTE["muted"]).pack(side="right", padx=(0, 12))

        nb = ttk.Notebook(self.container)
        nb.pack(fill="both", expand=True, pady=(12, 0))
        self.nb = nb                       # the Notes tab hands work back to Send
        send_tab = ttk.Frame(nb, padding=14)
        notes_tab = ttk.Frame(nb, padding=14)
        groups_tab = ttk.Frame(nb, padding=14)
        sched_tab = ttk.Frame(nb, padding=14)
        security_tab = ttk.Frame(nb, padding=14)
        nb.add(send_tab, text="  Send  ")
        nb.add(notes_tab, text="  Notes  ")
        nb.add(groups_tab, text="  Groups  ")
        nb.add(sched_tab, text="  Schedule  ")
        nb.add(security_tab, text="  Security  ")

        self._build_send_tab(send_tab)
        self._build_notes_tab(notes_tab)
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

        # Style picker. Signal carries formatting as separate range metadata, not in the
        # text, so this is the only way to send italics — pasting styled text can't work.
        # The choice applies to the whole message and is saved to config.toml straight
        # away, so a scheduled auto-send uses it too.
        style_row = ttk.Frame(tab)
        style_row.pack(fill="x", pady=(8, 0))
        ttk.Label(style_row, text="Style", foreground=PALETTE["muted"]).pack(side="left", padx=(0, 8))
        try:
            current = engine.load_config().message_style
        except engine.BroadcastError:
            current = engine.DEFAULT_MESSAGE_STYLE
        self.style_var = tk.StringVar(value=current)
        for key, label in engine.MESSAGE_STYLE_LABELS:
            ttk.Radiobutton(style_row, text=label, value=key, variable=self.style_var,
                            command=self._on_style_change).pack(side="left", padx=(0, 6))
        self._apply_style_preview()

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
        self.photo_strip = PhotoStrip(tab, on_change=self._on_photos_changed)
        self.photo_strip.pack(fill="x", pady=(6, 0))
        # Start shut: reopening the app shouldn't cost you the Send button.
        self.photo_strip.set_paths(self.selected_images, expand=False)
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

        # Shown only after an interrupted run (app killed mid-send): finish the un-sent
        # groups without re-sending the ones that already went out. See _refresh_resume.
        self.resume_bar = ttk.Frame(tab)
        self.resume_label = ttk.Label(self.resume_bar, foreground=PALETTE["error"],
                                      wraplength=440, justify="left")
        self.resume_label.pack(side="left")
        ttk.Button(self.resume_bar, text="Discard", command=self._discard_interrupted).pack(side="right")
        self.resume_btn = ttk.Button(self.resume_bar, text="Resume", command=self._on_resume_interrupted)
        self.resume_btn.pack(side="right", padx=6)

        # Indeterminate (back-and-forth) loader: it bounces continuously while a send is
        # running — far more reassuring than a determinate bar that sits still for the
        # minutes a big group takes. Overall progress is the "X / N" counter below it,
        # and the heartbeat line gives the elapsed seconds on the current group.
        self.progress = ttk.Progressbar(tab, mode="indeterminate")
        self.progress.pack(fill="x", pady=(6, 2))
        self.counter = ttk.Label(tab, text="", foreground=PALETTE["muted"])
        self.counter.pack(anchor="w")
        # Live in-flight line: which group(s) are sending right now, each with its own
        # elapsed time. Vital with parallel sending (several at once) and it doubles as a
        # heartbeat — a big group can take minutes, so a ticking timer here (plus the
        # bouncing loader) is how a slow-but-healthy send reads as alive, not frozen.
        # Wraps instead of widening the window when 2-3 groups are listed.
        self.heartbeat = ttk.Label(tab, text="", foreground=PALETTE["muted"],
                                   wraplength=600, justify="left")
        self.heartbeat.pack(anchor="w", fill="x")

        activity_row = ttk.Frame(tab)
        activity_row.pack(fill="x", pady=(12, 2))
        ttk.Label(activity_row, text="Activity", font=("", 12, "bold")).pack(side="left")
        ttk.Button(activity_row, text="Clear", command=self._clear_activity).pack(side="right")
        log_frame = ttk.Frame(tab)
        log_frame.pack(fill="both", expand=True)
        self.log_box = self._text_widget(log_frame, height=9, wrap="word", state="disabled")
        for name in ("error", "ok", "muted"):
            self.log_box.tag_configure(name, foreground=PALETTE[name])
        scroll = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._refresh_resume()

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
        n = len(self.selected_images)
        # Just the count — the strip underneath names them in order, so repeating that
        # here would be two lines saying one thing.
        self.img_label.configure(text="" if not n else f"{n} photo{'' if n == 1 else 's'}")

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
        tail = f", uncertain {s.uncertain}" if s.uncertain else ""
        tail += f", skipped {s.skipped}" if s.skipped else ""
        self.last_send_label.configure(
            text=f"Last send: {when} — sent {s.sent}, failed {s.failed}{tail}",
            foreground=PALETTE["error"] if (s.failed or s.uncertain) else PALETTE["muted"])

    def _refresh_resume(self) -> None:
        """Show the resume banner only if a previous run was interrupted (the app was
        killed mid-send). broadcast() clears the marker on a normal finish, so a
        surviving one means a real crash/force-quit."""
        if not hasattr(self, "resume_bar"):
            return
        try:
            run = engine.read_interrupted_run()
        except Exception:
            run = None
        self._interrupted = run
        if run:
            parts = [f"⚠ A previous send was interrupted — {run.done} of {run.total} done."]
            if run.uncertain:
                # Killed mid-send or timed out — may already have gone out. Never resent.
                parts.append(f"{len(run.uncertain)} may already have been sent (check Signal); "
                             "those won't be re-sent.")
            if run.remaining:
                parts.append(f"Resume the remaining {len(run.remaining)} (won't re-send the rest)?")
                self.resume_btn.configure(state="normal")
            else:
                parts.append("Nothing left to resume.")
                self.resume_btn.configure(state="disabled")
            self.resume_label.configure(text=" ".join(parts))
            self.resume_bar.pack(fill="x", pady=(2, 6))
        else:
            self.resume_bar.pack_forget()

    def _on_resume_interrupted(self) -> None:
        run = getattr(self, "_interrupted", None)
        if not run or not run.remaining:  # nothing safely resumable (button is disabled too)
            return
        try:
            cfg = engine.load_config()
            message = engine.read_message()
            attachments = engine.read_attachments()
        except engine.BroadcastError as exc:
            messagebox.showerror("Can't resume", str(exc))
            return
        if engine.message_fingerprint(message, attachments) != run.fingerprint:
            if not messagebox.askyesno("Message changed",
                    "The saved message has changed since the interrupted run.\n\n"
                    "Resume and send the CURRENT message to the remaining groups?"):
                return
        self.resume_bar.pack_forget()
        self._begin_send(cfg, run.remaining, message, attachments)

    def _discard_interrupted(self) -> None:
        engine.clear_run_progress()
        self._refresh_resume()

    # ----------------------------------------------------------------- images
    def _add_images(self) -> None:
        skipped = []
        for p in filedialog.askopenfilenames(title="Choose images", filetypes=IMAGE_TYPES):
            if not Path(p).is_file():  # vanished between dialog and now — warn at pick time
                skipped.append(p)
                continue
            if p not in self.selected_images:
                self.selected_images.append(p)
        if skipped:
            messagebox.showwarning("Couldn't add some images",
                "These files couldn't be read and were skipped:\n\n" + "\n".join(skipped))
        self._sync_photos()

    def _clear_images(self) -> None:
        self.selected_images = []
        self._sync_photos()

    def _sync_photos(self) -> None:
        """Push self.selected_images into the strip. The list stays the source of
        truth; the strip is a view of it that can hand back a new order."""
        self.photo_strip.set_paths(self.selected_images)
        self._refresh_img_label()

    def _on_photos_changed(self, paths: list[str]) -> None:
        """The strip was reordered or a photo removed. Not written to disk here — like
        the message, attachments are committed on Send or Save for auto-send."""
        self.selected_images = list(paths)
        self._refresh_img_label()

    # ------------------------------------------------------------------ notes
    def _build_notes_tab(self, tab) -> None:
        """Notes you wrote to yourself on the phone, ready to become a broadcast.

        Signal's "Note to Self" is mirrored to every linked device, so this Mac already
        receives them — it just used to throw them away. Checking is a button, not a
        poll: a check holds the same lock as a send, and a background one could collide
        with the scheduler."""
        ttk.Label(tab, text="Notes from your phone", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(tab, wraplength=620, justify="left", foreground=PALETTE["muted"], text=(
            "Whatever you write in Signal's “Note to Self” chat on your phone — words, "
            "photos, or both — turns up here, ready to send on. Messages to anyone else "
            "are never read or kept.")).pack(anchor="w", pady=(2, 10))

        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.notes_btn = ttk.Button(top, text="Check for new notes", command=self._fetch_notes)
        self.notes_btn.pack(side="left")
        self.notes_status = ttk.Label(top, text="", foreground=PALETTE["muted"],
                                      wraplength=430, justify="left")
        self.notes_status.pack(side="left", padx=10)
        self.notes_progress = ttk.Progressbar(tab, mode="indeterminate")
        # What the last drain actually contained. Kept on screen (not just in the log)
        # because "found nothing" is the case that needs explaining.
        self.notes_detail = ttk.Label(tab, text="", foreground=PALETTE["muted"], font=("", 10))
        self.notes_detail.pack(anchor="w", pady=(4, 0))

        listwrap = ttk.Frame(tab)
        listwrap.pack(fill="both", expand=True, pady=(10, 0))
        bar = ttk.Scrollbar(listwrap, orient="vertical")
        bar.pack(side="right", fill="y")
        self.notes_list = tk.Listbox(
            listwrap, height=8, activestyle="none", exportselection=False,
            background=PALETTE["text_bg"], foreground=PALETTE["text_fg"],
            selectbackground=PALETTE["accent"], selectforeground=PALETTE["accent_fg"],
            relief="flat", highlightthickness=1, highlightbackground="#888",
            yscrollcommand=bar.set)
        self.notes_list.pack(side="left", fill="both", expand=True)
        bar.configure(command=self.notes_list.yview)
        self.notes_list.bind("<<ListboxSelect>>", lambda _e: self._show_note())
        self.notes_list.bind("<Double-Button-1>", lambda _e: self._use_note())

        self.note_text = self._text_widget(tab, height=5, wrap="word")
        self.note_text.pack(fill="x", pady=(8, 0))
        self.note_text.configure(state="disabled")

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(8, 0))
        self.note_use_btn = ttk.Button(row, text="Use as message", command=self._use_note)
        self.note_use_btn.pack(side="left")
        self.note_copy_btn = ttk.Button(row, text="Copy text", command=self._copy_note)
        self.note_copy_btn.pack(side="left", padx=6)
        self.note_del_btn = ttk.Button(row, text="Delete", command=self._delete_note)
        self.note_del_btn.pack(side="left")
        self.note_photos = ttk.Label(row, text="", foreground=PALETTE["muted"])
        self.note_photos.pack(side="left", padx=10)

        self._notes: list[dict] = []
        self._checking_notes = False
        self._render_notes()

    def _render_notes(self) -> None:
        self._notes = engine.read_notes()
        self.notes_list.delete(0, "end")
        for n in self._notes:
            when = datetime.fromtimestamp(n.get("ts", 0) / 1000).strftime("%d %b %H:%M")
            first = next((ln for ln in (n.get("text") or "").splitlines() if ln.strip()), "")
            shots = len(n.get("photos") or []) + int(n.get("missing_photos") or 0)
            tag = f"  [{shots} photo{'' if shots == 1 else 's'}]" if shots else ""
            self.notes_list.insert("end", f"{when}{tag}   {first[:70] or '(photo only)'}")
        if not self._notes:
            self.notes_status.configure(
                text="No notes yet — write one in Note to Self, then check.")
        self._show_note()

    def _selected_note(self) -> dict | None:
        sel = self.notes_list.curselection()
        return self._notes[sel[0]] if sel and sel[0] < len(self._notes) else None

    def _show_note(self) -> None:
        note = self._selected_note()
        self.note_text.configure(state="normal")
        self.note_text.delete("1.0", "end")
        self.note_text.insert("1.0", (note or {}).get("text", ""))
        self.note_text.configure(state="disabled")
        for b in (self.note_use_btn, self.note_copy_btn, self.note_del_btn):
            b.configure(state="normal" if note else "disabled")
        if not note:
            self.note_photos.configure(text="")
            return
        photos, missing = len(note.get("photos") or []), int(note.get("missing_photos") or 0)
        transient = int(note.get("view_once_photos") or 0)
        parts = []
        if photos:
            parts.append(f"{photos} photo{'' if photos == 1 else 's'} attached")
        if transient:
            parts.append(f"{transient} view-once photo{'' if transient == 1 else 's'} — not kept")
        if missing:
            # Arrived while the group sync was draining the queue, which can't download
            # media. Say so plainly — the photos aren't coming back on their own.
            parts.append(f"{missing} photo{'' if missing == 1 else 's'} weren't downloaded "
                         "(arrived during a group sync) — send the note again to get them")
        self.note_photos.configure(text=" · ".join(parts))

    def _fetch_notes(self) -> None:
        # signal-cli allows one operation per account, and a group sync doesn't take the
        # send lock (it predates it), so the two would collide as an unexplained red
        # error. They're both buttons in this window, so keep them out of each other's
        # way here rather than letting signal-cli arbitrate.
        if self._refreshing:
            self.notes_status.configure(
                text="Groups are syncing — try again when that finishes.",
                foreground=PALETTE["muted"])
            return
        try:
            account = engine.load_config().account
        except engine.BroadcastError as exc:
            messagebox.showerror("Can't check for notes", str(exc))
            return
        self._checking_notes = True
        self.notes_btn.configure(state="disabled")
        self.notes_status.configure(text="Checking your phone…", foreground=PALETTE["muted"])
        self.notes_progress.pack(fill="x", pady=(8, 0))
        self.notes_progress.start(15)

        def work():
            try:
                self.events.put(("notes_done", engine.fetch_notes(account)))
            except engine.BroadcastError as exc:
                self.events.put(("notes_done", str(exc)))
            except Exception as exc:  # noqa: BLE001 — the event MUST be posted: it's
                # what re-enables the button and clears _checking_notes. An escaped
                # exception here would leave the Notes and Groups buttons dead until
                # restart.
                self.events.put(("notes_done", f"Unexpected error: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _finish_notes(self, result) -> None:
        self._checking_notes = False
        self.notes_progress.stop()
        self.notes_progress.pack_forget()
        self.notes_btn.configure(state="normal")
        self._render_notes()
        if isinstance(result, str):
            self.notes_status.configure(text=result, foreground=PALETTE["error"])
            self.notes_detail.configure(text="")
            self._log(f"Notes check failed: {result}", "error")
            return
        new = result.get("new", 0)
        # Always show what the drain actually contained. "Nothing new" on its own can't
        # tell you whether the note never arrived, arrived and was filtered out, or was
        # already stored — and that's exactly the moment you need to know.
        self.notes_detail.configure(text=(
            f"Last check took {result.get('seconds', 0)}s · "
            f"{result.get('envelopes', 0)} message(s) waiting · "
            f"{result.get('transcripts', 0)} sent from your phone · "
            f"{result.get('notes', 0)} to yourself"))
        self._log(f"Notes check: {result.get('envelopes', 0)} waiting, "
                  f"{result.get('notes', 0)} note(s) to self, {new} new.", "muted")
        if new:
            self.notes_status.configure(
                text=f"{new} new note{'' if new == 1 else 's'}.", foreground=PALETTE["ok"])
            self.notes_list.selection_clear(0, "end")
            self.notes_list.selection_set(0)     # newest first — land on what just arrived
            self.notes_list.see(0)
            self._show_note()
        elif result.get("notes"):
            self.notes_status.configure(text="Nothing new — those notes are already here.",
                                        foreground=PALETTE["muted"])
        elif result.get("transcripts"):
            self.notes_status.configure(
                text="Messages arrived, but none were notes to yourself.",
                foreground=PALETTE["muted"])
        else:
            self.notes_status.configure(
                text="Signal had nothing waiting for this Mac. Write a note on your "
                     "phone, give it a few seconds, then check again.",
                foreground=PALETTE["muted"])

    def _use_note(self) -> None:
        """Drop this note into the Send tab — text into the message, photos into the
        strip — and switch to it. The note itself stays here."""
        note = self._selected_note()
        if not note:
            return
        self.msg_text.delete("1.0", "end")
        self.msg_text.insert("1.0", note.get("text", ""))
        self._apply_style_preview()
        photos = [p["path"] for p in (note.get("photos") or []) if Path(p["path"]).is_file()]
        if photos:
            self.selected_images = list(photos)
            self._sync_photos()
        self.nb.select(0)
        self.msg_text.focus_set()
        gone = len(note.get("photos") or []) - len(photos)
        self._log("Note loaded into the message." +
                  (f" {gone} photo(s) no longer on disk." if gone else ""), "ok")

    def _copy_note(self) -> None:
        note = self._selected_note()
        if not note:
            return
        self.clipboard_clear()
        self.clipboard_append(note.get("text", ""))
        self.notes_status.configure(text="Copied.", foreground=PALETTE["ok"])

    def _delete_note(self) -> None:
        note = self._selected_note()
        if not note:
            return
        engine.delete_note(note["ts"])
        self._render_notes()
        self.notes_status.configure(text="Note deleted from this Mac (your phone keeps it).",
                                    foreground=PALETTE["muted"])

    # ----------------------------------------------------------------- groups
    def _build_groups_tab(self, tab) -> None:
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Groups", font=("", 12, "bold")).pack(side="left")
        ttk.Button(top, text="Select none", command=lambda: self._set_all_groups(False)).pack(side="right")
        ttk.Button(top, text="Select all", command=lambda: self._set_all_groups(True)).pack(side="right", padx=6)
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "Select the groups to send to; unselected groups are skipped. Click "
            "“Save selection” to apply — your choices are kept even when you update "
            "the list from your phone.")
        ).pack(anchor="w", pady=(2, 6))
        self.group_count_label = ttk.Label(tab, text="", foreground=PALETTE["muted"])
        self.group_count_label.pack(anchor="w")

        search_row = ttk.Frame(tab)
        search_row.pack(fill="x", pady=(4, 0))
        ttk.Label(search_row, text="Search:").pack(side="left")
        self.group_search = tk.StringVar()
        self.group_search.trace_add("write", lambda *_: self._schedule_group_render())
        self.group_search_entry = ttk.Entry(search_row, textvariable=self.group_search)
        self.group_search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(search_row, text="Clear", width=6,
                   command=lambda: self.group_search.set("")).pack(side="left", padx=(6, 0))

        listwrap = ttk.Frame(tab)
        listwrap.pack(fill="both", expand=True, pady=(4, 8))
        self.groups_list = tk.Listbox(listwrap, selectmode="multiple", exportselection=False,
                                     activestyle="none")
        group_scroll = ttk.Scrollbar(listwrap, orient="vertical", command=self.groups_list.yview)
        self.groups_list.configure(yscrollcommand=group_scroll.set)
        self.groups_list.pack(side="left", fill="both", expand=True)
        group_scroll.pack(side="right", fill="y")
        self.groups_list.bind("<<ListboxSelect>>", self._on_group_selection)
        self._group_render_job = None

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

    def _populate_groups(self, *, check_permissions: bool = True) -> None:
        """Load the groups once: one persistent BooleanVar per group (so tick state
        survives search filtering), then draw them via _render_groups."""
        self.group_entries = engine.read_group_entries()
        self._enabled_group_ids = {e.group_id for e in self.group_entries if e.enabled}
        self._visible_ids = []
        self._render_groups()
        if check_permissions:
            self._check_group_perms()  # mark admin-only groups in the background

    def _check_group_perms(self) -> None:
        """Find which groups are admin-only (can't post) off the UI thread, then
        re-render to label them. Best-effort — a failure just leaves them unmarked."""
        if not self.group_entries:
            return

        def work():
            try:
                account = engine.load_config().account
                with engine.signal_cli_operation("checking group permissions"):
                    ids = engine.unsendable_groups(account)
            except engine.BroadcastError:
                ids = set()
            self.events.put(("group_perms", ids))
        threading.Thread(target=work, daemon=True).start()

    def _schedule_group_render(self) -> None:
        if self._group_render_job is not None:
            self.after_cancel(self._group_render_job)
        self._group_render_job = self.after(120, self._render_groups)

    def _sync_visible_group_selection(self) -> None:
        if self.__dict__.get("_rendering_groups", False):
            return
        selected = {int(index) for index in self.groups_list.curselection()}
        for index, group_id in enumerate(getattr(self, "_visible_ids", [])):
            if index in selected:
                self._enabled_group_ids.add(group_id)
            else:
                self._enabled_group_ids.discard(group_id)

    def _render_groups(self) -> None:
        """Render every visible group in one native list widget."""
        self._group_render_job = None
        self._sync_visible_group_selection()
        query = self.group_search.get().strip().lower() if hasattr(self, "group_search") else ""
        blocked = getattr(self, "_unsendable_ids", set())
        self._visible_ids = []
        self._rendering_groups = True
        self.groups_list.delete(0, "end")
        for e in self.group_entries:
            if query and query not in e.name.lower():
                continue
            index = len(self._visible_ids)
            self._visible_ids.append(e.group_id)
            label = f"{e.name}   ·  admin-only (skipped)" if e.group_id in blocked else e.name
            self.groups_list.insert("end", label)
            if e.group_id in self._enabled_group_ids:
                self.groups_list.selection_set(index)
        self._rendering_groups = False
        if not self.group_entries:
            self.groups_list.insert("end", "No groups yet — link your phone first.")
        elif not self._visible_ids:
            self.groups_list.insert("end", "No groups match your search.")
        self._update_group_count()

    def _on_group_selection(self, _event=None) -> None:
        self._sync_visible_group_selection()
        self._update_group_count()

    def _set_all_groups(self, value: bool) -> None:
        """Select all / none — limited to the groups currently shown, so it respects
        an active search (with no search, that's every group)."""
        visible = getattr(self, "_visible_ids", [])
        if value:
            self._enabled_group_ids.update(visible)
            if visible:
                self.groups_list.selection_set(0, len(visible) - 1)
        else:
            self._enabled_group_ids.difference_update(visible)
            self.groups_list.selection_clear(0, "end")
        self._update_group_count()

    def _update_group_count(self) -> None:
        total = len(self.group_entries)
        selected = len(self._enabled_group_ids)
        text = f"{selected} of {total} selected"
        shown = len(getattr(self, "_visible_ids", []))
        if shown != total:
            text += f"   ·   showing {shown}"
        self.group_count_label.configure(text=text)

    def _save_groups(self) -> None:
        self._sync_visible_group_selection()
        enabled = set(self._enabled_group_ids)
        engine.write_group_selection(enabled)
        self._refresh_status()
        messagebox.showinfo("Saved", f"{len(enabled)} of {len(self.group_entries)} "
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

    def _build_security_tab(self, tab) -> None:
        # Send pace is fixed to the tightest safe value (base_delay_seconds /
        # jitter_seconds in config.toml, defaulting to ~10s ± 3s) and has no UI: a
        # big group's own send time already exceeds the gap, so the pace only affects
        # small groups and isn't worth a control. The engine's 10s hard floor still
        # applies, so a run can never burst fast enough to risk a ban.
        # ---- Parallel sending ----------------------------------------------
        ttk.Label(tab, text="Parallel sending", font=("", 12, "bold")).pack(anchor="w")
        try:
            conc_now = engine.load_config().concurrent_sends
        except engine.BroadcastError:
            conc_now = 1
        self.conc_var = tk.IntVar(value=conc_now if conc_now in (1, 2, 3) else 1)
        for n, label in ((1, "Off — one group at a time (recommended)"),
                         (2, "2 groups at once"),
                         (3, "3 groups at once")):
            ttk.Radiobutton(tab, text=label, value=n, variable=self.conc_var,
                            command=self._apply_concurrency).pack(anchor="w")
        self.conc_note = ttk.Label(tab, text="", foreground=PALETTE["muted"])
        self.conc_note.pack(anchor="w", pady=(2, 0))
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "Lets more than one group send at the same time. It finishes a run sooner only "
            "if Signal actually overlaps the sends, and it starts new sends more often, "
            "which raises the risk of hitting Signal's rate limit (a temporary block on "
            "your number). Each group is still sent exactly once. Raise it gradually and "
            "watch the activity log for “Throttled” — back off if you see it.")
        ).pack(anchor="w", pady=(2, 0))

        ttk.Separator(tab).pack(fill="x", pady=12)

        # ---- Logging --------------------------------------------------------
        ttk.Label(tab, text="Logging", font=("", 12, "bold")).pack(anchor="w")
        try:
            debug_on = engine.load_config().debug
        except engine.BroadcastError:
            debug_on = False
        self.debug_var = tk.BooleanVar(value=debug_on)
        ttk.Checkbutton(tab, variable=self.debug_var, command=self._toggle_debug,
                        text="Save a debug log of send errors (off by default)").pack(anchor="w", pady=(2, 0))
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "Off keeps things private: only a counts-only activity log is kept. Turn on "
            "only while troubleshooting — the debug log can contain group ids. Every log "
            "line is timestamped.")
        ).pack(anchor="w", pady=(0, 4))
        logbtns = ttk.Frame(tab)
        logbtns.pack(anchor="w")
        ttk.Button(logbtns, text="Open logs folder", command=self._open_logs).pack(side="left")
        ttk.Button(logbtns, text="Clear logs", command=self._clear_logs).pack(side="left", padx=6)

        ttk.Separator(tab).pack(fill="x", pady=12)

        # ---- Wipe on quit ---------------------------------------------------
        ttk.Label(tab, text="Wipe when I quit", font=("", 12, "bold")).pack(anchor="w")
        try:
            wipe_on = engine.load_config().wipe_on_close
        except engine.BroadcastError:
            wipe_on = False
        self.wipe_var = tk.BooleanVar(value=wipe_on)
        ttk.Checkbutton(tab, variable=self.wipe_var, command=self._toggle_wipe_on_close,
                        text="Erase all data every time I close the app").pack(anchor="w", pady=(2, 0))
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "When armed, quitting erases the Signal link, groups, message, saved notes, "
            "schedule, and "
            "logs — and deletes the image files you attached, from wherever they live on "
            "this Mac. You confirm once at quit, then re-link next time. Off by default.")
        ).pack(anchor="w", pady=(0, 4))

        ttk.Separator(tab).pack(fill="x", pady=12)

        # ---- Station mode ---------------------------------------------------
        ttk.Label(tab, text="Station mode", font=("", 12, "bold")).pack(anchor="w")
        self.station_status = ttk.Label(tab, text="", font=("", 14, "bold"))
        self.station_status.pack(anchor="w", pady=(4, 10))
        ttk.Label(tab, wraplength=600, justify="left", foreground=PALETTE["muted"], text=(
            "For a Mac that stays plugged in at one spot. When armed, unplugging the "
            "power automatically ERASES all of this app's data after a 10-second grace "
            "— the Signal link, your groups, the message, your saved notes, the "
            "schedule, logs, and the "
            "image files you attached. Apart from those images it touches nothing else "
            "on the Mac. Plug back in within those 10 seconds to cancel. After a wipe "
            "you scan the QR to link again.")
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

    def _apply_concurrency(self) -> None:
        n = self.conc_var.get()
        engine.set_config_value("concurrent_sends", n)
        if n <= 1:
            self.conc_note.configure(text="Saved: off — one group at a time (safest).")
        else:
            self.conc_note.configure(
                text=f"Saved: up to {n} groups at once (more at once = higher rate-limit risk).")

    def _toggle_debug(self) -> None:
        engine.set_config_value("debug", self.debug_var.get())

    def _open_logs(self) -> None:
        engine.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["open", str(engine.LOGS_DIR)], check=False)
        except OSError as exc:
            messagebox.showerror("Couldn't open logs", str(exc))

    def _clear_logs(self) -> None:
        if not messagebox.askyesno("Clear logs?",
                "Delete all activity and debug logs kept by this app? This does not "
                "touch your Signal link, groups, or message.", icon="warning"):
            return
        engine.clear_logs()
        messagebox.showinfo("Logs cleared", "All logs were deleted.")

    def _toggle_wipe_on_close(self) -> None:
        on = self.wipe_var.get()
        if on and not messagebox.askyesno("Arm wipe-on-quit?",
                "From now on, every time you quit the app it will ERASE all of its data "
                "(the Signal link, groups, message, saved notes, schedule, and logs) AND delete the "
                "image files you attached, from wherever they live on this Mac. You'll "
                "confirm once at quit, then re-link next time you open it.\n\nArm it?",
                icon="warning", default="no"):
            self.wipe_var.set(False)
            return
        engine.set_config_value("wipe_on_close", on)

    def _arm_station(self) -> None:
        if not engine.on_ac_power():
            messagebox.showwarning("Plug in first",
                                   "Plug into power before arming station mode.")
            return
        if not messagebox.askyesno("Arm station mode?",
                "From now on, unplugging this Mac will ERASE all of this app's data "
                "(the Signal link, groups, message, saved notes, schedule, and logs) AND delete the "
                "image files you attached, from wherever they live on this Mac, after a "
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

    # ----------------------------------------------------------- message style
    def _apply_style_preview(self) -> None:
        """Show the chosen style in the message box itself. It's an approximation —
        Signal does the real rendering — but it's enough to see what you're sending.
        Spoiler has no font equivalent (Signal hides it behind a tap), so it previews
        as normal text."""
        spec = {
            "italic":        ("", 13, "italic"),
            "bold":          ("", 13, "bold"),
            "bold_italic":   ("", 13, "bold italic"),
            "monospace":     ("Menlo", 12),
            "strikethrough": ("", 13, "overstrike"),
        }.get(self.style_var.get(), ("", 13))
        self.msg_text.configure(font=spec)

    def _on_style_change(self) -> None:
        style = engine.normalize_message_style(self.style_var.get())
        engine.set_config_value("message_style", style)  # so auto-send uses it too
        self._apply_style_preview()
        self._log(f"Style: {dict(engine.MESSAGE_STYLE_LABELS)[style]}.", "muted")

    # --------------------------------------------------------------- sending
    def _on_save(self) -> None:
        """Persist the message + images without sending, so the scheduled run
        picks up the new text on its next fire."""
        text = self.msg_text.get("1.0", "end").strip()
        if engine.nothing_to_send(text, self.selected_images):
            messagebox.showwarning("Nothing to save",
                                   "Type a message or add a photo before saving.")
            return
        engine.write_message(text)
        engine.write_attachments(self.selected_images)
        self._log("Saved. The schedule will send this next time it runs.", "ok")

    def _on_send(self) -> None:
        text = self.msg_text.get("1.0", "end").strip()
        if engine.nothing_to_send(text, self.selected_images):
            messagebox.showwarning("Nothing to send",
                                   "Type a message or add a photo before sending.")
            return
        try:
            cfg = engine.load_config()
            groups = engine.read_groups()
        except engine.BroadcastError as exc:
            messagebox.showerror("Can't send", str(exc))
            return
        # Work from the in-memory message/images; don't persist them to disk until the
        # send is actually confirmed. Otherwise cancelling at the confirm or cooldown
        # prompt would still overwrite the saved message that a scheduled run sends.
        message = text
        attachments = list(self.selected_images)
        missing = engine.missing_attachments(attachments)
        if missing:
            messagebox.showerror("Missing images",
                "These attached images can't be found:\n\n" + "\n".join(missing) +
                "\n\nRe-add them or clear the attachments, then try again.")
            return
        if not self._confirm_send(cfg, groups, message, attachments):
            return
        blocked = engine.cooldown_blocks_run(cfg.cooldown_hours)
        if blocked and not messagebox.askyesno("Cooldown", f"{blocked}\n\nSend anyway?"):
            return
        engine.write_message(message)          # commit only now that we're really sending
        engine.write_attachments(attachments)
        self._begin_send(cfg, groups, message, attachments)

    def _confirm_send(self, cfg, groups, message, attachments) -> bool:
        """Last check before a real blast: show count, preview, and rough duration."""
        preview = next((ln for ln in message.splitlines() if ln.strip()), "")[:80] or "(no caption)"
        imgs = len(attachments)
        img_note = f"{imgs} image(s) attached." if imgs else "No images (text only)."
        if cfg.message_style != engine.DEFAULT_MESSAGE_STYLE:
            img_note += f"\nSent as {dict(engine.MESSAGE_STYLE_LABELS)[cfg.message_style].lower()}."
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
        # Single chokepoint for every send trigger (Send, Resend, Resume). Guard against
        # a second concurrent run: without it, Resend/Resume could re-enter while a send
        # is live and only the engine's flock would reject it (as a red log line). The
        # flag is cleared in _finish_send, which always fires via the send_done event.
        if getattr(self, "_sending", False):
            self._log("A send is already running — wait for it to finish or press Stop.", "muted")
            return
        self._sending = True
        self.stop_event.clear()
        self.failed_results = []
        self._sending_groups = list(groups)  # so a stop can resume the un-sent tail
        self.send_btn.set_enabled(False)
        self.resend_btn.configure(state="disabled")
        if hasattr(self, "resume_bar"):
            self.resume_bar.pack_forget()  # a new run supersedes any interrupted one
        self.stop_btn.configure(state="normal", text="Stop")
        # The run's fingerprint covers the attachment order, so reordering mid-send
        # would break the resume-after-crash match. Lock the strip until it's done.
        if hasattr(self, "photo_strip"):
            self.photo_strip.set_enabled(False)
        self.progress.start(15)  # animate the back-and-forth loader for the whole run
        self.counter.configure(text=f"0 / {len(groups)}")
        self._done_count = 0  # completions so far; shown in the "X / N" counter
        self._total = len(groups)
        self._inflight = {}   # pos -> (name, start_monotonic): groups sending right now
        self._tick_heartbeat()
        threading.Thread(target=self._send_worker,
                         args=(cfg, groups, message, attachments), daemon=True).start()

    def _send_worker(self, cfg, groups, message, attachments) -> None:
        try:
            results = engine.broadcast(
                config=cfg, groups=groups, message=message, attachments=attachments,
                on_log=lambda m: self.events.put(("log", m)),
                on_progress=lambda d, t, n, status, secs: self.events.put(("progress", (d, t, n, status, secs))),
                on_group_start=lambda pos, name: self.events.put(("group_start", (pos, name))),
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
        # Same account, one signal-cli operation at a time — see _fetch_notes.
        if getattr(self, "_checking_notes", False):
            self.groups_sync_label.configure(
                text="Checking for notes — try again in a moment.",
                foreground=PALETTE["muted"])
            return
        self._refreshing = True
        self.refresh_btn.configure(state="disabled")
        self.groups_progress.pack(anchor="w", pady=(4, 0))
        self.groups_progress.start()
        self.groups_sync_label.configure(text="Syncing…", foreground=PALETTE["muted"])

        def work():
            try:
                number = engine.load_config().account
                count = engine.sync_groups(number, on_log=lambda m: self.events.put(("refresh_status", m)))
                self.events.put(("refresh_done", count))
            except engine.BroadcastError as exc:
                self.events.put(("refresh_done", f"Error: {exc}"))
                if engine.link_is_broken():  # "not registered" → only relinking helps
                    self.events.put(("relink_needed", None))
        threading.Thread(target=work, daemon=True).start()

    def _finish_refresh(self, result) -> None:
        self._refreshing = False
        self.refresh_btn.configure(state="normal")
        self.groups_progress.stop()
        self.groups_progress.pack_forget()
        if isinstance(result, int):
            self.groups_sync_label.configure(text=f"Updated — {result} groups.",
                                             foreground=PALETTE["muted"])
            # A sync drains the same queue notes arrive on, so it may have picked some up.
            if hasattr(self, "notes_list"):
                self._render_notes()
            account = engine.load_config().account
            self._unsendable_ids = engine.cached_unsendable_groups(account)
            self._populate_groups(check_permissions=False)
            self._refresh_status()
        else:
            self.groups_sync_label.configure(text=result, foreground=PALETTE["error"])

    def _unlink(self) -> None:
        if not messagebox.askyesno("Unlink and erase the app's data?",
                "This signs this Mac out of Signal and deletes all the data this app "
                "stored here — the link keys, your groups, the message, your saved notes, "
                "the schedule, "
                "and logs — and deletes the image files you attached, from wherever they "
                "live on this Mac. Nothing else on the Mac is touched, and nothing "
                "personal is left behind.\n\nUse this before handing the Mac to someone "
                "else. Your phone is not affected.\n\nContinue?", icon="warning"):
            return
        try:
            engine.unlink()
            engine.disable_watcher()
            thumbs.clear()   # the strip's thumbnails are copies of the photos just deleted
        except Exception as exc:
            messagebox.showerror("Couldn't fully erase", str(exc))
            return
        messagebox.showinfo("Erased", "Signed out and erased. To also remove this Mac "
                            "from your account, open Signal on your phone → Settings → "
                            "Linked Devices and delete it there.")
        self.show_link()

    def _check_update(self) -> None:
        """Update the app: git pull in the project folder, then relaunch if there was
        anything new. Runs the pull off the UI thread so the window stays responsive."""
        self.update_btn.configure(state="disabled", text="Updating…")

        def work():
            self.events.put(("update_done", engine.git_pull()))
        threading.Thread(target=work, daemon=True).start()

    def _finish_update(self, result: tuple[bool, str]) -> None:
        changed, message = result
        self.update_btn.configure(state="normal", text="Update")
        if not changed:
            messagebox.showinfo("Update", message)
            return
        # Don't show the raw git output — just confirm and offer the restart.
        if messagebox.askyesno("Update installed",
                "A new version was downloaded.\n\nRestart now to use the new version?"):
            self._restart()

    def _restart(self) -> None:
        """Relaunch the app on the freshly-pulled code, replacing this process."""
        gui_path = str(Path(__file__).resolve())
        try:
            os.execv(sys.executable, [sys.executable, gui_path])
        except OSError as exc:
            messagebox.showerror("Couldn't restart",
                f"Update downloaded — please close and reopen the app.\n\n{exc}")

    def _quit(self) -> None:
        """Close the app. If 'wipe on close' is armed (Security tab), erase all data
        first — with one confirmation, since it's destructive and re-links next time."""
        try:
            armed = engine.load_config().wipe_on_close
        except engine.BroadcastError:
            armed = False  # not linked / no valid config yet — nothing to protect
        if armed:
            if not messagebox.askyesno("Wipe everything and quit?",
                    "“Wipe when I quit” is armed, so quitting now ERASES all of this "
                    "app's data — the Signal link, your groups, the message, your saved "
                    "notes, the schedule, and logs — and deletes the image files you attached, from "
                    "wherever they live on this Mac. You'll scan the QR to link again "
                    "next time.\n\nQuit and erase?", icon="warning", default="no"):
                return
            try:
                engine.unlink()
                engine.disable_watcher()
            except Exception:
                pass  # best effort — still quit
        thumbs.clear()   # also registered with atexit, in case we're killed some other way
        self.destroy()

    # --------------------------------------------------------------- event loop
    def _poll(self) -> None:
        # Drain worker events on the main thread. An exception in _handle must never
        # escape: if it did, the self.after() re-arm below would be skipped and the
        # whole event pump would die — freezing the log, progress, and link screens
        # for the rest of the session. So guard each event and always re-arm.
        try:
            while True:
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(kind, payload)
                except Exception as exc:  # noqa: BLE001 — one bad event can't kill the pump
                    self._log(f"Internal error handling a '{kind}' event: {exc}", "error")
        finally:
            self.after(80, self._poll)

    # Events produced by main-screen work (sends, group syncs). Routing to the link
    # screen destroys those widgets, but a worker thread mid-flight keeps queueing
    # these — rendering one into a destroyed widget raises TclError. Drop them instead:
    # the work they'd report on belongs to a screen that no longer exists.
    _MAIN_SCREEN_EVENTS = frozenset({
        "log", "group_start", "progress", "send_done",
        "refresh_status", "refresh_done", "group_perms", "notes_done",
    })

    def _handle(self, kind: str, payload) -> None:
        if kind in self._MAIN_SCREEN_EVENTS and self._screen != "main":
            return
        if kind == "qr":
            try:
                self._qr_img = tk.PhotoImage(file=payload)
            except tk.TclError:
                # A missing/corrupt QR png would otherwise wedge the link screen on
                # "Starting…" with the spinner running. Surface it and let them retry.
                self._stop_link_progress()
                self.link_status.configure(text="Couldn't render the QR code — try again.",
                                           foreground=PALETTE["error"])
                self.link_retry.configure(state="normal", text="Try again")
                return
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
            self._refresh_groups()
        elif kind == "relink_needed":
            # The on-disk link is dead (removed from the phone, or a link that never
            # finished). The main screen would be a dead end — route back to linking.
            if self._screen == "main" and not getattr(self, "_sending", False):
                self.show_link(notice=(
                    "This Mac's Signal link is no longer valid — it was removed from "
                    "your phone's Linked Devices, or an earlier link didn't finish. "
                    "Link again to continue."))
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
        elif kind == "group_start":
            pos, name = payload  # a group's send just went in flight
            self._inflight[pos] = (name, time.monotonic())
        elif kind == "progress":
            pos, total, name, status, secs = payload  # pos = group's stable position in the run
            # The loader just bounces (it's a liveness cue, not completion); the "X / N"
            # counter carries the real progress. The log label uses the stable position
            # so each line maps to a specific group even when sends finish out of order
            # under parallel sending. The group NAME is shown too (see _log): the names
            # already live in groups.txt, and a wipe erases logs too.
            self._done_count = getattr(self, "_done_count", 0) + 1
            self._inflight.pop(pos, None)  # this one finished — drop it from the live view
            self.counter.configure(text=f"{self._done_count} / {total}")
            if status == "skipped":
                self._log(f"[{pos}/{total}] {name} — skipped (admin-only)", "muted")
            elif status == "sent":
                self._log(f"[{pos}/{total}] {name} — sent in {secs:.1f}s", "ok")
            elif status == "uncertain":
                self._log(f"[{pos}/{total}] {name} — unconfirmed after {secs:.0f}s, MAY have sent", "error")
            else:
                self._log(f"[{pos}/{total}] {name} — failed after {secs:.1f}s", "error")
        elif kind == "send_done":
            self._finish_send(payload)
        elif kind == "refresh_status":
            if hasattr(self, "groups_sync_label"):
                self.groups_sync_label.configure(text=payload)
        elif kind == "refresh_done":
            self._finish_refresh(payload)
        elif kind == "notes_done":
            self._finish_notes(payload)
        elif kind == "update_done":
            self._finish_update(payload)
        elif kind == "group_perms":
            self._unsendable_ids = payload
            if hasattr(self, "group_search"):
                self._render_groups()

    def _tick_heartbeat(self) -> None:
        """Refresh the in-flight view every second: every group sending right now, each
        with its own elapsed time. With parallel sending this shows all of them at once,
        so K>1 reads clearly; with one at a time it's just the current group. The ticking
        time + the bouncing loader together tell a slow-but-healthy send from a frozen
        app. self._inflight is keyed by position and maintained from group_start/progress
        events on the main thread, so no locking is needed here."""
        if not getattr(self, "_sending", False):
            self.heartbeat.configure(text="")
            return
        now = time.monotonic()
        inflight = sorted(getattr(self, "_inflight", {}).values(), key=lambda v: v[1])  # oldest first
        done, total = getattr(self, "_done_count", 0), getattr(self, "_total", 0)
        if not inflight:
            # Nothing sending this instant: either the deliberate pacing gap between
            # quick sends, or the very start. Say which, and show progress, so it never
            # looks stuck. (Big groups send back-to-back, so this rarely shows for them.)
            text = "Starting…" if done == 0 else f"Pausing between groups — {done}/{total} done"
        elif len(inflight) == 1:
            name, start = inflight[0]
            text = f"Sending {name} — {self._fmt_secs(now - start)}"
        else:
            listed = ", ".join(f"{name} {self._fmt_secs(now - start)}" for name, start in inflight)
            text = f"Sending {len(inflight)} at once — {listed}"
        if inflight and (now - inflight[0][1]) > 90:  # the oldest has been going a while
            text += "  ·  a large group can take several minutes (reports by 15 min)"
        self.heartbeat.configure(text=text)
        self._heartbeat_job = self.after(1000, self._tick_heartbeat)

    @staticmethod
    def _fmt_secs(s: float) -> str:
        s = int(s)
        return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"

    def _finish_send(self, results: list[engine.GroupSendResult]) -> None:
        self._sending = False  # release the in-progress guard set in _begin_send
        self._inflight = {}    # nothing in flight once the run ends
        self.progress.stop()   # halt the back-and-forth loader
        job = getattr(self, "_heartbeat_job", None)
        if job:
            self.after_cancel(job)
            self._heartbeat_job = None
        self.heartbeat.configure(text="")
        self.stop_btn.configure(state="disabled", text="Stop")
        self.send_btn.set_enabled(True)
        if hasattr(self, "photo_strip"):
            self.photo_strip.set_enabled(True)
        stopped = self.stop_event.is_set()
        skipped = [r for r in results if r.skipped]
        uncertain = [r for r in results if r.uncertain]
        failed = [r for r in results if not r.ok and not r.skipped and not r.uncertain]
        sent = sum(1 for r in results if r.ok)
        # On a stop, the groups never reached are resumable too — fold them in so
        # “Resend failed” finishes the run. (These never left the machine, so they're
        # safe to resend, unlike the uncertain ones below.)
        pending: list[engine.GroupSendResult] = []
        if stopped:
            done_ids = {r.group_id for r in results}
            pending = [engine.GroupSendResult(gid, name, False)
                       for gid, name in getattr(self, "_sending_groups", [])
                       if gid not in done_ids]
        # Skipped (admin-only) and uncertain (timed out — may have delivered) groups
        # are NOT added to failed_results. Resending a skipped one just fails again;
        # resending an uncertain one could DUPLICATE a message that already went out.
        self.failed_results = failed + pending
        if skipped:
            self._log(f"Skipped {len(skipped)} admin-only group(s) you can't post in.", "muted")
        if uncertain:
            self._log(f"⚠ {len(uncertain)} group(s) couldn't be confirmed and MAY already have "
                      "sent — NOT resent, to avoid duplicates. Check Signal before resending.", "error")
        if stopped:
            self._log(f"Stopped. Sent {sent}; {len(self.failed_results)} not sent.", "muted")
        else:
            tail = f", uncertain {len(uncertain)}" if uncertain else ""
            tail += f", skipped {len(skipped)}" if skipped else ""
            self._log(f"Done. Sent {sent}, failed {len(failed)}{tail}.",
                      "error" if (failed or uncertain) else "ok")
        breakdown = engine.failure_breakdown(results)
        if breakdown:
            # Counts by cause only — no group names/ids — so it's safe in the activity log
            # and tells you WHY a big run lost groups (network, rate limit, attachment…).
            self._log(f"Failures by cause: {breakdown}.", "error")
        if self.failed_results:
            verb = "finish the run." if stopped else "retry them."
            n = len(self.failed_results)
            self._log(f"{n} group(s) not sent — use “Resend failed” to {verb}", "muted")
            self.resend_btn.configure(state="normal")
        self._refresh_status()
        if hasattr(self, "last_send_label"):
            self._refresh_last_send()
        self._refresh_resume()  # clears the banner after a clean finish; re-shows if still pending


if __name__ == "__main__":
    App().mainloop()
