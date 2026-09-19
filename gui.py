"""
gui.py

Tkinter front-end for the Granny Legacy seed predictor.

The user assigns each of the 31 items a constraint (Pin / Prefer / Don't
care) and a target slot (one of 87: 71 free spots + 16 puzzles), clicks
Generate, and gets back ranked candidate seeds from a search backend
(`gpu_backend.GpuSearchBackend` if an OpenCL device is available, else
`search.CpuSearchBackend`) running on a background thread (so the UI never
freezes).

Applying a found seed: the *reliable*, primary path is the game's own
built-in Seed menu (type the seed, press Enter) -- confirmed on a real run.
Writing the seed to the Windows registry was found to NOT reliably reach the
game, so it is offered only as a clearly-labelled "advanced / experimental"
fallback, behind an explicit confirmation dialog. See CLAUDE_FINDINGS.md
S20/S21.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# pythonw.exe COMPATIBILITY -- must run before any import that can warn.
#
# Launched via pythonw (e.g. run.bat), there is no console, so sys.stdout and
# sys.stderr are None. PyOpenCL emits a CompilerWarning while building the
# kernel; the warnings machinery writes to sys.stderr, which raises on None
# and kills the process before the window ever appears -- the GUI simply
# never opens, with no visible error.
#
# Point the missing streams at a log file so warnings and tracebacks are
# recorded instead of fatal.
# --------------------------------------------------------------------------
import os as _os
import sys as _sys

if _sys.stdout is None or _sys.stderr is None:
    try:
        _log_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "logs")
        _os.makedirs(_log_dir, exist_ok=True)
        _log_fh = open(_os.path.join(_log_dir, "gui.log"), "a", buffering=1, encoding="utf-8")
    except Exception:
        _log_fh = open(_os.devnull, "w", encoding="utf-8")
    if _sys.stdout is None:
        _sys.stdout = _log_fh
    if _sys.stderr is None:
        _sys.stderr = _log_fh


import glob
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import search
import seed_registry
import gpu_backend
import variants
from simulator import load_config


def _format_duration(seconds: float) -> str:
    """Human-readable duration for ETA display. `float('inf')` / NaN /
    negative -> "unknown"."""
    if seconds != seconds or seconds < 0 or seconds == float("inf"):
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


CATEGORY_LABELS = {
    1: "Escape",
    2: "Escape+Puzzle",
    3: "Puzzle",
    4: "Free-only",
}

STATE_DONT_CARE = "Don't care"
STATE_PIN = "Pin"
STATE_PREFER = "Prefer"
STATES = (STATE_DONT_CARE, STATE_PIN, STATE_PREFER)


# ---------------------------------------------------------------------------
# House-version -> in-game PlayerPrefs mapping (see re_variant_activation.md).
#
# `ObjectsManager.Start()` picks which SeedSystemManager_* GameObject is
# active from three PlayerPrefs ints written by real MainMenu widgets:
#   - Toggle "Flash"            -> FlashL   (must be 1, or the seed system is
#                                             off entirely and the old preset
#                                             layout is used instead)
#   - Slider "Game Version"     -> CURVERSION (0..8; label reads "1.<value>")
#   - Toggle "Extras"           -> Extras     (only reachable at slider = 8)
#
# Each entry: variantName -> (CURVERSION, Extras, in-game "Game Version (..)" label).
# FlashL is always 1 for every variant (a seed only ever applies with the
# seed system switched on).
# ---------------------------------------------------------------------------
VARIANT_INGAME_SETTINGS = {
    "1.0": (0, 0, "1.0"),
    "1.1": (1, 0, "1.1"),
    "1.2": (2, 0, "1.2"),
    "1.3": (3, 0, "1.3"),
    "1.4": (4, 0, "1.4"),
    "1.5": (5, 0, "1.5"),
    "1.6": (6, 0, "1.6"),
    "1.7": (7, 0, "1.7"),
    "Normal": (8, 0, "1.8"),
    "More": (8, 1, "1.8"),
}


def _variant_ingame_instruction(variant_name: str) -> str:
    """Human-readable "how to select this house version in-game" line,
    generated from VARIANT_INGAME_SETTINGS (see re_variant_activation.md).
    Returns "" if the variant is unrecognized."""
    setting = VARIANT_INGAME_SETTINGS.get(variant_name)
    if setting is None:
        return ""
    curversion, extras, version_label = setting
    extras_text = "ON" if extras else "OFF"
    return (
        f"In-game: Flash ON, Game Version ({version_label}), Extras {extras_text} "
        f"(CURVERSION={curversion}, Extras={extras}, FlashL=1)"
    )


def _newest_config_path() -> str:
    dumps_dir = Path(__file__).parent / "dumps"
    files = sorted(dumps_dir.glob("config_*.json"))
    if not files:
        raise FileNotFoundError(f"No config_*.json files found in {dumps_dir}")
    return str(files[-1])


def build_slot_catalog(config: dict) -> list[tuple[str, str]]:
    """Returns a list of (display_text, canonical_slot_label) covering all
    87 slots, FREE grouped by area (area/spot order), then PUZZLE slots.
    `canonical_slot_label` is exactly the string simulate() would output
    ("FREE:Area/Spot (n)" or "PUZZLE:Name").
    """
    entries = []
    for area in config["spawnAreas"]:
        for spot in area.freeSpots:
            label = f"FREE:{area.areaName}/{spot.name}"
            entries.append((f"FREE   {area.areaName} / {spot.name}", label))
    puzzles = sorted(config["puzzleDefs"], key=lambda p: p.puzzleName)
    for p in puzzles:
        if p.spawnPointId is None:
            continue
        label = f"PUZZLE:{p.puzzleName}"
        entries.append((f"PUZZLE {p.puzzleName}", label))
    return entries


# ---------------------------------------------------------------------------
# Searchable slot picker dialog
# ---------------------------------------------------------------------------

class SlotPickerDialog(tk.Toplevel):
    def __init__(self, parent, catalog: list[tuple[str, str]], current_label: str | None):
        super().__init__(parent)
        self.title("Choose slot")
        self.geometry("480x520")
        self.transient(parent)
        self.grab_set()
        self.catalog = catalog
        self.result: str | None = None

        tk.Label(self, text="Filter (area name, spot number, or puzzle name):").pack(
            anchor="w", padx=8, pady=(8, 0))
        self.filter_var = tk.StringVar()
        entry = tk.Entry(self, textvariable=self.filter_var)
        entry.pack(fill="x", padx=8, pady=4)
        entry.bind("<KeyRelease>", self._refilter)
        entry.focus_set()

        list_frame = tk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=8, pady=4)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical")
        self.listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, exportselection=False)
        scrollbar.config(command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind("<Double-Button-1>", lambda e: self._accept())

        btn_frame = tk.Frame(self)
        btn_frame.pack(fill="x", padx=8, pady=8)
        tk.Button(btn_frame, text="Clear (Don't care)", command=self._clear).pack(side="left")
        tk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="right")
        tk.Button(btn_frame, text="OK", command=self._accept).pack(side="right", padx=4)

        self._filtered = list(catalog)
        self._populate()
        if current_label:
            for i, (_, lbl) in enumerate(self._filtered):
                if lbl == current_label:
                    self.listbox.selection_set(i)
                    self.listbox.see(i)
                    break

        self.wait_window(self)

    def _populate(self):
        self.listbox.delete(0, tk.END)
        for text, _ in self._filtered:
            self.listbox.insert(tk.END, text)

    def _refilter(self, event=None):
        needle = self.filter_var.get().strip().lower()
        if not needle:
            self._filtered = list(self.catalog)
        else:
            self._filtered = [(t, l) for (t, l) in self.catalog if needle in t.lower()]
        self._populate()

    def _accept(self):
        sel = self.listbox.curselection()
        if sel:
            self.result = self._filtered[sel[0]][1]
        self.destroy()

    def _clear(self):
        self.result = ""  # sentinel meaning "clear slot"
        self.destroy()


# ---------------------------------------------------------------------------
# One item row in the scrollable table
# ---------------------------------------------------------------------------

class ItemRow:
    def __init__(self, parent, row_index: int, item_name: str, category_label: str,
                 slot_catalog: list[tuple[str, str]], on_change):
        self.item_name = item_name
        self.slot_catalog = slot_catalog
        self.slot_label: str | None = None
        self.on_change = on_change

        bg = "#f4f4f4" if row_index % 2 == 0 else "#ffffff"
        tk.Label(parent, text=item_name, anchor="w", bg=bg, width=16).grid(
            row=row_index, column=0, sticky="ew", padx=2, pady=1)
        tk.Label(parent, text=category_label, anchor="w", bg=bg, width=14).grid(
            row=row_index, column=1, sticky="ew", padx=2, pady=1)

        self.state_var = tk.StringVar(value=STATE_DONT_CARE)
        state_box = ttk.Combobox(parent, textvariable=self.state_var, values=STATES,
                                  state="readonly", width=11)
        state_box.grid(row=row_index, column=2, sticky="ew", padx=2, pady=1)
        state_box.bind("<<ComboboxSelected>>", self._state_changed)

        self.slot_button = tk.Button(parent, text="(choose slot)", anchor="w",
                                      command=self._pick_slot, width=32)
        self.slot_button.grid(row=row_index, column=3, sticky="ew", padx=2, pady=1)

    def _state_changed(self, event=None):
        self.on_change()

    def _pick_slot(self):
        dlg = SlotPickerDialog(self.slot_button.winfo_toplevel(), self.slot_catalog, self.slot_label)
        if dlg.result is not None:
            if dlg.result == "":
                self.slot_label = None
                self.slot_button.config(text="(choose slot)")
            else:
                self.slot_label = dlg.result
                self.slot_button.config(text=dlg.result)
            self.on_change()

    def kind(self) -> str | None:
        s = self.state_var.get()
        if s == STATE_PIN:
            return "pin"
        if s == STATE_PREFER:
            return "prefer"
        return None

    def to_constraint(self) -> search.Constraint | None:
        k = self.kind()
        if k is None or not self.slot_label:
            return None
        return search.Constraint(item_name=self.item_name, slot_label=self.slot_label, kind=k)

    def set_state(self, kind: str | None, slot_label: str | None):
        if kind == "pin":
            self.state_var.set(STATE_PIN)
        elif kind == "prefer":
            self.state_var.set(STATE_PREFER)
        else:
            self.state_var.set(STATE_DONT_CARE)
        self.slot_label = slot_label
        self.slot_button.config(text=slot_label if slot_label else "(choose slot)")


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class SeedPredictorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Granny Legacy Seed Predictor")
        root.geometry("980x760")

        # -- House-version (SeedManager variant) selection -----------------
        # `variants.py` reads scene_data.json; if that file (or the module
        # itself) is missing/unloadable, fall back to today's behavior
        # (newest mod dump) and surface the reason in the UI instead of
        # crashing.
        self.variant_names: list[str] = []
        self.variant_load_error: str | None = None
        try:
            self.variant_names = variants.list_variants()
            if not self.variant_names:
                raise RuntimeError("scene_data.json contains no seedManagerVariants")
        except Exception as e:
            self.variant_load_error = str(e)
            self.variant_names = []

        self.using_variants = bool(self.variant_names)
        if self.using_variants:
            default_variant = "Normal" if "Normal" in self.variant_names else self.variant_names[0]
            self.variant_var = tk.StringVar(value=default_variant)
            self.config_path = None
            self.config = variants.load_variant_config(default_variant)
        else:
            self.variant_var = tk.StringVar(value="")
            self.config_path = _newest_config_path()
            self.config = load_config(self.config_path)
        self.slot_catalog = build_slot_catalog(self.config)

        # -- Backend auto-selection: try GPU first, fall back to CPU. -----
        # chunk_size is deliberately smaller than CpuSearchBackend's own
        # default (100_000): Cancel is only checked between completed
        # chunks, so a smaller chunk keeps Cancel responsive in the GUI
        # (a few seconds) instead of tens of seconds to minutes.
        self.cpu_backend = search.CpuSearchBackend(chunk_size=5_000)
        self.gpu_backend_obj = None
        self.gpu_available = False
        self.gpu_device_info = "GPU backend not attempted."
        try:
            gb = gpu_backend.GpuSearchBackend(self.config)
            self.gpu_backend_obj = gb
            self.gpu_available = gb.available()
            self.gpu_device_info = gb.device_info()
        except Exception as e:
            self.gpu_available = False
            self.gpu_device_info = f"GPU unavailable -- using CPU ({e})"

        self.backend_choice = tk.StringVar(value="gpu" if self.gpu_available else "cpu")
        self.backend = self.gpu_backend_obj if self.gpu_available else self.cpu_backend

        self.search_thread: threading.Thread | None = None
        self.cancel_evt = threading.Event()
        self.progress_queue: queue.Queue = queue.Queue()
        self.last_results: list[search.SeedResult] = []
        self._selected_seed: int | None = None
        self._search_start_time: float = 0.0
        self._last_seed_range: tuple[int, int] | None = None

        self._build_ui()
        self._refresh_current_seed()
        self._update_variant_stats_label()
        self._update_variant_ingame_label()
        self._update_apply_instructions_label()
        self._update_feasibility()
        self._update_device_info_label()
        self.root.after(100, self._poll_queue)

    # -- UI construction -----------------------------------------------

    def _build_ui(self):
        top = tk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)

        self.config_label_var = tk.StringVar(value=self._config_label_text())
        tk.Label(top, textvariable=self.config_label_var, fg="#555").pack(side="left")

        seed_frame = tk.Frame(top)
        seed_frame.pack(side="right")
        tk.Label(seed_frame, text="Current registry seed:").pack(side="left")
        self.current_seed_var = tk.StringVar(value="(unknown)")
        tk.Label(seed_frame, textvariable=self.current_seed_var, font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)
        tk.Button(seed_frame, text="Refresh", command=self._refresh_current_seed).pack(side="left")

        # House version selector
        version_frame = tk.LabelFrame(self.root, text="House version")
        version_frame.pack(fill="x", padx=8, pady=4)

        version_row = tk.Frame(version_frame)
        version_row.pack(fill="x")
        if self.using_variants:
            version_box = ttk.Combobox(
                version_row, textvariable=self.variant_var, values=self.variant_names,
                state="readonly", width=10)
            version_box.pack(side="left", padx=(6, 8), pady=4)
            version_box.bind("<<ComboboxSelected>>", self._on_variant_change)
        else:
            tk.Label(
                version_row,
                text=f"House-version selector unavailable ({self.variant_load_error}) -- "
                     f"using newest mod dump.",
                fg="#b71c1c",
            ).pack(side="left", padx=6, pady=4)
        self.variant_stats_var = tk.StringVar()
        tk.Label(version_row, textvariable=self.variant_stats_var, fg="#555", anchor="w",
                 justify="left", wraplength=760).pack(side="left", padx=(4, 6), pady=4, fill="x", expand=True)

        # In-game "how to select this house version" line -- only meaningful
        # when a real variant is loaded (the fallback newest-mod-dump path
        # has no corresponding PlayerPrefs mapping).
        self.variant_ingame_var = tk.StringVar()
        if self.using_variants:
            tk.Label(version_frame, textvariable=self.variant_ingame_var, fg="#1565c0",
                     anchor="w", justify="left", wraplength=940).pack(
                fill="x", padx=6, pady=(0, 4))

        # Backend selector
        backend_frame = tk.LabelFrame(self.root, text="Search backend")
        backend_frame.pack(fill="x", padx=8, pady=4)
        tk.Radiobutton(
            backend_frame, text="GPU (OpenCL -- full 2^32 scan in ~12 min)",
            variable=self.backend_choice, value="gpu", command=self._on_backend_change,
            state="normal" if self.gpu_available else "disabled",
        ).pack(side="left")
        tk.Radiobutton(
            backend_frame, text="CPU (multiprocessing -- quick scan recommended)",
            variable=self.backend_choice, value="cpu", command=self._on_backend_change,
        ).pack(side="left", padx=(8, 0))
        self.device_info_var = tk.StringVar()
        tk.Label(backend_frame, textvariable=self.device_info_var, fg="#555",
                 anchor="w").pack(side="left", padx=(16, 0))

        # Feasibility panel
        feas_frame = tk.LabelFrame(self.root, text="Feasibility (live estimate)")
        feas_frame.pack(fill="x", padx=8, pady=4)
        self.feasibility_var = tk.StringVar()
        tk.Label(feas_frame, textvariable=self.feasibility_var, anchor="w",
                 justify="left", wraplength=940).pack(fill="x", padx=6, pady=4)

        # Scrollable item table
        table_outer = tk.LabelFrame(self.root, text="Items")
        table_outer.pack(fill="both", expand=True, padx=8, pady=4)
        self.table_outer = table_outer

        header = tk.Frame(table_outer)
        header.pack(fill="x")
        for col, (text, w) in enumerate([("Item", 16), ("Category", 14), ("State", 11), ("Target slot", 32)]):
            tk.Label(header, text=text, font=("Segoe UI", 9, "bold"), width=w, anchor="w").grid(
                row=0, column=col, padx=2)

        canvas = tk.Canvas(table_outer, highlightthickness=0)
        vsb = tk.Scrollbar(table_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self.item_table_canvas = canvas
        self.item_table_inner = inner

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self.rows: dict[str, ItemRow] = {}
        self._rebuild_item_table()

        # Search controls
        ctrl = tk.LabelFrame(self.root, text="Search")
        ctrl.pack(fill="x", padx=8, pady=4)

        range_frame = tk.Frame(ctrl)
        range_frame.pack(fill="x", padx=4, pady=2)
        self.range_choice = tk.StringVar(value="full" if self.gpu_available else "quick")
        tk.Radiobutton(range_frame, text="Quick scan (0 .. 100,000,000)", variable=self.range_choice,
                       value="quick").pack(side="left")
        tk.Radiobutton(range_frame, text="Full 32-bit range", variable=self.range_choice,
                       value="full").pack(side="left")
        tk.Radiobutton(range_frame, text="Custom:", variable=self.range_choice,
                       value="custom").pack(side="left")
        self.custom_start_var = tk.StringVar(value="0")
        self.custom_end_var = tk.StringVar(value="100000000")
        tk.Entry(range_frame, textvariable=self.custom_start_var, width=12).pack(side="left", padx=2)
        tk.Label(range_frame, text="to").pack(side="left")
        tk.Entry(range_frame, textvariable=self.custom_end_var, width=12).pack(side="left", padx=2)

        tk.Label(range_frame, text="   Max results:").pack(side="left", padx=(12, 2))
        self.limit_var = tk.StringVar(value="50")
        tk.Entry(range_frame, textvariable=self.limit_var, width=6).pack(side="left")

        btn_frame = tk.Frame(ctrl)
        btn_frame.pack(fill="x", padx=4, pady=4)
        self.generate_btn = tk.Button(btn_frame, text="Generate", command=self._on_generate,
                                      bg="#2e7d32", fg="white", font=("Segoe UI", 10, "bold"))
        self.generate_btn.pack(side="left")
        self.cancel_btn = tk.Button(btn_frame, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)

        tk.Button(btn_frame, text="Save constraints...", command=self._save_constraints).pack(side="right")
        tk.Button(btn_frame, text="Load constraints...", command=self._load_constraints).pack(side="right", padx=6)

        self.progress = ttk.Progressbar(ctrl, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", padx=4, pady=(0, 4))
        self.progress_label_var = tk.StringVar(value="Idle.")
        tk.Label(ctrl, textvariable=self.progress_label_var, anchor="w").pack(fill="x", padx=4)

        # Results
        results_outer = tk.LabelFrame(self.root, text="Results")
        results_outer.pack(fill="both", expand=True, padx=8, pady=4)

        results_pane = tk.PanedWindow(results_outer, orient="horizontal")
        results_pane.pack(fill="both", expand=True)

        left = tk.Frame(results_pane)
        self.results_tree = ttk.Treeview(left, columns=("seed", "pins", "prefs"), show="headings", height=8)
        self.results_tree.heading("seed", text="Seed")
        self.results_tree.heading("pins", text="Pins matched")
        self.results_tree.heading("prefs", text="Prefs matched")
        self.results_tree.column("seed", width=140)
        self.results_tree.column("pins", width=100)
        self.results_tree.column("prefs", width=100)
        self.results_tree.pack(fill="both", expand=True)
        self.results_tree.bind("<<TreeviewSelect>>", self._on_result_selected)
        results_pane.add(left)

        right = tk.Frame(results_pane)
        tk.Label(right, text="Full placement for selected seed:").pack(anchor="w")
        self.placement_tree = ttk.Treeview(right, columns=("item", "slot"), show="headings", height=8)
        self.placement_tree.heading("item", text="Item")
        self.placement_tree.heading("slot", text="Slot")
        self.placement_tree.column("item", width=140)
        self.placement_tree.column("slot", width=260)
        self.placement_tree.pack(fill="both", expand=True)
        results_pane.add(right)

        # --- Primary: apply the seed via the game's own Seed menu ---------
        primary_frame = tk.LabelFrame(results_outer, text="Apply this seed (recommended)")
        primary_frame.pack(fill="x", pady=(8, 4))

        seed_row = tk.Frame(primary_frame)
        seed_row.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(seed_row, text="Seed:", font=("Segoe UI", 11)).pack(side="left")
        self.big_seed_var = tk.StringVar(value="(select a result)")
        tk.Label(seed_row, textvariable=self.big_seed_var,
                 font=("Segoe UI", 22, "bold"), fg="#1b5e20").pack(side="left", padx=10)
        self.copy_btn = tk.Button(seed_row, text="Copy seed to clipboard",
                                  command=self._copy_seed_to_clipboard, state="disabled")
        self.copy_btn.pack(side="left", padx=10)
        self.copy_status_var = tk.StringVar(value="")
        tk.Label(seed_row, textvariable=self.copy_status_var, fg="#1b5e20").pack(side="left")

        self.apply_instructions_var = tk.StringVar()
        tk.Label(
            primary_frame, textvariable=self.apply_instructions_var,
            anchor="w", justify="left", wraplength=940,
        ).pack(fill="x", padx=6, pady=(0, 6))

        # --- Advanced / experimental: direct registry write ----------------
        advanced_frame = tk.LabelFrame(
            results_outer, text="Advanced / experimental (not recommended)", fg="#b71c1c")
        advanced_frame.pack(fill="x", pady=(4, 8))
        tk.Label(
            advanced_frame,
            text=("Writing the seed directly to the Windows registry is KNOWN TO BE "
                  "UNRELIABLE -- in testing this alone did not reliably reach the "
                  "game. Use the Seed menu above instead. The game must be fully "
                  "closed before writing."),
            anchor="w", justify="left", wraplength=940, fg="#b71c1c",
        ).pack(fill="x", padx=6, pady=(6, 2))
        self.apply_btn = tk.Button(advanced_frame, text="Write seed to registry (experimental)...",
                                   command=self._on_apply_to_game, state="disabled")
        self.apply_btn.pack(anchor="w", padx=6, pady=(0, 6))

    # -- House version (SeedManager variant) ------------------------------

    def _config_label_text(self) -> str:
        if self.using_variants:
            raw = self.config.get("variantName", self.variant_var.get())
            return f"Config: house version '{raw}' (scene_data.json)"
        return f"Config: {Path(self.config_path).name}"

    def _update_config_label(self):
        self.config_label_var.set(self._config_label_text())

    @staticmethod
    def _pluralize(n: int, word: str) -> str:
        return f"{n} {word}" if n == 1 else f"{n} {word}s"

    def _update_variant_stats_label(self):
        if not self.using_variants:
            self.variant_stats_var.set("")
            return
        # Source of truth is the currently-loaded config's own variant name
        # (not self.variant_var), so this stays correct regardless of how
        # `self.config` got set.
        name = self.config.get("variantName", self.variant_var.get())
        try:
            s = variants.variant_stats(name)
        except Exception as e:
            self.variant_stats_var.set(f"(could not read stats: {e})")
            return

        line = " | ".join([
            self._pluralize(s["items"], "item"),
            self._pluralize(s["puzzles"], "puzzle"),
            self._pluralize(s["areas"], "area"),
            self._pluralize(s["freeSpots"], "free spot"),
            self._pluralize(s["totalSlots"], "total slot"),
        ])

        notes = []
        if not s["gpu32bitOk"]:
            notes.append(
                f"Note: {s['items']} items -- requires the GPU backend's 64-bit mask mode."
            )
        unmatched = self.config.get("unmatched") or []
        if unmatched:
            notes.append(
                f"Note: {len(unmatched)} object(s) in this version have no position data "
                f"from the mod dump join (see console/log for details)."
            )
        if notes:
            line = line + "  --  " + "  ".join(notes)
        self.variant_stats_var.set(line)

    def _update_variant_ingame_label(self):
        if not self.using_variants:
            return
        name = self.config.get("variantName", self.variant_var.get())
        self.variant_ingame_var.set(_variant_ingame_instruction(name))

    def _update_apply_instructions_label(self):
        """Ordered "how to use this seed" text in the Results panel. Includes
        the house-version steps only when a real variant is loaded -- a
        correct seed on the wrong Game Version/Extras setting produces a
        different item layout, so the version must be set as well as the
        seed."""
        if self.using_variants:
            name = self.config.get("variantName", self.variant_var.get())
            setting = VARIANT_INGAME_SETTINGS.get(name)
            version_step = f"Set the Game Version slider to ({setting[2]})" if setting else \
                "Set the Game Version slider to match this house version"
            if setting and setting[1]:
                version_step += " and turn Extras ON"
            self.apply_instructions_var.set(
                "To use this seed, in this exact order:\n"
                "1. In the main menu, set Flash ON (the seed-system switch).\n"
                f"2. {version_step}.\n"
                "3. Open the Seed menu, type this seed, press Enter.\n"
                "4. Start a new run.\n"
                "The house version must match the seed as well -- this seed on the "
                "wrong Game Version/Extras setting will place items differently."
            )
        else:
            self.apply_instructions_var.set(
                "To use this seed: open Granny Legacy -> Seed menu -> type this "
                "seed -> press Enter -> start a new run."
            )

    def _on_variant_change(self, event=None):
        self._rebuild_for_variant(self.variant_var.get())

    def _rebuild_for_variant(self, display_name: str):
        try:
            new_config = variants.load_variant_config(display_name)
        except Exception as e:
            messagebox.showerror("Failed to load house version", str(e))
            return
        self.variant_var.set(display_name)
        self.config = new_config
        self.slot_catalog = build_slot_catalog(self.config)
        self._rebuild_item_table()
        self._update_config_label()
        self._update_variant_stats_label()
        self._update_variant_ingame_label()
        self._update_apply_instructions_label()
        self._update_feasibility()

    # -- Item table --------------------------------------------------------

    def _rebuild_item_table(self):
        """(Re)builds the scrollable item-table rows from `self.config`.
        Destroys any existing rows first and resets every pin/prefer state
        to "Don't care" -- old targets are meaningless across a house-version
        change, since slot layouts differ between versions.
        """
        for child in self.item_table_inner.winfo_children():
            child.destroy()

        valid_items = [i for i in self.config["items"] if i.valid]
        self.table_outer.config(text=f"Items ({len(valid_items)})")

        self.rows = {}
        for idx, item in enumerate(valid_items):
            cat_label = CATEGORY_LABELS.get(item.category, str(item.category))
            row = ItemRow(self.item_table_inner, idx, item.itemName, cat_label, self.slot_catalog,
                          on_change=self._update_feasibility)
            self.rows[item.itemName] = row

        self.item_table_inner.update_idletasks()
        self.item_table_canvas.configure(scrollregion=self.item_table_canvas.bbox("all"))
        self.item_table_canvas.yview_moveto(0)

    # -- Feasibility ------------------------------------------------------

    def _current_constraints(self) -> list[search.Constraint]:
        out = []
        for row in self.rows.values():
            c = row.to_constraint()
            if c is not None:
                out.append(c)
        return out

    def _update_feasibility(self):
        constraints = self._current_constraints()
        num_pins = sum(1 for c in constraints if c.kind == "pin")
        total_slots = search.total_slots_for_config(self.config)
        self.feasibility_var.set(search.feasibility_message(num_pins, total_slots))

    def _no_results_message(self) -> str:
        """Explains WHY no seed was found, in terms of the feasibility model
        (expected matches shrink fast per pin, and how fast depends on the
        selected house version's total slot count -- smaller versions have
        fewer slots per pin, so tolerate more pins before it gets hopeless),
        and suggests a concrete next step.
        """
        num_pins = sum(1 for c in self._current_constraints() if c.kind == "pin")
        total_slots = search.total_slots_for_config(self.config)
        feas = search.feasibility_message(num_pins, total_slots)
        scanned_everything = self._last_seed_range == search.DEFAULT_FULL_RANGE
        if scanned_everything and num_pins >= 1:
            return (
                f"No seed found. {feas} This search already covered the ENTIRE "
                f"2^32 seed space, so no seed satisfies these exact pins in this "
                f"game version -- remove at least one pin constraint and search again.")
        return (
            f"No seed found in this range. {feas} Try removing a pin constraint, "
            f"widening the range, or (on the GPU backend) the full 32-bit range "
            f"(~12 minutes) to search exhaustively.")

    # -- Backend selection --------------------------------------------------

    def _update_device_info_label(self):
        if self.backend_choice.get() == "gpu" and self.gpu_available:
            self.device_info_var.set(self.gpu_device_info)
        elif self.gpu_available:
            self.device_info_var.set(
                f"CPU backend active ({self.cpu_backend.num_workers} worker processes). "
                f"GPU available: {self.gpu_device_info}")
        else:
            self.device_info_var.set(
                f"GPU unavailable -- using CPU ({self.cpu_backend.num_workers} worker "
                f"processes). {self.gpu_device_info}")

    def _on_backend_change(self):
        choice = self.backend_choice.get()
        if choice == "gpu" and not self.gpu_available:
            messagebox.showwarning(
                "GPU unavailable",
                f"The GPU backend is not available on this system:\n\n{self.gpu_device_info}\n\n"
                "Falling back to CPU.")
            self.backend_choice.set("cpu")
            choice = "cpu"

        if choice == "gpu":
            self.backend = self.gpu_backend_obj
            # Full 2^32 is practical on GPU (~12 min); switch the default up
            # from the CPU-oriented "quick scan" unless the user already
            # picked something else.
            if self.range_choice.get() == "quick":
                self.range_choice.set("full")
        else:
            self.backend = self.cpu_backend
            # Full 2^32 is impractical on CPU; switch back to quick scan.
            if self.range_choice.get() == "full":
                self.range_choice.set("quick")

        self._update_device_info_label()

    # -- Current seed panel ------------------------------------------------

    def _refresh_current_seed(self):
        try:
            seed = seed_registry.read_seed()
            self.current_seed_var.set(str(seed) if seed is not None else "(no fixed seed set)")
        except FileNotFoundError:
            self.current_seed_var.set("(game has not been run yet)")
        except Exception as e:
            self.current_seed_var.set(f"(error: {e})")

    # -- Generate / Cancel -----------------------------------------------

    def _get_seed_range(self) -> tuple[int, int] | None:
        choice = self.range_choice.get()
        if choice == "quick":
            return search.DEFAULT_QUICK_RANGE
        if choice == "full":
            return search.DEFAULT_FULL_RANGE
        try:
            start = int(self.custom_start_var.get())
            end = int(self.custom_end_var.get())
        except ValueError:
            messagebox.showerror("Invalid range", "Custom start/end must be integers.")
            return None
        if end < start:
            messagebox.showerror("Invalid range", "End must be >= start.")
            return None
        return (start, end)

    def _on_generate(self):
        constraints = self._current_constraints()
        num_pins = sum(1 for c in constraints if c.kind == "pin")
        total_slots = search.total_slots_for_config(self.config)
        if search.estimate_expected_matches(num_pins, total_slots) < 1:
            if not messagebox.askyesno(
                "Almost certainly impossible",
                f"{num_pins} pins is essentially impossible to satisfy (see feasibility "
                f"estimate: {search.feasibility_message(num_pins, total_slots)}). Search anyway?"):
                return

        seed_range = self._get_seed_range()
        if seed_range is None:
            return
        try:
            limit = int(self.limit_var.get())
        except ValueError:
            messagebox.showerror("Invalid limit", "Max results must be an integer.")
            return
        # A non-positive limit is falsy downstream, which disables BOTH the
        # per-batch break and the CPU-verification break in the backends --
        # turning a low-selectivity search (e.g. a single pin over the full
        # range) into an effectively unbounded CPU grind with an unresponsive
        # Cancel. Require at least 1.
        if limit < 1:
            messagebox.showerror(
                "Invalid limit",
                "Max results must be at least 1. An unlimited search over "
                "the full seed range can take hours for loosely-constrained "
                "searches.",
            )
            return

        self.cancel_evt.clear()
        self.results_tree.delete(*self.results_tree.get_children())
        self.placement_tree.delete(*self.placement_tree.get_children())
        self.last_results = []
        self.apply_btn.config(state="disabled")
        self.copy_btn.config(state="disabled")
        self.big_seed_var.set("(select a result)")
        self._selected_seed = None

        self.generate_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress["value"] = 0
        self.progress["maximum"] = seed_range[1] - seed_range[0] + 1
        self.progress_label_var.set("Starting search...")
        self._search_start_time = time.time()
        self._last_seed_range = seed_range

        def progress_cb(seeds_done, total, hits_found):
            self.progress_queue.put(("progress", seeds_done, total, hits_found))

        def worker():
            try:
                results = self.backend.search(
                    self.config, constraints, seed_range=seed_range, limit=limit,
                    progress_cb=progress_cb, cancel_evt=self.cancel_evt,
                )
                self.progress_queue.put(("done", results))
            except Exception as e:
                self.progress_queue.put(("error", str(e)))

        self.search_thread = threading.Thread(target=worker, daemon=True)
        self.search_thread.start()

    def _on_cancel(self):
        self.cancel_evt.set()
        self.progress_label_var.set("Cancelling...")

    def _poll_queue(self):
        try:
            while True:
                msg = self.progress_queue.get_nowait()
                if msg[0] == "progress":
                    _, seeds_done, total, hits_found = msg
                    self.progress["maximum"] = total
                    self.progress["value"] = seeds_done
                    pct = (seeds_done / total * 100) if total else 0
                    elapsed = time.time() - self._search_start_time
                    rate = seeds_done / elapsed if elapsed > 0 and seeds_done > 0 else 0
                    if rate > 0:
                        eta = _format_duration((total - seeds_done) / rate)
                        rate_str = f"{rate:,.0f} seeds/sec"
                    else:
                        eta = "estimating..."
                        rate_str = "measuring..."
                    self.progress_label_var.set(
                        f"Scanned {seeds_done:,} / {total:,} ({pct:.1f}%) -- {rate_str} -- "
                        f"ETA {eta} -- {hits_found} hit(s) found")
                elif msg[0] == "done":
                    results = msg[1]
                    self._on_search_done(results)
                elif msg[0] == "error":
                    self.generate_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    messagebox.showerror("Search error", msg[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _on_search_done(self, results: list[search.SeedResult]):
        self.generate_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.last_results = results
        if not results:
            self.progress_label_var.set(self._no_results_message())
        else:
            self.progress_label_var.set(f"Done. {len(results)} hit(s) found.")
        for r in results:
            self.results_tree.insert("", "end", iid=str(r.seed),
                                     values=(r.seed, r.pins_matched, r.prefs_matched))

    def _on_result_selected(self, event=None):
        sel = self.results_tree.selection()
        self.placement_tree.delete(*self.placement_tree.get_children())
        if not sel:
            self._clear_selected_seed()
            return
        seed = int(sel[0])
        result = next((r for r in self.last_results if r.seed == seed), None)
        if result is None:
            self._clear_selected_seed()
            return
        for item_name, slot in sorted(result.placement.items()):
            self.placement_tree.insert("", "end", values=(item_name, slot))
        self.apply_btn.config(state="normal")
        self.copy_btn.config(state="normal")
        self.copy_status_var.set("")
        self.big_seed_var.set(str(seed))
        self._selected_seed = seed

    def _clear_selected_seed(self):
        self.apply_btn.config(state="disabled")
        self.copy_btn.config(state="disabled")
        self.big_seed_var.set("(select a result)")
        self._selected_seed = None

    # -- Apply to game -----------------------------------------------------

    def _copy_seed_to_clipboard(self):
        if self._selected_seed is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(str(self._selected_seed))
        self.copy_status_var.set("Copied!")
        self.root.after(2000, lambda: self.copy_status_var.set(""))

    def _on_apply_to_game(self):
        seed = self._selected_seed
        if seed is None:
            return

        warning = (
            f"You are about to write seed {seed} to the Windows registry.\n\n"
            "This method is KNOWN TO BE UNRELIABLE -- in testing, writing the "
            "registry value alone did NOT reliably reach the game. The recommended "
            "way to apply a seed is the game's own Seed menu (see the section "
            "above). Only use this if you understand it may silently not work.\n\n"
            "IMPORTANT: The game must be CLOSED first. Unity rewrites all "
            "PlayerPrefs from memory on exit, which would silently overwrite "
            "this change if the game is still running.\n\n"
            "A backup of your current PlayerPrefs will be made automatically "
            "before the first write this session.\n\n"
            "Proceed anyway?"
        )
        if not messagebox.askyesno("Confirm: write seed to registry (experimental)",
                                    warning, icon="warning"):
            return

        try:
            seed_registry.write_seed(seed)
        except Exception as e:
            messagebox.showerror("Failed to write seed", str(e))
            return

        messagebox.showinfo(
            "Seed written",
            f"Seed {seed} written to the registry.\n\n"
            "This may or may not take effect -- if the game does not pick it up, "
            "use the in-game Seed menu instead (type the seed, press Enter).")
        self._refresh_current_seed()

    # -- Save / Load constraints --------------------------------------------

    def _save_constraints(self):
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if not path:
            return
        data = []
        for row in self.rows.values():
            k = row.kind()
            if k is not None and row.slot_label:
                data.append({"item_name": row.item_name, "kind": k, "slot_label": row.slot_label})
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        messagebox.showinfo("Saved", f"Constraints saved to {path}")

    def _load_constraints(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Failed to load", str(e))
            return
        for row in self.rows.values():
            row.set_state(None, None)
        for entry in data:
            row = self.rows.get(entry.get("item_name"))
            if row is not None:
                row.set_state(entry.get("kind"), entry.get("slot_label"))
        self._update_feasibility()
        messagebox.showinfo("Loaded", f"Constraints loaded from {path}")


def main():
    root = tk.Tk()
    app = SeedPredictorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
