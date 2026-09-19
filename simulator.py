"""
simulator.py

Bit-exact Python reimplementation of Granny Legacy's `SeedManager.GeneratePlacement`.

Spec sources: re_generateplacement.md (pipeline steps A-F, RNG call order) and
re_predicates.md (predicates/validators, produced from IDA disassembly --
authoritative over any earlier empirical inference wherever the two
disagree). See re_simulator.md for the full reconciliation history between
this module and re_predicates.md.

Predicate provenance (see each function's docstring for the specific
re_predicates.md section cited):

  * IsItemAllowedForPuzzle -- VERIFIED (S1). Allow-list membership if
    non-empty, else not-in-exclude-list; requiredItemNames is NOT consulted
    (confirmed a behavioral no-op on this build's data: no puzzle's own
    requiredItemNames ever overlaps what it could otherwise allow -- see
    re_simulator.md). Name matching is name_eq/name_in (Trim +
    OrdinalIgnoreCase).

  * category filter: item.category != 4 (category 4 = "free" items) --
    VERIFIED (S6, `b__20_5`: `cmp dword ptr [rax+28h], 4`).

  * valid(item): item has a non-null ItemSeedData (itemName is not None) --
    matches the report's `b__20_0` filter.

  * IsSafeContainerPlacement -- VERIFIED (S2). An item's containedItems is
    checked against every PuzzleDef's requiredItemNames (not the unrelated
    PuzzleDef.containedItems field); unsafe iff a puzzle whose
    requiredItemNames demands a contained name shares the *candidate*
    puzzle's own spawn point. Name comparison here is plain case-sensitive
    `in` (NOT name_eq) -- the decompile shows a bare `List<string>.Contains`,
    unlike the OrdinalIgnoreCase lambda in IsItemAllowedForPuzzle.

  * ValidatePuzzleDependencies / DetectCircularDependencies -- VERIFIED
    (S4/S5), including GetEffectivePuzzleOfItem's spatial (<0.01 unit)
    resolution (S3). Both consume zero RNG; their boolean result only
    decides *which* attempt's RNG stream wins the retry loop, never a given
    attempt's own RNG sequence.

The one place behavior goes beyond re_predicates.md's literal text is name
matching for containers not explicitly decompiled with a comparer constant
(usedItemNames, SpawnArea.allowedItemNames, itemsByName, requiredEscapeItemNames)
-- marked INFERRED EXTENSION at the name_eq/name_in helpers, by consistency
with every other name-keyed container the report *does* confirm as
OrdinalIgnoreCase.
"""

import json
import struct
from dataclasses import dataclass, field
from typing import Optional

from net_random import NetRandom


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    index: int
    goName: str
    itemName: Optional[str]
    category: Optional[int]
    containedItems: tuple
    valid: bool
    # Original scene-authored transform position (startPosition). Used only by
    # GetEffectivePuzzleOfItem's spatial <0.01-unit proximity check
    # (re_predicates.md S3) as the position of an item that GeneratePlacement
    # has not (yet, or ever) moved this attempt.
    position: Optional[tuple] = None


@dataclass(frozen=True)
class PuzzleDef:
    index: int
    puzzleName: str
    spawnPointId: Optional[int]  # instanceId of spawnPoint, or None if no spawnPoint
    allowedItemNames: tuple
    excludeItemNames: tuple
    requiredItemNames: tuple
    containedItems: tuple  # puzzle-level containedItems (distinct from Item.containedItems)
    priority: int
    # spawnPoint.position -- VERIFIED needed by GetEffectivePuzzleOfItem
    # (re_predicates.md S3) and IsSafeContainerPlacement's identity check
    # (approximated via spawnPointId, see is_safe_container_placement).
    spawnPointPosition: Optional[tuple] = None


@dataclass(frozen=True)
class Spot:
    name: str
    instanceId: int
    position: Optional[tuple] = None


@dataclass
class SpawnArea:
    index: int
    areaName: str
    maxItems: int
    mandatoryItemName: str
    allowedItemNames: tuple
    freeSpots: tuple
    currentItems: int = 0
    usedSpots: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _as_float32(x) -> float:
    """Promote a value through IEEE-754 binary32 exactly as IL2CPP does when a
    C# `float` field is widened to `double` for comparison."""
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


def load_config(path: str) -> dict:
    with open(path) as f:
        raw = json.load(f)

    def pos_tuple(pos_dict):
        if not pos_dict:
            return None
        return (pos_dict["x"], pos_dict["y"], pos_dict["z"])

    items = []
    for it in raw["allItems"]:
        sd = it.get("itemSeedData")
        pos = pos_tuple(it.get("startPosition"))
        if sd:
            items.append(Item(
                index=it["index"],
                goName=it.get("goName", it.get("name", "")),
                itemName=sd["itemName"],
                category=sd["category"],
                containedItems=tuple(sd.get("containedItems") or []),
                valid=True,
                position=pos,
            ))
        else:
            items.append(Item(
                index=it["index"],
                goName=it.get("goName", it.get("name", "")),
                itemName=None,
                category=None,
                containedItems=(),
                valid=False,
                position=pos,
            ))

    puzzles = []
    for p in raw["puzzleDefs"]:
        sp = p.get("spawnPoint")
        puzzles.append(PuzzleDef(
            index=p["index"],
            puzzleName=p["puzzleName"],
            spawnPointId=sp["instanceId"] if sp else None,
            allowedItemNames=tuple(p.get("allowedItemNames") or []),
            excludeItemNames=tuple(p.get("excludeItemNames") or []),
            requiredItemNames=tuple(p.get("requiredItemNames") or []),
            containedItems=tuple(p.get("containedItems") or []),
            priority=p["priority"],
            spawnPointPosition=pos_tuple(sp["position"]) if sp else None,
        ))

    areas = []
    for a in raw["spawnAreas"]:
        spots = tuple(
            Spot(name=s["name"], instanceId=s["instanceId"], position=pos_tuple(s.get("position")))
            for s in (a.get("freeSpots") or [])
        )
        areas.append(SpawnArea(
            index=a["index"],
            areaName=a["areaName"],
            maxItems=a["maxItems"],
            mandatoryItemName=a.get("mandatoryItemName") or "",
            allowedItemNames=tuple(a.get("allowedItemNames") or []),
            freeSpots=spots,
        ))

    # EscapeItemPuzzleChance is a C# `float` (float32). The game compares
    # `rnd.NextDouble() < this.EscapeItemPuzzleChance`, which promotes the
    # float32 to double -- 0.7f promotes to 0.699999988079071, NOT 0.7. The mod
    # dump serializes it as the shortest round-tripping float32 literal ("0.7"),
    # so reading it straight from JSON as a Python double is very slightly too
    # large and flips the comparison for rolls in [0.699999988079071, 0.7).
    # Round-tripping through float32 restores the exact value the game uses.
    return {
        "escapeItemPuzzleChance": _as_float32(raw["escapeItemPuzzleChance"]),
        "fillAllFreeSpawns": raw["fillAllFreeSpawns"],
        "items": items,
        "puzzleDefs": puzzles,
        "requiredEscapeItemNames": list(raw.get("requiredEscapeItemNames") or []),
        "spawnAreas": areas,
        "gameSeed": raw.get("playerPrefs", {}).get("GameSeed"),
    }


# ---------------------------------------------------------------------------
# Name matching (VERIFIED: re_predicates.md S1 -- IsItemAllowedForPuzzle's
# folded lambda at 0x1802567C0 is `x.Trim().Equals(itemSeedData.itemName.Trim(),
# StringComparison.OrdinalIgnoreCase)`; S3 -- GetEffectivePuzzleOfItem's
# containedItems match is `n.Trim().Equals(itemName, OrdinalIgnoreCase)`; S4/S5
# -- ValidatePuzzleDependencies/DetectCircularDependencies build every
# Dictionary/HashSet keyed on item/puzzle names with
# `StringComparer.OrdinalIgnoreCase`.
#
# INFERRED EXTENSION: the report does not explicitly decompile the comparer
# used for `usedItemNames` membership (`b__20_3`), `SpawnArea.allowedItemNames`
# (`PlaceItemInFreeArea_b__0`), `itemsByName` lookups, or
# `requiredEscapeItemNames` membership -- those lambdas are documented as
# plain `.Contains(...)` / dictionary lookups without a decompiled comparer
# constant. Given every *other* name-keyed container in this codebase that
# *was* decompiled uses OrdinalIgnoreCase (+ Trim where an inline lambda is
# involved), we apply the same helper uniformly here for consistency, per the
# task's explicit instruction. This is the one place where behavior beyond
# the report's literal text is inferred by extrapolation from its pattern.
# ---------------------------------------------------------------------------

def _norm_name(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    return s.strip().lower()


def name_eq(a: Optional[str], b: Optional[str]) -> bool:
    """Trim + OrdinalIgnoreCase equality (see block comment above)."""
    if a is None or b is None:
        return a is b
    return _norm_name(a) == _norm_name(b)


def name_in(name: Optional[str], names) -> bool:
    """Trim + OrdinalIgnoreCase membership test of `name` in an iterable of names."""
    if name is None:
        return False
    n = _norm_name(name)
    return any(n == _norm_name(x) for x in names)


# ---------------------------------------------------------------------------
# Predicates (see module docstring for provenance / inference notes)
# ---------------------------------------------------------------------------

def is_item_allowed_for_puzzle(item: Item, puzzle: PuzzleDef) -> bool:
    # VERIFIED: re_predicates.md S1 -- requiredItemNames clause removed
    # (IsItemAllowedForPuzzle never reads PuzzleDef.requiredItemNames).
    # Confirmed a no-op for this build's actual data: no puzzle's own
    # requiredItemNames overlaps its (implicit-or-explicit) allowed set, so
    # removing the clause does not change the test result -- see
    # re_simulator.md for the check performed.
    if puzzle.allowedItemNames:
        allowed = name_in(item.itemName, puzzle.allowedItemNames)
    else:
        allowed = not name_in(item.itemName, puzzle.excludeItemNames)
    return allowed


def category_ok(item: Item) -> bool:
    """True unless item.category == 4 (the 'free-only' category)."""
    return item.category != 4


def is_safe_container_placement(item: Item, candidate_puzzle: PuzzleDef, puzzle_defs) -> bool:
    """VERIFIED: re_predicates.md S2 (`IsSafeContainerPlacement`, 0x1802523F0).

    Literal port of the decompiled body:
        for containedName in item.containedItems:
            for p in puzzle_defs:
                if p.requiredItemNames contains containedName
                        and p.spawnPoint == candidatePuzzle.spawnPoint:
                    return False
        return True

    Direction of comparison (confirmed, S2, "an earlier pass ... mistakenly
    assumed the PuzzleDef-side field was containedItems"): the item's own
    ItemSeedData.containedItems (what it hides) is checked against every
    PuzzleDef's requiredItemNames (what that puzzle needs) -- NOT against
    PuzzleDef.containedItems, a different, unrelated field.

    Case/trim handling (VERIFIED, S2 -- distinct from IsItemAllowedForPuzzle):
    the decompile shows a plain `List<string>.Contains(containedName)`, i.e.
    the *default* string comparer -- ordinal, case-SENSITIVE, no Trim. This is
    deliberately NOT run through name_eq/name_in; unlike the folded
    OrdinalIgnoreCase lambda in IsItemAllowedForPuzzle, this call site has no
    comparer argument in the disassembly.

    `p.spawnPoint == candidatePuzzle.spawnPoint` is a Unity Object/Transform
    reference-equality test. We approximate it with instanceId equality
    (spawnPointId), which is exact given that every puzzle in this build's
    scene data has a unique spawn point instanceId (verified: 16/16 unique)
    -- so this check only ever fires for p is candidate_puzzle itself,
    matching the previously-INFERRED single-puzzle rule byte-for-byte on this
    build's data, while remaining faithful to the literal all-puzzles loop in
    case a future scene variant reuses a spawn point across puzzles."""
    if not item.containedItems:
        return True
    for contained_name in item.containedItems:
        for p in puzzle_defs:
            if p.requiredItemNames and contained_name in p.requiredItemNames:
                if (p.spawnPointId is not None
                        and p.spawnPointId == candidate_puzzle.spawnPointId):
                    return False
    return True


def _distance(a: tuple, b: tuple) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def make_effective_puzzle_resolver(items_by_name: dict, valid_items_all: list,
                                    puzzle_defs: list, position_of) -> callable:
    """VERIFIED: re_predicates.md S3 -- `GetEffectivePuzzleOfItem(itemName,
    visited)` (0x180251930). Resolves "which puzzle produces this item" via
    spatial proximity (< 0.01 units) between a candidate container's *live*
    position and a puzzle's spawnPoint position -- there is no explicit
    owning-puzzle field on either class. Bound to one attempt's placement
    state via `position_of` (itself closed over that attempt's `result`
    dict), since the relationship depends on where GeneratePlacement actually
    put things this attempt.

    Cycle guard (`visited`) and item-name matching both VERIFIED per S3:
    `visited` is an OrdinalIgnoreCase-keyed set; contained-item-name matching
    is `n.Trim().Equals(itemName, OrdinalIgnoreCase)`, i.e. `name_eq`."""

    def resolve(item_name: Optional[str], visited: Optional[set] = None):
        if not item_name:
            return None
        if visited is None:
            visited = set()
        key = _norm_name(item_name)
        if key in visited:
            return None  # cycle guard
        visited.add(key)
        if items_by_name.get(key) is None:
            return None
        for p in puzzle_defs:
            if p.spawnPointPosition is None:
                continue
            for g in valid_items_all:
                if not g.containedItems:
                    continue
                if not any(name_eq(n, item_name) for n in g.containedItems):
                    continue
                g_pos = position_of(g.itemName)
                if g_pos is not None and _distance(g_pos, p.spawnPointPosition) < 0.01:
                    return p
                nested = resolve(g.itemName, visited)
                if nested is not None:
                    return nested
        return None

    return resolve


def validate_puzzle_dependencies(items_by_name: dict, puzzle_defs: list,
                                  required_escape: list, resolve_effective_puzzle) -> bool:
    """VERIFIED: re_predicates.md S4 -- `ValidatePuzzleDependencies()`
    (0x180253CF0), a pure-bookkeeping fixed-point reachability check with no
    RNG. Faithfully reproduces the disclosed quirk: a PuzzleDef with an empty
    or null `requiredItemNames` is *never* added to `processedPuzzles` by the
    propagation loop, so the items it unlocks only become obtainable if they
    were already freely obtainable -- this is intentionally NOT "fixed" by
    special-casing empty `requiredItemNames` as trivially satisfied."""
    effective_puzzle_of = {}
    for norm_name, item in items_by_name.items():
        if item.itemName:
            effective_puzzle_of[norm_name] = resolve_effective_puzzle(item.itemName)

    puzzle_unlocks_items = {
        _norm_name(p.puzzleName): set()
        for p in puzzle_defs if p is not None and p.puzzleName
    }
    freely_obtainable = set()
    for norm_name, item in items_by_name.items():
        puzzle = effective_puzzle_of.get(norm_name)
        if puzzle is not None:
            puzzle_unlocks_items.setdefault(_norm_name(puzzle.puzzleName), set()).add(norm_name)
        else:
            freely_obtainable.add(norm_name)

    required_names = set()
    for p in puzzle_defs:
        if p is not None and p.requiredItemNames:
            for n in p.requiredItemNames:
                if n:
                    required_names.add(_norm_name(n))
    for n in required_escape:
        if n:
            required_names.add(_norm_name(n))

    obtainable_items = set(freely_obtainable)
    processed_puzzles = set()
    changed = True
    while changed:
        changed = False
        for p in puzzle_defs:
            if p is None or not p.puzzleName:
                continue
            pkey = _norm_name(p.puzzleName)
            if pkey in processed_puzzles:
                continue
            if not p.requiredItemNames:
                continue  # quirk (S4) -- see docstring
            if all(_norm_name(n) in obtainable_items for n in p.requiredItemNames if n):
                processed_puzzles.add(pkey)
                for item_norm in puzzle_unlocks_items.get(pkey, ()):
                    obtainable_items.add(item_norm)
                changed = True

    for n in required_names:
        if n not in obtainable_items and resolve_effective_puzzle(n) is not None:
            return False
    return True


def detect_circular_dependencies(items_by_name: dict, puzzle_defs: list,
                                  resolve_effective_puzzle) -> bool:
    """VERIFIED: re_predicates.md S5 -- `DetectCircularDependencies()`
    (0x18024EC00 + DFS helper 0x180253760), a pure white/gray/black DFS cycle
    check over a bipartite Puzzle<->Item dependency graph, no RNG. The real
    code namespaces graph node keys with two undisclosed string-literal
    prefixes (S8 item 1, purely internal collision-avoidance, no effect on
    correctness); `('P', name)` / `('I', name)` tuples serve the same role
    here."""
    graph: dict = {}

    def ensure(node):
        if node not in graph:
            graph[node] = []

    for p in puzzle_defs:
        if p is None or not p.puzzleName:
            continue
        puzzle_key = ("P", _norm_name(p.puzzleName))
        ensure(puzzle_key)
        if p.requiredItemNames:
            for n in p.requiredItemNames:
                if n:
                    item_key = ("I", _norm_name(n))
                    ensure(item_key)
                    graph[puzzle_key].append(item_key)

    for norm_name, item in items_by_name.items():
        item_key = ("I", norm_name)
        ensure(item_key)
        eff = resolve_effective_puzzle(item.itemName)
        if eff is not None:
            puzzle_key = ("P", _norm_name(eff.puzzleName))
            ensure(puzzle_key)
            graph[item_key].append(puzzle_key)

    color: dict = {}
    has_cycle = False

    def dfs(node):
        nonlocal has_cycle
        if has_cycle:
            return
        color[node] = 1
        for n in graph.get(node, ()):
            if has_cycle:
                return
            c = color.get(n, 0)
            if c == 0:
                dfs(n)
                if has_cycle:
                    return
            elif c == 1:
                has_cycle = True
                return
        color[node] = 2

    for node in graph:
        if node not in color:
            color[node] = 0
    for node in graph:
        if has_cycle:
            break
        if color[node] == 0:
            dfs(node)
    return has_cycle


# ---------------------------------------------------------------------------
# Shuffle (backward Durstenfeld Fisher-Yates, exact per RE report)
# ---------------------------------------------------------------------------

def _shuffle(rnd: NetRandom, lst: list, log=None, step: str = "", context: str = "") -> None:
    for i in range(len(lst) - 1, 0, -1):
        j = rnd.next_int(i + 1)
        if log is not None:
            log("next_int", i + 1, j, step, f"{context} shuffle[{i}]")
        lst[i], lst[j] = lst[j], lst[i]


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def simulate_verbose(seed: int, config: dict, max_attempts: int = 50) -> dict:
    """
    Run the full GeneratePlacement pipeline.

    Returns a dict with:
        result: {itemName: slot_label}
        attempt: which attempt (1-based) succeeded (or the last one tried)
        success: bool
        trace: list of {attempt, step, method, arg, result, context} RNG call records
    """
    items = config["items"]
    puzzle_defs = config["puzzleDefs"]
    required_escape = config["requiredEscapeItemNames"]
    escape_chance = config["escapeItemPuzzleChance"]
    fill_all = config["fillAllFreeSpawns"]
    spawn_areas_template = config["spawnAreas"]

    valid_items_all = [i for i in items if i.valid]
    # INFERRED extension of name_eq/name_in (see block comment above the
    # helpers): keyed on the trimmed/lower-cased name so lookups are
    # case-insensitive and whitespace-insensitive, consistent with every
    # other name-keyed container the report *does* confirm uses
    # StringComparer.OrdinalIgnoreCase.
    items_by_name = {_norm_name(i.itemName): i for i in valid_items_all}

    ordered_puzzles = sorted(
        [p for p in puzzle_defs if p.spawnPointId is not None],
        key=lambda p: p.priority,
    )  # Python's sorted() is stable -> ties preserve original puzzleDefs order

    # Static scene geometry used only by GetEffectivePuzzleOfItem (positions
    # never change between attempts; only which item ends up at which
    # position changes).
    puzzle_pos_by_name = {
        _norm_name(p.puzzleName): p.spawnPointPosition
        for p in puzzle_defs if p is not None and p.spawnPointPosition is not None
    }
    area_spot_pos = {}
    for a in spawn_areas_template:
        for s in a.freeSpots:
            if s.position is not None:
                area_spot_pos[(a.areaName, s.name)] = s.position

    trace = []
    attempt = 0
    success = False
    result = {}

    while attempt < max_attempts and not success:
        attempt += 1
        rnd = NetRandom(seed + attempt)

        spawn_areas = [
            SpawnArea(a.index, a.areaName, a.maxItems, a.mandatoryItemName,
                      a.allowedItemNames, a.freeSpots)
            for a in spawn_areas_template
        ]
        used_item_names = set()
        used_puzzle_spawns = set()
        puzzle_has_item = {p.puzzleName: False for p in puzzle_defs}
        result = {}

        def log(method, arg, ret, step, context):
            trace.append({
                "attempt": attempt, "step": step, "method": method,
                "arg": arg, "result": ret, "context": context,
            })

        def rnd_next_int(n, step, context):
            r = rnd.next_int(n)
            log("next_int", n, r, step, context)
            return r

        def rnd_next_double(step, context):
            r = rnd.next_double()
            log("next_double", None, r, step, context)
            return r

        def mark_used(name):
            used_item_names.add(_norm_name(name))

        def is_used(name):
            return _norm_name(name) in used_item_names

        def shuffle(lst, step, context):
            for i in range(len(lst) - 1, 0, -1):
                j = rnd_next_int(i + 1, step, f"{context} shuffle[{i}]")
                lst[i], lst[j] = lst[j], lst[i]

        def place_item_in_free_area(item, step, context):
            areas = [
                a for a in spawn_areas
                if a.currentItems < a.maxItems and len(a.freeSpots) != 0
                and (len(a.allowedItemNames) == 0 or name_in(item.itemName, a.allowedItemNames))
            ]
            shuffle(areas, step, f"{context} PlaceFreeArea({item.itemName}) areaShuffle")
            for a in areas:
                free = [s for s in a.freeSpots if s.name not in a.usedSpots]
                if free:
                    idx = rnd_next_int(len(free), step,
                                        f"{context} PlaceFreeArea({item.itemName}) spotPick@{a.areaName}")
                    spot = free[idx]
                    a.currentItems += 1
                    a.usedSpots.add(spot.name)
                    result[item.itemName] = f"FREE:{a.areaName}/{spot.name}"
                    return True
            # fallback: linear scan over the ORIGINAL (unshuffled) spawnAreas order
            for a in spawn_areas:
                if not (a.currentItems < a.maxItems):
                    continue
                free = [s for s in a.freeSpots if s.name not in a.usedSpots]
                if free:
                    idx = rnd_next_int(len(free), step,
                                        f"{context} PlaceFreeArea({item.itemName}) FALLBACK@{a.areaName}")
                    spot = free[idx]
                    a.currentItems += 1
                    a.usedSpots.add(spot.name)
                    result[item.itemName] = f"FREE:{a.areaName}/{spot.name}"
                    return True
            return False

        def place_item_in_specific_free_area(item, area, step, context):
            free = [s for s in area.freeSpots if s.name not in area.usedSpots]
            if not free:
                return False
            idx = rnd_next_int(len(free), step, context)
            spot = free[idx]
            area.currentItems += 1
            area.usedSpots.add(spot.name)
            result[item.itemName] = f"FREE:{area.areaName}/{spot.name}"
            return True

        # --- Step A: greedy puzzle<-item assignment ---
        candidate_pools = {}
        for p in ordered_puzzles:
            pool = [
                i for i in valid_items_all
                if not is_used(i.itemName)
                and is_item_allowed_for_puzzle(i, p)
                and category_ok(i)
                and is_safe_container_placement(i, p, puzzle_defs)
            ]
            candidate_pools[p.puzzleName] = pool

        assigned_item = {}
        for p in ordered_puzzles:
            pool = candidate_pools[p.puzzleName]
            assigned_item[p.puzzleName] = None
            if pool:
                idx = rnd_next_int(len(pool), "A", f"assign puzzle={p.puzzleName} poolSize={len(pool)}")
                chosen = pool[idx]
                assigned_item[p.puzzleName] = chosen
                for other_name, other_pool in candidate_pools.items():
                    if other_name != p.puzzleName and chosen in other_pool:
                        other_pool.remove(chosen)

        # --- Step B: required escape items ---
        assigned_names = {_norm_name(v.itemName) for v in assigned_item.values() if v is not None}
        for name in required_escape:
            item = items_by_name.get(_norm_name(name))
            if item is None:
                continue
            if _norm_name(item.itemName) in assigned_names:
                continue
            roll = rnd_next_double("B", f"escapeRoll item={name}")
            if escape_chance <= roll:
                place_item_in_free_area(item, "B", f"escapeFree item={name}")
                mark_used(name)
            else:
                if len(puzzle_defs) == 0:
                    place_item_in_free_area(item, "B", f"escapeFree(nopuzzles) item={name}")
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
                        idx = rnd_next_int(len(eligible), "B",
                                            f"escapePuzzlePick item={name} n={len(eligible)}")
                        p = eligible[idx]
                        result[item.itemName] = f"PUZZLE:{p.puzzleName}"
                        used_puzzle_spawns.add(p.spawnPointId)
                        mark_used(name)
                        puzzle_has_item[p.puzzleName] = True
                    else:
                        place_item_in_free_area(item, "B", f"escapeFree(noeligible) item={name}")
                        mark_used(name)

        # --- Step C: commit Step-A assignments ---
        for p in ordered_puzzles:
            if p.spawnPointId not in used_puzzle_spawns and assigned_item.get(p.puzzleName) is not None:
                item = assigned_item[p.puzzleName]
                if is_used(item.itemName):
                    assigned_item[p.puzzleName] = None
                else:
                    result[item.itemName] = f"PUZZLE:{p.puzzleName}"
                    used_puzzle_spawns.add(p.spawnPointId)
                    mark_used(item.itemName)
                    puzzle_has_item[p.puzzleName] = True

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
                    pool = [
                        i for i in valid_items_all
                        if not is_used(i.itemName) and category_ok(i)
                    ]
                if pool:
                    idx = rnd_next_int(len(pool), "D", f"backfill puzzle={p.puzzleName} n={len(pool)}")
                    item = pool[idx]
                    result[item.itemName] = f"PUZZLE:{p.puzzleName}"
                    used_puzzle_spawns.add(p.spawnPointId)
                    mark_used(item.itemName)
                    puzzle_has_item[p.puzzleName] = True

        # --- Step E: mandatory-item spawn areas (dead in this build) ---
        for a in spawn_areas:
            if a.mandatoryItemName:
                item = items_by_name.get(_norm_name(a.mandatoryItemName))
                if item is not None and not is_used(a.mandatoryItemName):
                    place_item_in_specific_free_area(
                        item, a, "E", f"mandatory area={a.areaName} item={a.mandatoryItemName}")
                    mark_used(a.mandatoryItemName)

        # --- Step F: fill remaining free spawns ---
        if fill_all:
            remaining = [i for i in valid_items_all if not is_used(i.itemName)]
            shuffle(remaining, "F", "remainingShuffle")
            for item in remaining:
                if place_item_in_free_area(item, "F", f"fill item={item.itemName}"):
                    mark_used(item.itemName)

        # --- Acceptance: !DetectCircularDependencies() && ValidatePuzzleDependencies() ---
        # VERIFIED (re_predicates.md S4/S5): both are pure bookkeeping, zero
        # RNG; their boolean result only decides which `attempt`'s RNG stream
        # is the one that wins, never a given attempt's own RNG sequence.
        def _position_of(item_name):
            """Live position of `item_name` after this attempt's placement,
            for GetEffectivePuzzleOfItem's spatial check. Falls back to the
            item's original scene startPosition if it wasn't placed this
            attempt (INFERRED: the report does not describe this case, since
            an unplaced item implies the attempt already failed by other
            means; only matters for attempts that don't fully succeed)."""
            item = items_by_name.get(_norm_name(item_name))
            if item is None:
                return None
            label = result.get(item.itemName)
            if label is None:
                return item.position
            if label.startswith("PUZZLE:"):
                return puzzle_pos_by_name.get(_norm_name(label[len("PUZZLE:"):]))
            rest = label[len("FREE:"):]
            area_name, spot_name = rest.split("/", 1)
            return area_spot_pos.get((area_name, spot_name))

        resolve_effective_puzzle = make_effective_puzzle_resolver(
            items_by_name, valid_items_all, puzzle_defs, _position_of)
        success = (
            not detect_circular_dependencies(items_by_name, puzzle_defs, resolve_effective_puzzle)
            and validate_puzzle_dependencies(items_by_name, puzzle_defs, required_escape,
                                              resolve_effective_puzzle)
        )

    return {
        "result": result,
        "attempt": attempt,
        "success": success,
        "trace": trace,
    }


def simulate(seed: int, config: dict, max_attempts: int = 50) -> dict:
    """Convenience wrapper returning just {itemName: slot_label}."""
    return simulate_verbose(seed, config, max_attempts=max_attempts)["result"]


if __name__ == "__main__":
    from pathlib import Path
    import glob

    dumps_dir = Path(__file__).parent / "dumps"
    config_files = sorted(dumps_dir.glob("config_*.json"))
    cfg_path = str(config_files[-1])
    print(f"Using config: {cfg_path}")
    cfg = load_config(cfg_path)

    seed = cfg["gameSeed"]
    print(f"Simulating seed={seed}")
    out = simulate_verbose(seed, cfg)
    print(f"attempt={out['attempt']} success={out['success']} items placed={len(out['result'])}")
    for k, v in sorted(out["result"].items()):
        print(f"  {k:20} -> {v}")
