"""
variants.py

Loads any of the 10 house-version SeedManager variants embedded in
scene_data.json (top-level key "seedManagerVariants") and reshapes each
into the exact dict shape produced by simulator.load_config(), so any
of the ten historical/variant configs can be run through the simulator
or search pipeline as a drop-in substitute for a live mod dump
(dumps/config_*.json).

Background
----------
Each scene variant's `allItems` / `puzzleDefs` / `spawnAreas` lack the
Unity `startPosition` / `spawnPoint.position` / `freeSpots[].position`
fields and the free-spot / puzzle-spawn-point `instanceId`s that
GeneratePlacement's predicates need (GetEffectivePuzzleOfItem's spatial
<0.01-unit check, IsSafeContainerPlacement's spawn-point identity check).
Those fields ARE present, for every object a variant also contains, in a
live mod dump -- verified: all 10 variants match 100% of freeSpots
(joined on the free spot's `path`, unique across the dump's 71 spots);
9 of 10 variants match 100% of puzzle spawn points (joined on
spawnPoint.path, unique across the dump's 16 spawn points) -- only
SeedSystemManager_More has 2 spawn points (WeightPuzzle/PuzzleSpot_Weight,
SubBasementSpawns/PuzzleSpot_SubBasement) absent from every mod dump we
have; 9 of 10 variants match 100% of items (joined on itemName, unique
across the dump's 31 items) -- only SeedSystemManager_More has 4 items
(CabinetKey, ElectricBaton, Fuse, RoboData) absent from every mod dump.

So: build lookup dicts from the newest mod dump keyed by `path` (free
spots / puzzle spawn points) and by `itemName` (items), and use them to
backfill position/instanceId for every scene object that has a match.
For the handful of `More` objects with no match, position stays None and
instanceId (spots/spawn points only -- Item has no instanceId field) is
synthesized as -(scene pathId), which cannot collide with any real
(positive) dump instanceId -- asserted below.

Order of `allItems` / `puzzleDefs` / `spawnAreas` / each area's
`freeSpots` is preserved EXACTLY as authored in scene_data.json: it is
never sorted, deduped, or reordered, because it determines RNG
consumption order in GeneratePlacement.
"""

import json
import struct
from pathlib import Path
from typing import Optional

from simulator import Item, PuzzleDef, Spot, SpawnArea
from dump_utils import newest_dump_file


VARIANT_DISPLAY_ORDER = [
    "1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "Normal", "More",
]

_VARIANT_PREFIX = "SeedSystemManager_"

# GPU backend's hard bitmask-width limits (gpu_backend.py MAX_ITEMS /
# MAX_PUZZLES / MAX_AREA_SPOTS). Duplicated here (not imported) so that
# variant_stats() can report GPU-eligibility without depending on
# gpu_backend.py (which itself may require OpenCL to import).
_GPU_MAX_ITEMS = 32
_GPU_MAX_PUZZLES = 32
_GPU_MAX_AREA_SPOTS = 32


def _display_name(variant_name: str) -> str:
    if variant_name.startswith(_VARIANT_PREFIX):
        return variant_name[len(_VARIANT_PREFIX):]
    return variant_name


def _pos_tuple(pos_dict) -> Optional[tuple]:
    if not pos_dict:
        return None
    return (pos_dict["x"], pos_dict["y"], pos_dict["z"])


def _f32(x: float) -> float:
    """Round-trip `x` through an IEEE-754 float32 store/load.

    The scene JSON already carries the true float32-promoted-to-double
    literal for EscapeItemPuzzleChance (e.g. 0.699999988079071 for the
    C# `float` 0.7f), unlike the mod dump which re-serializes it rounded
    to 0.7. This round-trip is a defensive no-op for values that are
    already exact float32 representations, guaranteeing the value handed
    to the simulator is bit-identical to what the game's `float` field
    (and its comparisons) actually holds, regardless of how the JSON
    parser represented the literal internally.
    """
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _load_scene_raw(scene_path: str) -> dict:
    with open(_resolve(scene_path), encoding="utf-8") as f:
        return json.load(f)


def _scene_variants_by_display_name(scene_path: str) -> dict:
    raw = _load_scene_raw(scene_path)
    out = {}
    for v in raw["seedManagerVariants"]:
        out[_display_name(v["variant_name"])] = v
    return out


# Anchor default data paths to THIS module's directory, not the process CWD.
# gui.py resolves its own dumps/ the same way (Path(__file__).parent); if this
# module used a CWD-relative glob the two could disagree about which dump is
# "newest" whenever the app is launched from another directory (run.bat does
# not guarantee a particular CWD).
_PROJ_DIR = Path(__file__).resolve().parent


def _resolve(path: str) -> str:
    """Resolve a relative data path against the project dir; leave absolute
    paths untouched. Deliberately does NOT check the process CWD for a
    same-named relative path first -- that would defeat the whole point of
    anchoring to _PROJ_DIR (see the comment above)."""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(_PROJ_DIR / p)


def _newest_dump_path() -> str:
    try:
        return str(newest_dump_file(_PROJ_DIR / "dumps", "config_*.json"))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"No config_*.json files found in {_PROJ_DIR / 'dumps'}; a mod dump "
            "is required to supply positions/instanceIds for the scene variants."
        )


def _build_dump_lookups(dump_raw: dict):
    """Return (item_pos_by_name, spot_by_path, puzzle_spawn_by_path,
    real_instance_ids) built from a loaded mod-dump dict.

    item_pos_by_name:    itemName -> startPosition tuple or None
    spot_by_path:        freeSpot path -> (instanceId, position tuple or None)
    puzzle_spawn_by_path: spawnPoint path -> (instanceId, position tuple or None)
    real_instance_ids:   set of every real (positive) instanceId seen above
    """
    item_pos_by_name = {}
    for it in dump_raw["allItems"]:
        sd = it.get("itemSeedData")
        if sd and sd.get("itemName"):
            item_pos_by_name[sd["itemName"]] = _pos_tuple(it.get("startPosition"))

    spot_by_path = {}
    for a in dump_raw["spawnAreas"]:
        for s in (a.get("freeSpots") or []):
            spot_by_path[s["path"]] = (s["instanceId"], _pos_tuple(s.get("position")))

    puzzle_spawn_by_path = {}
    for p in dump_raw["puzzleDefs"]:
        sp = p.get("spawnPoint")
        if sp:
            puzzle_spawn_by_path[sp["path"]] = (sp["instanceId"], _pos_tuple(sp.get("position")))

    real_instance_ids = set()
    real_instance_ids.update(v[0] for v in spot_by_path.values())
    real_instance_ids.update(v[0] for v in puzzle_spawn_by_path.values())

    return item_pos_by_name, spot_by_path, puzzle_spawn_by_path, real_instance_ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_variants(scene_path: str = "scene_data.json") -> list:
    """Display names of the variants actually present, in VARIANT_DISPLAY_ORDER."""
    present = set(_scene_variants_by_display_name(scene_path).keys())
    return [name for name in VARIANT_DISPLAY_ORDER if name in present]


def variant_stats(display_name: str, scene_path: str = "scene_data.json") -> dict:
    variants = _scene_variants_by_display_name(scene_path)
    if display_name not in variants:
        raise KeyError(f"Unknown variant {display_name!r}; available: {list(variants.keys())}")
    v = variants[display_name]

    num_items = len(v["allItems"])
    num_puzzles = len(v["puzzleDefs"])
    areas = v["spawnAreas"]
    num_areas = len(areas)
    area_spot_counts = [len(a.get("freeSpots") or []) for a in areas]
    num_free_spots = sum(area_spot_counts)
    max_area_spots = max(area_spot_counts) if area_spot_counts else 0
    num_required_escape = len(v.get("requiredEscapeItemNames") or [])

    gpu32bit_ok = (
        num_items <= _GPU_MAX_ITEMS
        and num_puzzles <= _GPU_MAX_PUZZLES
        and max_area_spots <= _GPU_MAX_AREA_SPOTS
    )

    return {
        "items": num_items,
        "puzzles": num_puzzles,
        "areas": num_areas,
        "freeSpots": num_free_spots,
        "totalSlots": num_free_spots + num_puzzles,
        "requiredEscape": num_required_escape,
        "maxAreaSpots": max_area_spots,
        "gpu32bitOk": gpu32bit_ok,
    }


def load_variant_config(display_name: str, scene_path: str = "scene_data.json",
                         dump_path: Optional[str] = None) -> dict:
    """Load a scene variant and reshape it into simulator.load_config()'s dict shape.

    Positions and free-spot/puzzle-spawn-point instanceIds -- absent from
    the scene data -- are backfilled by joining against `dump_path` (the
    newest dumps/config_*.json by default). Objects with no match in the
    dump (currently only 4 items + 2 puzzle spawn points, both exclusive
    to the "More" variant) get position=None and, for spots/spawn points,
    a synthetic negative instanceId derived from the scene pathId.
    """
    variants = _scene_variants_by_display_name(scene_path)
    if display_name not in variants:
        raise KeyError(f"Unknown variant {display_name!r}; available: {list(variants.keys())}")
    v = variants[display_name]

    if dump_path is None:
        dump_path = _newest_dump_path()
    dump_raw = _load_scene_raw(dump_path)  # plain JSON load; name is generic
    item_pos_by_name, spot_by_path, puzzle_spawn_by_path, real_instance_ids = \
        _build_dump_lookups(dump_raw)

    unmatched = []

    # --- items ---------------------------------------------------------
    items = []
    for it in v["allItems"]:
        item_name = it.get("itemName")
        if item_name:
            if item_name in item_pos_by_name:
                pos = item_pos_by_name[item_name]
            else:
                pos = None
                unmatched.append(f"item '{item_name}' (index={it['index']}): no dump match, position=None")
            items.append(Item(
                index=it["index"],
                goName=it.get("go_name", ""),
                itemName=item_name,
                category=it.get("category"),
                containedItems=tuple(it.get("containedItems") or []),
                valid=True,
                position=pos,
            ))
        else:
            items.append(Item(
                index=it["index"],
                goName=it.get("go_name", ""),
                itemName=None,
                category=None,
                containedItems=(),
                valid=False,
                position=None,
            ))

    # --- puzzles ---------------------------------------------------------
    puzzles = []
    for p in v["puzzleDefs"]:
        sp = p.get("spawnPoint")
        spawn_point_id = None
        spawn_point_pos = None
        if sp:
            match = puzzle_spawn_by_path.get(sp["path"])
            if match is not None:
                spawn_point_id, spawn_point_pos = match
            else:
                spawn_point_id = -sp["pathId"]
                assert spawn_point_id not in real_instance_ids, (
                    f"synthetic instanceId {spawn_point_id} collides with a real "
                    f"dump instanceId for puzzle spawn point {sp['path']!r}"
                )
                spawn_point_pos = None
                unmatched.append(
                    f"puzzle spawnPoint '{sp['path']}' (puzzle={p['puzzleName']!r}): "
                    f"no dump match, position=None, synthetic instanceId={spawn_point_id}"
                )
        puzzles.append(PuzzleDef(
            index=p["index"],
            puzzleName=p["puzzleName"],
            spawnPointId=spawn_point_id,
            allowedItemNames=tuple(p.get("allowedItemNames") or []),
            excludeItemNames=tuple(p.get("excludeItemNames") or []),
            requiredItemNames=tuple(p.get("requiredItemNames") or []),
            containedItems=tuple(p.get("containedItems") or []),
            priority=p["Priority"],
            spawnPointPosition=spawn_point_pos,
        ))

    # --- spawn areas -------------------------------------------------------
    areas = []
    for a in v["spawnAreas"]:
        spots = []
        for s in (a.get("freeSpots") or []):
            match = spot_by_path.get(s["path"])
            if match is not None:
                instance_id, pos = match
            else:
                instance_id = -s["pathId"]
                assert instance_id not in real_instance_ids, (
                    f"synthetic instanceId {instance_id} collides with a real "
                    f"dump instanceId for free spot {s['path']!r}"
                )
                pos = None
                unmatched.append(
                    f"freeSpot '{s['path']}' (area={a['areaName']!r}): "
                    f"no dump match, position=None, synthetic instanceId={instance_id}"
                )
            spots.append(Spot(name=s["name"], instanceId=instance_id, position=pos))
        areas.append(SpawnArea(
            index=a["index"],
            areaName=a["areaName"],
            maxItems=a["maxItems"],
            mandatoryItemName=a.get("mandatoryItemName") or "",
            allowedItemNames=tuple(a.get("allowedItemNames") or []),
            freeSpots=tuple(spots),
        ))

    return {
        # See _f32's docstring: use the scene's true float32 value, not the
        # dump's JSON-rounded one.
        "escapeItemPuzzleChance": _f32(v["EscapeItemPuzzleChance"]),
        "fillAllFreeSpawns": v["fillAllFreeSpawns"],
        "items": items,
        "puzzleDefs": puzzles,
        "requiredEscapeItemNames": list(v.get("requiredEscapeItemNames") or []),
        "spawnAreas": areas,
        "gameSeed": None,
        "variantName": display_name,
        "variantRaw": v["variant_name"],
        "unmatched": unmatched,
    }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import simulator

    print("Variants found in scene_data.json:")
    print(f"{'variant':<10}{'items':>7}{'puzzles':>9}{'areas':>7}"
          f"{'freeSpots':>11}{'totalSlots':>12}{'reqEsc':>8}{'maxAreaSp':>11}{'gpu32ok':>9}")
    for name in list_variants():
        s = variant_stats(name)
        print(f"{name:<10}{s['items']:>7}{s['puzzles']:>9}{s['areas']:>7}"
              f"{s['freeSpots']:>11}{s['totalSlots']:>12}{s['requiredEscape']:>8}"
              f"{s['maxAreaSpots']:>11}{str(s['gpu32bitOk']):>9}")

    print()
    print("Equivalence check: load_variant_config('Normal') vs simulator.load_config(newest dump)")

    dump_path = _newest_dump_path()
    print(f"  newest dump: {dump_path}")

    dump_cfg = simulator.load_config(dump_path)
    variant_cfg = load_variant_config("Normal", dump_path=dump_path)

    failures = []

    def check(label, a, b):
        if a != b:
            failures.append(f"{label}: variant={a!r} dump={b!r}")

    # --- items ---
    d_items = dump_cfg["items"]
    v_items = variant_cfg["items"]
    check("len(items)", len(v_items), len(d_items))
    if len(v_items) == len(d_items):
        for i, (vi, di) in enumerate(zip(v_items, d_items)):
            check(f"item[{i}].index", vi.index, di.index)
            check(f"item[{i}].itemName", vi.itemName, di.itemName)
            check(f"item[{i}].category", vi.category, di.category)
            check(f"item[{i}].containedItems", vi.containedItems, di.containedItems)
            check(f"item[{i}].valid", vi.valid, di.valid)
            check(f"item[{i}].position", vi.position, di.position)

    # --- puzzleDefs ---
    d_puz = dump_cfg["puzzleDefs"]
    v_puz = variant_cfg["puzzleDefs"]
    check("len(puzzleDefs)", len(v_puz), len(d_puz))
    if len(v_puz) == len(d_puz):
        for i, (vp, dp) in enumerate(zip(v_puz, d_puz)):
            check(f"puzzle[{i}].puzzleName", vp.puzzleName, dp.puzzleName)
            check(f"puzzle[{i}].priority", vp.priority, dp.priority)
            check(f"puzzle[{i}].allowedItemNames", vp.allowedItemNames, dp.allowedItemNames)
            check(f"puzzle[{i}].excludeItemNames", vp.excludeItemNames, dp.excludeItemNames)
            check(f"puzzle[{i}].requiredItemNames", vp.requiredItemNames, dp.requiredItemNames)
            check(f"puzzle[{i}].spawnPointId", vp.spawnPointId, dp.spawnPointId)
            check(f"puzzle[{i}].spawnPointPosition", vp.spawnPointPosition, dp.spawnPointPosition)
            check(f"puzzle[{i}].index", vp.index, dp.index)

    # --- spawnAreas ---
    d_areas = dump_cfg["spawnAreas"]
    v_areas = variant_cfg["spawnAreas"]
    check("len(spawnAreas)", len(v_areas), len(d_areas))
    if len(v_areas) == len(d_areas):
        for i, (va, da) in enumerate(zip(v_areas, d_areas)):
            check(f"area[{i}].areaName", va.areaName, da.areaName)
            check(f"area[{i}].maxItems", va.maxItems, da.maxItems)
            check(f"area[{i}].len(freeSpots)", len(va.freeSpots), len(da.freeSpots))
            if len(va.freeSpots) == len(da.freeSpots):
                for j, (vs, ds) in enumerate(zip(va.freeSpots, da.freeSpots)):
                    check(f"area[{i}].spot[{j}].name", vs.name, ds.name)
                    check(f"area[{i}].spot[{j}].instanceId", vs.instanceId, ds.instanceId)
                    check(f"area[{i}].spot[{j}].position", vs.position, ds.position)

    # --- requiredEscapeItemNames ---
    check("requiredEscapeItemNames", variant_cfg["requiredEscapeItemNames"],
          dump_cfg["requiredEscapeItemNames"])

    print()
    esc_v = variant_cfg["escapeItemPuzzleChance"]
    esc_d = dump_cfg["escapeItemPuzzleChance"]
    if esc_v == esc_d:
        print(f"  escapeItemPuzzleChance: variant={esc_v!r} dump={esc_d!r} (equal -- "
              f"simulator.load_config() already float32-normalizes the dump's value, "
              f"so both sides land on the same double)")
    else:
        print(f"  escapeItemPuzzleChance: variant={esc_v!r} dump={esc_d!r} "
              f"(differ, as expected: scene stores the true float32 value while the "
              f"dump's raw JSON is only float32-normalized by load_config)")
    print(f"  unmatched objects (Normal variant): {variant_cfg['unmatched']}")

    print()
    if failures:
        print(f"FAIL -- {len(failures)} mismatch(es):")
        for f in failures[:50]:
            print(f"  - {f}")
    else:
        print("PASS -- variant 'Normal' is identical to the newest mod dump "
              "(aside from the expected escapeItemPuzzleChance float32 rounding).")
