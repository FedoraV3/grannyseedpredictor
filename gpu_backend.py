"""
gpu_backend.py

OpenCL GPU-accelerated seed-search backend for the Granny Legacy seed
predictor. AMD-first (tested on RX 7700 XT / gfx1101) but vendor-neutral --
any OpenCL 1.2+ device exposing `cl_khr_fp64` will work.

Design summary (see re_gpu.md for the full writeup)
-----------------------------------------------------
`kernel.cl` ports simulator.py's `GeneratePlacement` pipeline for the FULL
retry loop (attempts 1..50, `NetRandom(seed + attempt)` re-seeded every
attempt, exactly mirroring `simulate_verbose`'s own loop) -- not just
attempt 1. Each attempt's acceptance (`DetectCircularDependencies` /
`ValidatePuzzleDependencies`) is decided via the same analytically-proven
criterion `search.py` uses (see `re_search.md` S4.2 and `kernel.cl`'s module
docstring): puzzle placements only ever happen in Steps A-D, and the
acceptance predicates only ever depend on whether some "container" item
(non-empty `containedItems`) ended up at a puzzle spawn. So:

  1. If no container is at a puzzle spawn right after Step D, acceptance is
     analytically GUARANTEED true for the rest of the attempt (no matter
     what Steps E/F do) -- the real graph check is skipped entirely, and
     PIN early-abort is safe to enable for the remainder of the attempt.
  2. Otherwise, the real check is computed for real, on-device
     (`compute_real_acceptance` in `kernel.cl`) -- a bitmask/array port of
     `validate_puzzle_dependencies`'s fixed-point "obtainable items"
     propagation and `detect_circular_dependencies`'s white/gray/black DFS.
  3. If an attempt is not accepted, the kernel moves on to
     `NetRandom(seed + attempt + 1)` -- exactly like the real game -- so the
     kernel's own final placement for a seed is always the one
     `simulator.simulate()` would independently produce; there is no
     "attempt-1-only" approximation left.

Every GPU-reported candidate seed is still re-verified on the CPU by calling
`simulator.simulate()` -- the same 31/31-validated, fully faithful reference
implementation used everywhere else in this project -- before being
returned. This is cheap insurance (candidates are rare once a search has
more than 1-2 pins) and guarantees zero false positives even if a residual
edge case (see `kernel.cl`'s docstring on containers-that-fail-to-be-placed)
were ever to matter on a future scene config.

The kernel's own bitmask/predicate tables are built by calling simulator.py's
*own* validated predicate functions directly (`is_item_allowed_for_puzzle`,
`category_ok`, `is_safe_container_placement`, `name_in`) rather than
re-deriving name-matching logic independently on the host -- eliminating an
entire class of host-side porting bugs.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np

try:
    import pyopencl as cl
    _PYOPENCL_IMPORT_ERROR: Optional[str] = None
except Exception as _e:  # pragma: no cover - environment dependent
    cl = None
    _PYOPENCL_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

import simulator
from simulator import (
    _norm_name, name_in,
    is_item_allowed_for_puzzle, category_ok, is_safe_container_placement,
)

# ---------------------------------------------------------------------------
# Public data model -- prefer search.py's own classes so instances returned
# by this backend are interchangeable with CpuSearchBackend's; fall back to
# local equivalents if search.py can't be imported (it's being edited
# concurrently by another agent -- see task instructions).
# ---------------------------------------------------------------------------
try:
    from search import Constraint, SeedResult  # type: ignore
except Exception:
    @dataclass
    class Constraint:  # type: ignore[no-redef]
        item_name: str
        slot_label: str
        kind: str  # "pin" | "prefer"

    @dataclass
    class SeedResult:  # type: ignore[no-redef]
        seed: int
        pins_matched: int
        prefs_matched: int
        placement: dict


ProgressCb = Optional[Callable[[int, int, int], None]]

_KERNEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kernel.cl")

# Hard limits baked into kernel.cl's bitmask widths (see its module docstring).
# Item-indexed masks (category (A) -- see kernel.cl's ITEM_MASK_BITS) widen
# from 32-bit to 64-bit automatically once a scene needs more than 32 items;
# puzzle-indexed and area-spot masks (categories (B)/(C)) always stay 32-bit.
MAX_ITEMS = 64
MAX_PUZZLES = 32
MAX_AREA_SPOTS = 32  # per-area free-spot bitmask width


# ---------------------------------------------------------------------------
# SceneLayout -- builds every seed-independent flat array / bitmask kernel.cl
# needs, from the dict returned by simulator.load_config(). Every predicate
# bitmask is computed by calling simulator.py's own validated predicate
# functions -- never reimplemented here.
# ---------------------------------------------------------------------------
class SceneLayout:
    def __init__(self, config: dict):
        self.config = config
        items = config["items"]
        puzzle_defs = config["puzzleDefs"]
        areas = config["spawnAreas"]

        valid_items = [i for i in items if i.valid]
        if len(valid_items) > MAX_ITEMS:
            raise ValueError(
                f"GPU backend supports at most {MAX_ITEMS} items (64-bit item "
                f"bitmasks in kernel.cl); this config has {len(valid_items)}."
            )
        self.valid_items = valid_items
        self.num_items = len(valid_items)
        self.item_index = {_norm_name(it.itemName): idx for idx, it in enumerate(valid_items)}
        self.item_name_by_idx = [it.itemName for it in valid_items]

        # Item-indexed ("category (A)") masks widen to 64-bit only when this
        # scene actually needs more than 32 items -- the default 32-bit path
        # is otherwise unchanged (see kernel.cl's ITEM_MASK_BITS). Every
        # category-(A) numpy array/scalar in this class uses this dtype;
        # category-(B)/(C) arrays (area-spot / puzzle-indexed masks) always
        # stay np.uint32 regardless.
        self.item_mask_bits = 64 if self.num_items > 32 else 32
        self.imask_dtype = np.uint64 if self.item_mask_bits == 64 else np.uint32

        puzzles_with_spawn = [p for p in puzzle_defs if p.spawnPointId is not None]
        if len(puzzles_with_spawn) > MAX_PUZZLES:
            raise ValueError(
                f"GPU backend supports at most {MAX_PUZZLES} puzzles (32-bit "
                f"puzzle bitmasks in kernel.cl); this config has "
                f"{len(puzzles_with_spawn)}."
            )
        self.puzzles = puzzles_with_spawn
        self.num_puzzles = len(puzzles_with_spawn)
        # Puzzles without a spawnPoint are unreachable by any code path in
        # simulator.py's pipeline (excluded from ordered_puzzles, from Step
        # A/C/D, and from Step B's `eligible` filter) -- they are simply
        # omitted from this index space entirely, matching that behavior.

        if self.num_puzzles == 0:
            raise ValueError("GPU backend requires at least one puzzle with a spawn point.")

        self.num_areas = len(areas)
        if self.num_areas == 0:
            raise ValueError("GPU backend requires at least one spawn area.")
        for a in areas:
            if len(a.freeSpots) > MAX_AREA_SPOTS:
                raise ValueError(
                    f"GPU backend supports at most {MAX_AREA_SPOTS} spots per "
                    f"area (32-bit per-area spot bitmask in kernel.cl); area "
                    f"'{a.areaName}' has {len(a.freeSpots)}."
                )

        # --- Slot id table: 0..num_puzzles-1 = PUZZLE slots (in puzzle
        # index order above); num_puzzles..num_puzzles+total_free-1 = FREE
        # slots, concatenated per area in area order, per spot in that
        # area's freeSpots order (must match kernel.cl's slot-id encoding).
        self.slot_labels: list[str] = [f"PUZZLE:{p.puzzleName}" for p in puzzles_with_spawn]
        self.area_num_spots = np.zeros(self.num_areas, dtype=np.int32)
        self.area_spot_offset = np.zeros(self.num_areas, dtype=np.int32)
        offset = 0
        for ai, a in enumerate(areas):
            self.area_num_spots[ai] = len(a.freeSpots)
            self.area_spot_offset[ai] = offset
            for spot in a.freeSpots:
                self.slot_labels.append(f"FREE:{a.areaName}/{spot.name}")
            offset += len(a.freeSpots)
        self.total_free_spots = offset
        self.total_slots = self.num_puzzles + self.total_free_spots
        self.slot_label_to_id = {label: idx for idx, label in enumerate(self.slot_labels)}

        # --- category_mask: bit i set iff item i's category != 4 ---
        category_mask = 0
        for idx, it in enumerate(valid_items):
            if category_ok(it):
                category_mask |= (1 << idx)
        self.category_mask = self.imask_dtype(category_mask)

        # --- per-puzzle item bitmasks ---
        puzzle_allow_mask = np.zeros(self.num_puzzles, dtype=self.imask_dtype)
        puzzle_tier1_mask = np.zeros(self.num_puzzles, dtype=self.imask_dtype)
        puzzle_candidate_mask = np.zeros(self.num_puzzles, dtype=self.imask_dtype)
        for pi, p in enumerate(puzzles_with_spawn):
            allow = 0
            tier1 = 0
            cand = 0
            for idx, it in enumerate(valid_items):
                a_ok = is_item_allowed_for_puzzle(it, p)
                if a_ok:
                    allow |= (1 << idx)
                c_ok = a_ok and category_ok(it)
                if c_ok:
                    tier1 |= (1 << idx)
                    if is_safe_container_placement(it, p, puzzle_defs):
                        cand |= (1 << idx)
            puzzle_allow_mask[pi] = allow
            puzzle_tier1_mask[pi] = tier1
            puzzle_candidate_mask[pi] = cand
        self.puzzle_allow_mask = puzzle_allow_mask
        self.puzzle_tier1_mask = puzzle_tier1_mask
        self.puzzle_candidate_mask = puzzle_candidate_mask

        # --- puzzle priority order (stable sort, matches simulator.py's
        # `ordered_puzzles = sorted(..., key=lambda p: p.priority)`) ---
        order = sorted(range(self.num_puzzles), key=lambda i: puzzles_with_spawn[i].priority)
        self.puzzle_order = np.array(order, dtype=np.int32)

        # --- required escape items, resolved to item indices (unresolved
        # names are silently skipped, matching simulate_verbose Step B) ---
        escape_idx = []
        for name in config["requiredEscapeItemNames"]:
            idx = self.item_index.get(_norm_name(name))
            if idx is not None:
                escape_idx.append(idx)
        self.num_escape = len(escape_idx)
        self.escape_item_idx = np.array(escape_idx if escape_idx else [0], dtype=np.int32)
        self.escape_chance = np.float64(config["escapeItemPuzzleChance"])
        self.fill_all = np.int32(1 if config["fillAllFreeSpawns"] else 0)

        # --- per-area item-eligibility masks (Step B/F's PlaceFreeArea
        # filter) and mandatory-item index (Step E) ---
        area_allow_all = np.zeros(self.num_areas, dtype=np.int32)
        area_allow_mask = np.zeros(self.num_areas, dtype=self.imask_dtype)
        area_max_items = np.zeros(self.num_areas, dtype=np.int32)
        area_mandatory_idx = np.full(self.num_areas, -1, dtype=np.int32)
        for ai, a in enumerate(areas):
            area_max_items[ai] = a.maxItems
            if not a.allowedItemNames:
                area_allow_all[ai] = 1
            else:
                m = 0
                for idx, it in enumerate(valid_items):
                    if name_in(it.itemName, a.allowedItemNames):
                        m |= (1 << idx)
                area_allow_mask[ai] = m
            if a.mandatoryItemName:
                mi = self.item_index.get(_norm_name(a.mandatoryItemName))
                if mi is not None:
                    area_mandatory_idx[ai] = mi
        self.area_allow_all = area_allow_all
        self.area_allow_mask = area_allow_mask
        self.area_max_items = area_max_items
        self.area_mandatory_item_idx = area_mandatory_idx

        # --- Acceptance-test data (the correctness fix this module ports):
        # "container" items (non-empty containedItems) are the ONLY items
        # GetEffectivePuzzleOfItem can ever resolve to a puzzle for -- see
        # re_search.md S4.2 and kernel.cl's module docstring for the full
        # proof this is built on. ---
        containers = [it for it in valid_items if it.containedItems]
        container_item_idx = []
        container_contains_mask = []
        for it in containers:
            idx = self.item_index[_norm_name(it.itemName)]
            container_item_idx.append(idx)
            mask = 0
            for n in it.containedItems:
                cidx = self.item_index.get(_norm_name(n))
                if cidx is not None:
                    mask |= (1 << cidx)
            container_contains_mask.append(mask)
        self.num_containers = len(containers)
        self.container_item_idx = np.array(
            container_item_idx if container_item_idx else [0], dtype=np.int32)
        self.container_contains_mask = np.array(
            container_contains_mask if container_contains_mask else [0], dtype=self.imask_dtype)

        # --- Per-puzzle requiredItemNames data, restricted to puzzles WITH a
        # spawn point (the only ones that can ever be an effective-puzzle
        # target, or take part in a dependency cycle -- see kernel.cl's
        # docstring for why puzzles without a spawn point can safely be
        # excluded from this graph entirely: they can never receive an
        # incoming Item->Puzzle edge, so they can neither gate nor
        # participate in a cycle). `puzzle_active[p]` mirrors
        # validate_puzzle_dependencies's documented quirk: a puzzle whose
        # requiredItemNames is empty, OR names an item that doesn't exist in
        # this scene at all, can NEVER be added to `processed_puzzles` (the
        # `all(...)` check over its requirement names can never become
        # True) -- ported exactly, not "fixed". ---
        puzzle_requires_mask = np.zeros(self.num_puzzles, dtype=self.imask_dtype)
        puzzle_active = np.zeros(self.num_puzzles, dtype=np.int32)
        for pi, p in enumerate(puzzles_with_spawn):
            names = p.requiredItemNames
            if not names:
                continue  # puzzle_active[pi] stays 0 -- matches the quirk
            mask = 0
            has_named_entry = False
            always_satisfiable = True
            for n in names:
                if not n:
                    continue
                has_named_entry = True
                idx = self.item_index.get(_norm_name(n))
                if idx is None:
                    always_satisfiable = False  # can never become obtainable
                else:
                    mask |= (1 << idx)
            puzzle_requires_mask[pi] = mask
            if not has_named_entry:
                # requiredItemNames non-empty but every entry falsy -> the
                # Python `all(... for n in names if n)` is vacuously True.
                puzzle_active[pi] = 1
            elif always_satisfiable:
                puzzle_active[pi] = 1
            # else: some named requirement can never resolve to a real item
            # -> this puzzle can never be processed; leave puzzle_active=0.
        self.puzzle_requires_mask = puzzle_requires_mask
        self.puzzle_active = puzzle_active

        # --- required_item_mask: every item name named by ANY puzzle's
        # requiredItemNames (not just puzzles-with-spawn -- a puzzle without
        # a spawn point can still name a container's contents in its own
        # requiredItemNames, and validate_puzzle_dependencies's final check
        # iterates ALL puzzle_defs for this, see kernel.cl's docstring) plus
        # every required-escape item. Names that don't resolve to a real
        # item are correctly omitted: resolve_effective_puzzle(n) is always
        # None for a name that isn't a valid item, so such names can never
        # trigger validate_puzzle_dependencies's failure branch regardless
        # of "obtainable" status. ---
        required_item_mask = 0
        for p in puzzle_defs:
            if p is not None and p.requiredItemNames:
                for n in p.requiredItemNames:
                    if n:
                        idx = self.item_index.get(_norm_name(n))
                        if idx is not None:
                            required_item_mask |= (1 << idx)
        for idx in escape_idx:
            required_item_mask |= (1 << idx)
        self.required_item_mask = self.imask_dtype(required_item_mask)

        # OpenCL state, filled in by GpuSearchBackend once a context exists.
        self.program = None
        self._buffers: dict[str, "cl.Buffer"] = {}

    # -----------------------------------------------------------------
    def build_pin_arrays(self, constraints: list) -> tuple[int, np.ndarray, bool, int]:
        """Returns (pin_mask, pin_target[num_items], unresolvable, total_pins).

        `unresolvable=True` means at least one PIN constraint names an item
        or slot label that does not exist in this scene -- such a PIN can
        never be satisfied by any seed, so the caller should short-circuit
        to an empty result set (correct: zero false positives, and no
        seed could ever have matched anyway, so no false negative either).
        """
        pins = [c for c in constraints if c.kind == "pin"]
        pin_target = np.full(self.num_items, -1, dtype=np.int32)
        pin_mask = 0
        unresolvable = False
        for c in pins:
            idx = self.item_index.get(_norm_name(c.item_name))
            slot_id = self.slot_label_to_id.get(c.slot_label)
            if idx is None or slot_id is None:
                unresolvable = True
                continue
            pin_mask |= (1 << idx)
            pin_target[idx] = slot_id
        return pin_mask, pin_target, unresolvable, len(pins)

    def build_options(self) -> list[str]:
        return [
            f"-D NUM_ITEMS={self.num_items}",
            f"-D NUM_PUZZLES={self.num_puzzles}",
            f"-D NUM_AREAS={self.num_areas}",
            f"-D NUM_ESCAPE={max(1, self.num_escape)}",
            f"-D ITEM_MASK_BITS={self.item_mask_bits}",
        ]


# ---------------------------------------------------------------------------
# GpuSearchBackend
# ---------------------------------------------------------------------------
class GpuSearchBackend:
    """OpenCL GPU search backend. Implements the `SearchBackend` protocol
    (see search.py) as a drop-in accelerator for `CpuSearchBackend`.
    """

    # Target wall-clock time per kernel dispatch. Windows TDR resets the
    # driver on kernels running too long (default timeout is commonly ~2s);
    # we stay far under that. See re_gpu.md for what was actually measured.
    _TARGET_DISPATCH_SECONDS = 0.15
    _MIN_BATCH = 1 << 14
    _MAX_BATCH = 1 << 23

    def __init__(self, config: dict, device_preference: str = "gpu",
                 local_work_size: Optional[int] = 128):
        self._unavailable_reason: Optional[str] = None
        self.ctx = None
        self.queue = None
        self.device = None
        # pyopencl wants local_work_size as a tuple (or None to let the
        # driver choose); accept a plain int for convenience.
        self.local_work_size = (local_work_size,) if isinstance(local_work_size, int) else local_work_size
        # Keyed by id(config) -- see _get_layout for why each entry also
        # retains a strong reference to the config dict itself.
        self._layout_cache: dict[int, tuple[dict, SceneLayout]] = {}

        if cl is None:
            self._unavailable_reason = f"pyopencl not importable ({_PYOPENCL_IMPORT_ERROR})"
            return

        try:
            device = self._pick_device(device_preference)
        except Exception as e:
            self._unavailable_reason = str(e)
            return

        if device is None:
            self._unavailable_reason = (
                f"no OpenCL device matching preference={device_preference!r} with "
                f"cl_khr_fp64 support was found"
            )
            return

        self.device = device
        try:
            self.ctx = cl.Context([device])
            self.queue = cl.CommandQueue(self.ctx)
        except Exception as e:
            self._unavailable_reason = f"failed to create OpenCL context/queue: {e}"
            self.ctx = None
            self.queue = None
            return

        # Warm the layout/program cache for the initial config so the first
        # real search() call doesn't pay compile latency.
        try:
            self._get_layout(config)
        except Exception as e:
            self._unavailable_reason = f"failed to build initial scene layout: {e}"

    # -----------------------------------------------------------------
    @staticmethod
    def _pick_device(preference: str):
        if cl is None:
            return None
        wanted_types = {
            "gpu": [cl.device_type.GPU],
            "cpu": [cl.device_type.CPU],
            "any": [cl.device_type.GPU, cl.device_type.CPU, cl.device_type.ACCELERATOR],
        }.get(preference, [cl.device_type.GPU])

        candidates = []
        any_devices_of_type = []
        for plat in cl.get_platforms():
            try:
                devices = plat.get_devices()
            except Exception:
                continue
            for d in devices:
                if d.type in wanted_types or any(d.type & t for t in wanted_types):
                    any_devices_of_type.append(d)
                    if "cl_khr_fp64" in d.extensions:
                        candidates.append(d)

        if candidates:
            # Prefer AMD first (task is AMD-first), else first match.
            for d in candidates:
                if "advanced micro devices" in d.vendor.lower() or "amd" in d.vendor.lower():
                    return d
            return candidates[0]

        if any_devices_of_type:
            names = ", ".join(f"{d.name} ({d.vendor})" for d in any_devices_of_type)
            raise RuntimeError(
                f"found device(s) matching preference={preference!r} but none support "
                f"cl_khr_fp64 (required for bit-exact double-precision RNG -- refusing "
                f"to silently degrade to float32): {names}"
            )
        return None

    # -----------------------------------------------------------------
    def available(self) -> bool:
        return self.ctx is not None and self._unavailable_reason is None

    def device_info(self) -> str:
        if not self.available():
            return f"GPU backend unavailable: {self._unavailable_reason}"
        d = self.device
        return (
            f"{d.name} ({d.vendor}) -- {d.max_compute_units} CUs, "
            f"OpenCL C {d.opencl_c_version}, "
            f"{d.global_mem_size / (1024**3):.1f} GB global, "
            f"{d.local_mem_size // 1024} KB local, fp64=yes"
        )

    # -----------------------------------------------------------------
    def _get_layout(self, config: dict) -> SceneLayout:
        # NOTE: id() is only unique among *live* objects. The house-version
        # selector hands us a brand-new config dict per variant and drops the
        # previous one, so without the strong reference stored alongside the
        # layout below, a garbage-collected config's id could be recycled by a
        # later config of a DIFFERENT variant -- silently reusing e.g. a
        # 31-item layout for a 6-item one. Retaining the config in the cache
        # entry pins the id for as long as the entry lives, which makes the
        # key unique by construction. (At most ~10 variants are ever cached.)
        key = id(config)
        cached = self._layout_cache.get(key)
        if cached is not None:
            cached_config, layout = cached
            if cached_config is config:
                return layout

        layout = SceneLayout(config)
        src = open(_KERNEL_PATH, "r", encoding="utf-8").read()
        program = cl.Program(self.ctx, src).build(options=layout.build_options())
        layout.program = program
        # Retrieve each kernel exactly once and reuse the Kernel object --
        # re-fetching `program.kernel_name` on every dispatch creates a new
        # independent kernel object each time (wasteful, and pyopencl warns).
        layout.kernel_debug = cl.Kernel(program, "debug_placement")
        layout.kernel_search = cl.Kernel(program, "search_kernel")

        mf = cl.mem_flags

        def const_buf(arr, dtype):
            a = np.asarray(arr, dtype=dtype)
            if a.size == 0:
                a = np.zeros(1, dtype=dtype)
            return cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=a)

        layout._buffers = {
            "puzzle_allow_mask": const_buf(layout.puzzle_allow_mask, layout.imask_dtype),
            "puzzle_tier1_mask": const_buf(layout.puzzle_tier1_mask, layout.imask_dtype),
            "puzzle_candidate_mask": const_buf(layout.puzzle_candidate_mask, layout.imask_dtype),
            "puzzle_order": const_buf(layout.puzzle_order, np.int32),
            "escape_item_idx": const_buf(layout.escape_item_idx, np.int32),
            "area_allow_all": const_buf(layout.area_allow_all, np.int32),
            "area_allow_mask": const_buf(layout.area_allow_mask, layout.imask_dtype),
            "area_max_items": const_buf(layout.area_max_items, np.int32),
            "area_num_spots": const_buf(layout.area_num_spots, np.int32),
            "area_spot_offset": const_buf(layout.area_spot_offset, np.int32),
            "area_mandatory_item_idx": const_buf(layout.area_mandatory_item_idx, np.int32),
            "container_item_idx": const_buf(layout.container_item_idx, np.int32),
            "container_contains_mask": const_buf(layout.container_contains_mask, layout.imask_dtype),
            "puzzle_requires_mask": const_buf(layout.puzzle_requires_mask, layout.imask_dtype),
            "puzzle_active": const_buf(layout.puzzle_active, np.int32),
        }
        self._layout_cache[key] = (config, layout)
        return layout

    # -----------------------------------------------------------------
    def _static_kernel_args(self, layout: SceneLayout) -> list:
        b = layout._buffers
        return [
            layout.category_mask,
            b["puzzle_allow_mask"], b["puzzle_tier1_mask"], b["puzzle_candidate_mask"],
            b["puzzle_order"], b["escape_item_idx"], layout.escape_chance,
            b["area_allow_all"], b["area_allow_mask"], b["area_max_items"],
            b["area_num_spots"], b["area_spot_offset"], b["area_mandatory_item_idx"],
            layout.fill_all,
            b["container_item_idx"], b["container_contains_mask"], np.int32(layout.num_containers),
            b["puzzle_requires_mask"], b["puzzle_active"], layout.required_item_mask,
        ]

    # -----------------------------------------------------------------
    def debug_placements(self, config: dict, seeds: list[int]) -> dict[int, dict]:
        """Runs the kernel's `debug_placement` entry point (no PIN
        filtering) for an explicit list of seeds and decodes each into an
        {itemName: slot_label} dict -- the kernel's own full retry-loop
        placement, which should be bit-exact against `simulator.simulate()`.
        Used by test_gpu.py's bit-exactness harness; not used by `search()`.
        """
        if not self.available():
            raise RuntimeError(f"GPU backend not available: {self._unavailable_reason}")
        layout = self._get_layout(config)
        n = len(seeds)
        if n == 0:
            return {}

        mf = cl.mem_flags
        dummy_pin_mask = layout.imask_dtype(0)
        dummy_pin_target = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                                      hostbuf=np.full(layout.num_items, -1, dtype=np.int32))
        out_buf = cl.Buffer(self.ctx, mf.WRITE_ONLY, size=n * layout.num_items * 4)

        results: dict[int, dict] = {}
        # Seeds need not be contiguous; run one dispatch per contiguous run
        # for efficiency, but the simple/robust approach (one dispatch per
        # seed) is plenty fast for the sample sizes test_gpu.py uses and
        # keeps the seed_start/seed_count contract simple. We batch by
        # dispatching each seed individually via seed_start=seed,
        # seed_count=1 -- correctness over cleverness here.
        out_host = np.empty(layout.num_items, dtype=np.int32)
        for seed in seeds:
            layout.kernel_debug(
                self.queue, (1,), None,
                np.int64(seed), np.uint32(1),
                *self._static_kernel_args(layout),
                dummy_pin_mask, dummy_pin_target,
                out_buf,
            )
            cl.enqueue_copy(self.queue, out_host, out_buf)
            placement = {}
            for idx, slot in enumerate(out_host):
                if slot >= 0:
                    placement[layout.item_name_by_idx[idx]] = layout.slot_labels[int(slot)]
            results[seed] = placement
        self.queue.finish()
        return results

    # -----------------------------------------------------------------
    def search(
        self,
        config: dict,
        constraints: list,
        seed_range: tuple[int, int] = (-2147483648, 2147483647),
        limit: int = 200,
        progress_cb: ProgressCb = None,
        cancel_evt: Any = None,
    ) -> list:
        if not self.available():
            raise RuntimeError(f"GPU backend not available: {self._unavailable_reason}")

        layout = self._get_layout(config)
        start, end = seed_range
        if end < start:
            raise ValueError(f"Invalid seed_range: {seed_range}")
        total = end - start + 1

        pin_mask, pin_target, unresolvable, total_pins = layout.build_pin_arrays(constraints)
        if unresolvable:
            # A PIN names an item/slot that doesn't exist in this scene --
            # it can never be satisfied by any seed. Correctly return no
            # results instead of scanning for nothing.
            if progress_cb is not None:
                progress_cb(total, total, 0)
            return []

        mf = cl.mem_flags
        pin_target_buf = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=pin_target)

        max_hits_buffer = max(4096, (limit or 0) * 8, 4096)
        hit_seeds_buf = cl.Buffer(self.ctx, mf.WRITE_ONLY, size=max_hits_buffer * 4)
        hit_count_buf = cl.Buffer(self.ctx, mf.READ_WRITE, size=4)

        seeds_done = 0
        all_hits: list = []
        seed_ptr = start
        batch = 1 << 18  # conservative first dispatch; adapted after measuring

        static_args = self._static_kernel_args(layout)

        while seed_ptr <= end:
            if cancel_evt is not None and cancel_evt.is_set():
                break

            this_batch = min(batch, end - seed_ptr + 1)

            # OpenCL requires global_size to be a multiple of local_size on
            # some (esp. pre-2.0 / non-uniform-work-group-less) devices; pad
            # up and let the kernel's own `if (gid >= seed_count) return;`
            # bounds check discard the extra work items.
            if self.local_work_size is not None:
                lws = self.local_work_size[0]
                global_size = ((this_batch + lws - 1) // lws) * lws
            else:
                global_size = this_batch

            cl.enqueue_fill_buffer(self.queue, hit_count_buf, np.uint32(0), 0, 4)

            t0 = time.perf_counter()
            layout.kernel_search(
                self.queue, (global_size,), self.local_work_size,
                np.int64(seed_ptr), np.uint32(this_batch),
                *static_args,
                layout.imask_dtype(pin_mask), pin_target_buf,
                hit_seeds_buf, np.uint32(max_hits_buffer), hit_count_buf,
            )
            self.queue.finish()
            dt = time.perf_counter() - t0

            hit_count_host = np.empty(1, dtype=np.uint32)
            cl.enqueue_copy(self.queue, hit_count_host, hit_count_buf)
            n_hits = int(hit_count_host[0])

            if n_hits > max_hits_buffer:
                # Buffer-full signal: some hits were dropped by the kernel
                # this dispatch. Grow the buffer and REDO this exact batch
                # (seed_ptr/this_batch unchanged) so nothing is silently
                # lost.
                max_hits_buffer = n_hits * 2
                hit_seeds_buf = cl.Buffer(self.ctx, mf.WRITE_ONLY, size=max_hits_buffer * 4)
                continue

            cancelled_during_verify = False
            if n_hits > 0:
                hit_seeds_host = np.empty(n_hits, dtype=np.int32)
                cl.enqueue_copy(self.queue, hit_seeds_host, hit_seeds_buf)
                # CPU re-verification is ~164 seeds/sec, i.e. ~40,000x slower
                # than the GPU dispatch that produced these candidates. With a
                # low-selectivity constraint set (e.g. a single PIN, ~1/71 hit
                # rate) a large batch yields hundreds of thousands of
                # candidates, so this loop -- not the GPU -- is the bottleneck.
                # It therefore MUST honour `limit` and `cancel_evt` internally
                # and report progress, or the UI appears frozen with the GPU
                # idle and Cancel unable to take effect.
                for idx, s in enumerate(hit_seeds_host):
                    if limit and len(all_hits) >= limit:
                        break
                    if (idx & 0xF) == 0 and cancel_evt is not None and cancel_evt.is_set():
                        cancelled_during_verify = True
                        break
                    r = self._cpu_verify(int(s), config, constraints, total_pins)
                    if r is not None:
                        all_hits.append(r)
                    if progress_cb is not None and (idx & 0x3F) == 0:
                        progress_cb(seeds_done, total, len(all_hits))

            seeds_done += this_batch
            seed_ptr += this_batch

            # Adapt batch size toward the target wall-clock time per batch.
            # NOTE: this deliberately uses TOTAL elapsed time (GPU dispatch +
            # CPU verification), not `dt` (dispatch only). Using dispatch time
            # alone grows the batch without bound whenever verification is the
            # real cost, which is exactly the low-selectivity case that made
            # the UI hang. Total time is also strictly >= dispatch time, so
            # this remains at least as safe for Windows TDR as before.
            dt_total = time.perf_counter() - t0
            if dt_total > 0:
                factor = self._TARGET_DISPATCH_SECONDS / dt_total
                batch = int(max(self._MIN_BATCH, min(self._MAX_BATCH, batch * factor)))

            if progress_cb is not None:
                progress_cb(seeds_done, total, len(all_hits))

            if cancelled_during_verify:
                break

            if limit and len(all_hits) >= limit:
                break

        all_hits.sort(key=lambda r: (-r.pins_matched, -r.prefs_matched))
        return all_hits[:limit] if limit else all_hits

    # -----------------------------------------------------------------
    @staticmethod
    def _cpu_verify(seed: int, config: dict, constraints: list, total_pins: int):
        """Authoritative re-check of a GPU-reported candidate: calls
        simulator.py's fully faithful, retry-aware `simulate()` (the same
        31/31-validated reference used everywhere else in this project) and
        only accepts the seed if EVERY pin still matches. This is the sole
        source of truth for what `search()` returns -- the GPU is only ever
        a filter, never the final word.
        """
        placement = simulator.simulate(seed, config)
        pins_matched = sum(
            1 for c in constraints if c.kind == "pin" and placement.get(c.item_name) == c.slot_label
        )
        if pins_matched < total_pins:
            return None
        prefs_matched = sum(
            1 for c in constraints if c.kind == "prefer" and placement.get(c.item_name) == c.slot_label
        )
        return SeedResult(seed=seed, pins_matched=pins_matched, prefs_matched=prefs_matched,
                           placement=dict(placement))


if __name__ == "__main__":
    import glob

    dumps_dir = "dumps"
    cfg_path = sorted(glob.glob(f"{dumps_dir}/config_*.json"))[-1]
    print(f"Using config: {cfg_path}")
    cfg = simulator.load_config(cfg_path)

    backend = GpuSearchBackend(cfg)
    print("available:", backend.available())
    print("device_info:", backend.device_info())

    if backend.available():
        constraints = [Constraint(item_name="Pliers", slot_label="FREE:OldHouse/Spot (82)", kind="pin")]
        t0 = time.time()
        results = backend.search(cfg, constraints, seed_range=(915074000, 915076000), limit=50)
        dt = time.time() - t0
        found = [r for r in results if r.seed == 915074960]
        print(f"scanned 2001 seeds in {dt:.3f}s")
        print(f"seed 915074960 found: {bool(found)}")
        if found:
            print(f"  Pliers -> {found[0].placement.get('Pliers')}")
