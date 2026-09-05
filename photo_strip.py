"""Numbered photo thumbnails shared by the legacy and protected Mac interfaces."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import thumbs


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

    def __init__(self, parent, on_change, *, palette, make_thumbnail=thumbs.make) -> None:
        super().__init__(parent)
        self._on_change = on_change
        self.palette = palette
        self._make_thumbnail = make_thumbnail
        self._closed = threading.Event()
        self._result_lock = threading.Lock()
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
        self.summary = ttk.Label(head, foreground=self.palette["muted"], text="",
                                 wraplength=430, justify="left")
        self.summary.pack(side="left", padx=(8, 0))

        self.well = ttk.Frame(self)
        self.canvas = tk.Canvas(self.well, height=self.EMPTY_H, bd=0, highlightthickness=1,
                                highlightbackground="#888", background=self.palette["text_bg"])
        scrollbar = ttk.Scrollbar(self.well, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scrollbar.set, yscrollincrement=24)
        self.canvas.pack(side="left", fill="x", expand=True)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
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
        self.hint = ttk.Label(row, foreground=self.palette["muted"], text="")
        self.hint.pack(side="left", padx=(6, 0))
        self._sync_layout()
        self._drain_after = self.after(120, self._drain_thumbs)
        self._resize_binding = self.winfo_toplevel().bind("<Configure>", self._window_resized, add="+")

    def _window_resized(self, event):
        if event.widget is self.winfo_toplevel() and not self._closed.is_set():
            self._redraw()

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
            self.well.pack(fill="x", pady=(6, 0))
            self.row.pack(fill="x", pady=(5, 0))
            self.toggle_btn.configure(text="Hide ▾")
        else:
            self.well.pack_forget()
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
            return "No photos yet. Add photos to choose the send order."
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

    def _thumb_worker(self, paths: list[str]) -> None:
        for p in paths:
            if self._closed.is_set():
                return
            png = self._make_thumbnail(p, self.IMG)
            with self._result_lock:
                if not self._closed.is_set():
                    self._ready.put((p, png, False))

    @staticmethod
    def _photo(png):
        if isinstance(png, bytes):
            return tk.PhotoImage(data=png)
        return tk.PhotoImage(file=str(png)) if png else None

    def _drain_thumbs(self) -> None:
        if self._closed.is_set():
            return
        got = False
        while True:
            try:
                path, png, preview = self._ready.get_nowait()
            except queue.Empty:
                break
            if preview:
                self._show_preview(path, png)
                continue
            self._pending.discard(path)
            if path not in self._paths:
                continue
            try:
                self._photos[path] = self._photo(png)
            except tk.TclError:
                self._photos[path] = None
            got = True
        try:
            if got:
                self._redraw()
            self._drain_after = self.after(120, self._drain_thumbs)
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
        visible_rows = 1 if self.winfo_toplevel().winfo_height() < 740 else 2
        viewport = min(height, 2 * self.PAD + visible_rows * self.IMG + (visible_rows - 1) * self.GAP)
        self.canvas.configure(scrollregion=(0, 0, width, height))
        if self.canvas.winfo_height() != viewport:
            self.canvas.configure(height=viewport)

        self.canvas.delete("all")
        if not self._paths:
            self.canvas.create_text(self.PAD + 2, self.EMPTY_H // 2, anchor="w",
                                    fill=self.palette["muted"],
                                    text="No photos yet. Add photos to choose the send order.")
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
        c.create_rectangle(x, y, x + s, y + s, fill=self.palette["tile_bg"], tags=tags,
                           outline=self.palette["accent"] if hot else self.palette["tile_line"],
                           width=2 if hot else 1)
        photo = self._photos.get(path)
        if photo is not None:
            c.create_image(x + s // 2, y + s // 2, image=photo, tags=tags)
        else:
            waiting = path in self._pending
            c.create_text(x + s // 2, y + s // 2, fill=self.palette["muted"], tags=tags,
                          text="…" if waiting else (Path(path).suffix.upper().lstrip(".") or "?"))
        # Send position. The badge is the whole point of the strip: the order is legible
        # at a glance, whether or not anyone ever drags a tile.
        c.create_oval(x + 4, y + 4, x + 24, y + 24, fill=self.palette["accent"], outline="", tags=tags)
        c.create_text(x + 14, y + 14, text=str(i + 1), font=("", 11, "bold"),
                      fill=self.palette["accent_fg"], tags=tags)
        if self._enabled:
            c.create_oval(x + s - 24, y + 4, x + s - 4, y + 24,
                          fill=self.palette["badge_bg"], outline="", tags=tags)
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
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        i, on_close = self._hit(x, y)
        if i is None:
            self._sel = None
            self._redraw()
            return
        if on_close:
            self._remove(i)
            return
        tx, ty = self._slot_xy(i)
        self._sel = i
        self._drag = {"i": i, "start": (x, y), "off": (x - tx, y - ty),
                      "xy": (tx, ty), "moved": False}
        self._redraw()

    def _on_motion(self, e) -> None:
        d = self._drag
        if not d or not self._enabled:
            return
        if e.y < 12:
            self.canvas.yview_scroll(-1, "units")
        elif e.y > self.canvas.winfo_height() - 12:
            self.canvas.yview_scroll(1, "units")
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if not d["moved"]:
            # A few pixels of wobble during a click shouldn't lift the tile.
            if abs(x - d["start"][0]) < 4 and abs(y - d["start"][1]) < 4:
                return
            d["moved"] = True
        old_x, old_y = d["xy"]
        d["xy"] = (x - d["off"][0], y - d["off"][1])
        target = self._slot_at(x, y)
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
        i, on_close = self._hit(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
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
        def work():
            png = self._make_thumbnail(path, 720)
            with self._result_lock:
                if not self._closed.is_set():
                    self._ready.put((path, png, True))
        threading.Thread(target=work, daemon=True).start()

    def _show_preview(self, path, png):
        if path not in self._paths:
            return
        try:
            img = self._photo(png)
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
        holder = tk.Label(win, image=img, bd=0, background=self.palette["text_bg"])
        holder.pack()
        ttk.Label(win, text=Path(path).name, foreground=self.palette["muted"]).pack(pady=(6, 8))
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

    def destroy(self):
        self.winfo_toplevel().unbind("<Configure>", self._resize_binding)
        self.after_cancel(self._drain_after)
        with self._result_lock:
            self._closed.set()
            while not self._ready.empty():
                self._ready.get_nowait()
        self._paths.clear()
        self._photos.clear()
        self._previews.clear()
        self._pending.clear()
        self._drag = None
        super().destroy()
