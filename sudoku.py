"""A complete, offline Sudoku game. On the Mac it is also the locked app's front.

Nothing in here touches protected data. The Puzzle code box hands any text that is
not a six-digit puzzle code to `on_code`; the app sends it to the local service,
which decides whether it unlocks, wipes or counts as a wrong attempt.
"""
from __future__ import annotations

import json
import os
import random
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

from mac_security import ROOT, is_puzzle_code

STATE_FILE = ROOT / "game.json"  # Beside the encrypted library, so the folder looks like a game's.
DIFFICULTIES = ("Easy", "Medium", "Hard")
CLUES = {"Easy": 40, "Medium": 32, "Hard": 26}
ALL = 0b1111111110  # bits 1..9
ROW = [i // 9 for i in range(81)]
COL = [i % 9 for i in range(81)]
BOX = [(i // 27) * 3 + (i % 9) // 3 for i in range(81)]
PEERS = [sorted({j for j in range(81) if j != i and (ROW[j] == ROW[i] or COL[j] == COL[i] or BOX[j] == BOX[i])})
         for i in range(81)]


def _search(grid: list[int], limit: int, rng: random.Random | None = None) -> int:
    """Count solutions up to `limit`, filling `grid` with the first one when rng is given."""
    rows, cols, boxes = [0] * 9, [0] * 9, [0] * 9
    for i, value in enumerate(grid):
        if value:
            bit = 1 << value
            if (rows[ROW[i]] | cols[COL[i]] | boxes[BOX[i]]) & bit:
                return 0
            rows[ROW[i]] |= bit
            cols[COL[i]] |= bit
            boxes[BOX[i]] |= bit
    empty = [i for i, value in enumerate(grid) if not value]
    found = 0

    def step() -> bool:
        nonlocal found
        best, options, fewest = -1, 0, 10
        for i in empty:
            if grid[i]:
                continue
            free = ALL & ~(rows[ROW[i]] | cols[COL[i]] | boxes[BOX[i]])
            count = bin(free).count("1")
            if count < fewest:
                best, options, fewest = i, free, count
                if count <= 1:
                    break
        if best < 0:
            found += 1
            return found >= limit
        values = [v for v in range(1, 10) if options >> v & 1]
        if rng:
            rng.shuffle(values)
        r, c, b = ROW[best], COL[best], BOX[best]
        for value in values:
            bit = 1 << value
            grid[best] = value
            rows[r] |= bit
            cols[c] |= bit
            boxes[b] |= bit
            done = step()
            rows[r] ^= bit
            cols[c] ^= bit
            boxes[b] ^= bit
            if done:
                if not rng:
                    grid[best] = 0
                return True
        grid[best] = 0
        return False

    step()
    return found


def difficulty_for(code: str) -> str:
    return DIFFICULTIES[(int(code[0]) - 1) % 3]


def random_code(difficulty: str) -> str:
    first = random.choice([d for d in range(10) if difficulty_for(str(d)) == difficulty])
    return f"{first}{random.randrange(100000):05d}"


def make_puzzle(code: str) -> tuple[list[int], list[int]]:
    """The same six-digit code always produces the same unique-solution puzzle."""
    rng = random.Random(int(code))
    solution = [0] * 81
    _search(solution, 1, rng)
    puzzle = solution[:]
    target = CLUES[difficulty_for(code)]
    clues = 81
    for i in rng.sample(range(81), 81):
        if clues <= target:
            break
        held, puzzle[i] = puzzle[i], 0
        if _search(puzzle[:], 2) != 1:
            puzzle[i] = held
        else:
            clues -= 1
    return puzzle, solution


def _palette(dark: bool) -> dict:
    if dark:
        return {"bg": "#18191c", "board": "#222429", "thin": "#363940", "thick": "#c9c4ba",
                "given": "#efece6", "entry": "#86aaff", "note": "#8c9098", "select": "#334670",
                "peer": "#272b33", "same": "#2d3b5c", "bad": "#ff7b7b", "bad_bg": "#44282b",
                "text": "#efece6", "muted": "#8c9098", "button": "#2a2c31", "button_hi": "#34373e",
                "accent": "#86aaff", "accent_fg": "#14161a", "done": "#59c27c"}
    return {"bg": "#f5f2ec", "board": "#fffdf9", "thin": "#dcd6ca", "thick": "#2e2a25",
            "given": "#2e2a25", "entry": "#2f63d6", "note": "#8b8578", "select": "#c9dbff",
            "peer": "#eef1f8", "same": "#dce6fb", "bad": "#d23f3f", "bad_bg": "#fbe3e1",
            "text": "#2e2a25", "muted": "#8b8578", "button": "#ebe6dc", "button_hi": "#e0dacd",
            "accent": "#2f63d6", "accent_fg": "#ffffff", "done": "#2e8a4f"}


def _family(root) -> str:
    families = set(tkfont.families(root))
    return next((name for name in ("SF Pro Rounded", "Avenir Next", "Helvetica Neue") if name in families), "Helvetica")


class Pill(tk.Label):
    """A flat rounded-feeling button whose colours follow the game palette."""
    def __init__(self, parent, text, command, colors, *, accent=False, font=("Helvetica", 13)):
        self.colors, self.accent, self.command = colors, accent, command
        super().__init__(parent, text=text, font=font, cursor="pointinghand", padx=14, pady=7)
        self.active = False
        self.paint()
        self.bind("<Enter>", lambda _: self.paint(hover=True))
        self.bind("<Leave>", lambda _: self.paint())
        self.bind("<ButtonRelease-1>", lambda _: self.command())

    def paint(self, hover=False):
        c = self.colors
        if self.accent or self.active:
            self.configure(background=c["accent"], foreground=c["accent_fg"])
        else:
            self.configure(background=c["button_hi"] if hover else c["button"], foreground=c["text"])


class SudokuView(tk.Frame):
    """Board, number pad and controls. `on_code(text, reply)` handles non-puzzle codes;
    call `reply("ok" | "invalid" | "error")` when the answer is known."""

    def __init__(self, parent, *, dark=False, on_code=None, state_file: Path | None = None):
        self.colors = c = _palette(dark)
        super().__init__(parent, background=c["bg"])
        self.on_code, self.state_file = on_code, state_file or STATE_FILE
        family = _family(self)
        self.fonts = {"title": (family, 26, "bold"), "meta": (family, 13), "given": (family, 24, "bold"),
                      "entry": (family, 24), "note": (family, 10), "button": (family, 13),
                      "pad": (family, 20), "count": (family, 9), "banner": (family, 18, "bold")}
        self.cell = 48
        self.selected = 40
        self.pencil = False
        self.history: list[tuple[int, int, frozenset]] = []
        self.flash: set[int] = set()
        self.dialog = None
        self.started = time.monotonic()
        self.build()
        if not self.load_state():
            self.new_game("Medium")
        self.tick()

    # ---------- layout ----------
    def build(self):
        c = self.colors
        top = tk.Frame(self, background=c["bg"])
        top.pack(fill="x", padx=28, pady=(16, 8))
        tk.Label(top, text="Sudoku", font=self.fonts["title"], background=c["bg"], foreground=c["text"]).pack(side="left")
        self.timer = tk.Label(top, font=self.fonts["meta"], background=c["bg"], foreground=c["muted"])
        self.timer.pack(side="right")
        self.meta = tk.Label(top, font=self.fonts["meta"], background=c["bg"], foreground=c["muted"])
        self.meta.pack(side="right", padx=14)

        size = self.cell * 9 + 8
        self.board = tk.Canvas(self, width=size, height=size, background=c["bg"], highlightthickness=0)
        self.board.pack(padx=28)
        self.board.bind("<Button-1>", self.click)

        pad = tk.Frame(self, background=c["bg"])
        pad.pack(pady=(14, 4))
        self.pad_buttons = []
        for digit in range(1, 10):
            frame = tk.Frame(pad, background=c["button"], cursor="pointinghand")
            frame.pack(side="left", padx=3)
            number = tk.Label(frame, text=str(digit), font=self.fonts["pad"], width=2,
                              background=c["button"], foreground=c["entry"])
            number.pack(padx=4, pady=(4, 0))
            left = tk.Label(frame, font=self.fonts["count"], background=c["button"], foreground=c["muted"])
            left.pack(pady=(0, 4))
            for widget in (frame, number, left):
                widget.bind("<ButtonRelease-1>", lambda _, d=digit: self.enter(d))
            self.pad_buttons.append((frame, number, left))

        tools = tk.Frame(self, background=c["bg"])
        tools.pack(pady=(8, 4))
        Pill(tools, "Undo", self.undo, c, font=self.fonts["button"]).pack(side="left", padx=4)
        Pill(tools, "Erase", lambda: self.enter(0), c, font=self.fonts["button"]).pack(side="left", padx=4)
        self.pencil_button = Pill(tools, "Notes: Off", self.toggle_pencil, c, font=self.fonts["button"])
        self.pencil_button.pack(side="left", padx=4)
        Pill(tools, "Hint", self.hint, c, font=self.fonts["button"]).pack(side="left", padx=4)
        Pill(tools, "Check", self.check, c, font=self.fonts["button"]).pack(side="left", padx=4)

        bottom = tk.Frame(self, background=c["bg"])
        bottom.pack(pady=(6, 6))
        for name in DIFFICULTIES:
            Pill(bottom, f"New {name.lower()}", lambda n=name: self.new_game(n), c,
                 font=self.fonts["button"]).pack(side="left", padx=4)
        Pill(bottom, "Puzzle code…", self.ask_code, c, font=self.fonts["button"]).pack(side="left", padx=(14, 4))
        self.status = tk.Label(self, font=self.fonts["meta"], background=c["bg"], foreground=c["muted"])
        self.status.pack(pady=(2, 10))

        # Keys belong to the board, so they disappear with it when the app unlocks.
        board = self.board
        for key in "123456789":
            board.bind(f"<KeyPress-{key}>", lambda _, d=int(key): self.enter(d))
        for key in ("BackSpace", "Delete", "0"):
            board.bind(f"<KeyPress-{key}>", lambda _: self.enter(0))
        for key, delta in (("Left", -1), ("Right", 1), ("Up", -9), ("Down", 9)):
            board.bind(f"<KeyPress-{key}>", lambda _, d=delta: self.move(d))
        board.bind("<KeyPress-n>", lambda _: self.toggle_pencil())
        board.bind("<Command-z>", lambda _: self.undo())
        self.after_idle(board.focus_set)

    # ---------- game state ----------
    def new_game(self, difficulty, code=None):
        self.code = code or random_code(difficulty)
        self.puzzle, self.solution = make_puzzle(self.code)
        self.entries = self.puzzle[:]
        self.notes = [set() for _ in range(81)]
        self.history.clear()
        self.elapsed_before = 0
        self.started = time.monotonic()
        self.solved = False
        self.selected = next((i for i in range(81) if not self.puzzle[i]), 40)
        self.status.configure(text="")
        self.save_state()
        self.draw()

    def elapsed(self) -> int:
        return int(self.elapsed_before + (0 if self.solved else time.monotonic() - self.started))

    def load_state(self) -> bool:
        try:
            data = json.loads(self.state_file.read_text())
            code = str(data["code"])
            if not is_puzzle_code(code):
                return False
            self.code = code.lstrip("#")
            self.puzzle, self.solution = make_puzzle(self.code)
            entries = [int(v) for v in data["entries"]]
            if len(entries) != 81 or any(self.puzzle[i] and entries[i] != self.puzzle[i] for i in range(81)):
                return False
            self.entries = entries
            self.notes = [set(int(v) for v in cell) for cell in data.get("notes", [[]] * 81)][:81]
            self.elapsed_before = int(data.get("elapsed", 0))
            self.solved = self.entries == self.solution
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self.started = time.monotonic()
        self.draw()
        return True

    def save_state(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"code": self.code, "entries": self.entries,
                                             "notes": [sorted(n) for n in self.notes],
                                             "elapsed": self.elapsed()}))
            os.replace(temporary, self.state_file)
        except OSError:
            pass  # A game that cannot save is still a game.

    # ---------- input ----------
    def click(self, event):
        col, row = (event.x - 4) // self.cell, (event.y - 4) // self.cell
        self.board.focus_set()
        if 0 <= col < 9 and 0 <= row < 9:
            self.selected = row * 9 + col
            self.draw()

    def move(self, delta):
        index = self.selected + delta
        if delta in (-1, 1) and index // 9 != self.selected // 9:
            return
        if 0 <= index < 81:
            self.selected = index
            self.draw()

    def enter(self, digit):
        i = self.selected
        if self.solved or self.puzzle[i]:
            return
        before = (i, self.entries[i], frozenset(self.notes[i]))
        if self.pencil and digit:
            if self.entries[i]:
                return
            self.notes[i] ^= {digit}
        else:
            if self.entries[i] == digit and not self.notes[i]:
                return
            self.entries[i] = digit
            self.notes[i] = set()
            if digit:
                for peer in PEERS[i]:
                    self.notes[peer].discard(digit)
        self.history.append(before)
        self.flash.discard(i)
        if self.entries == self.solution:
            self.elapsed_before, self.solved = self.elapsed(), True
            minutes, seconds = divmod(self.elapsed_before, 60)
            self.status.configure(text=f"Solved in {minutes}:{seconds:02d}. Nicely done.",
                                  foreground=self.colors["done"])
        self.save_state()
        self.draw()

    def undo(self):
        if self.history and not self.solved:
            i, value, notes = self.history.pop()
            self.entries[i], self.notes[i] = value, set(notes)
            self.selected = i
            self.save_state()
            self.draw()

    def toggle_pencil(self):
        self.pencil = not self.pencil
        self.pencil_button.configure(text=f"Notes: {'On' if self.pencil else 'Off'}")
        self.pencil_button.active = self.pencil
        self.pencil_button.paint()

    def hint(self):
        empty = [i for i in range(81) if self.entries[i] != self.solution[i]]
        if self.solved or not empty:
            return
        self.selected = self.selected if self.selected in empty else random.choice(empty)
        pencil, self.pencil = self.pencil, False
        self.enter(self.solution[self.selected])
        self.pencil = pencil

    def check(self):
        wrong = {i for i in range(81) if self.entries[i] and self.entries[i] != self.solution[i]}
        self.flash = wrong
        if self.solved:
            text = "Solved."
        elif wrong:
            text = f"{len(wrong)} {'cell needs' if len(wrong) == 1 else 'cells need'} another look."
        else:
            text = f"Looking good. {sum(1 for v in self.entries if not v)} to go."
        self.status.configure(text=text, foreground=self.colors["bad"] if wrong else self.colors["muted"])
        self.draw()
        self.after(1800, self.clear_flash)

    def clear_flash(self):
        if self.winfo_exists():
            self.flash = set()
            self.draw()

    # ---------- drawing ----------
    def tick(self):
        if not self.winfo_exists():
            return
        minutes, seconds = divmod(self.elapsed(), 60)
        self.timer.configure(text=f"{minutes}:{seconds:02d}")
        self.after(1000, self.tick)

    def conflicts(self) -> set[int]:
        result = set()
        for i, value in enumerate(self.entries):
            if value and any(self.entries[p] == value for p in PEERS[i]):
                result.add(i)
        return result

    def draw(self):
        c, canvas, size, o = self.colors, self.board, self.cell, 4
        canvas.delete("all")
        full = size * 9
        canvas.create_rectangle(o, o, o + full, o + full, fill=c["board"], outline="")
        selected_value = self.entries[self.selected]
        conflicts = self.conflicts()
        for i in range(81):
            x, y = o + COL[i] * size, o + ROW[i] * size
            fill = None
            if i == self.selected:
                fill = c["select"]
            elif i in self.flash or (i in conflicts and not self.puzzle[i]):
                fill = c["bad_bg"]
            elif selected_value and self.entries[i] == selected_value:
                fill = c["same"]
            elif i in PEERS[self.selected]:
                fill = c["peer"]
            if fill:
                canvas.create_rectangle(x, y, x + size, y + size, fill=fill, outline="")
            value = self.entries[i]
            if value:
                given = bool(self.puzzle[i])
                color = c["given"] if given else (c["bad"] if i in conflicts or i in self.flash else c["entry"])
                canvas.create_text(x + size / 2, y + size / 2 + 1, text=str(value), fill=color,
                                   font=self.fonts["given" if given else "entry"])
            elif self.notes[i]:
                third = size / 3
                for note in self.notes[i]:
                    nx, ny = (note - 1) % 3, (note - 1) // 3
                    canvas.create_text(x + third * (nx + 0.5), y + third * (ny + 0.5), text=str(note),
                                       fill=c["note"], font=self.fonts["note"])
        for line in range(10):
            thick = line % 3 == 0
            position = o + line * size
            width, color = (2.5, c["thick"]) if thick else (1, c["thin"])
            if not thick:
                canvas.create_line(o, position, o + full, position, fill=color, width=width)
                canvas.create_line(position, o, position, o + full, fill=color, width=width)
        for line in range(0, 10, 3):
            position = o + line * size
            canvas.create_line(o, position, o + full, position, fill=c["thick"], width=2.5, capstyle="projecting")
            canvas.create_line(position, o, position, o + full, fill=c["thick"], width=2.5, capstyle="projecting")
        self.meta.configure(text=f"{difficulty_for(self.code)} · #{self.code}")
        for digit, (frame, number, left) in enumerate(self.pad_buttons, start=1):
            remaining = 9 - self.entries.count(digit)
            left.configure(text=str(remaining) if remaining > 0 else "✓")
            number.configure(foreground=c["muted"] if remaining <= 0 else c["entry"])

    # ---------- puzzle codes ----------
    def ask_code(self):
        if self.dialog:
            self.dialog.lift()
            return
        c = self.colors
        dialog = self.dialog = tk.Toplevel(self, background=c["bg"], padx=24, pady=20)
        dialog.title("Puzzle code")
        dialog.resizable(False, False)
        dialog.transient(self.winfo_toplevel())
        tk.Label(dialog, text="Enter a puzzle code", font=self.fonts["banner"], background=c["bg"],
                 foreground=c["text"]).pack(anchor="w")
        tk.Label(dialog, text="Share the code under the title to play the same puzzle on another Mac.",
                 font=self.fonts["meta"], background=c["bg"], foreground=c["muted"], wraplength=320,
                 justify="left").pack(anchor="w", pady=(4, 12))
        entry = tk.Entry(dialog, show="•", width=30, font=self.fonts["meta"], relief="flat",
                         background=c["board"], foreground=c["text"], insertbackground=c["text"],
                         highlightthickness=1, highlightbackground=c["thin"], highlightcolor=c["accent"])
        entry.pack(fill="x", ipady=6)
        message = tk.Label(dialog, text="", font=self.fonts["meta"], background=c["bg"], foreground=c["bad"])
        message.pack(anchor="w", pady=(8, 0))
        row = tk.Frame(dialog, background=c["bg"])
        row.pack(fill="x", pady=(10, 0))
        pending = {"active": False}

        def close():
            self.dialog = None
            dialog.destroy()
            if self.winfo_exists():
                self.board.focus_set()

        def reply(answer):
            pending["active"] = False
            if not dialog.winfo_exists():
                return
            if answer == "ok":
                close()
                return
            entry.configure(state="normal")
            entry.delete(0, "end")
            entry.focus_set()
            message.configure(text="Invalid puzzle code." if answer == "invalid"
                              else "Couldn't load that puzzle. Try again.")

        def submit(_=None):
            text = entry.get()
            if pending["active"] or not text.strip():
                return
            if is_puzzle_code(text):
                code = text.strip().lstrip("#")
                close()
                self.new_game(difficulty_for(code), code)
                return
            if not self.on_code:
                reply("invalid")
                return
            pending["active"] = True
            message.configure(text="Loading…", foreground=c["muted"])
            entry.configure(state="disabled")
            self.on_code(text, lambda answer: (message.configure(foreground=c["bad"]), reply(answer)))

        Pill(row, "Cancel", close, c, font=self.fonts["button"]).pack(side="right", padx=(6, 0))
        Pill(row, "Load", submit, c, accent=True, font=self.fonts["button"]).pack(side="right")
        entry.bind("<Return>", submit)
        entry.bind("<Escape>", lambda _: close())
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.update_idletasks()
        top = self.winfo_toplevel()
        dialog.geometry(f"+{top.winfo_rootx() + (top.winfo_width() - dialog.winfo_width()) // 2}"
                        f"+{top.winfo_rooty() + 140}")
        entry.focus_set()

    def destroy(self):
        self.save_state()
        if self.dialog:
            self.dialog.destroy()
            self.dialog = None
        super().destroy()


if __name__ == "__main__":
    from ui_theme import DARK
    window = tk.Tk()
    window.title("Sudoku")
    window.configure(background=_palette(DARK)["bg"])
    SudokuView(window, dark=DARK, state_file=Path(os.environ.get("TMPDIR", "/tmp")) / "sudoku-preview.json").pack()
    window.mainloop()
