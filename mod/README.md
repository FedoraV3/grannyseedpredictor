# SeedDumper

A MelonLoader mod for **Granny: Legacy** that dumps `SeedManager`'s complete
serialized configuration (the simulation *input*) and the item placement it
produces (the *ground truth*) to JSON. Built to validate a Python
reimplementation of the game's item randomizer.

It patches `Il2Cpp.SeedManager.GeneratePlacement` with a Harmony Prefix
(dumps config, before placement) and Postfix (dumps the resulting placement,
after). It does not change any game behavior, does not touch `Seed` /
`RandomizeSeed`, and does not block or alter the original method — it only
observes.

## Build

```
cd mod
dotnet build -c Release
```

Output: `mod\bin\Release\SeedDumper.dll` (also copied to the project root as
`SeedDumper.dll`).

Targets `net6.0` and references, by `HintPath`, the game's own proxy
assemblies under `MelonLoader\Il2CppAssemblies\` and MelonLoader's own
`MelonLoader\net6\` runtime assemblies — both resolved from the game
install in place, read-only. If your game lives somewhere other than
`C:\Users\<you>\Desktop\Granny_Legacy`, override it:

```
dotnet build -c Release -p:GameDir="D:\Games\Granny_Legacy"
```

The build does **not** copy the DLL into the game's `Mods` folder. That is a
deliberate, manual step — see Install below.

## Install (manual — do this yourself)

1. Close the game if it's running.
2. Copy `SeedDumper.dll` into `Granny_Legacy\Mods\`.
3. Launch the game normally. MelonLoader will load the mod; check
   `MelonLoader\Latest.log` for a `SeedDumper loaded.` line.
4. Play until item placement happens (this is driven by
   `SeedManager.GeneratePlacement`, which the RE notes indicate runs once per
   level load with an internal retry loop — you do not need to do anything
   special to trigger it beyond loading into a level that uses the
   randomizer).
5. To remove: close the game, delete `SeedDumper.dll` from `Mods\`.

## Output

Two JSON files per `GeneratePlacement` call, written to:

```
C:\Users\ir0n1c\grannyseedpredictor\dumps\
```

- `config_<seed>_<timestamp>.json` — written in the **Prefix**, before
  placement runs. `<seed>` is whatever `SeedManager.Seed` reports at that
  instant (it may already reflect the value read from
  `PlayerPrefs["GameSeed"]`/`PlayerPrefs["RandomSeed"]` if those are applied
  earlier in `Awake`/`Start`; both raw PlayerPrefs values are also recorded
  separately inside the file so this is never ambiguous).
- `placement_<seed>_<timestamp>.json` — written in the **Postfix**, right
  after placement completes. `<seed>` is `SeedManager.Seed` at that point,
  i.e. the value actually used for the run (per the RE notes, the algorithm
  reseeds as `new Random(Seed + attempt)` per retry, but only ever exposes
  the base `Seed`, not the attempt count — if you need the attempt count,
  that is not currently observable from outside `GeneratePlacement` and
  would need its own patch).

Filenames include a timestamp so repeated calls (e.g. reloading a level, or
the algorithm's internal retry loop, which is *internal* to one
`GeneratePlacement` call and does not produce multiple dumps) never
overwrite each other. Every call is also logged with a running call index
(`GeneratePlacement call #N`) via `MelonLogger`, so you can correlate a
config/placement pair to a specific log line even if two calls land in the
same second.

A dump failure (unexpected null, missing component, etc.) is caught, logged
as an error via `MelonLogger`, and otherwise swallowed — it can never crash
or desync the game. If a dump is missing, check `Latest.log` for a
`... dump FAILED: ...` line.

### `config_*.json` schema

```jsonc
{
  "dumpType": "config",
  "callIndex": 1,                 // increments once per GeneratePlacement call this session
  "capturedAtUtc": "...",
  "seed": 900658064,
  "randomizeSeed": false,
  "activatePlacedItems": true,
  "fillAllFreeSpawns": true,
  "rigid": false,
  "escapeItemPuzzleChance": 0.5,
  "playerPrefs": { "GameSeed": 900658064, "RandomSeed": 0 },

  // sm.allItems, in exact list order (index order is RNG-relevant — never sorted).
  "allItems": [
    {
      "index": 0,
      "name": "Pliers",                 // GameObject.name
      "path": "Root/.../Pliers",        // full hierarchy path, root to leaf
      "itemSeedData": {                 // null if the GameObject has no ItemSeedData
        "itemName": "Pliers",
        "category": 3,                  // 1=escape, 2=escape+puzzle, 3=puzzle, 4=free (per RE notes)
        "containedItems": []
      }
    },
    ...
  ],

  "requiredEscapeItemNames": ["...", ...],   // in exact order

  // sm.puzzleDefs, in exact list order.
  "puzzleDefs": [
    {
      "index": 0,
      "puzzleName": "...",
      "priority": 0,
      "spawnPoint": { "name": "...", "path": "...", "instanceId": 12345 },
      "allowedItemNames": ["..."],
      "excludeItemNames": ["..."],
      "requiredItemNames": ["..."],
      "containedItems": ["..."],
      "forbiddenCombos": [
        { "itemName": "...", "forbiddenItem": "...", "forbiddenPuzzles": ["..."] }
      ]
    },
    ...
  ],

  // sm.spawnAreas, in exact array order.
  "spawnAreas": [
    {
      "index": 0,
      "areaName": "...",
      "maxItems": 3,
      "mandatoryItemName": null,
      "allowedItemNames": ["..."],
      "freeSpots": [                          // in exact array order
        { "name": "...", "path": "...", "instanceId": 67890 },
        ...
      ]
    },
    ...
  ]
}
```

### `placement_*.json` schema

```jsonc
{
  "dumpType": "placement",
  "callIndex": 1,          // matches the config dump's callIndex for the same GeneratePlacement call
  "capturedAtUtc": "...",
  "seed": 900658064,
  "items": [                // sm.allItems again, same order/indices as the config dump -> join on "index"
    {
      "index": 0,
      "name": "Pliers",
      "itemName": "Pliers",              // from ItemSeedData, null if the item has none
      "position": { "x": 1.0, "y": 2.0, "z": 3.0 },   // final world position
      "parent": { "name": "...", "path": "...", "instanceId": 67890 },  // null fields if unparented
      "activeSelf": true
    },
    ...
  ]
}
```

`callIndex` and array `index` are the join keys between the two files and
between `config` entries and `placement` entries for the same item/spawn
point. Transform `instanceId` is the disambiguator when two spawn points or
objects share a name.

## Files

- `SeedDumper.csproj` — project file; `<GameDir>` property points at the
  game install for resolving reference `HintPath`s.
- `src/SeedDumperMod.cs` — `MelonMod` entry point, assembly `MelonInfo`/
  `MelonGame` attributes, logger wiring.
- `src/SeedManagerPatch.cs` — the Harmony `[HarmonyPatch]` on
  `SeedManager.GeneratePlacement` (Prefix + Postfix) and all the dump logic.
- `src/JsonWriter.cs` — minimal hand-rolled JSON writer (no
  `System.Text.Json`; see the file's header comment for why).
- `src/DumpUtil.cs` — output directory / hierarchy-path helpers shared by
  the patch.
