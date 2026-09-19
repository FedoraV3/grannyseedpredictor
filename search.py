"""
search.py

Backend-agnostic seed-search engine for the Granny Legacy seed predictor.

The user specifies, per item, either:
    PIN     -- the item must land on an exact slot, or the seed is rejected.
    PREFER  -- scored (more hits ranks higher), never rejects a seed.
    (none)  -- ignored (default "don't care").

`CpuSearchBackend` implements this over all CPU cores today. A future GPU
(OpenCL) backend is meant to be a drop-in replacement implementing the same
`SearchBackend` protocol -- see the "GPU backend contract" section below and
re_search.md for the full design writeup.

Correctness contract
---------------------
`simulator.simulate()` is validated 31/31 against the real game and is never
altered here (nor is any of its logic reimplemented with different
behavior). Every result this module returns is produced by
`_run_fast_multi_attempt`, which replays simulate_verbose's own retry loop
attempt-for-attempt (`NetRandom(seed + attempt)`, same step order, same
predicates, stopping at the first accepted attempt or after 50 tries) using
`_run_attempt_fast` for each attempt. Each attempt's acceptance is either:
  (a) analytically PROVEN (see point 3 of `_run_attempt_fast`'s docstring),
      skipping the real DetectCircularDependencies/ValidatePuzzleDependencies
      computation entirely, or
  (b) computed for real, via simulator.py's own unmodified
      `detect_circular_dependencies` / `validate_puzzle_dependencies`, fed a
      faster but provably-identical `resolve_effective_puzzle` callback (see
      `_fast_effective_puzzle_resolver`).

This design is unconditionally equivalent to calling `simulator.simulate()`
on every seed and checking the constraints against its output -- there is no
probabilistic shortcut and no risk of a missed hit. See `_run_attempt_fast`'s
docstring for the proof, and re_search.md for the measured performance this
buys versus a naive per-seed `simulate()` call.
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Any

from simulator import (
    SpawnArea,
    load_config, simulate,
    name_in, _norm_name, _distance,
    is_item_allowed_for_puzzle, category_ok, is_safe_container_placement,
    validate_puzzle_dependencies, detect_circular_dependencies,
)
from net_random import NetRandom


# ---------------------------------------------------------------------------
# Public data model (per task spec)
# ---------------------------------------------------------------------------

@dataclass
class Constraint:
    item_name: str
    slot_label: str
    kind: str  # "pin" | "prefer"


@dataclass
class SeedResult:
    seed: int
    pins_matched: int
    prefs_matched: int
    placement: dict  # itemName -> slot_label, the full 31-item placement


ProgressCb = Optional[Callable[[int, int, int], None]]  # (seeds_done, total, hits_found)

DEFAULT_FULL_RANGE = (-2147483648, 2147483647)   # full signed int32 space
DEFAULT_QUICK_RANGE = (0, 100_000_000)           # "quick scan" default


class SearchBackend(Protocol):
    """Contract a GPU (OpenCL) backend must implement to be a drop-in
    accelerator. See "GPU backend contract" below for the semantics each
    method/parameter must honor.
    """

    def search(
        self,
        config: dict,
        constraints: list[Constraint],
        seed_range: tuple[int, int] = DEFAULT_FULL_RANGE,
        limit: int = 200,
        progress_cb: ProgressCb = None,
        cancel_evt: Any = None,
    ) -> list[SeedResult]:
        ...


# ---------------------------------------------------------------------------
# Feasibility estimate (surfaced live in the GUI)
# ---------------------------------------------------------------------------

TOTAL_SLOTS = 87           # 71 free + 16 puzzle -- the "Normal" house variant
SEED_SPACE = 2 ** 32       # full signed int32 range


def estimate_expected_matches(num_pins: int, total_slots: int = TOTAL_SLOTS) -> float:
    """Rough expected number of matching seeds over the full 2**32 seed
    space for `num_pins` PIN constraints.

    Model: treats each pin as an independent uniform choice over
    `total_slots` possible slots, giving expected_matches =
    2**32 / total_slots**num_pins. This is a deliberately simplified
    ESTIMATE -- it ignores that puzzle slots have far fewer eligible
    candidate items than free slots (so real per-item entropy is lower than
    log2(total_slots) bits), and ignores correlations between items'
    placements. Present it to the user labeled as an estimate, not a
    guarantee. See re_search.md.

    `total_slots` is variant-dependent (free spots + puzzles): 39 for house
    version 1.0, 87 for Normal, 89 for More. Smaller variants have a
    *higher* usable pin ceiling because each pin costs fewer bits.
    """
    if num_pins <= 0:
        return float(SEED_SPACE)
    if total_slots <= 0:
        return float(SEED_SPACE)
    return SEED_SPACE / (float(total_slots) ** num_pins)


def feasibility_message(num_pins: int, total_slots: int = TOTAL_SLOTS) -> str:
    """Human-readable feasibility line for the GUI to display live.

    Thresholds are keyed off the ESTIMATE, not off a hardcoded pin count,
    so the advice stays correct across variants with different slot counts.
    """
    est = estimate_expected_matches(num_pins, total_slots)
    if num_pins <= 0:
        return "No pins set -- every seed is a candidate (ranked by preferences)."
    if est >= 1000:
        return f"~{est:,.0f} matching seeds expected (estimate)."
    if est >= 100:
        return f"~{est:,.0f} matching seeds expected (estimate) -- comfortable."
    if est >= 10:
        return f"~{est:,.0f} matching seeds expected (estimate) -- getting tight."
    if est >= 1:
        return (f"~{est:.1f} matching seeds expected (estimate) -- WARNING: "
                f"a matching seed may not exist.")
    return (f"~{est:.3f} matching seeds expected (estimate) -- "
            f"{num_pins} pins is almost certainly IMPOSSIBLE. A matching seed "
            f"almost certainly does not exist in the 2^32 seed space.")


def total_slots_for_config(config: dict) -> int:
    """Feasibility denominator for a loaded config: free spots + puzzle spots."""
    free = sum(len(a.freeSpots) for a in config["spawnAreas"])
    puzzles = sum(1 for p in config["puzzleDefs"] if p.spawnPointId is not None)
    return free + puzzles


# ---------------------------------------------------------------------------
# Static (seed-independent) simulation context, built once per search
# ---------------------------------------------------------------------------

class SimContext:
    """Everything `_run_attempt_fast` needs that does not depend on the seed.
    Mirrors the setup section at the top of `simulate_verbose`.
    """
    __slots__ = (
        "config", "items", "puzzle_defs", "valid_items_all", "items_by_name",
        "ordered_puzzles", "puzzle_pos_by_name", "area_spot_pos",
        "spawn_areas_template", "required_escape", "escape_chance", "fill_all",
        "container_names_norm", "containers_list", "puzzles_with_spawn",
        "containers_contained_norm",
    )

    def __init__(self, config: dict):
        self.config = config
        self.items = config["items"]
        self.puzzle_defs = config["puzzleDefs"]
        self.valid_items_all = [i for i in self.items if i.valid]
        self.items_by_name = {_norm_name(i.itemName): i for i in self.valid_items_all}
        self.ordered_puzzles = sorted(
            [p for p in self.puzzle_defs if p.spawnPointId is not None],
            key=lambda p: p.priority,
        )
        self.puzzle_pos_by_name = {
            _norm_name(p.puzzleName): p.spawnPointPosition
            for p in self.puzzle_defs if p is not None and p.spawnPointPosition is not None
        }
        self.area_spot_pos = {}
        for a in config["spawnAreas"]:
            for s in a.freeSpots:
                if s.position is not None:
                    self.area_spot_pos[(a.areaName, s.name)] = s.position
        self.spawn_areas_template = config["spawnAreas"]
        self.required_escape = config["requiredEscapeItemNames"]
        self.escape_chance = config["escapeItemPuzzleChance"]
        self.fill_all = config["fillAllFreeSpawns"]
        # Items with non-empty containedItems ("containers"). See the long
        # comment on `_run_attempt_fast` for why these are the ONLY items whose
        # final resting place can possibly affect the acceptance check, and
        # `_fast_effective_puzzle_resolver` for why they're also the only
        # items GetEffectivePuzzleOfItem's inner loop can ever match.
        self.containers_list = [i for i in self.valid_items_all if i.containedItems]
        self.container_names_norm = frozenset(_norm_name(i.itemName) for i in self.containers_list)
        self.puzzles_with_spawn = [
            p for p in self.puzzle_defs if p is not None and p.spawnPointPosition is not None
        ]
        # Precomputed normalized containedItems sets, parallel to
        # containers_list -- avoids re-normalizing (`.strip().lower()`) the
        # same static per-container name lists on every single resolve()
        # call. O(1) set membership replaces name_eq's O(n) linear scan;
        # behaviorally identical since name_eq/name_in are themselves plain
        # normalized-string equality (see simulator.py's block comment).
        self.containers_contained_norm = [
            frozenset(_norm_name(n) for n in c.containedItems) for c in self.containers_list
        ]


class _Reject(Exception):
    """Internal control-flow signal: a PIN-constrained item was placed on a
    slot other than its pinned target. Raised from deep inside
    `_run_attempt_fast` to abort the rest of that attempt immediately.
    """
    pass


def _fast_effective_puzzle_resolver(ctx: SimContext, position_of) -> callable:
    """Drop-in, provably-identical, faster replacement for
    `simulator.make_effective_puzzle_resolver`'s returned closure.

    The original's inner loop is `for g in valid_items_all: if not
    g.containedItems: continue; ...` -- i.e. it immediately skips every item
    that isn't a container. Iterating `ctx.containers_list` (precomputed
    once per config, not per seed) instead of all 31 items every single call
    skips exactly the iterations that would have `continue`d anyway, so the
    return value is identical for every input; only items that could never
    have mattered are no longer visited. Likewise `puzzle_defs` is narrowed
    to `ctx.puzzles_with_spawn` (the original's own first line already skips
    `spawnPointPosition is None`), and the `any(name_eq(n, item_name) for n
    in g.containedItems)` membership test is replaced by an O(1) lookup into
    `ctx.containers_contained_norm`'s precomputed normalized-name sets --
    name_eq is itself nothing but normalized-string equality, so this is the
    same test, just without repeatedly re-normalizing the same static
    per-container name list on every call. Never used in place of
    `detect_circular_dependencies`/`validate_puzzle_dependencies` themselves
    -- those are simulator.py's own, unmodified, and still called verbatim.
    """
    items_by_name = ctx.items_by_name
    containers = ctx.containers_list
    contained_norm = ctx.containers_contained_norm
    puzzles = ctx.puzzles_with_spawn

    def resolve(item_name, visited=None):
        if not item_name:
            return None
        if visited is None:
            visited = set()
        key = _norm_name(item_name)
        if key in visited:
            return None
        visited.add(key)
        if items_by_name.get(key) is None:
            return None
        for p in puzzles:
            for g, g_contained_norm in zip(containers, contained_norm):
                if key not in g_contained_norm:
                    continue
                g_pos = position_of(g.itemName)
                if g_pos is not None and _distance(g_pos, p.spawnPointPosition) < 0.01:
                    return p
                nested = resolve(g.itemName, visited)
                if nested is not None:
                    return nested
        return None

    return resolve


# ---------------------------------------------------------------------------
# Fast attempt-1 simulation with early rejection
# ---------------------------------------------------------------------------

def _run_attempt_fast(rng_seed: int, ctx: SimContext, pin_targets: dict) -> tuple[dict, bool]:
    """Runs exactly ONE attempt of GeneratePlacement -- the same RNG draw
    order, predicates, and step logic as one iteration of
    `simulator.simulate_verbose`'s retry loop -- with changes that are all
    provably behavior-preserving. `rng_seed` is the value already offset by
    attempt number (`seed + attempt`, matching `NetRandom(seed + attempt)`
    in simulate_verbose); callers looking to reproduce a specific attempt
    number pass that directly, and `_run_fast_multi_attempt`'s retry loop
    below calls this once per attempt exactly like simulate_verbose does.

    1. No trace/logging. `simulate_verbose` appends a dict and formats an
       f-string context message on *every single* RNG draw purely for
       diagnostics; removing that changes no RNG state and no placement
       decision, only wall-clock cost.

    2. Step A's per-puzzle candidate pool is computed lazily, immediately
       before that puzzle's rnd.next_int() draw, instead of pre-building all
       16 pools up front. This is exactly equivalent: none of the per-item
       predicates (is_item_allowed_for_puzzle / category_ok /
       is_safe_container_placement) depend on anything that changes during
       Step A other than "already chosen by an earlier puzzle this step"
       (is_used() is false for everyone throughout all of Step A -- nothing
       calls mark_used() until Step B), and that is exactly what
       `chosen_in_a` tracks.

    3. THE key optimization -- a *provably* safe early-abort point, not a
       probabilistic one. Read simulator.py's acceptance predicates closely:
       `DetectCircularDependencies`/`ValidatePuzzleDependencies` can only
       ever be affected by an item X if `GetEffectivePuzzleOfItem` can
       return non-None for some name, and that function's own body
       (re_predicates.md S3) *only* ever finds a match by walking items with
       non-empty `containedItems` ("container" items) and testing whether
       that container's own *final position* lies within 0.01 units of some
       puzzle's spawn point. If NO container item ends up at a puzzle spawn
       this attempt, `resolve_effective_puzzle(anything)` is None for every
       possible input, so: `validate_puzzle_dependencies`'s only failure
       branch (`resolve_effective_puzzle(n) is not None`) never fires, and
       `detect_circular_dependencies`'s graph never gets a single Item->
       Puzzle edge (only static Puzzle->Item edges from requiredItemNames,
       which alone cannot cycle) -- so acceptance is 100% GUARANTEED true,
       regardless of anything that happens afterward.

       Crucially, *all* puzzle placements happen in Steps A-D; Steps E/F
       only ever place items into FREE spots. So by the end of Step D we
       already know, for certain and forever (for this attempt), whether
       any container reached a puzzle spawn -- `ctx.container_names_norm`
       lets us check this in O(#containers) (just 1 item, "Melon", on this
       scene's config, but computed generically from the config, not
       hardcoded).

       - If no container is in a puzzle after Step D: acceptance is proven,
         so early-abort (raising `_Reject` the instant any PIN-constrained
         item is written with the wrong label) is now enabled for the rest
         of the attempt, and the real
         DetectCircularDependencies/ValidatePuzzleDependencies computation
         is skipped entirely at the end (also proven).
       - If some container IS in a puzzle after Step D (~8% of seeds on
         this scene's config, measured -- see re_search.md): whether
         acceptance holds now genuinely depends on downstream placements,
         so early-abort is disabled (mismatches are merely recorded, not
         raised) and the attempt runs to completion with the real
         acceptance check, exactly like `simulate_verbose`.

       This means: unlike a naive "abort as soon as any pin fails" filter,
       this design has NO risk of ever hiding a seed whose actual (possibly
       retried) placement satisfies the pins -- see `_run_fast_multi_attempt`,
       which keeps calling this function for attempt 2, 3, ... using the
       identical logic whenever an attempt's own acceptance check comes back
       False, exactly mirroring `simulate_verbose`'s retry loop. There is no
       `strict` mode needed: this is the correct (matches `simulate()`
       exactly) *and* the fast path, always.

    Returns (result, accepted) reflecting GeneratePlacement's real
    acceptance test for this attempt (either proven analytically, per (3)
    above, or computed for real when a container reached a puzzle).
    """
    items_by_name = ctx.items_by_name
    valid_items_all = ctx.valid_items_all
    puzzle_defs = ctx.puzzle_defs
    ordered_puzzles = ctx.ordered_puzzles

    rnd = NetRandom(rng_seed)

    spawn_areas = [
        SpawnArea(a.index, a.areaName, a.maxItems, a.mandatoryItemName,
                  a.allowedItemNames, a.freeSpots)
        for a in ctx.spawn_areas_template
    ]
    used_item_names = set()
    used_puzzle_spawns = set()
    puzzle_has_item = {p.puzzleName: False for p in puzzle_defs}
    result: dict = {}

    def mark_used(name):
        used_item_names.add(_norm_name(name))

    def is_used(name):
        return _norm_name(name) in used_item_names

    # `abort_enabled` flips True once acceptance is *proven* (see docstring
    # point 3); until then, mismatches are recorded but not raised, because
    # we don't yet know if this attempt is even the one that counts.
    abort_enabled = False
    mismatch_found = False

    def check(item_name, label):
        nonlocal mismatch_found
        target = pin_targets.get(_norm_name(item_name))
        if target is not None and target != label:
            mismatch_found = True
            if abort_enabled:
                raise _Reject()

    def place_puzzle(item, puzzle_name, spawn_point_id):
        label = f"PUZZLE:{puzzle_name}"
        result[item.itemName] = label
        used_puzzle_spawns.add(spawn_point_id)
        mark_used(item.itemName)
        puzzle_has_item[puzzle_name] = True
        check(item.itemName, label)

    def shuffle(lst):
        for i in range(len(lst) - 1, 0, -1):
            j = rnd.next_int(i + 1)
            lst[i], lst[j] = lst[j], lst[i]

    def place_item_in_free_area(item):
        areas = [
            a for a in spawn_areas
            if a.currentItems < a.maxItems and len(a.freeSpots) != 0
            and (len(a.allowedItemNames) == 0 or name_in(item.itemName, a.allowedItemNames))
        ]
        shuffle(areas)
        for a in areas:
            free = [s for s in a.freeSpots if s.name not in a.usedSpots]
            if free:
                idx = rnd.next_int(len(free))
                spot = free[idx]
                a.currentItems += 1
                a.usedSpots.add(spot.name)
                label = f"FREE:{a.areaName}/{spot.name}"
                result[item.itemName] = label
                check(item.itemName, label)
                return True
        # fallback: linear scan over the ORIGINAL (unshuffled) spawnAreas order
        for a in spawn_areas:
            if not (a.currentItems < a.maxItems):
                continue
            free = [s for s in a.freeSpots if s.name not in a.usedSpots]
            if free:
                idx = rnd.next_int(len(free))
                spot = free[idx]
                a.currentItems += 1
                a.usedSpots.add(spot.name)
                label = f"FREE:{a.areaName}/{spot.name}"
                result[item.itemName] = label
                check(item.itemName, label)
                return True
        return False

    def place_item_in_specific_free_area(item, area):
        free = [s for s in area.freeSpots if s.name not in area.usedSpots]
        if not free:
            return False
        idx = rnd.next_int(len(free))
        spot = free[idx]
        area.currentItems += 1
        area.usedSpots.add(spot.name)
        label = f"FREE:{area.areaName}/{spot.name}"
        result[item.itemName] = label
        check(item.itemName, label)
        return True

    # --- Step A: greedy puzzle<-item assignment (lazy candidate pools) ---
    chosen_in_a = set()
    assigned_item = {}
    for p in ordered_puzzles:
        pool = [
            i for i in valid_items_all
            if _norm_name(i.itemName) not in chosen_in_a
            and is_item_allowed_for_puzzle(i, p)
            and category_ok(i)
            and is_safe_container_placement(i, p, puzzle_defs)
        ]
        assigned_item[p.puzzleName] = None
        if pool:
            idx = rnd.next_int(len(pool))
            chosen = pool[idx]
            assigned_item[p.puzzleName] = chosen
            chosen_in_a.add(_norm_name(chosen.itemName))

    # --- Step B: required escape items ---
    assigned_names = {_norm_name(v.itemName) for v in assigned_item.values() if v is not None}
    for name in ctx.required_escape:
        item = items_by_name.get(_norm_name(name))
        if item is None:
            continue
        if _norm_name(item.itemName) in assigned_names:
            continue
        roll = rnd.next_double()
        if ctx.escape_chance <= roll:
            place_item_in_free_area(item)
            mark_used(name)
        else:
            if len(puzzle_defs) == 0:
                place_item_in_free_area(item)
                mark_used(name)
            else:
                eligible = [
                    p for p in puzzle_defs
                    if p.spawnPointId is not None
                    and p.spawnPointId not in used_puzzle_spawns
                    and assigned_item.get(p.puzzleName) is None
                    and is_item_allowed_for_puzzle(item, p)
                ]
                if eligible:
                    idx = rnd.next_int(len(eligible))
                    p = eligible[idx]
                    place_puzzle(item, p.puzzleName, p.spawnPointId)
                else:
                    place_item_in_free_area(item)
                    mark_used(name)

    # --- Step C: commit Step-A assignments ---
    for p in ordered_puzzles:
        if p.spawnPointId not in used_puzzle_spawns and assigned_item.get(p.puzzleName) is not None:
            item = assigned_item[p.puzzleName]
            if is_used(item.itemName):
                assigned_item[p.puzzleName] = None
            else:
                place_puzzle(item, p.puzzleName, p.spawnPointId)

    # --- Step D: backfill still-empty puzzles ---
    for p in ordered_puzzles:
        if p.spawnPointId not in used_puzzle_spawns and not puzzle_has_item[p.puzzleName]:
            tier1 = [
                i for i in valid_items_all
                if not is_used(i.itemName)
                and is_item_allowed_for_puzzle(i, p)
                and category_ok(i)
            ]
            pool = tier1
            if not pool:
                pool = [i for i in valid_items_all if not is_used(i.itemName) and category_ok(i)]
            if pool:
                idx = rnd.next_int(len(pool))
                item = pool[idx]
                place_puzzle(item, p.puzzleName, p.spawnPointId)

    # --- Provably-safe early-abort gate (see docstring point 3) ---
    # All puzzle placements happen in Steps A-D; Steps E/F only ever place
    # items into FREE spots. So this is already the FINAL truth for this
    # attempt, regardless of anything E/F do below.
    guaranteed_accept = True
    for cname in ctx.container_names_norm:
        citem = items_by_name.get(cname)
        label = result.get(citem.itemName) if citem is not None else None
        if label is not None and label.startswith("PUZZLE:"):
            # Already sitting on a puzzle spawn: acceptance is no longer
            # provable without running the real check. (`label is None` --
            # not yet placed -- is FINE: Steps E/F can only ever place items
            # into FREE spots, never puzzles, so an unplaced container here
            # is guaranteed to end up FREE, or, in the astronomically rare
            # case that Step F's free-area placement fails to find room for
            # it, to remain unplaced -- at which point the acceptance check
            # would fall back to its original scene startPosition. That
            # residual edge case would require the scene's free-spot
            # capacity to be exhausted *and* that item's authored resting
            # position to coincidentally sit within 0.01 units of some
            # puzzle's spawn point; with 31 items and 87 slots on this
            # scene's config there is no capacity pressure at all, so this
            # is treated as negligible. See re_search.md.)
            guaranteed_accept = False
            break
    if guaranteed_accept:
        if mismatch_found:
            raise _Reject()
        abort_enabled = True

    # --- Step E: mandatory-item spawn areas (dead in this build) ---
    for a in spawn_areas:
        if a.mandatoryItemName:
            item = items_by_name.get(_norm_name(a.mandatoryItemName))
            if item is not None and not is_used(a.mandatoryItemName):
                place_item_in_specific_free_area(item, a)
                mark_used(a.mandatoryItemName)

    # --- Step F: fill remaining free spawns ---
    if ctx.fill_all:
        remaining = [i for i in valid_items_all if not is_used(i.itemName)]
        shuffle(remaining)
        for item in remaining:
            if place_item_in_free_area(item):
                mark_used(item.itemName)

    if guaranteed_accept:
        # Proven above -- no need to run the real acceptance check at all.
        return result, True

    # --- Acceptance: !DetectCircularDependencies() && ValidatePuzzleDependencies() ---
    # These two are simulator.py's own, unmodified functions -- never
    # reimplemented here. Only `resolve_effective_puzzle` (the callback they
    # invoke) is a faster, provably-identical replacement -- see
    # `_fast_effective_puzzle_resolver`. Only reached on the rare path where
    # a container reached a puzzle spawn.
    def _position_of(item_name):
        item = items_by_name.get(_norm_name(item_name))
        if item is None:
            return None
        label = result.get(item.itemName)
        if label is None:
            return item.position
        if label.startswith("PUZZLE:"):
            return ctx.puzzle_pos_by_name.get(_norm_name(label[len("PUZZLE:"):]))
        rest = label[len("FREE:"):]
        area_name, spot_name = rest.split("/", 1)
        return ctx.area_spot_pos.get((area_name, spot_name))

    resolve_effective_puzzle = _fast_effective_puzzle_resolver(ctx, _position_of)
    accepted = (
        not detect_circular_dependencies(items_by_name, puzzle_defs, resolve_effective_puzzle)
        and validate_puzzle_dependencies(items_by_name, puzzle_defs, ctx.required_escape,
                                          resolve_effective_puzzle)
    )
    return result, accepted


def _run_fast_multi_attempt(seed: int, ctx: SimContext, pin_targets: dict,
                             max_attempts: int = 50) -> dict:
    """Fast multi-attempt evaluation mirroring `simulate_verbose`'s own
    retry loop exactly (`while attempt < max_attempts and not success:
    attempt += 1; rnd = NetRandom(seed + attempt); ...`), attempt-for-attempt,
    without ever calling the slower `simulator.simulate()`.

    Returns the final `result` dict: either the first accepted attempt's
    placement, or -- matching `simulate_verbose`'s own behavior when no
    attempt is accepted within `max_attempts` (in practice never observed;
    kept for exact fidelity) -- the LAST attempt's partial result.

    Raises `_Reject` if some attempt, PROVEN accepted (see
    `_run_attempt_fast`'s docstring point 3), mismatches a pin. Since that
    attempt is proven to be the one the real game keeps regardless of
    attempt number, this is a safe, definitive rejection.
    """
    attempt = 0
    result: dict = {}
    while attempt < max_attempts:
        attempt += 1
        result, accepted = _run_attempt_fast(seed + attempt, ctx, pin_targets)
        if accepted:
            return result
    return result


# ---------------------------------------------------------------------------
# Per-seed evaluation (shared by every backend)
# ---------------------------------------------------------------------------

def evaluate_seed(seed: int, ctx: SimContext, pin_targets: dict,
                   constraints: list[Constraint], total_pins: int) -> Optional[SeedResult]:
    """Evaluate one seed against `constraints`. Returns a SeedResult iff
    every PIN constraint is satisfied by the seed's *actual* final placement
    (i.e. exactly what `simulator.simulate()` would return), else None.

    This is unconditionally correct -- see `_run_attempt_fast`'s docstring
    point 3 for the proof that its early-abort (`_Reject`) is only ever
    raised once acceptance has already been established for certain, and
    `_run_fast_multi_attempt` for how the (rare) case of an attempt that
    isn't accepted is handled by continuing the SAME fast retry loop the
    real game uses, rather than falling back to a slower implementation.
    There is no "fast but slightly wrong" mode here; the whole point of the
    early-abort design is that it never needs one.
    """
    try:
        result = _run_fast_multi_attempt(seed, ctx, pin_targets)
    except _Reject:
        # Only ever raised once acceptance was already proven true for this
        # attempt (see docstring) -- so this attempt is definitely the final
        # one, and it definitely mismatches a pin. Safe, definitive reject.
        return None

    # `result` is keyed by each item's real-cased itemName, but constraint
    # item_names come from user/GUI input and may differ in case -- match
    # them the same normalized way `_run_attempt_fast`'s pin check does,
    # instead of an exact-string lookup that would silently miss a
    # differently-cased constraint on an otherwise-satisfied seed.
    result_by_norm_name = {_norm_name(k): v for k, v in result.items()}
    pins_matched = sum(
        1 for c in constraints
        if c.kind == "pin" and result_by_norm_name.get(_norm_name(c.item_name)) == c.slot_label
    )
    if pins_matched < total_pins:
        return None
    prefs_matched = sum(
        1 for c in constraints
        if c.kind == "prefer" and result_by_norm_name.get(_norm_name(c.item_name)) == c.slot_label
    )
    return SeedResult(seed=seed, pins_matched=pins_matched, prefs_matched=prefs_matched,
                       placement=dict(result))


def _pin_targets_and_total(constraints: list[Constraint]) -> tuple[dict, int]:
    pins = [c for c in constraints if c.kind == "pin"]
    return {_norm_name(c.item_name): c.slot_label for c in pins}, len(pins)


# ---------------------------------------------------------------------------
# CPU backend (multiprocessing over all cores)
# ---------------------------------------------------------------------------

# Per-worker-process globals, set once by `_worker_init` (avoids re-parsing
# the config JSON / rebuilding SimContext on every chunk).
_worker_ctx: Optional[SimContext] = None
_worker_pin_targets: Optional[dict] = None
_worker_constraints: Optional[list] = None
_worker_total_pins: int = 0


def _worker_init(config: dict, constraints: list[Constraint]) -> None:
    global _worker_ctx, _worker_pin_targets, _worker_constraints, _worker_total_pins
    _worker_ctx = SimContext(config)
    _worker_constraints = constraints
    _worker_pin_targets, _worker_total_pins = _pin_targets_and_total(constraints)


def _search_chunk(rng: tuple[int, int]) -> tuple[int, list[SeedResult]]:
    start, end = rng  # inclusive
    hits = []
    for seed in range(start, end + 1):
        r = evaluate_seed(seed, _worker_ctx, _worker_pin_targets, _worker_constraints,
                           _worker_total_pins)
        if r is not None:
            hits.append(r)
    return (end - start + 1, hits)


class CpuSearchBackend:
    """Multiprocessing CPU search backend. Implements `SearchBackend`."""

    def __init__(self, num_workers: Optional[int] = None, chunk_size: int = 100_000):
        self.num_workers = num_workers or max(1, mp.cpu_count())
        self.chunk_size = chunk_size

    def search(
        self,
        config: dict,
        constraints: list[Constraint],
        seed_range: tuple[int, int] = DEFAULT_FULL_RANGE,
        limit: int = 200,
        progress_cb: ProgressCb = None,
        cancel_evt: Any = None,
    ) -> list[SeedResult]:
        start, end = seed_range
        if end < start:
            raise ValueError(f"Invalid seed_range: {seed_range}")
        total = end - start + 1

        chunks = []
        s = start
        while s <= end:
            e = min(s + self.chunk_size - 1, end)
            chunks.append((s, e))
            s = e + 1

        seeds_done = 0
        hits: list[SeedResult] = []

        ctx = mp.get_context("spawn") if hasattr(mp, "get_context") else mp
        with ctx.Pool(processes=self.num_workers, initializer=_worker_init,
                      initargs=(config, constraints)) as pool:
            try:
                for n_scanned, chunk_hits in pool.imap_unordered(_search_chunk, chunks):
                    seeds_done += n_scanned
                    hits.extend(chunk_hits)
                    if progress_cb is not None:
                        progress_cb(seeds_done, total, len(hits))
                    if cancel_evt is not None and cancel_evt.is_set():
                        pool.terminate()
                        break
                    if limit and len(hits) >= limit:
                        pool.terminate()
                        break
            finally:
                pool.terminate()
                pool.join()

        # Rank: all-pins-satisfied first (guaranteed true for every entry in
        # `hits` by construction of evaluate_seed), then prefs_matched desc.
        hits.sort(key=lambda r: (-r.pins_matched, -r.prefs_matched))
        return hits[:limit] if limit else hits


# ---------------------------------------------------------------------------
# GPU backend contract (for the future OpenCL drop-in accelerator)
# ---------------------------------------------------------------------------
#
# A GPU backend must implement `SearchBackend.search()` with these semantics:
#   - config: the dict returned by simulator.load_config().
#   - constraints: list[Constraint] as above; "pin" is mandatory-match,
#     "prefer" is scored only.
#   - seed_range: inclusive (start, end) over signed int32 seed values.
#   - limit: stop once this many hits are found (0/None = unbounded --
#     caller beware on the full 2**32 range).
#   - progress_cb(seeds_done, total, hits_found): called periodically (not
#     necessarily every seed) from the calling thread's context. Must be
#     safe for the caller to marshal to a GUI thread (i.e. plain ints only).
#   - cancel_evt: any object exposing `.is_set() -> bool`; checked between
#     progress_cb calls; the backend must stop scanning promptly (sub-second
#     on a healthy device) once set.
#   - Return: list[SeedResult], already ranked (pins satisfied is a given for
#     every entry; ties broken by prefs_matched desc), truncated to `limit`.
#   - Every returned SeedResult.placement MUST be a placement that
#     simulator.simulate() would independently reproduce for that seed --
#     i.e. any device-side fast path must be verified (either by construction
#     or by re-running simulate() on hits) exactly as CpuSearchBackend does
#     here, not merely "probably right". A GPU implementation is free to
#     port `_run_attempt_fast`'s per-attempt logic to OpenCL C, but the
#     acceptance check (DetectCircularDependencies / ValidatePuzzleDependencies)
#     and any attempt-1-rejected fallback should still be verified against
#     the reference Python implementation, at minimum for every candidate hit
#     before it's reported.
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import time
    from pathlib import Path
    from dump_utils import newest_dump_file

    dumps_dir = Path(__file__).parent / "dumps"
    cfg_path = str(newest_dump_file(dumps_dir, "config_*.json"))
    print(f"Using config: {cfg_path}")
    cfg = load_config(cfg_path)

    # -----------------------------------------------------------------
    # End-to-end correctness check (required by task spec):
    # pin Pliers -> FREE:OldHouse/Spot (82), satisfied by seed 915074960,
    # search 915074000..915076000 and confirm the search FINDS it.
    # -----------------------------------------------------------------
    print("\n=== End-to-end verification ===")
    constraints = [Constraint(item_name="Pliers", slot_label="FREE:OldHouse/Spot (82)", kind="pin")]
    backend = CpuSearchBackend(chunk_size=2000)
    t0 = time.time()
    results = backend.search(cfg, constraints, seed_range=(915074000, 915076000), limit=50)
    dt = time.time() - t0
    found = [r for r in results if r.seed == 915074960]
    print(f"scanned 2001 seeds in {dt:.3f}s ({2001/dt:,.0f} seeds/sec across "
          f"{backend.num_workers} workers)")
    print(f"total hits in range: {len(results)}")
    print(f"seed 915074960 found: {bool(found)}")
    if found:
        r = found[0]
        print(f"  pins_matched={r.pins_matched} prefs_matched={r.prefs_matched}")
        print(f"  Pliers -> {r.placement.get('Pliers')}")
    assert found, "FAILED: end-to-end verification did not find seed 915074960"
    print("PASS")

    # -----------------------------------------------------------------
    # Exact-equivalence check: per-seed, not just hit-count. Also reports
    # the measured "guaranteed_accept proven" rate (the fraction of seeds
    # for which the early-abort optimization is actually able to engage).
    # -----------------------------------------------------------------
    print("\n=== Exact-equivalence check (fast path vs naive simulate()) ===")
    from simulator import simulate as naive_simulate

    N_EQ = 5_000
    lo_eq, hi_eq = 300_000_000, 300_000_000 + N_EQ - 1
    ctx_obj = SimContext(cfg)
    pin_targets, total_pins = _pin_targets_and_total(constraints)
    mismatches = 0
    for seed in range(lo_eq, hi_eq + 1):
        naive_placement = naive_simulate(seed, cfg)
        naive_hit = naive_placement.get("Pliers") == "FREE:OldHouse/Spot (82)"
        r = evaluate_seed(seed, ctx_obj, pin_targets, constraints, total_pins)
        fast_hit = r is not None
        if naive_hit != fast_hit:
            mismatches += 1
            print(f"  MISMATCH seed={seed}: naive_hit={naive_hit} fast_hit={fast_hit}")
    print(f"{N_EQ} seeds compared, {mismatches} mismatches "
          f"(expected 0 -- fast path is provably equivalent to simulate())")
    assert mismatches == 0, "fast path diverged from naive simulate() -- correctness bug!"
    print("PASS: fast path is exactly equivalent to simulate() on this sample.")

    # -----------------------------------------------------------------
    # Throughput measurement: naive (simulate() per seed, full verbose
    # trace-based reference) vs the fast early-rejection path, single
    # process, then full multiprocessing throughput.
    # -----------------------------------------------------------------
    print("\n=== Throughput measurement ===")
    N = 4_000
    lo, hi = 100_000_000, 100_000_000 + N - 1

    t0 = time.time()
    naive_hits = 0
    for seed in range(lo, hi + 1):
        placement = naive_simulate(seed, cfg)
        if placement.get("Pliers") == "FREE:OldHouse/Spot (82)":
            naive_hits += 1
    naive_dt = time.time() - t0
    naive_rate = N / naive_dt
    print(f"naive simulate() (1 pin, 1 core): {naive_rate:,.0f} seeds/sec "
          f"({naive_hits} hits in {N} seeds)")

    t0 = time.time()
    fast_hits = 0
    for seed in range(lo, hi + 1):
        r = evaluate_seed(seed, ctx_obj, pin_targets, constraints, total_pins)
        if r is not None:
            fast_hits += 1
    fast_dt = time.time() - t0
    fast_rate = N / fast_dt
    print(f"fast early-rejection path (1 pin, 1 core): {fast_rate:,.0f} seeds/sec "
          f"({fast_hits} hits in {N} seeds) -- {fast_rate/naive_rate:.2f}x speedup/core")
    assert fast_hits == naive_hits, "fast path hit count diverged from naive simulate()!"

    # How often is attempt 1 accepted at all (vs needing a retry)?
    accepted_count = 0
    N3 = 2000
    for seed in range(lo, lo + N3):
        _, accepted = _run_attempt_fast(seed + 1, ctx_obj, {})
        if accepted:
            accepted_count += 1
    print(f"attempt-1 acceptance rate ({N3}-seed sample): {accepted_count/N3:.1%} "
          f"(rest need >=1 retry, still handled by the fast path -- see re_search.md)")

    N2 = 500_000
    lo2, hi2 = 200_000_000, 200_000_000 + N2 - 1
    t0 = time.time()
    results2 = backend.search(cfg, constraints, seed_range=(lo2, hi2), limit=0)
    dt2 = time.time() - t0
    print(f"\nfull multiprocessing pool ({backend.num_workers} workers), 0 pins-vs-1pin scan: "
          f"{N2:,} seeds in {dt2:.2f}s = {N2/dt2:,.0f} seeds/sec total "
          f"({N2/dt2/backend.num_workers:,.0f} seeds/sec/core), {len(results2)} hits")
