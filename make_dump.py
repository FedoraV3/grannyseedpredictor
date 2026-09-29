"""
make_dump.py

Builds a config dump (dumps/config_*.json) straight from the game files, so
no mod or game run is needed. Reads Granny Legacy's `level1` scene with
UnityPy, parses the SeedManager exactly like extract_scene.py does, and
fills in what the scene data alone lacks:

  - world positions of every item, free spot and puzzle spawn point,
    computed from the scene's Transform hierarchy (local position /
    rotation / scale composed up to the root, as Unity does);
  - instanceIds. Unity assigns those at runtime, so they cannot be read
    from the files. The simulator only compares them for identity, so
    -(scene pathId) is used instead: unique per object within the scene.

The dump is written for the "More" variant by default: its objects are a
superset of every other variant's (all 124 items / spots / spawn points),
so variants.py finds a real position for every object of every variant.

USAGE
-----
    python make_dump.py                      (opens a file selector)
    python make_dump.py "D:\\Games\\Granny Legacy"
    python make_dump.py "<...>\\Granny Legacy_Data\\level1" --variant Normal

Needs UnityPy (`python -m pip install UnityPy`).
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

try:
    import UnityPy
except ImportError:
    sys.exit("UnityPy is not installed. Run:  python -m pip install UnityPy")

from extract_scene import SceneResolver, build_seed_manager_entry, find_seed_managers

LEVEL_FILE = "level1"
PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
TRANSFORM_TYPES = ("Transform", "RectTransform")


# ---------------------------------------------------------------------------
# Locating the game
# ---------------------------------------------------------------------------

def _pick_file() -> str:
    """Ask for the game's .exe or its level1 file with a file dialog."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        parent=root,
        title="Select Granny Legacy's .exe (or level1 in its _Data folder)",
        filetypes=[("Game or level1", "*.exe level1"), ("All files", "*.*")],
    )
    root.destroy()
    if not path:
        sys.exit("No file selected.")
    return path


def find_level_file(arg) -> str:
    """Resolve the game .exe, game folder, *_Data folder or level1 path;
    with no argument, ask for it with a file dialog."""
    path = arg or _pick_file()
    if os.path.isfile(path) and path.lower().endswith(".exe"):
        path = os.path.dirname(path)
    if os.path.isfile(path):
        return path
    direct = os.path.join(path, LEVEL_FILE)
    if os.path.isfile(direct):
        return direct
    for data_dir in glob.glob(os.path.join(path, "*_Data")):
        level = os.path.join(data_dir, LEVEL_FILE)
        if os.path.isfile(level):
            return level
    sys.exit(f"Could not find {LEVEL_FILE} for {path!r}. Select the game's .exe, "
             f"or the level1 file inside its '..._Data' folder.")


# ---------------------------------------------------------------------------
# World positions
# ---------------------------------------------------------------------------

def _rotate(q, v):
    """Rotate vector v by unit quaternion q = (x, y, z, w)."""
    qx, qy, qz, qw = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v); v' = v + w*t + cross(q.xyz, t)
    tx = 2 * (qy * vz - qz * vy)
    ty = 2 * (qz * vx - qx * vz)
    tz = 2 * (qx * vy - qy * vx)
    return (vx + qw * tx + (qy * tz - qz * ty),
            vy + qw * ty + (qz * tx - qx * tz),
            vz + qw * tz + (qx * ty - qy * tx))


class WorldPositions:
    def __init__(self, resolver: SceneResolver):
        self.r = resolver
        self._cache = {}

    def _transform(self, path_id):
        obj = self.r._obj(path_id)
        if obj is None or obj.type.name not in TRANSFORM_TYPES:
            return None
        return obj.read()

    def of_transform(self, path_id):
        """World position of a Transform: its local position pushed through
        each ancestor's scale, rotation and translation up to the root."""
        if path_id in self._cache:
            return self._cache[path_id]
        t = self._transform(path_id)
        if t is None:
            return None
        lp = t.m_LocalPosition
        pos = (lp.x, lp.y, lp.z)
        cur = t.m_Father.m_PathID
        depth = 0
        while cur != 0 and depth < 256:
            p = self._transform(cur)
            if p is None:
                return None
            s, q, pp = p.m_LocalScale, p.m_LocalRotation, p.m_LocalPosition
            pos = _rotate((q.x, q.y, q.z, q.w), (pos[0] * s.x, pos[1] * s.y, pos[2] * s.z))
            pos = (pos[0] + pp.x, pos[1] + pp.y, pos[2] + pp.z)
            cur = p.m_Father.m_PathID
            depth += 1
        self._cache[path_id] = pos
        return pos

    def of_gameobject(self, go_path_id):
        obj = self.r._obj(go_path_id)
        if obj is None or obj.type.name != "GameObject":
            return None
        for pair in obj.read().m_Component:
            comp = self.r._obj(pair.component.m_PathID)
            if comp is not None and comp.type.name in TRANSFORM_TYPES:
                return self.of_transform(comp.path_id)
        return None


def _vec(pos):
    return None if pos is None else {"x": pos[0], "y": pos[1], "z": pos[2]}


# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------

def _transform_ref(ref, world: WorldPositions):
    return {
        "name": ref["name"],
        "path": ref["path"],
        "instanceId": -ref["pathId"],
        "position": _vec(world.of_transform(ref["pathId"])),
    }


def build_dump(entry: dict, world: WorldPositions) -> dict:
    """Reshape an extract_scene SeedManager entry into the mod's config dump
    format (the fields simulator.load_config / variants.py read)."""
    items = []
    for it in entry["allItems"]:
        has_data = bool(it.get("itemName"))
        items.append({
            "index": it["index"],
            "goName": it.get("go_name") or "",
            "startPosition": _vec(world.of_gameobject(it["pathId"])),
            "itemSeedData": {
                "itemName": it["itemName"],
                "category": it["category"],
                "containedItems": it.get("containedItems") or [],
            } if has_data else None,
        })

    puzzles = []
    for p in entry["puzzleDefs"]:
        sp = p.get("spawnPoint")
        puzzles.append({
            "index": p["index"],
            "puzzleName": p["puzzleName"],
            "spawnPoint": _transform_ref(sp, world) if sp and sp.get("pathId") else None,
            "allowedItemNames": p.get("allowedItemNames") or [],
            "excludeItemNames": p.get("excludeItemNames") or [],
            "priority": p["Priority"],
            "requiredItemNames": p.get("requiredItemNames") or [],
            "containedItems": p.get("containedItems") or [],
        })

    areas = []
    for a in entry["spawnAreas"]:
        areas.append({
            "index": a["index"],
            "areaName": a["areaName"],
            "maxItems": a["maxItems"],
            "mandatoryItemName": a.get("mandatoryItemName") or "",
            "allowedItemNames": a.get("allowedItemNames") or [],
            "freeSpots": [_transform_ref(s, world) for s in (a.get("freeSpots") or [])],
        })

    now = datetime.now(timezone.utc)
    return {
        "dumpType": "config",
        "generatedBy": "make_dump.py (from game files, not a live capture)",
        "variantName": entry["variant_name"],
        "capturedAtUtc": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}0Z",
        "callIndex": 0,
        "playerPrefs": {},
        "randomizeSeed": entry["RandomizeSeed"],
        "escapeItemPuzzleChance": entry["EscapeItemPuzzleChance"],
        "fillAllFreeSpawns": entry["fillAllFreeSpawns"],
        "requiredEscapeItemNames": entry["requiredEscapeItemNames"],
        "allItems": items,
        "puzzleDefs": puzzles,
        "spawnAreas": areas,
    }


def main():
    ap = argparse.ArgumentParser(description="Build a config dump from the game files.")
    ap.add_argument("game_path", nargs="?",
                    help="game .exe or folder, its _Data folder, or the level1 file (default: file selector)")
    ap.add_argument("--variant", default="More",
                    help="SeedManager variant to dump (default: More, which covers every object)")
    ap.add_argument("--out-dir", default=os.path.join(PROJ_DIR, "dumps"))
    args = ap.parse_args()

    level_path = find_level_file(args.game_path)
    print(f"Loading {level_path} ...")
    env = UnityPy.load(level_path)
    resolver = SceneResolver(env)
    world = WorldPositions(resolver)

    wanted = "SeedSystemManager_" + args.variant
    managers = find_seed_managers(env)
    entry = None
    for obj in managers:
        e = build_seed_manager_entry(obj, resolver, os.path.basename(level_path))
        if e["variant_name"] in (wanted, args.variant):
            entry = e
            break
    if entry is None:
        sys.exit(f"Variant {args.variant!r} not found in {level_path}")

    dump = build_dump(entry, world)
    missing = (
        [f"item {i['goName']}" for i in dump["allItems"] if i["startPosition"] is None]
        + [f"spawn point {p['spawnPoint']['path']}" for p in dump["puzzleDefs"]
           if p["spawnPoint"] and p["spawnPoint"]["position"] is None]
        + [f"spot {s['path']}" for a in dump["spawnAreas"] for s in a["freeSpots"]
           if s["position"] is None]
    )
    for m in missing:
        print(f"  warning: no position for {m}")

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_") + f"{datetime.now().microsecond // 1000:03d}"
    out = os.path.join(args.out_dir, f"config_0_{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out}  ({entry['variant_name']}: {len(dump['allItems'])} items, "
          f"{len(dump['puzzleDefs'])} puzzles, {len(dump['spawnAreas'])} areas)")


if __name__ == "__main__":
    main()
