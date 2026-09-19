"""
extract_scene.py

Extracts the serialized Unity scene data for every `SeedManager` MonoBehaviour
found in Granny Legacy's level1 scene file, plus the `ItemSeedData` component
attached to every item GameObject it references, and writes the result to
`scene_data.json`.

BACKGROUND
----------
Granny Legacy is an IL2CPP-built Unity game. IL2CPP builds do not ship a
TypeTree for user (non-engine) MonoBehaviour classes, so generic Unity
asset-parsing libraries (UnityPy included) cannot automatically deserialize
custom script fields the way they can for a Mono build. UnityPy CAN still
read:
  - the base MonoBehaviour header (m_GameObject, m_Enabled, m_Script, m_Name)
    via `ObjectReader.parse_monobehaviour_head()`, and
  - built-in engine types (GameObject, Transform, MonoScript, ...) which DO
    ship with a full TypeTree.

Everything after the MonoBehaviour header (i.e. every field declared on
SeedManager / PuzzleDef / SpawnArea / ItemSeedData themselves) has to be
parsed by hand from the raw object bytes, using Unity's well known binary
serialization rules:
  - PPtr<T>            = int32 m_FileID + int64 m_PathID           (12 bytes)
  - string              = int32 length + UTF8 bytes, then align to 4
  - List<T> / T[]       = int32 count + <count> elements (no extra alignment)
  - int / float         = 4 bytes little-endian
  - bool                = 1 byte, then align to 4
  - [Serializable] class = fields serialized in declaration order, no header

The exact field order for each class below was taken from the IL2CppDumper
dump (dump.cs) of this exact game build, and then EMPIRICALLY VALIDATED by
parsing every instance and requiring the parser to consume the object's raw
data down to the very last byte (see re_scene_data.md for the validation
methodology and one important discrepancy that was found and had to be
corrected: SpawnArea.allowedItemNames / SpawnArea.mandatoryItemName are
declared in the IL2Cpp dump but are NOT present in the actual shipped binary
layout of this build).

USAGE
-----
    python extract_scene.py

Reads (read-only) from:
    C:\\Users\\ir0n1c\\Desktop\\Granny_Legacy\\Granny Legacy_Data\\level1

Writes:
    C:\\Users\\ir0n1c\\grannyseedpredictor\\scene_data.json
"""

import json
import os
import struct
import sys

import UnityPy

DATA_DIR = r"C:\Users\ir0n1c\Desktop\Granny_Legacy\Granny Legacy_Data"
LEVEL_FILE = "level1"
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scene_data.json")


# ---------------------------------------------------------------------------
# Low-level binary reader implementing Unity's serialization primitives.
# ---------------------------------------------------------------------------
class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.off = 0

    def align4(self):
        pad = (-self.off) % 4
        self.off += pad

    def i32(self):
        v = struct.unpack_from("<i", self.data, self.off)[0]
        self.off += 4
        return v

    def i64(self):
        v = struct.unpack_from("<q", self.data, self.off)[0]
        self.off += 8
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.data, self.off)[0]
        self.off += 4
        return v

    def boolean(self):
        v = self.data[self.off]
        self.off += 1
        self.align4()
        return bool(v)

    def pptr(self):
        """Returns (fileID, pathID). fileID==0 means 'this same SerializedFile'."""
        file_id = self.i32()
        path_id = self.i64()
        return (file_id, path_id)

    def string(self):
        n = self.i32()
        if not (0 <= n <= 1_000_000):
            raise ValueError(f"insane string length {n} at offset {self.off - 4}")
        s = self.data[self.off:self.off + n].decode("utf-8")
        self.off += n
        self.align4()
        return s

    def list_of(self, elem_fn, max_count=100_000):
        n = self.i32()
        if not (0 <= n <= max_count):
            raise ValueError(f"insane list count {n} at offset {self.off - 4}")
        return [elem_fn() for _ in range(n)]

    def string_list(self):
        return self.list_of(self.string)

    def pptr_list(self):
        return self.list_of(self.pptr)


# ---------------------------------------------------------------------------
# Per-class field parsers (byte layout validated against dump.cs + empirical
# round-trip byte consumption checks across every instance in the scene).
# ---------------------------------------------------------------------------
def parse_forbidden_combo(r: Reader):
    itemName = r.string()
    forbiddenItem = r.string()
    forbiddenPuzzles = r.string_list()
    return {
        "itemName": itemName,
        "forbiddenItem": forbiddenItem,
        "forbiddenPuzzles": forbiddenPuzzles,
    }


def parse_puzzle_def(r: Reader):
    puzzleName = r.string()
    spawnPoint = r.pptr()
    allowedItemNames = r.string_list()
    excludeItemNames = r.string_list()
    priority = r.i32()
    requiredItemNames = r.string_list()
    forbiddenCombos = r.list_of(lambda: parse_forbidden_combo(r))
    containedItems = r.string_list()
    return {
        "puzzleName": puzzleName,
        "spawnPoint_pptr": spawnPoint,
        "allowedItemNames": allowedItemNames,
        "excludeItemNames": excludeItemNames,
        "Priority": priority,
        "requiredItemNames": requiredItemNames,
        "forbiddenCombos": forbiddenCombos,
        "containedItems": containedItems,
    }


def parse_spawn_area(r: Reader):
    # NOTE: dump.cs declares SpawnArea as:
    #   string areaName; Transform[] freeSpots; int maxItems; int currentItems;
    #   List<Transform> usedSpots; List<string> allowedItemNames; string mandatoryItemName;
    # Empirically, EVERY SpawnArea instance in this build's shipped binary
    # only has ONE trailing int32 field after currentItems (always observed
    # as 0), not three. The whole SeedManager object parses byte-perfectly
    # (offset == raw length, for all 10 variants) only under this corrected
    # layout. See re_scene_data.md for the full trace. allowedItemNames /
    # mandatoryItemName are therefore NOT present in the data and are
    # reported as null below rather than guessed.
    areaName = r.string()
    freeSpots = r.pptr_list()
    maxItems = r.i32()
    currentItems = r.i32()
    trailing_field = r.i32()  # believed to be usedSpots' count; always 0 observed
    return {
        "areaName": areaName,
        "freeSpots_pptr": freeSpots,
        "maxItems": maxItems,
        "currentItems": currentItems,
        "usedSpots_pptr": [],
        "allowedItemNames": None,
        "mandatoryItemName": None,
        "_trailing_field_raw": trailing_field,
    }


def parse_seed_manager_body(data: bytes, body_offset: int):
    r = Reader(data)
    r.off = body_offset
    allItems = r.pptr_list()
    puzzleDefs = r.list_of(lambda: parse_puzzle_def(r))
    requiredEscapeItemNames = r.string_list()
    spawnAreas = r.list_of(lambda: parse_spawn_area(r))
    seed = r.i32()
    randomizeSeed = r.boolean()
    activatePlacedItems = r.boolean()
    fillAllFreeSpawns = r.boolean()
    rigid = r.boolean()
    escapeItemPuzzleChance = r.f32()
    if r.off != len(data):
        raise ValueError(f"SeedManager parse did not consume all bytes: {r.off} != {len(data)}")
    return {
        "allItems_pptr": allItems,
        "puzzleDefs": puzzleDefs,
        "requiredEscapeItemNames": requiredEscapeItemNames,
        "spawnAreas": spawnAreas,
        "Seed": seed,
        "RandomizeSeed": randomizeSeed,
        "activatePlacedItems": activatePlacedItems,
        "fillAllFreeSpawns": fillAllFreeSpawns,
        "Rigid": rigid,
        "EscapeItemPuzzleChance": escapeItemPuzzleChance,
    }


def parse_item_seed_data(data: bytes, body_offset: int):
    r = Reader(data)
    r.off = body_offset
    itemName = r.string()
    category = r.i32()
    containedItems = r.string_list()
    if r.off != len(data):
        raise ValueError(f"ItemSeedData parse did not consume all bytes: {r.off} != {len(data)}")
    return {"itemName": itemName, "category": category, "containedItems": containedItems}


# ---------------------------------------------------------------------------
# Resolution helpers: PPtr -> human readable GameObject / Transform info.
# ---------------------------------------------------------------------------
class SceneResolver:
    def __init__(self, env):
        self.objs_by_pathid = {obj.path_id: obj for obj in env.objects}
        self._go_cache = {}
        self._transform_path_cache = {}
        self._item_seed_data_cache = {}

    def _obj(self, path_id):
        return self.objs_by_pathid.get(path_id)

    def gameobject_name(self, path_id):
        if path_id == 0:
            return None
        if path_id in self._go_cache:
            return self._go_cache[path_id]
        obj = self._obj(path_id)
        name = None
        if obj is not None and obj.type.name == "GameObject":
            try:
                name = obj.read().m_Name
            except Exception as e:
                name = f"<error reading GameObject {path_id}: {e}>"
        self._go_cache[path_id] = name
        return name

    def transform_full_path(self, path_id):
        """Given the pathID of a Transform object, walk m_Father up to the
        root and return the full hierarchy path, e.g. 'House/Kitchen/Drawer_01'.
        """
        if path_id == 0:
            return None
        if path_id in self._transform_path_cache:
            return self._transform_path_cache[path_id]

        names = []
        cur = path_id
        depth = 0
        while cur != 0 and depth < 64:
            obj = self._obj(cur)
            if obj is None or obj.type.name != "Transform":
                names.append(f"<missing transform {cur}>")
                break
            t = obj.read()
            go_name = self.gameobject_name(t.m_GameObject.m_PathID)
            names.append(go_name if go_name is not None else f"<unnamed GO for transform {cur}>")
            cur = t.m_Father.m_PathID
            depth += 1
        path = "/".join(reversed(names))
        self._transform_path_cache[path_id] = path
        return path

    def transform_info(self, pptr):
        """pptr = (fileID, pathID) pointing at a Transform. Returns dict with
        pathId, immediate GameObject name, and full hierarchy path."""
        file_id, path_id = pptr
        if path_id == 0:
            return {"pathId": 0, "name": None, "path": None}
        obj = self._obj(path_id)
        name = None
        if obj is not None and obj.type.name == "Transform":
            try:
                t = obj.read()
                name = self.gameobject_name(t.m_GameObject.m_PathID)
            except Exception as e:
                name = f"<error: {e}>"
        return {"pathId": path_id, "name": name, "path": self.transform_full_path(path_id)}

    def item_seed_data_for_gameobject(self, go_path_id):
        """Find the ItemSeedData MonoBehaviour (if any) attached to the given
        GameObject and parse its custom fields."""
        if go_path_id in self._item_seed_data_cache:
            return self._item_seed_data_cache[go_path_id]
        result = None
        obj = self._obj(go_path_id)
        if obj is not None and obj.type.name == "GameObject":
            try:
                go = obj.read()
                for comp_pair in go.m_Component:
                    comp_ptr = comp_pair.component
                    comp_obj = self._obj(comp_ptr.m_PathID)
                    if comp_obj is None or comp_obj.type.name != "MonoBehaviour":
                        continue
                    head = comp_obj.parse_monobehaviour_head()
                    try:
                        script = head.m_Script.read()
                    except Exception:
                        continue
                    if script.m_ClassName != "ItemSeedData":
                        continue
                    raw = comp_obj.get_raw_data()
                    result = parse_item_seed_data(raw, 32)
                    break
            except Exception as e:
                result = {"error": str(e)}
        self._item_seed_data_cache[go_path_id] = result
        return result

    def item_info(self, pptr):
        """pptr = (fileID, pathID) pointing at a GameObject that is (or
        should be) an item, i.e. carries an ItemSeedData component."""
        file_id, path_id = pptr
        go_name = self.gameobject_name(path_id)
        isd = self.item_seed_data_for_gameobject(path_id)
        entry = {"pathId": path_id, "go_name": go_name}
        if isd is not None and "error" not in isd:
            entry.update(isd)
        else:
            entry["itemName"] = None
            entry["category"] = None
            entry["containedItems"] = None
            if isd is not None:
                entry["_error"] = isd.get("error")
        return entry


# ---------------------------------------------------------------------------
# Top level: find every SeedManager MonoBehaviour, parse + resolve it.
# ---------------------------------------------------------------------------
def find_seed_managers(env):
    found = []
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            head = obj.parse_monobehaviour_head()
            script = head.m_Script.read()
        except Exception:
            continue
        if script.m_ClassName == "SeedManager":
            found.append(obj)
    found.sort(key=lambda o: o.path_id)
    return found


def build_seed_manager_entry(obj, resolver: SceneResolver, source_file: str):
    head = obj.parse_monobehaviour_head()
    go = head.m_GameObject.read()
    raw = obj.get_raw_data()
    parsed = parse_seed_manager_body(raw, 32)

    all_items = [resolver.item_info(p) for p in parsed["allItems_pptr"]]
    for i, entry in enumerate(all_items):
        entry["index"] = i

    puzzle_defs = []
    for i, pd in enumerate(parsed["puzzleDefs"]):
        puzzle_defs.append({
            "index": i,
            "puzzleName": pd["puzzleName"],
            "spawnPoint": resolver.transform_info(pd["spawnPoint_pptr"]),
            "allowedItemNames": pd["allowedItemNames"],
            "excludeItemNames": pd["excludeItemNames"],
            "Priority": pd["Priority"],
            "requiredItemNames": pd["requiredItemNames"],
            "forbiddenCombos": pd["forbiddenCombos"],
            "containedItems": pd["containedItems"],
        })

    spawn_areas = []
    for i, sa in enumerate(parsed["spawnAreas"]):
        spawn_areas.append({
            "index": i,
            "areaName": sa["areaName"],
            "freeSpots": [resolver.transform_info(p) for p in sa["freeSpots_pptr"]],
            "maxItems": sa["maxItems"],
            "currentItems": sa["currentItems"],
            "usedSpots": [resolver.transform_info(p) for p in sa["usedSpots_pptr"]],
            "allowedItemNames": sa["allowedItemNames"],
            "mandatoryItemName": sa["mandatoryItemName"],
        })

    return {
        "variant_name": go.m_Name,
        "source_file": source_file,
        "path_id": obj.path_id,
        "go_active_in_scene": bool(go.m_IsActive) if hasattr(go, "m_IsActive") else None,
        "Seed": parsed["Seed"],
        "RandomizeSeed": parsed["RandomizeSeed"],
        "activatePlacedItems": parsed["activatePlacedItems"],
        "fillAllFreeSpawns": parsed["fillAllFreeSpawns"],
        "Rigid": parsed["Rigid"],
        "EscapeItemPuzzleChance": parsed["EscapeItemPuzzleChance"],
        "allItems": all_items,
        "requiredEscapeItemNames": parsed["requiredEscapeItemNames"],
        "puzzleDefs": puzzle_defs,
        "spawnAreas": spawn_areas,
    }


def main():
    level_path = os.path.join(DATA_DIR, LEVEL_FILE)
    if not os.path.exists(level_path):
        print(f"ERROR: level file not found: {level_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {level_path} ...")
    env = UnityPy.load(level_path)

    print("Scanning MonoBehaviours for SeedManager ...")
    seed_manager_objs = find_seed_managers(env)
    print(f"Found {len(seed_manager_objs)} SeedManager instance(s).")

    resolver = SceneResolver(env)

    variants = []
    for obj in seed_manager_objs:
        entry = build_seed_manager_entry(obj, resolver, LEVEL_FILE)
        variants.append(entry)
        n_free_spots = sum(len(sa["freeSpots"]) for sa in entry["spawnAreas"])
        print(f"  {entry['variant_name']:35s} path_id={entry['path_id']:>6} "
              f"allItems={len(entry['allItems']):3d} puzzleDefs={len(entry['puzzleDefs']):3d} "
              f"spawnAreas={len(entry['spawnAreas']):3d} freeSpots_total={n_free_spots:3d}")

    default_variant = next((v for v in variants if v["variant_name"] == "SeedSystemManager_Normal"), variants[0])

    output = {
        "_notes": (
            "This scene contains MULTIPLE SeedManager instances (one inactive "
            "GameObject per house-version variant under GameManager/HandleObjects), "
            "not a single one. All variants are included under 'seedManagerVariants'. "
            "The top-level fields mirror 'SeedSystemManager_Normal' (the variant name "
            "most plausibly corresponding to standard/default gameplay) for "
            "convenience/schema-compatibility; verify this is the correct variant "
            "for your simulation before relying on the top-level fields alone. "
            "See re_scene_data.md for full detail, counts per variant, and the "
            "SpawnArea binary-layout discrepancy found during extraction."
        ),
        "source_file": default_variant["source_file"],
        "path_id": default_variant["path_id"],
        "variant_name": default_variant["variant_name"],
        "Seed": default_variant["Seed"],
        "RandomizeSeed": default_variant["RandomizeSeed"],
        "fillAllFreeSpawns": default_variant["fillAllFreeSpawns"],
        "Rigid": default_variant["Rigid"],
        "activatePlacedItems": default_variant["activatePlacedItems"],
        "EscapeItemPuzzleChance": default_variant["EscapeItemPuzzleChance"],
        "allItems": default_variant["allItems"],
        "requiredEscapeItemNames": default_variant["requiredEscapeItemNames"],
        "puzzleDefs": default_variant["puzzleDefs"],
        "spawnAreas": default_variant["spawnAreas"],
        "seedManagerVariants": variants,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
