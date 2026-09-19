# Granny Legacy `SeedManager` Scene Data Extraction — Report

## Location

- Game data root: `C:\Users\ir0n1c\Desktop\Granny_Legacy\Granny Legacy_Data\`
- File containing the scene data: **`level1`** (this is the main gameplay scene — it is by far
  the largest level file, with 3435 raw MonoBehaviours vs. 1180 in `level0`, 40 in `level2`).
- `SeedManager` MonoBehaviours were found at these path IDs inside `level1`:

| path_id | GameObject name             | allItems | puzzleDefs | spawnAreas | freeSpots total |
|--------:|------------------------------|---------:|-----------:|-----------:|-----------------:|
| 26563   | SeedSystemManager_1.5        | 25 | 9  | 10 | 62 |
| 27026   | **SeedSystemManager_Normal** | **31** | **16** | **12** | **71** |
| 27102   | SeedSystemManager_1.0        | 6  | 1  | 6  | 38 |
| 27118   | SeedSystemManager_1.6        | 26 | 10 | 10 | 62 |
| 28036   | SeedSystemManager_1.2        | 7  | 4  | 7  | 47 |
| 28041   | SeedSystemManager_1.7        | 27 | 13 | 11 | 66 |
| 28160   | SeedSystemManager_1.1        | 7  | 2  | 7  | 47 |
| 28189   | SeedSystemManager_1.3        | 13 | 7  | 8  | 51 |
| 28759   | SeedSystemManager_More       | 35 | 18 | 12 | 71 |
| 28938   | SeedSystemManager_1.4        | 17 | 8  | 9  | 59 |

**Important finding — there is not a single `SeedManager`, there are 10.** All 10 are children of
`GameManager/HandleObjects` and all 10 are on GameObjects with `m_IsActive = False` in the scene
file itself. `dump.cs` contains a `VersionControl` MonoBehaviour class with fields like
`DisableOneSixObjects`, `DisableOneFiveObjects`, ... and a `VersionControlItemChange` class with
`VersionPlayerPrefs1/2/3` fields, strongly suggesting the game supports multiple selectable
"house version" configurations (matching Granny's real-world versions 1.0–1.7, a "Normal" mode,
and a "More" [items] mode), and one of the ten `SeedSystemManager_*` GameObjects gets activated
at runtime based on a saved PlayerPrefs value. I could not find, in the scene data itself, which
one is activated by default (that logic lives in code, not in serialized data), so **I extracted
all 10 rather than guessing which one is "the" active one.**

`scene_data.json`'s top level mirrors `SeedSystemManager_Normal` (the variant whose name most
plausibly corresponds to standard/default gameplay) for convenience, and `seedManagerVariants`
contains the full data for all 10 variants, in path-ID order. **Verify against the game's actual
version-selection UI/PlayerPrefs which variant applies to your target playthrough before trusting
the top-level fields alone.**

## Counts (headline numbers, "Normal" variant)

- `len(allItems)` = **31**
- `len(puzzleDefs)` = **16**
- `len(spawnAreas)` = **12**
- total free-spot count = **71**

(See the table above for all 10 variants; `SeedSystemManager_More` is the largest with 35 items /
18 puzzles / 12 areas / 71 free spots, `SeedSystemManager_1.0` the smallest with 6 items / 1
puzzle / 6 areas / 38 free spots.)

All 10 `SeedManager` instances share the same global settings:
`Seed = 99999`, `RandomizeSeed = True`, `activatePlacedItems = True`, `fillAllFreeSpawns = True`,
`Rigid = False`, `EscapeItemPuzzleChance = 0.7`.

Separately, **218** `ItemSeedData` MonoBehaviours were found in `level1` (scene-instance items),
plus **60** more in `sharedassets1.assets` (these are prefab-template copies, not scene instances,
and were not used for resolving `allItems` — only the 218 scene instances in `level1` matter for
`SeedManager.allItems`, since `SeedManager.allItems` PPtrs all resolve locally within `level1`).

## How the data was extracted

### 1. Locating the objects
UnityPy 1.25.3 was installed (`pip install UnityPy`) and used to enumerate every `MonoBehaviour`
object in each asset/level file. Because Granny Legacy is IL2CPP-built, custom script classes
ship **without a TypeTree**, so `ObjectReader.read()` (which needs the full TypeTree) throws
`ValueError: Expected to read N bytes, but only read 32 bytes` for every `SeedManager`/
`ItemSeedData`/etc. object — 32 bytes is exactly the size of the base `MonoBehaviour` header
(see below), confirming that only the built-in header fields have a TypeTree.

`ObjectReader.parse_monobehaviour_head()` (an internal UnityPy helper, called with
`check_read=False`) reads *only* that 32-byte header and doesn't error, giving access to
`m_GameObject`, `m_Enabled`, `m_Script`, `m_Name`. `m_Script` is a `PPtr<MonoScript>`, and
`MonoScript` *is* a built-in engine type with a full TypeTree, so `head.m_Script.read()` gives
`m_ClassName` — this is how every `SeedManager` / `ItemSeedData` instance was located across all
game files.

### 2. Byte layout (verified against dump.cs and empirically corrected)

Base `MonoBehaviour` header (32 bytes, present on every MonoBehaviour object in this build):

| offset | field | type | size |
|---|---|---|---|
| 0  | m_GameObject.m_FileID | int32 | 4 |
| 4  | m_GameObject.m_PathID | int64 | 8 |
| 12 | m_Enabled | bool (+3 pad) | 4 |
| 16 | m_Script.m_FileID | int32 | 4 |
| 20 | m_Script.m_PathID | int64 | 8 |
| 28 | m_Name (length=0 in every instance seen) | int32 len (+bytes+align) | 4 |

→ **PPtr = int32 fileID + int64 pathID = 12 bytes**, confirmed byte-for-byte against UnityPy's
own parsed `PPtr` values for `m_GameObject` and `m_Script` on multiple objects.

Custom fields start at offset 32. General Unity binary serialization rules used throughout:
- `string` = int32 length + UTF-8 bytes, then align to 4 bytes.
- `List<T>` / `T[]` = int32 count + `count` elements, identical wire format for both.
- `int`/`float` = 4 bytes little-endian, no padding.
- `bool` = 1 byte, then align to 4 bytes (pad 3).
- `[Serializable]` plain classes (`PuzzleDef`, `SpawnArea`, `PuzzleForbiddenCombo`) have **no**
  header — their fields are serialized directly, in declaration order.

**`SeedManager` body** (offset 32 onward), matching dump.cs exactly:

```
List<GameObject> allItems
List<PuzzleDef>  puzzleDefs
List<string>     requiredEscapeItemNames
SpawnArea[]      spawnAreas
int   Seed
bool  RandomizeSeed
bool  activatePlacedItems
bool  fillAllFreeSpawns
bool  Rigid
float EscapeItemPuzzleChance
```

**`PuzzleDef`**, matching dump.cs exactly:
```
string puzzleName
Transform spawnPoint          (PPtr)
List<string> allowedItemNames
List<string> excludeItemNames
int Priority
List<string> requiredItemNames
List<PuzzleForbiddenCombo> forbiddenCombos
List<string> containedItems
```

**`PuzzleForbiddenCombo`**, matching dump.cs exactly:
```
string itemName
string forbiddenItem
List<string> forbiddenPuzzles
```

**`SpawnArea` — DISCREPANCY FOUND AND CORRECTED.** dump.cs declares:
```
string areaName
Transform[] freeSpots
int maxItems
int currentItems
List<Transform> usedSpots        <-- NOT present in the shipped binary (see below)
List<string> allowedItemNames    <-- NOT present in the shipped binary (see below)
string mandatoryItemName         <-- NOT present in the shipped binary (see below)
```
Parsing strictly per dump.cs failed on the very first `SpawnArea` instance: after `currentItems`,
the next 4 bytes decoded as a plausible empty-list count (0), but the field after that decoded as
a string length of `1769367884` — garbage, because the following bytes were literally the ASCII
text `"Living"` (the *next* spawn area's name), not a valid list-of-strings element. Manually
walking the raw bytes with hex offsets (documented candidate hypotheses tested in the scratch
script) showed that **every single `SpawnArea` instance across all 10 `SeedManager` variants (65
areas total) has exactly one trailing int32 field after `currentItems` — always observed as 0 —
and then the next area's `areaName` (or, for the last area, `Seed`) begins immediately.** Under
this corrected 5-field layout, all 10 `SeedManager` objects parse with **zero leftover/missing
bytes** (`parsed_offset == len(raw_data)` exactly, for every one of the 10 variants — 972, 1216,
1476, 2116, 2540, 3436, 3692, 4372, 5368, and 6160 bytes respectively). This is strong,
unambiguous evidence for the corrected layout.

Working theory: this build was compiled at a point where `SpawnArea` only had a single extra
field (most likely `usedSpots`, a runtime-only bookkeeping list, which makes sense being always 0
in freshly-authored/never-played scene data), and `allowedItemNames` / `mandatoryItemName` were
added to the class in a later revision reflected in `dump.cs` but not present in the
`Granny_Legacy` build sitting in the Desktop folder. **`allowedItemNames` and `mandatoryItemName`
are therefore emitted as `null` in `scene_data.json` rather than guessed — they simply do not
exist in this build's serialized data.** `usedSpots` is emitted as an empty list for the same
reason (schema compatibility) but its true identity (vs. some other single field) is not
100% certain — see "Uncertain / unresolved" below.

**`ItemSeedData` body** (offset 32 onward), matching dump.cs exactly, validated with **zero
mismatches across all 218 instances** in `level1` (every single one consumed its raw data down to
the last byte):
```
string itemName
int category
List<string> containedItems
```
(One instance, the `Melon` item, has `category=3` and 11 `containedItems`, matching the
`MelonContainItem` class seen in dump.cs, which further corroborates correctness.)

### 3. Resolving PPtrs to human-readable data
- `GameObject` and `Transform` are built-in engine types with a full TypeTree, so
  `pptr.read()` works directly via UnityPy for these (no manual parsing needed).
- Full hierarchy paths were built by resolving a `Transform`'s `m_GameObject.m_Name`, then
  walking `m_Father` PPtrs up to the scene root, joining names with `/`
  (e.g. `Objects/MainItemSelection/ItemSpawnsAcrossMap (Seed System)/PuzzleSpots/PuzzleSpot_ScrewHoleCellar`).
- For each `GameObject` referenced by `allItems`, its `m_Component` list was scanned; for every
  component that is a `MonoBehaviour` whose resolved `MonoScript.m_ClassName == "ItemSeedData"`,
  the raw bytes were parsed with the `ItemSeedData` parser above to recover `itemName`,
  `category`, and `containedItems`.
- All PPtrs encountered (`m_GameObject`, `spawnPoint`, `freeSpots`, item `allItems` entries) had
  `m_FileID == 0` in every case observed, i.e. they all resolve within `level1` itself — no
  cross-file PPtr resolution was needed for scene object references (only `m_Script` PPtrs point
  externally, to the assembly's `MonoScript` table, resolved automatically by UnityPy).

### 4. Validation methodology
- Every one of the 10 `SeedManager` objects and all 218 `ItemSeedData` objects were parsed with
  hard assertions that (a) every list/array count is in `[0, 100000]` (sanity bound) and (b) the
  parser's final read offset equals the object's exact raw byte length — i.e. **not a single byte
  left over or under-read**, for all 228 objects.
- Cross-checked decoded content for plausibility: item names (`Pliers`, `MasterKey`, `Hammer`,
  `SafeKey`, ...), puzzle names (`Safe`, `ScrewHoleCellar`, `AtticSpiderLocker`, ...), spawn area
  names (`BasementArea`, `Kitchen`, `Bathroom`, `Bedrooms`, ...), and hierarchy paths all match
  what one would expect from a Granny-style house escape game, and are internally consistent
  (e.g. `PuzzleDef.requiredItemNames` / `forbiddenCombos` reference item names that also appear in
  `allItems`).
- Verified zero unresolved PPtrs (no `null` names/paths) across every field, in every one of the
  10 variants, in the final `scene_data.json`.

## Deliverables

- `C:\Users\ir0n1c\grannyseedpredictor\scene_data.json` — extracted data. Top level mirrors the
  `SeedSystemManager_Normal` variant; `seedManagerVariants` holds all 10 variants.
- `C:\Users\ir0n1c\grannyseedpredictor\extract_scene.py` — reusable, commented extraction script
  (re-run with `python extract_scene.py`; read-only against the game data folder).
- `C:\Users\ir0n1c\grannyseedpredictor\re_scene_data.md` — this report.

## Uncertain / unresolved (explicitly not guessed)

1. **Which of the 10 `SeedManager` variants is actually used in a given playthrough** is
   determined by runtime code (a `VersionPlayerPrefs`-style PlayerPrefs value read by
   `VersionControl`/`VersionControlItemChange`), not by scene data. All 10 are provided; the
   caller must pick the one matching their target house version/difficulty.
2. **`SpawnArea.allowedItemNames` and `SpawnArea.mandatoryItemName`** are not present in this
   build's serialized data (see discrepancy above) and are set to `null` rather than guessed.
3. **`SpawnArea.usedSpots`** is set to an empty list based on the working theory that the single
   observed trailing field is `usedSpots`'s count (always 0); this identification is a reasonable
   inference from field order and semantics (a runtime-only field would naturally be 0 in
   unplayed scene data) but is not independently proven, since a zero-valued field of any of the
   three original candidate fields would look identical on the wire.
4. `sharedassets1.assets` contains 60 additional `ItemSeedData` MonoBehaviours that are prefab
   templates, not scene instances — they were not resolved/included since `SeedManager.allItems`
   PPtrs only ever point into `level1`.
