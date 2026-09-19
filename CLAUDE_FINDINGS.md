# Granny Legacy — Reverse Engineering Findings

**Target:** Granny Legacy (Unity 2022.3.62f2, IL2CPP, x64)
**Goal:** Python GUI tool for **inverse seed search** — the user picks where every item goes, clicks *Generate*, and the tool returns a seed that produces exactly that layout.
**Started:** 2026-09-17

---

## 1. Environment & Assets

| Item | Value |
|---|---|
| Game root | `C:\Users\ir0n1c\Desktop\Granny_Legacy\` |
| Binary under analysis | `GameAssembly.dll` (14.2 MB) |
| IDA database | `GameAssembly.dll.i64` (loaded, Hex-Rays ready, imagebase `0x180000000`) |
| Unity version | 2022.3.62f2 |
| Scripting backend | IL2CPP |
| Metadata | `Granny Legacy_Data\il2cpp_data\Metadata\global-metadata.dat` (4,320,316 bytes) |
| Existing dump | `C:\Users\ir0n1c\Desktop\Il2CppDumper-win-v6.7.46\` (dump.cs 6.4 MB, script.json 13.9 MB, il2cpp.h 11.2 MB, stringliteral.json 563 KB, DummyDll/) |

### Notable extras
- `GameAssembly2.dll` — same size as GameAssembly.dll, purpose TBD (backup / variant?).
- **MelonLoader is installed** (`MelonLoader\`, `Mods\`, `Plugins\`, `UserLibs\`, `version.dll` as proxy loader) — a C# runtime mod is a viable route for validating predictions against the live game.

---

## 2. The Actual Deliverable

A **Python** desktop app:
1. GUI showing every item and every possible spawn location for the map.
2. User assigns each item to a location of their choosing.
3. "Generate" → tool searches the seed space for a seed whose placement algorithm output matches that exact assignment.
4. Output: the seed, and instructions/means to apply it in-game.

### What this requires from RE (in dependency order)
1. **Which RNG** — `UnityEngine.Random` (Xorshift128, trivially reimplementable) vs `System.Random` (Knuth subtractive lagged Fibonacci) vs custom. Determines the Python core.
2. **Seed entropy + seeding site** — what value is fed to `InitState`/`new Random(x)`, and when.
3. **The placement algorithm, instruction-exact** — the precise order and count of RNG draws, the slot list, rejection/retry loops. Any deviation breaks reproduction entirely.
4. **Seed injectability** — a generated seed is worthless unless it can be applied. Is the seed persisted (save file / `UserData` / PlayerPrefs) or derived from clock/GUID? If not settable, a **MelonLoader C# mod** forcing `InitState(seed)` is the delivery mechanism (MelonLoader is already installed — see §1).

### ⚠ Feasibility risk to resolve early
Seed space is likely 2^32 (~4.29e9). The number of distinct layouts the algorithm can express may **exceed** that. If `#layouts >> 2^32`, then most user-chosen layouts have **no** corresponding seed, and the honest product is "find the closest achievable layout" or "constrain a subset of items." Must compute layout-space size as soon as the algorithm is known, and tell the user before building the GUI.

---

## 3. Open Questions
- [ ] Does the existing Il2CppDumper output match this exact GameAssembly.dll build?
- [ ] Which RNG does the game use for spawn/seed logic (UnityEngine.Random vs System.Random vs custom)?
- [ ] What is seeded, when, and from what entropy source?
- [ ] What observable in-game outputs derive from the seed (item placement, key spawns, etc.)?

---

## 4. Findings

_(in progress)_

### 4.1 Dump validated ✓
- `dump.cs` (Sep 5) is **newer** than `GameAssembly.dll` (Aug 27) → dump corresponds to this build.
- 299 hits for Granny/Slendrina/DVloper/Spider. `DummyDll/Assembly-CSharp.dll` present (548,352 bytes).
- **The existing dump is usable. No need to re-run Il2CppDumper.**

### 4.2 🎯 `SeedManager` — the randomizer is built into the game

The game ships an explicit, seed-driven item randomizer. This is the whole target.

```csharp
class SeedManager {                          // TypeDefIndex 2503
    List<GameObject> allItems;               // 0x20
    List<PuzzleDef>  puzzleDefs;             // 0x28
    List<string>     requiredEscapeItemNames;// 0x30
    SpawnArea[]      spawnAreas;             // 0x38
    int   Seed;                              // 0x40  <-- SETTABLE
    bool  RandomizeSeed;                     // 0x44
    bool  activatePlacedItems;               // 0x45
    bool  fillAllFreeSpawns;                 // 0x46
    bool  Rigid;                             // 0x47
    float EscapeItemPuzzleChance;            // 0x48
    Random rnd;                              // 0x50  <-- System.Random
    Dictionary<string,GameObject> itemsByName;    // 0x58
    HashSet<string>    usedItemNames;             // 0x60
    HashSet<Transform> usedPuzzleSpawns;          // 0x68
    Dictionary<string,bool> puzzleHasItem;        // 0x70
}
```

| Method | RVA | VA (base `0x180000000`) | Role |
|---|---|---|---|
| `GeneratePlacement()` | `0x24F770` | `0x18024F770` | **THE algorithm** |
| `BuildLookup()` | `0x24E920` | `0x18024E920` | name→GameObject map |
| `PlaceItemInFreeArea()` | `0x2529E0` | `0x1802529E0` | free-spawn placement |
| `PlaceItemInSpecificFreeArea()` | `0x252FD0` | `0x180252FD0` | area-constrained placement |
| `Shuffle<T>()` | `0x2D7590` | `0x1802D7590` | list shuffle (RNG draw order!) |
| `ValidatePuzzleDependencies()` | `0x253CF0` | `0x180253CF0` | constraint check → retry loops |
| `DetectCircularDependencies()` | `0x24EC00` | `0x18024EC00` | constraint check |

### 4.3 RNG identification
- `UnityEngine.Random`: **0** occurrences. `InitState`: **0**. → Unity's Xorshift128 is NOT used.
- Randomizer uses **`System.Random`**, constructed as `.ctor(int Seed)` @ RVA `0x4680D0`, with `InternalSample()` @ `0x467D10` and an `int[] _seedArray` field.
- ⚠ Correction to first-pass note: `System.Random` is **not** an LCG. The `_seedArray` is Knuth's **subtractive lagged-Fibonacci** generator (`Net5CompatSeedImpl`). The presence of `GenerateGlobalSeed()` indicates a modern corelib where the *parameterless* ctor uses xoshiro256\*\* but the *seeded* ctor falls back to the legacy subtractive algorithm. **This must be confirmed against the binary, not assumed** — the exact variant decides the Python core.

### 4.4 Supporting data structures
```csharp
class PuzzleDef {  string puzzleName; Transform spawnPoint;
                   List<string> allowedItemNames, excludeItemNames, requiredItemNames, containedItems;
                   int Priority; List<PuzzleForbiddenCombo> forbiddenCombos; }

class SpawnArea { string areaName; Transform[] freeSpots; int maxItems, currentItems;
                  List<Transform> usedSpots; List<string> allowedItemNames; string mandatoryItemName; }

class ItemSeedData { string itemName; int category; List<string> containedItems; }
                   // category: 1=escape, 2=escape+puzzle, 3=puzzle, 4=free
```
- `ItemRepositionSeed` holds Transforms for **35 named key items**: Pliers, MasterKey, Hammer, PDKey, Code, SafeKey, WPKey, Battery, Winch, Melon, PlayHouseKey, RedCog, OrangeCog, Barrel, Buttstock, Trigger, CarKey, SparkPlug, Gas, Engine, CarBattery, Wrench, Book, Meat, SPKey, Remote, BirdSeed, WheelCrank, ChainCutter, WoodenStick, RustyKey, RoboData, Baton, ECKey, Fuse.
- `ItemSpawn` caches ~54 spawnable item GameObjects.

### 4.5 Implications for the deliverable
- **Injectability looks solved**: `Seed` is a public serialized field guarded by `RandomizeSeed`. A MelonLoader mod setting `Seed` + `RandomizeSeed=false` before `GeneratePlacement()` runs should fully control a run. Needs confirmation of who writes `Seed` and when.
- **Search cost** = one `GeneratePlacement()` simulation per candidate seed. Must be reimplemented exactly in Python, then made fast (early rejection on the first placed item prunes the vast majority of seeds).

---

## 5. RESOLVED: Seed injection path ✅ (no mod required)

Seed is read from **Unity PlayerPrefs → Windows registry**:

```
HKEY_CURRENT_USER\Software\Omega Mega Gigal Intel\Granny: Legacy
    GameSeed_h510350364      REG_DWORD   0x368aef90 = 915074960   <- THE SEED
    RandomSeed_h3548222249   REG_DWORD   0x0                      <- 0 = use GameSeed
```
- Company `Omega Mega Gigal Intel`, product `Granny: Legacy` (note the colon; `app.info` says "Granny Legacy" without it).
- `RandomSeed` is **already 0**, so the game already reads the fixed `GameSeed`. Writing `GameSeed` is sufficient — Python `winreg` can do it.
- 91 prefs total; no save files hold seed data (`AppData\LocalLow\...` has only logs).
- Note: `UserData\GrannyTAS\` exists — a TAS mod is already installed, and the IDB carried prior "TAS bonus" comments.

## 6. RESOLVED: The algorithm

### Seeding
```csharp
seed = (PlayerPrefs.GetInt("RandomSeed")==1 && RandomizeSeed)
     ? UnityEngine.Random.RandomRangeInt(0, 999999999)   // fallback only
     : PlayerPrefs.GetInt("GameSeed");
this.Seed = seed;
attempt = 0;
while (attempt < 50 && !success) {
    attempt++;                              // starts at 1
    rnd = new System.Random(Seed + attempt); // RE-SEEDED EVERY ATTEMPT
    ... placement ...
    success = !DetectCircularDependencies() && ValidatePuzzleDependencies();
}
```
⚠ `rnd` is **not** a continuous stream — each retry is an independent `Random(Seed+attempt)`. Normal case is `attempt=1` → `Random(Seed+1)`.

### RNG primitive — confirmed bit-exact from disassembly
.NET Core **Knuth subtractive lagged-Fibonacci**. Not xoshiro (no Xoshiro/CompatPrng/Net5Compat symbols exist in the binary at all).
- `MSEED = 161803398` (verified as a unique immediate at `0x180468158`), `MBIG = 2147483647`
- Seed normalization: `(seed == int.MinValue) ? int.MaxValue : Math.Abs(seed)` — the .NET **Core** form
- `_seedArray[56]`, `_inext=0`, `_inextp=21`, 4 warm-up passes
- `InternalSample`: index advance wraps at 56 (`++x; if(x>=56) x=1` — not modulo)
- `Sample() = InternalSample() * (1.0/2147483647)`
- `Next(max) = (int)(Sample()*max)`; `Next(min,max) = min + (int)(Sample()*range)` for ranges ≤ int.MaxValue

### Shuffle — backward Durstenfeld Fisher-Yates
```python
for i in range(len(lst)-1, 0, -1):
    j = rnd.next_int(i+1)     # j in [0, i]
    lst[i], lst[j] = lst[j], lst[i]
```

### Pipeline (per attempt), in RNG-consumption order
| Step | What | RNG cost |
|---|---|---|
| A | Greedy puzzle←item assignment over `orderedPuzzles` (stable `OrderBy(Priority)`) | 1 `Next(n)` per puzzle with non-empty pool |
| B | Required escape items | 1 `NextDouble()` each; then 1 `Next(n)` or a `PlaceItemInFreeArea` |
| C | Commit Step-A assignments | **none** |
| D | Backfill empty puzzles (tier1 strict → tier2 relaxed) | 0-1 `Next(n)` per puzzle |
| E | Mandatory-item spawn areas | 0-1 `Next(n)` each |
| F | `fillAllFreeSpawns`: Shuffle(remaining) then place each | `n-1` + per-item cost |

`PlaceItemInFreeArea` = Shuffle(eligible areas) [`n-1` calls] + 1 `Next(freeSpots)`, with an unshuffled fallback scan.

### Key semantics
- `EscapeItemPuzzleChance` polarity is **inverted vs its name**: `if (chance <= roll) → free area`. Higher value ⇒ *more* free-area placement.
- `Rigid` affects physics only (`isKinematic`), **not** placement.
- **No HashSet/Dictionary enumeration affects placement** — everything RNG-relevant runs over `List`/arrays. Python `dict`/`set` are safe.

### Known quirks to reproduce faithfully (do NOT "fix")
- Step E marks a mandatory item as used **even when placement silently no-ops** (area had no free spot).
- If all 50 attempts fail, the last invalid layout stands.

### Still unverified (flagged GUESS by the analyst)
- [ ] Lambda `b__4` inferred as `IsItemAllowedForPuzzle` by structural symmetry, not decompiled.
- [ ] The `itemSeedData.<field> != 4` filter — direction confirmed, but which field / what `4` means is unverified (likely `category`, where 4 = "free").

---

## 7. Build decisions (user-directed)
- **Match modes:** all three — pinned (must match) / preferred (scored) / don't-care, in one GUI.
- **Search engine:** **GPU via OpenCL**, AMD-first (RX 7700 XT) but vendor-neutral so NVIDIA works too. PyOpenCL chosen because AMD's Windows driver ships an OpenCL runtime; ROCm/HIP is not viable on Windows for this card.
- **GUI:** Tkinter (stdlib, zero install).
- **Sequencing rule:** CPU reference must be validated against the real game **before** the OpenCL kernel is written. A subtly-wrong kernel silently emits bad seeds.

## 8. Toolchain inventory
| Tool | Status |
|---|---|
| Python | 3.11.9 |
| numpy / pyopencl | ✅ installed (2.4.6 / 2026.1.4) |
| UnityPy | ✅ 1.25.3 |
| .NET SDK | ✅ 10.0.301 (+ runtimes 6/8/10) — used as RNG oracle |
| MelonLoader | ✅ installed (net35/net472/net6), 1 mod: `GrannyTAS.dll` |
| GPU | ✅ AMD RX 7700 XT, `gfx1101`, 27 CU, 12 GB, maxWG 256, local 64 KB |
| **fp64 (`cl_khr_fp64`)** | ✅ **supported** — required for bit-exact `Sample()` double math |

## 9. ✅ VALIDATED: `net_random.py` is bit-exact

Cross-validated against **real .NET 10** (`dotnet` SDK as oracle; the seeded `Random(int)` ctor still routes to the frozen legacy Knuth path in .NET 6+, so it is a valid oracle).

- **Result: PASS — 1980/1980 values matched exactly, zero mismatches.**
- 11 seeds incl. `0`, `-1`, `int.MaxValue`, **`int.MinValue`** (exercises the seed-normalization branch), `161803398`, and the live `915074960`.
- 9 method categories: `Next()`, `Next(10/3/1/54)`, `NextDouble()` (compared via `"R"` round-trip format — no precision loss), `Next(-5,5)`, `Next(0,1000000)`, and `Next(int.MinValue,int.MaxValue)` to exercise the large-range `GetSampleForLargeRange` branch.
- Every branch identified in the RE report is now covered. No untested path remains.
- Implementation notes: explicit `_wrap32` at every point C# would overflow; `math.trunc` for C#'s truncate-toward-zero `(int)` cast. `internal_sample()` intentionally omits a wrap on the subtraction, matching the disassembly — the algorithm's invariant keeps values in `[0, MBIG-1]`.

Artifacts: `net_random.py`, `validate_random.py`, `re_random_validation.md`.

## 10. Remaining critical path
1. ⏳ **Scene data extraction** (`SeedManager` serialized config) — BLOCKER for any simulation.
2. ⬜ **Ground-truth validation** of the full placement pipeline against a real run.
3. ⬜ CPU reference simulator → verify → OpenCL kernel → GUI.

---

## 11. Scene data extracted — with two important surprises

Extracted via UnityPy + manual binary parsing of the serialized `MonoBehaviour` bytes → `scene_data.json` (296 KB), script `extract_scene.py`, report `re_scene_data.md`.

### Surprise 1: there are **10 `SeedManager` instances**, not one
All in `level1` (main gameplay scene) under `GameManager/HandleObjects`, all **inactive** in the saved scene. One is activated at runtime by a `VersionControl` / `VersionControlItemChange` system based on a saved house-version preference — that selection lives in **code, not scene data**.

| Variant | items | puzzles | areas | free spots | reqEscape |
|---|---|---|---|---|---|
| SeedSystemManager_1.0 | 6 | 1 | 6 | 38 | 4 |
| SeedSystemManager_1.1 | 7 | 2 | 7 | 47 | 4 |
| SeedSystemManager_1.2 | 7 | 4 | 7 | 47 | 4 |
| SeedSystemManager_1.3 | 13 | 7 | 8 | 51 | 4 |
| SeedSystemManager_1.4 | 17 | 8 | 9 | 59 | 4 |
| SeedSystemManager_1.5 | 25 | 9 | 10 | 62 | 11 |
| SeedSystemManager_1.6 | 26 | 10 | 10 | 62 | 11 |
| SeedSystemManager_1.7 | 27 | 13 | 11 | 66 | 11 |
| **SeedSystemManager_Normal** | **31** | **16** | **12** | **71** | **14** |
| SeedSystemManager_More | 35 | 18 | 12 | 71 | 18 |

`EscapeItemPuzzleChance = 0.7` and `fillAllFreeSpawns = true` for **all** variants.

Registry clue (unconfirmed): `CURVERSION = 8`. If the variants index 1.0→1.7 as 0–7, then 8 = `Normal`, 9 = `More`. **Plausible but must be verified** — also `Preset = 1`, `PresetChoosin = 5` exist.

### Surprise 2: ⚠ `SpawnArea` binary layout contradicts `dump.cs`
`dump.cs` declares 7 fields (`areaName, freeSpots, maxItems, currentItems, usedSpots, allowedItemNames, mandatoryItemName`), but the serialized data in this build parses byte-perfectly only with **5** fields — one trailing int (always 0) after `maxItems`, and no `usedSpots`/`allowedItemNames`/`mandatoryItemName`.

The extractor emitted those three as `null` rather than guessing. **This is not yet settled** and it matters a lot:
- If `allowedItemNames` is genuinely always empty, area filtering in `PlaceItemInFreeArea` is unrestricted.
- If `mandatoryItemName` is genuinely always null, **Step E never fires at all**, removing a whole RNG consumption stage.

Either way the algorithm gets *simpler*, but guessing wrong breaks bit-exactness. **Resolve via the live mod dump, not by inference.**

**Validation strength of the extraction:** all 228 parsed objects (10 SeedManagers + 218 ItemSeedData) consumed their raw bytes exactly — zero leftover, zero underflow. That's a strong structural check.

## 12. 📊 Feasibility answer — how many items can actually be pinned

With ~71 candidate locations per item and a 2^32 seed space, expected number of seeds matching *k* independently pinned items:

| Pins | Expected matching seeds | Verdict |
|---|---|---|
| 1 | ~60,000,000 | trivial |
| 2 | ~852,000 | trivial |
| 3 | ~12,000 | fine |
| 4 | ~169 | **borderline** |
| 5 | ~2.4 | at the edge — may not exist |
| 6+ | ~0.03 | effectively impossible |

**Conclusion: ~4 pins is the reliable ceiling, 5 is a coin flip, 6+ won't exist.** This is exactly why the full 4.3-billion-seed GPU sweep is necessary rather than a nicety — at 4-5 pins you must scan the entire space to find the handful of solutions. It also confirms "pin a few + best-effort on the rest" is the right product design.

## 13. Built (not installed): `SeedDumper` MelonLoader mod
- MelonLoader **0.7.3 Open-Beta**, net6, Il2CppInterop.Runtime 1.5.1.0 (matched against the working `GrannyTAS.dll`).
- Game types are in namespace `Il2Cpp.*` and Il2CppInterop exposes fields as **properties**; collections are `Il2CppSystem.Collections.Generic.List<T>` / `Il2CppReferenceArray<T>`. All member names confirmed from assembly metadata.
- Harmony **Prefix** on `SeedManager.GeneratePlacement` dumps full config; **Postfix** dumps resulting item positions + parent transform paths. Output → `dumps\config_<seed>_<ts>.json` / `placement_<seed>_<ts>.json`.
- Builds clean (0 warnings, 0 errors). **Not installed** — awaiting user approval.
- Known limitation: the postfix cannot observe *which* retry attempt (1–50) succeeded, only the final `Seed`. If validation shows a mismatch, add an inner patch point to capture the winning `Seed+attempt`.

---

## 14. Corrections
- **Current seed is `915074960`** (`0x368AEF90`). An earlier pass mis-converted this hex to `900658064`. All references corrected above. Verified independently via `winreg`.
- Pref count is **84** real registry values. The earlier "91" came from PowerShell `Get-ItemProperty`, which appends ~7 `PS*` metadata properties. Not a discrepancy.

## 15. ✅ `seed_registry.py` — seed injection module
Stdlib-only (`winreg`). API: `read_seed()`, `read_random_flag()`, `write_seed(seed, also_disable_random=True)`, `read_all_prefs()`, `backup_prefs()`, `restore_prefs()`, plus a CLI (`--set/--backup/--restore`).
- Handles the **REG_DWORD unsigned ↔ PlayerPrefs signed int32** conversion in both directions (12/12 conversion unit tests pass, incl. `-1`, `int.MinValue`, `int.MaxValue`).
- Auto-creates a timestamped full-key backup before the first write in a process.
- Verified read-only against the live registry: seed `915074960`, random flag `0`, 84 prefs.
- ⚠ Operational rule baked into the CLI: **the game must be closed when writing the seed** — Unity rewrites all PlayerPrefs from memory on exit and would clobber the change.

---

## 16. ✅ GROUND TRUTH CAPTURED — live dump succeeded

`SeedDumper` ran cleanly on the first attempt. `GeneratePlacement` fired **once**.
Files: `dumps/config_99999_20260917_171149_062.json`, `dumps/placement_915074960_20260917_171149_079.json`.

### Active variant confirmed: **`SeedSystemManager_Normal`**
31 items / 16 puzzleDefs / 12 spawnAreas / 14 requiredEscapeItemNames — an exact match to the `Normal` row of the variant table in §11. The `CURVERSION=8` hypothesis is consistent, though the dump makes the variant question moot for this save.

### Live config
```
seed(field at prefix time) = 99999   <- default; assigned later in the method
randomizeSeed = True                  <- SeedManager field
playerPrefs   = { GameSeed: 915074960, RandomSeed: 0 }
activatePlacedItems = True   fillAllFreeSpawns = True   rigid = False
escapeItemPuzzleChance = 0.7
```
Because `PlayerPrefs["RandomSeed"] == 0`, the condition `(RandomSeed==1 && RandomizeSeed)` is **false** → the game uses `GameSeed = 915074960`. Effective RNG on attempt 1 = `new System.Random(915074961)`.

### ✅ RESOLVED: the `SpawnArea` 5-vs-7 field contradiction
`mandatoryItemName` and `allowedItemNames` **do exist at runtime** — they are simply **empty on all 12 areas** (`''` and `[]`). The UnityPy parse saw them absent from the *serialized* bytes; the runtime objects have them defaulted. Consequences, now verified rather than inferred:
- **Step E (mandatory-item spawn areas) never fires** → that RNG stage is dead for this build. The `usedItemNames` quirk noted in §6 is therefore unreachable.
- Area filtering by `allowedItemNames` is **unrestricted** (empty ⇒ allow all).

### ✅ RESOLVED: the `!= 4` filter is `ItemSeedData.category`
Category 4 items are exactly: `Battery`, `Shotgun_Barrel`, `Shotgun_Buttstock`, `Shotgun_Trigger`, `Book`, `Code` — i.e. the non-puzzle "free" items. Confirms the analyst's flagged GUESS. Categories in use: 1, 2, 3, 4.

### The 12 spawn areas (order is RNG-significant)
| # | areaName | maxItems | freeSpots |
|---|---|---|---|
| 0 | BasementArea | 1 | 2 |
| 1 | Garage | 2 | 8 |
| 2 | Living | 2 | 7 |
| 3 | Kitchen | 3 | 16 |
| 4 | Bathroom | 1 | 4 |
| 5 | Bedrooms | 2 | 6 |
| 6 | Bedroom GrannyGrandpa | 1 | 4 |
| 7 | Cellar | 2 | 8 |
| 8 | OldHouse | 2 | 4 |
| 9 | Attic | 1 | 3 |
| 10 | Backyard | 1 | 4 |
| 11 | Spider Cellar | 2 | 5 |

**Total `maxItems` = 20, total freeSpots = 71.** With 31 items and only 20 free-area slots (+16 puzzle spawn points), not every item can be placed — expect leftovers. This is faithful game behavior, not a bug.

### The 31 items
Categories: `1`=escape, `2`=escape+puzzle, `3`=puzzle, `4`=free.
`Melon` is the only item with `containedItems` (11 entries) — it is a container, which is what `IsSafeContainerPlacement` guards.
`requiredEscapeItemNames` (14): Pliers, MasterKey, Hammer, PDKey, SafeKey, RustyKey, ChainCutter, WoodenStick, Wrench, CarKey, CarBattery, Engine, Gas, SparkPlug.

### 16 puzzles
Priorities 1–16 (note: **7 appears twice** — `DrawerCribRoom` and `CoffinSpiderCellar` — so the stable-sort tie-break matters and must preserve original `puzzleDefs` order). `forbiddenCombos` and `containedItems` are empty on all 16.

### ⚠ Gap found → mod updated to v1.1.0
Spawn points were dumped as `{name, path, instanceId}` with **no world position**, and items are re-parented to `Objects/MainItemSelection/MapItems` rather than to their spot — so a placement could not be mapped back to a named location. Mod v1.1.0 now also dumps:
- `position` (world, round-trip `"R"` precision) for every `freeSpots[]` entry and every `puzzleDefs[].spawnPoint`
- `startPosition` + `goName` per item in the config dump
- `instanceId` per item in the placement dump

**Requires one more game run** to regenerate the dumps with positions.

---

## 17. 🎯 GROUND TRUTH ESTABLISHED — seed `915074960`

Mod v1.1.0 run produced `dumps/config_99999_20260917_172110_489.json` + `dumps/placement_915074960_20260917_172110_502.json`, now with world positions. Joining item final positions against spawn-point positions gives an **exact** mapping:

**All 31/31 items matched a named slot at distance 0.0000.** Every item moved from its start position. 87 slots carry positions (71 free + 16 puzzle) as expected.

| Item | Placed at | Item | Placed at |
|---|---|---|---|
| Pliers | FREE OldHouse/Spot (82) | SparkPlug | PUZZLE ScrewHoleCellar |
| MasterKey | FREE Bedrooms/Spot (51) | Gas | FREE Bedrooms/Spot (56) |
| Hammer | FREE Kitchen/Spot (35) | Engine | PUZZLE Well |
| PDKey | FREE BasementArea/Spot (3) | CarBattery | PUZZLE RemoteRoom |
| SafeKey | FREE Backyard/Spot (87) | Wrench | PUZZLE BarrelOldHouse |
| WPKey | FREE Bedroom GrannyGrandpa/Spot (64) | Book | FREE Living/Spot (21) |
| Battery | FREE Cellar/Spot (77) | Meat | PUZZLE DrawerCribRoom |
| Winch | PUZZLE SpiderCellarPassage | SPKey | PUZZLE BehindFan |
| Melon | FREE Attic/Spot (84) | Remote | PUZZLE ToyLock |
| PlayKey | PUZZLE Car | BirdSeed | PUZZLE AtticSpiderLocker |
| RedCog | PUZZLE SewerTable | WheelCrank | PUZZLE BirdCage |
| OrangeCog | PUZZLE CoffinSpiderCellar | ChainCutter | FREE Bathroom/Spot (47) |
| Shotgun_Barrel | FREE Kitchen/Spot (41) | WoodenStick | PUZZLE Safe |
| Shotgun_Buttstock | FREE OldHouse/Spot (83) | RustyKey | PUZZLE Melon |
| Shotgun_Trigger | FREE Spider Cellar/Spot (96) | Code | FREE Cellar/Spot (72) |
| CarKey | PUZZLE ScrewHoleSpiderCellar | | |

**Breakdown: all 16 puzzle spawn points filled + 15 free-area placements = 31.** Free-area usage 15 of 20 `maxItems` capacity.

**This is the acceptance test.** `simulate(915074960)` must reproduce this table exactly, or the simulator is wrong.

### Empirical corroboration of predicate polarity (pending analyst confirmation)
- `allowedItemNames` empty ⇒ **allow all** (subject to `excludeItemNames`). Evidence: `AtticSpiderLocker` has `allowed=[]` and received `BirdSeed`, which is absent from its exclude list.
- An item never lands in a puzzle that lists it in `requiredItemNames` (the key is never locked inside its own lock). Evidence: `Safe` requires `SafeKey` and got `WoodenStick`; `SafeKey` went to a free spot. Same for `Car`/`CarKey`, `RemoteRoom`/`Remote`, `Well`/`Winch`.
- Reproducibility: both runs read the same `GameSeed=915074960`, and `GeneratePlacement` fired exactly once per launch.

---

## 18. ✅ SIMULATOR WORKS — 31/31 on real game data

`simulator.py` reproduces the live placement for seed `915074960` **exactly** (`ground_truth.compare() == (True, [])`), succeeding on **attempt 1** = `NetRandom(915074961)`. No hardcoding, no answer-keyed special cases.

### Convergent validation (the strongest evidence so far)
The builder derived `IsSafeContainerPlacement` empirically via RNG-trace debugging; the IDA analyst derived it independently from disassembly. **They agree**: a container item is unsafe for a puzzle iff the item's `containedItems` overlaps that puzzle's `requiredItemNames`. Two unrelated methods converging on a non-obvious rule is strong evidence it's correct.
(Evidence: `Melon.containedItems` ∋ `CarKey`, `Car.requiredItemNames == ["CarKey"]` ⇒ Melon excluded from Car's pool; ground truth has `PlayKey` at Car.)

### Predicates — final, disassembly-verified (`re_predicates.md`)
- **`IsItemAllowedForPuzzle`**: empty `allowedItemNames` ⇒ **allow-all** (falls through to exclude check); non-empty ⇒ allowlist-only. `excludeItemNames` applies **only** when the allowlist is empty. **Never reads `requiredItemNames`.** Matching is on `ItemSeedData.itemName`, **`.Trim()` both sides + `OrdinalIgnoreCase`** (comparisonType constant `5` in disasm).
- **`category != 4`**: definitively `ItemSeedData.category` at object offset `0x28` (`cmp dword ptr [rax+28h], 4`). The earlier "mistyped `byval_arg`" reading was a Hex-Rays display artifact. `dump.cs` even preserves the dev's tooltip `"1=escape-only, 2=escape+puzzle, 3=puzzle-only, 3=free"` (their typo for `4=free`).
- **`b__4` == `IsItemAllowedForPuzzle`** — confirmed: `b__4` and `b__11` share one native address (`0x180256550`) via IL2CPP identical-code-folding. Previously a GUESS, now fact.
- **`IsSafeContainerPlacement`** uses **plain case-sensitive, no-Trim** comparison — deliberately *different* from the OrdinalIgnoreCase used elsewhere.
- **`SpawnArea`**: the compiled class genuinely has all **7** fields (`0x10`–`0x38`) and the code actively reads both trailing ones. But `SpawnArea..ctor` sets `allowedItemNames = new List<string>()` and leaves `mandatoryItemName` **null**. Net: `allowedItemNames` is an empty-but-non-null no-op, `mandatoryItemName` is null ⇒ **Step E never fires on any of the 10 variants.** Not a contradiction after all — the earlier 5-vs-7 finding was about *serialization*, not the class layout.

### Corrections applied during reconciliation (each tested individually)
| Change | Effect |
|---|---|
| Removed the bogus `requiredItemNames` clause from `IsItemAllowedForPuzzle` | no change — verified a genuine no-op by scanning all 16 puzzles for allow/required overlaps (zero exist in this data) |
| Trim + OrdinalIgnoreCase name matching throughout | no change — this build's strings have no case/whitespace variance |
| `IsSafeContainerPlacement` rewritten to the literal all-puzzles form | no change — 16/16 spawn points unique, so it degenerates to the single-puzzle rule here |
| Replaced the "all spots filled" validator proxy with literal ports of `GetEffectivePuzzleOfItem` / `ValidatePuzzleDependencies` / `DetectCircularDependencies` | no change — and positively confirmed correct against synthetic deadlock/cycle cases, so it isn't vacuously `True` |

### ⚠ Remaining inferences (explicitly not byte-verified)
- Trim/OrdinalIgnoreCase applied to `usedItemNames`, `SpawnArea.allowedItemNames`, `itemsByName`, `requiredEscapeItemNames` — extended by consistency with confirmed containers, not individually decompiled.
- `GetEffectivePuzzleOfItem`'s fallback for an item not yet placed in the current attempt (falls back to `startPosition`) — unexercised by this seed, since attempt 1 places all 31 items.

## 19. Second validation sample in progress
Seed written to registry: **`123456789`** (prev `915074960` backed up → `backups/prefs_backup_20260917_174757.json`).
Purpose: a single-seed match could in principle be overfitting. A second, independently-chosen seed reproducing exactly is the real proof of generality.

---

## 20. 🔑 SEED LIFECYCLE — the game has a built-in Seed menu

Exhaustive xref analysis of the `"GameSeed"` (`0x180c4e0b0`) and `"RandomSeed"` (`0x180c39198`) literals — only 4 functions touch them in the entire binary.

| Function | VA | Role |
|---|---|---|
| `SeedManager.GeneratePlacement` | `0x18024f770` | **reads only**, never writes |
| `Menu_Seed..ctor` | `0x18024a100` | caches key names |
| `Menu_Seed.Start` | `0x180249eb0` | reads both to populate the **in-game seed UI** |
| `Menu_Seed.Update` | `0x180249fa0` | writes `RandomSeed` every frame the menu is open; writes `GameSeed` + `Save()` on Enter/Escape |
| `Menu_Seed.ExitTypingModeAndSave` | `0x180249e00` | writes+saves `GameSeed` from the field; **no static caller** — fired by Unity's UI events (`OnEndEdit`/`OnDeselect`), so it can re-commit *stale* text on focus loss |
| `MainMenu.Start` | `0x180221541` | one-time `CURVERSION`-gated migration that force-sets `RandomSeed = 1` (never touches `GameSeed`) |

### ✅ Key conclusions
1. **The game ships a seed input menu.** This is the *intended* injection path and is far more robust than poking the registry — no process-lifetime or flush-timing hazards. **This should be the tool's primary "apply" method**, with the registry write kept as a convenience.
2. **Nothing auto-copies a seed on new-game or continue.** No per-save seed storage exists — not in PlayerPrefs, not in Unity Visual Scripting `SavedVariables`. So the earlier hypothesis ("continue restores the saved run's seed") is **refuted**.
3. ⚠ `MainMenu.Start` can force `RandomSeed = 1` once after a version bump. If `RandomSeed == 1` **and** the scene's `RandomizeSeed` field is true, `GeneratePlacement` **ignores `GameSeed` entirely** and rolls `Random.RandomRangeInt(0, 999999999)`. The dumps confirm `RandomizeSeed == True` on the live object, so **`RandomSeed` must be 0** for any fixed seed to work.
4. `Menu_Seed.ExitTypingModeAndSave` re-committing stale field text is the most plausible explanation for the failed injection — **UNCONFIRMED**.

### ❗ Correction to §11
There is **no `SeedSystemManager` class** and **no `VersionControl`-driven variant selection**. `SeedSystemManager_1.0`…`_More` are **GameObject names**, not types — 10 scene instances of the single `SeedManager` class, of which one is active. `CURVERSION` is just a defaults-migration flag, unrelated to variant choice. The live dump already settled which instance is active (31 items = the `Normal` object).

### Failed injection attempt — what actually happened
Wrote `GameSeed = 123456789` at 17:47 (game closed). Game launched 17:51:38 (after the write) but `GeneratePlacement` read `915074960`. Registry still read `123456789` afterwards. Since no in-game code copies a seed, the cause is most likely the seed-menu UI re-committing stale text, or a prefs flush from the *previous* session landing after my write. **Unresolved — but made moot by using the in-game menu instead.**

---

## 21. ✅✅ SIMULATOR PROVEN GENERAL — independent seed validated

| Seed | Source | Result |
|---|---|---|
| 915074960 | original run (predicates partly tuned against this) | **31/31 PASS** |
| 915074960 | repeat run | **31/31 PASS** |
| **123456789** | **independent seed, never used in development** | **31/31 PASS** |

**28 of 31 items land in different slots between the two seeds** — the layouts are genuinely different, so the second match is not vacuous. This rules out overfitting: the simulator is a correct general implementation, not a fit to one sample.

### Seed injection — RESOLVED
Registry write alone did **not** reach the game (the seed menu still displayed the old value). Typing the seed into the **in-game Seed menu** and pressing Enter **worked** — the run used `123456789` and `SeedManager.Seed` confirmed it.

**⇒ The in-game Seed menu is the reliable apply path. The registry write is unreliable and must not be the primary mechanism.**
The likely cause is Unity's PlayerPrefs being cached in memory and re-flushed by `Menu_Seed`, but the exact mechanism is **UNCONFIRMED** — and moot, since the supported UI path works.

**Product implication:** the GUI should present the seed prominently as a **copyable value to type into the game's Seed menu**, and demote "write to registry" to a clearly-labelled experimental fallback.

## 22. Remaining work
1. 🔨 GUI + CPU search (in progress)
2. ⬜ OpenCL GPU backend (now unblocked — the CPU reference is proven)
3. ⬜ Wire GPU into the GUI as a selectable backend

---

## 23. ✅ GPU BACKEND — OpenCL, bit-exact

Files: `kernel.cl`, `gpu_backend.py`, `test_gpu.py`, `re_gpu.md`.

### Validation
- **Bit-exactness: 2009/2009 seeds matched `simulator.py` with zero mismatches**, reproduced across 3 separate runs. Covered 2000 random int32 seeds plus `0`, `±1`, `int.MinValue`, `int.MaxValue`, `915074960`, `123456789`.
- **End-to-end search PASS** for both validated seeds (`Pliers -> FREE:OldHouse/Spot (82)` finds 915074960; `Battery -> FREE:Kitchen/Spot (42)` finds 123456789).

### Performance (measured on gfx1101)
- **2.4M–6.9M seeds/sec** on the GPU → a full 2^32 sweep in roughly **10–30 minutes**.
- Occupancy was **measured, not guessed**: `CL_KERNEL_PRIVATE_MEM_SIZE` = 672 B/work-item; a 5-point local-work-size sweep (32/64/128/256/default) was flat within 8%, so 128 was chosen and no `__local` tiling was needed.
- ⚠ The reported CPU speedup ratio (~36,000x single-core) is **inflated by a slow measurement environment** and should not be quoted as a desktop figure. The GPU throughput itself is trustworthy.

### Design: GPU filters, CPU adjudicates
The kernel implements Steps A–F for **attempt 1 only** and does not implement the validators. Every GPU candidate is **re-verified on the CPU with `simulator.py`** before being reported.
- ⇒ **Zero false positives** by construction.
- ⇒ Residual risk is only a missed hit where attempt 1 fails a pin but a retry would have satisfied it — the same behavior as the CPU backend, not a new risk. Acceptable and documented.
- Strings never reach the GPU: the host compiles all predicates into 32-bit bitmasks over the 31 item indices, so the kernel is pure integer/double work with no dynamic allocation.
- `cl_khr_fp64` is **required and checked**; the backend fails loudly rather than degrading to float (which would silently produce wrong seeds).

### 🐛 Bug found in the CPU backend (flagged, not yet fixed)
While benchmarking, the GPU agent found that `search.py`'s `_attempt1_fast` / `check()` **early rejection never triggers** (0/50 on a sample with obvious pin mismatches), and its `multiprocessing.Pool` search **hung indefinitely**. This does not affect `gpu_backend.py` (which never calls that path) but must be fixed in `search.py`.

---

## 24. CPU search backend + GUI delivered

`search.py` (781 lines), `gui.py` (585 lines), `re_search.md`.

### ⚠ Important correctness finding: naive early-rejection is WRONG
The obvious optimization — abort a seed as soon as a pinned item mismatches — is **silently incorrect**. Measured: **18/20,000 seeds (0.09%) mispredicted**, because ~**8.2% of seeds fail attempt 1** and their real layout comes from `NetRandom(seed+2)` or later. A naive abort cannot know whether the attempt it is looking at is the one the game keeps.

**Provably-safe replacement:** an attempt is accepted iff **no container item has landed on a puzzle spawn point by the end of Step D**. Puzzle assignment happens only in Steps A–D, so this is decidable before Steps E/F run. In this scene only `Melon` has non-empty `containedItems`. Early-abort is enabled **only** once acceptance is proven this way; otherwise the real acceptance check runs. Verified against `simulator.simulate()` with **0 mismatches across several thousand seeds including retry-heavy ranges**.

### ❗ Correction: the "CPU backend bug" was not real
§23 recorded a report that `search.py`'s early rejection never fires and its `multiprocessing.Pool` hangs. **Re-tested against the finished code — both claims are false.**
- Search over `915074000..915076000` pinning `Pliers -> FREE:OldHouse/Spot (82)` returned **21 hits including `915074960`**, in ~4s, no hang.
- My own first re-test appeared to fail only because `limit=5` truncated the result list before reaching `915074960`, and a separate attempt failed because Windows `multiprocessing` cannot re-import a heredoc `<stdin>` script. Both were artifacts of the test harness, not the code.

Measured: ~130–170 seeds/s/core naive → ~810–850 seeds/s/core fast path (~5–6x); ~6,000 seeds/s across 16 workers. Numbers taken on a loaded machine — treat as a lower bound.

## 25. GPU retry-loop fix (written, validation in progress)
The attempt-1-only limitation in §23 meant the GPU would **miss ~8% of valid seeds** (false negatives; never false positives, thanks to CPU re-verification). Harmless at 1–3 pins, potentially fatal at 5 pins where only ~2 seeds exist.

`kernel.cl` has been rewritten to implement the **full retry loop** (attempts 1..50, re-seeding `NetRandom(seed+attempt)`) plus the provable acceptance test ported from `search.py`. Confirmed present in the source; kernel compiles and the device initialises (`gfx1101`, fp64=yes). **The agent doing this was cut off by a rate limit before running validation — results are therefore UNVERIFIED and are being re-validated now.**

## 26. ✅ GPU RETRY FIX VALIDATED

| Test | Result |
|---|---|
| Bit-exactness vs `simulator.simulate()` | **6009/6009 matched, 0 mismatches** |
| — attempt-1 seeds | 3655/3655 |
| — **retry-required seeds (the target of the fix)** | **345/345** |
| End-to-end search, seed 915074960 | PASS (21 hits in range, target found) |
| End-to-end search, seed 123456789 | PASS (12 hits in range, target found) |

- Retry rate measured empirically: **345/4000 = 8.6%** of seeds need attempt ≥ 2, confirming the ~8.2% estimate. The false-negative gap from §23/§25 is **closed**.
- **Throughput: 6,051,063 seeds/sec** (20M seeds in 3.31s) ⇒ **full 2^32 sweep in ~12 minutes**.
- CPU single-core baseline 163.6 seeds/sec ⇒ measured 36,986x vs one core. The "~2,300x vs 16 cores" figure is an **estimate** (assumes linear scaling, ignores pool overhead) — do not quote it as measured.

---

## 27. ✅ INTEGRATION COMPLETE

- **GPU auto-selected** on startup (`gfx1101`, fp64=yes), with a GPU/CPU selector and device-info label; falls back to CPU cleanly. Default range becomes full 2^32 when GPU is active, quick-scan on CPU. Live ETA shown during search.
- **Apply flow corrected.** The in-game **Seed menu** is now the primary, recommended path (large seed display + "Copy to clipboard" + instructions). The registry write is demoted to a red-flagged "Advanced / experimental" section stating plainly that it is **known to be unreliable**.
- `README.md` added.

### Known issues (measured, documented, not hidden)
- **Cancel latency** up to ~13s worst case (1 pin, no limit, full range): `gpu_backend` only checks cancellation between dispatches, and CPU re-verification of a batch runs synchronously first. ~1.1s with a typical 2-pin search.
- `CpuSearchBackend`'s "quick scan" default of `0..100,000,000` is **not quick on CPU** (hours at ~6k seeds/s). Pre-existing; only matters if the GPU is unavailable.
- The GUI's feasibility estimate uses `search.py`'s existing 87-slot model rather than the 71-slot figure in §12 — slightly more optimistic; both are approximations.

## 28. 🎯 FIRST REAL INVERSE-SEED RESULT

Requested layout (3 pins) → **seed `10408711`**:

| Pinned item | Requested slot |
|---|---|
| MasterKey | `FREE:Garage/Spot (5)` |
| Hammer | `FREE:Kitchen/Spot (29)` |
| CarKey | `PUZZLE:Safe` |

- 5 seeds satisfying all 3 pins were found within the first ~18.5M seeds in **0.8s** (search stopped at the `limit=5` cap — it did **not** scan the full 400M range requested).
- Other valid seeds: `13764754`, `13764755`, `18484608`, `18484609`.
- All 3 pins independently re-verified with `simulator.simulate()`.
- ⏳ **Pending: in-game confirmation.** This is the final end-to-end proof — everything upstream is validated, but the complete loop (choose layout → generate seed → apply in game → observe requested layout) has not yet been closed on real hardware.

---

## 29. 🏁 END-TO-END PROVEN IN-GAME

Seed `10408711` (generated from a 3-pin request) was applied via the in-game Seed menu. **Confirmed by the player in-game AND auto-verified 31/31 by the mod dump.**

| Item | Requested | In-game result |
|---|---|---|
| MasterKey | `FREE:Garage/Spot (5)` | ✅ in the garage |
| Hammer | `FREE:Kitchen/Spot (29)` | ✅ on the kitchen table |
| CarKey | `PUZZLE:Safe` | ✅ in the safe |
| **Pliers** | **not pinned — predicted** `PUZZLE:DrawerCribRoom` | ✅ **in a drawer in the crib room** |

The `Pliers` result is the strongest single piece of evidence: it was **not** a constraint, it was an out-of-sample prediction of where an unconstrained item would land — and it was correct.

### Final validation ledger
| Seed | Runs | Result |
|---|---|---|
| 915074960 | 2 | 31/31 each |
| 123456789 | 1 | 31/31 |
| **10408711** (tool-generated) | 2 | **31/31 each** |

**3 independent seeds, 5 runs, 155/155 item placements correct.**

## 30. Known usability gap (next improvement)
Spot labels are `Area/Spot (n)` — the **area is meaningful but the spot number is not**. The player reported not knowing where most predicted locations physically are. The data exists (world XYZ for all 87 slots) but isn't human-readable.

Options, cheapest first:
1. **In-game spot identifier** — extend `SeedDumper` with a keybind that logs the nearest spot label to the player's current position. Walk around, build a personal map. Simple and reliable.
2. **Reference table** — export all 87 slots with area + world coordinates (already available in the config dumps).
3. **Auto-naming** — label each spot by its nearest named scene object (furniture/room geometry) via the mod.

---

## 31. 🐛 v1.0.1 BUG FIX — one-pin search hung with idle GPU

**Reported:** single pin + Search → stuck on "Starting search...", GPU ~0%, Cancel stuck on "Cancelling...".

**Root cause:** CPU candidate verification, not the GPU. Three compounding defects in `GpuSearchBackend.search()`:
1. Verification loop ignored `limit` — verified every candidate even after enough results existed.
2. Verification loop ignored `cancel_evt` — cancel was only polled between GPU dispatches.
3. Batch adapter scaled on **GPU dispatch time only**, so the batch ballooned toward max exactly when CPU verification was the bottleneck.

One pin ⇒ ~1/71 hit rate ⇒ a multi-million-seed batch yields ~100k+ candidates × ~6ms each = tens of minutes of serial CPU with the GPU idle. Tighter pin sets hid the bug (far lower hit rates).

**Fix:** verification loop now honours `limit`, polls `cancel_evt` every 16 candidates, and reports progress every 64; batch adaptation uses total elapsed time (dispatch + verify); `gui.py` rejects `limit < 1` (a falsy limit disables both break conditions and reinstates the hang).

**Measured:** 1 pin over full 2^32 — **hang → 0.1s**; cancel **never → instant**.
**Regressions re-run:** bit-exactness **6009/6009** (incl. 345 retry seeds), 915074960 ✓, 10408711 ✓, 6.01M seeds/sec. Nothing broken.

---

## 32. Launcher — `run.bat` / `run_debug.bat`

`run.bat` starts the GUI with **`pythonw.exe`** (no console) and immediately `exit`s, so no console window lingers. Falls back through `pythonw.exe` on PATH → `pyw.exe` → `%LOCALAPPDATA%\Programs\Python\Python3xx\pythonw.exe`, and only shows an error + `pause` if none is found.
`run_debug.bat` runs with plain `python` and a visible console for troubleshooting.

### 🐛 Non-obvious trap: `pythonw.exe` makes `sys.stderr` **None**
Under `pythonw` there is no console, so `sys.stdout`/`sys.stderr` are `None`. PyOpenCL emits a `CompilerWarning` while building the kernel; the warnings machinery writes to `sys.stderr`, which **raises on `None` and kills the process before the window appears** — the GUI simply never opens, with no visible error anywhere.

**Fix:** a guard at the top of `gui.py` (immediately after `from __future__ import annotations`) redirects missing streams to `logs/gui.log`, falling back to `os.devnull`. Warnings and tracebacks are now recorded instead of fatal.

### Two process lessons from debugging this
- **`ast.parse()` does NOT enforce the "`from __future__` must come first" rule; `compile()` does.** An `ast.parse` syntax check reported OK on a file that could not actually be imported. Use `compile()` for real validation.
- Two "LAUNCH FAILED" results were **test-harness artifacts**, not script bugs: PowerShell's `Start-Process` does not inherit `Set-Location`, so a relative `run.bat` argument was never found. Verified working once given a full path + `-WorkingDirectory`.

**Verified:** `run.bat` → GUI launches (responding), console self-closes (0 leftover windows), `logs/gui.log` contains only the harmless PyOpenCL warning.
