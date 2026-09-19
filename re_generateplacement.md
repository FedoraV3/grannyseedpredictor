# SeedManager.GeneratePlacement — Full RE Report

Target: `GameAssembly.dll` (Granny Legacy, Unity 2022.3.62f2, IL2CPP x64), imagebase `0x180000000`.
All addresses below are VAs (imagebase already added).

Analyzed with `decompile` / `analyze_function` / `callees` / manual disasm cross-checks in IDA
(Hex-Rays decompiler, `il2cpp` fully-shared-generic codegen). Method bodies are heavily bloated
with IL2CPP metadata-init boilerplate (`sub_180138640` cctor triggers, `il2cpp_runtime_class_init`,
null-checks calling `sub_180138830`/`sub_180138820` throw-helpers) which has been stripped from the
pseudocode summaries below for readability; nothing semantically relevant was in that boilerplate.

Some IDB comments already existed from a prior session (tagged "TAS bonus:") at the Seed/rnd
construction site; I independently re-derived the same facts from the decompiler output and they
are confirmed correct.

---

## 1. Seed construction (Q1)

Location: `SeedManager.GeneratePlacement`, immediately before the retry loop.

```csharp
// pseudocode, faithful to decompile
SaveOriginalStates();               // snapshots each item's original transform (used to reset items between retries)
int attempt = 0;
bool success = false;

int seed;
if (PlayerPrefs.GetInt("RandomSeed") == 1 && this.RandomizeSeed)
    seed = UnityEngine.Random.RandomRangeInt(0, 999999999);   // Unity engine RNG, NOT System.Random
else
    seed = PlayerPrefs.GetInt("GameSeed");
this.Seed = seed;                    // Seed (field +0x40) IS written back — readable/displayable afterward

while (attempt < 50 && !success)
{
    attempt++;                                  // attempt starts at 1
    this.rnd = new System.Random(this.Seed + attempt);   // <-- deterministic RNG source
    RestoreOriginalStates();
    ... (placement logic, see below) ...
    success = !DetectCircularDependencies() && ValidatePuzzleDependencies();
}
```

Key facts:
* String literals resolved via repeatable comments in the IDB: `StringLiteral_2252` = `"RandomSeed"`,
  `StringLiteral_6078` = `"GameSeed"` (both `PlayerPrefs` keys).
* `UnityEngine.Random.RandomRangeInt(0, 999999999)` is used **only** as a one-shot fallback to pick a
  fresh `Seed` value when the `"RandomSeed"` PlayerPrefs toggle is `1` **and** `SeedManager.RandomizeSeed`
  (field `+0x44`) is `true`. This uses Unity's own engine RNG (`UnityEngine.Random`), completely
  separate from the `System.Random` instance used for placement. **If you already have a concrete
  integer `Seed` value you want to reproduce, this branch is irrelevant** — you only need to feed that
  integer into the loop below.
* `this.Seed` (field `+0x40`) is written unconditionally before the retry loop, so it is readable/
  displayable by other game code (e.g. UI) regardless of which branch set it.
* **Critical for a faithful Python port:** `this.rnd` (field `+0x50`) is **reconstructed fresh every
  retry attempt** as `new System.Random(Seed + attempt)`, where `attempt` starts at `1` (not `0`) and
  increments each failed validation. It is *not* one continuous RNG stream across attempts — each
  attempt gets an independent `System.Random` seeded with `Seed+attempt`.
* **`System.Random` algorithm — fully resolved by a prior session's independent RE** (see Section 10
  below): it is the standard **.NET Core Knuth subtractive lagged-Fibonacci generator**, confirmed
  bit-for-bit from the IL2CPP disassembly of the ctor/`InternalSample`/`Sample`/`Next*`/`NextDouble`.
  Use that dump directly for the Python port's RNG primitive — no further verification needed.

---

## 2. Shuffle&lt;T&gt; — exact algorithm (Q3)

`SeedManager.Shuffle<T>` at `0x1802D7590`. Decompiled and confirmed byte-for-byte:

```csharp
void Shuffle<T>(List<T> list)
{
    if (list == null) return;
    for (int i = list.Count - 1; i > 0; i--)
    {
        int j = rnd.Next(i + 1);      // Next(int) with upper bound = i+1, so j in [0, i] inclusive
        T temp = list[j];
        // decompiled order of operations:
        list[i] = temp;               // note: this stores old list[j] into slot i FIRST
        list[j] = /* old */ list[i];  // ...then old list[i] into slot j
        // net effect is an ordinary swap(list[i], list[j])
    }
}
```

This is the **classic backward (Durstenfeld) Fisher-Yates shuffle**, iterating `i` from
`Count-1` down to `1`, calling `rnd.Next(i+1)` (bound is `i+1`, giving `j ∈ [0,i]`), then swapping
`list[i]` and `list[j]`. This is exactly equivalent to a plain `swap(list[i], list[j])`; the
decompiler just materializes the temp copies in an order that looks unusual but resolves to a pure
swap (confirmed by tracing `sub_18019CD90`/struct-copy helper calls in the raw decompile — no data is
lost or duplicated). **A Python port must implement this precise variant** (not the alternate
"forward, `Next(0, n)`, no `+1`" variant) — getting this wrong changes every subsequent output.

Python equivalent:
```python
for i in range(len(lst) - 1, 0, -1):
    j = rnd.next_int(i + 1)   # your System.Random.Next(maxExclusive) port
    lst[i], lst[j] = lst[j], lst[i]
```

---

## 3. RNG call sequence inside one placement attempt (Q2)

All calls go through `this.rnd` (`System.Random` instance created fresh this attempt). In call order:

### Step A — Puzzle→Item greedy random assignment
For each `puzzleDef` in `orderedPuzzles = puzzleDefs.Where(p => p.spawnPoint != null).OrderBy(p => p.Priority).ToList()`
(LINQ `OrderBy` is a **stable** sort, so ties in `Priority` preserve the original `puzzleDefs` list order):

* A candidate pool is precomputed per puzzle (no RNG):
  `validItems.Where(notUsed).Where(IsItemAllowedForPuzzle(item,puzzle)).Where(itemSeedData.<field> != 4).Where(IsSafeContainerPlacement(item,puzzle)).ToList()`
* If pool non-empty: **`rnd.Next(pool.Count)`** — 1 call — picks item, assigns `assignedItem[puzzleDef] = item`,
  then removes that item from every *other* puzzle's candidate pool (list `.Remove()`, no RNG).
* If pool empty: 0 RNG calls, `assignedItem[puzzleDef] = null`.

→ **1 `Next(int)` call per puzzle with a non-empty pool**, in `orderedPuzzles` order.

### Step B — Required escape items
For each `name` in `requiredEscapeItemNames` (List, in list order):

* Look up `item = itemsByName[name]`. If not found, skip (0 RNG).
* If `item` is already one of the values assigned in Step A (`assignedItem.Values.Any(x=>x==item)`), skip (0 RNG).
* Otherwise: **`rnd.NextDouble()`** — 1 call, `roll`.
  * If `EscapeItemPuzzleChance <= roll` → place item in a **free area** via `PlaceItemInFreeArea`
    (see its own RNG cost below); mark used.
  * Else (roll wins the puzzle-chance check):
    * If `puzzleDefs.Count == 0` → free-area placement (as above).
    * Else compute `eligible = puzzleDefs.Where(p => p!=null && p.spawnPoint!=null && !usedPuzzleSpawns.Contains(p.spawnPoint) && assignedItem[p]==null && IsItemAllowedForPuzzle(item,p)).ToList()`
      (note: this scans the **original, unsorted, unfiltered `puzzleDefs` list**, not `orderedPuzzles`).
      * If `eligible.Count == 0` → free-area placement.
      * Else: **`rnd.Next(eligible.Count)`** — 1 call — picks puzzle, places item there directly
        (`PlaceItemAtTransform`, no further RNG), marks spawn/name used, `puzzleHasItem[puzzle]=true`.

→ Per escape item: `0` or `1` `NextDouble()`, and conditionally `0` or `1` `Next(int)`, plus whatever
`PlaceItemInFreeArea` costs if that branch is taken.

### Step C — Place the puzzle-assigned items from Step A
For each `puzzleDef` in `orderedPuzzles` again: if its spawn wasn't already consumed by Step B, and it
has an assigned item, place it directly at `puzzleDef.spawnPoint` (`PlaceItemAtTransform`).
**No RNG in this step** (the random choice already happened in Step A); if the assigned item turns out
to already be `usedItemNames` (collision with something Step B placed), the assignment is simply
cleared (`assignedItem[puzzleDef]=null`) and the puzzle is left for the backfill pass.

### Step D — Backfill empty puzzles
For each `puzzleDef` in `orderedPuzzles` again, if its spawn is still unused and `puzzleHasItem[name]==false`:

* Tier 1 (strict): `allItems.Where(valid).Where(notUsed).Where(IsItemAllowedForPuzzle(item,puzzleDef)).Where(typeFilter!=4).ToList()`.
  If non-empty: **`rnd.Next(count)`** — 1 call — place chosen item at the puzzle's spawn point.
* Else Tier 2 (relaxed — drops the `IsItemAllowedForPuzzle` check): `allItems.Where(valid).Where(notUsed).Where(typeFilter!=4).ToList()`.
  If non-empty: **`rnd.Next(count)`** — 1 call — place chosen item.
* Else: puzzle stays empty (0 RNG; this is presumably what `ValidatePuzzleDependencies` can later reject).

→ `0` or `1` `Next(int)` per still-empty puzzle, in `orderedPuzzles` order.

### Step E — Mandatory-item spawn areas
For each `spawnArea` in `this.spawnAreas` (array, in array order), if `mandatoryItemName` is set and
that item isn't already used:
`PlaceItemInSpecificFreeArea(item, spawnArea)` → internally:
`freeSpots = area.freeSpots.Where(spot => spot!=null && !area.usedSpots.Contains(spot)).ToList()`;
if `freeSpots.Count == 0` → **0 RNG, silent no-op** (item is *not* placed, but also not marked used in
this early-return case! — see `PlaceItemInSpecificFreeArea` decompile, function returns before adding
to `usedItemNames` if count is 0 — actually the `usedItemNames.Add` happens in the **caller**
(`GeneratePlacement`) only guarded by "item found in dict" and "not already used", i.e. even when
`PlaceItemInSpecificFreeArea` does nothing the caller still adds the name to `usedItemNames`. **This
looks like a bug/quirk in the original game code** — flagged below); else **`rnd.Next(freeSpots.Count)`**
— 1 call — pick and place, add to `area.usedSpots`.

### Step F — fillAllFreeSpawns (only if `this.fillAllFreeSpawns == true`)
```
remaining = allItems.Where(valid).Where(notUsed).ToList();
Shuffle(remaining);                       // Durstenfeld: (remaining.Count - 1) Next(int) calls if Count>1, else 0
foreach (item in remaining)               // shuffled order
    PlaceItemInFreeArea(item);            // see cost below, called once per item
```

### `PlaceItemInFreeArea(item)` RNG cost (called from Steps B, F, and standalone)
```
areas = spawnAreas.Where(a => a.currentItems < a.maxItems
                            && a.freeSpots.Length != 0
                            && (a.allowedItemNames.Count==0 || a.allowedItemNames.Contains(item.itemName)))
                   .ToList();
Shuffle(areas);                            // (areas.Count - 1) Next(int) calls if Count>1, else 0
foreach (area in areas)                    // shuffled order
{
    freeSpots = area.transforms.Where(s => s!=null && !area.usedSpots.Contains(s)).ToList();
    if (freeSpots.Count != 0)
    {
        idx = rnd.Next(freeSpots.Count);   // 1 call
        place at freeSpots[idx]; area.currentItems++; area.usedSpots.Add(freeSpots[idx]);
        return;                            // stop after first successful area
    }
    // else continue to next shuffled area, no RNG
}
// none of the shuffled areas had room — fallback: linear scan the ORIGINAL (unshuffled) spawnAreas array
foreach (area in this.spawnAreas)          // original array order, not shuffled
{
    if (!(area.currentItems < area.maxItems)) continue;
    freeSpots = area.transforms.Where(s => s!=null && !area.usedSpots.Contains(s)).ToList();
    if (freeSpots.Count != 0) { idx = rnd.Next(freeSpots.Count); place; break; }  // 1 call, then stop
}
```
So each `PlaceItemInFreeArea` call costs `(shuffleAreaCount-1)` `Next` calls for the shuffle (0 if
≤1 eligible area) **plus exactly 0 or 1** further `Next(int)` call for the spot pick (0 only if truly
no area anywhere has a free matching spot).

### `PlaceItemInSpecificFreeArea(item, area)` RNG cost
`freeSpots = area.freeSpots.Where(s=>s!=null && !area.usedSpots.Contains(s)).ToList()`; if `Count==0`
→ 0 RNG (silent no-op, see bug note above); else `rnd.Next(freeSpots.Count)` — 1 call.

### After the full pipeline
```
Debug.Log(...)  // iterates puzzleHasItem Dictionary<string,bool> for logging only — hash order, NO RNG effect, cosmetic only
success = !DetectCircularDependencies() && ValidatePuzzleDependencies();
```
Both `DetectCircularDependencies` (`0x18024EC00`) and `ValidatePuzzleDependencies` (`0x180253CF0`) were
fully decompiled and contain **zero RNG usage** — pure graph/HashSet/Dictionary bookkeeping (DFS cycle
check, orphan-item check). Confirmed via grep of their decompiled pseudocode for `Next`/`Random` (no hits).

---

## 4. Overall placement pipeline in execution order (Q4)

```
GeneratePlacement():
    SaveOriginalStates()
    seed = (RandomSeed pref==1 && RandomizeSeed) ? UnityEngine.Random.RandomRangeInt(0,999999999)
                                                  : PlayerPrefs.GetInt("GameSeed")
    this.Seed = seed
    attempt = 0; success = false
    while attempt < 50 and not success:
        attempt += 1
        rnd = new System.Random(seed + attempt)
        RestoreOriginalStates()
        usedItemNames.Clear(); usedPuzzleSpawns.Clear(); puzzleHasItem.Clear()
        for p in puzzleDefs: puzzleHasItem[p.puzzleName] = false
        for area in spawnAreas: area.currentItems = 0; area.usedSpots.Clear()
        BuildLookup()                                   # itemsByName[name] = gameObject, from allItems

        validItems   = allItems.Where(hasItemSeedData).ToList()
        orderedPuzzles = puzzleDefs.Where(hasSpawnPoint).OrderBy(Priority).ToList()   # stable sort

        # --- A: greedy random puzzle<-item assignment ---
        candidatePools = { p: validItems.Where(notUsed & allowedForPuzzle(p) & typeFilter & safeContainer).ToList()
                            for p in orderedPuzzles }
        assignedItem = {}
        for p in orderedPuzzles:
            assignedItem[p] = None
            pool = candidatePools[p]
            if pool: 
                chosen = pool[rnd.Next(len(pool))]
                assignedItem[p] = chosen
                for other in candidatePools: if other != p: candidatePools[other].remove(chosen)

        # --- B: required escape items ---
        for name in requiredEscapeItemNames:
            item = itemsByName.get(name)
            if item and item not in assignedItem.values():
                if EscapeItemPuzzleChance <= rnd.NextDouble():
                    PlaceItemInFreeArea(item); usedItemNames.add(name)
                else:
                    eligible = [p for p in puzzleDefs if p and p.spawnPoint and p.spawnPoint not in usedPuzzleSpawns
                                and assignedItem.get(p) is None and allowedForPuzzle(item, p)]
                    if eligible:
                        p = eligible[rnd.Next(len(eligible))]
                        PlaceItemAtTransform(item, p.spawnPoint, isFreeSpawn=False)
                        usedPuzzleSpawns.add(p.spawnPoint); usedItemNames.add(name); puzzleHasItem[p.puzzleName]=True
                    else:
                        PlaceItemInFreeArea(item); usedItemNames.add(name)

        # --- C: place Step-A assignments ---
        for p in orderedPuzzles:
            if p.spawnPoint not in usedPuzzleSpawns and assignedItem.get(p):
                item = assignedItem[p]; name = item.itemSeedData.itemName
                if name in usedItemNames: assignedItem[p] = None   # collided with an escape-item placement
                else:
                    PlaceItemAtTransform(item, p.spawnPoint, isFreeSpawn=False)
                    usedPuzzleSpawns.add(p.spawnPoint); usedItemNames.add(name); puzzleHasItem[p.puzzleName]=True

        # --- D: backfill still-empty puzzles ---
        for p in orderedPuzzles:
            if p.spawnPoint not in usedPuzzleSpawns and not puzzleHasItem[p.puzzleName]:
                tier1 = [i for i in allItems if valid(i) and notUsed(i) and allowedForPuzzle(i,p) and typeFilter(i)]
                pool = tier1 if tier1 else [i for i in allItems if valid(i) and notUsed(i) and typeFilter(i)]
                if pool:
                    item = pool[rnd.Next(len(pool))]
                    PlaceItemAtTransform(item, p.spawnPoint, isFreeSpawn=False)
                    usedPuzzleSpawns.add(p.spawnPoint); usedItemNames.add(item.name); puzzleHasItem[p.puzzleName]=True

        # --- E: mandatory-item spawn areas ---
        for area in spawnAreas:
            if area.mandatoryItemName:
                item = itemsByName.get(area.mandatoryItemName)
                if item and area.mandatoryItemName not in usedItemNames:
                    PlaceItemInSpecificFreeArea(item, area)     # may silently no-op if area has no free spot
                    usedItemNames.add(area.mandatoryItemName)   # added regardless of whether placement succeeded (see bug note)

        # --- F: fill remaining free spawns ---
        if fillAllFreeSpawns:
            remaining = [i for i in allItems if valid(i) and notUsed(i)]
            Shuffle(remaining)
            for item in remaining: PlaceItemInFreeArea(item)

        Debug.Log(...)   # per puzzleHasItem entry, dict order, cosmetic
        success = not DetectCircularDependencies() and ValidatePuzzleDependencies()
    # loop ends; if 50 attempts exhausted without success, the last attempt's (invalid) placement stands
```

**Placement priority order**: puzzle items are assigned/placed *before* free-area items — Steps A–D
(puzzle-related) all happen before Step F (`fillAllFreeSpawns`, generic free-area filling). Required
escape items (Step B) are interleaved between the initial greedy puzzle assignment (Step A) and the
final puzzle placement (Step C) — they get first refusal on unassigned puzzles that Step A didn't
already claim.

**`EscapeItemPuzzleChance`** is a `float` (field `+0x48`) compared against a `rnd.NextDouble()` roll
(`double` in `[0,1)`): `if (chance <= roll)` → place in free area (so a **higher** `chance` value makes
free-area placement *more* likely — i.e. despite the name, a chance of `1.0` means escape items almost
always end up in free areas, and `0.0` means the `roll` check is never satisfied and the code always
attempts puzzle-attachment first (falling back to free-area only if no eligible puzzle exists). This
polarity is easy to get backwards in a port — **verify against actual observed game behavior if the
naming assumption seems off; flagged as worth double-checking, though the comparison direction is
unambiguous in the decompiled code**.

**`Rigid`** (field `+0x47`) does not affect any RNG or placement algorithm — it only affects physics
(`Rigidbody.isKinematic`) in `PlaceItemAtTransform`: `isKinematic = !isFreeSpawn || !Rigid`, i.e. objects
placed in puzzles are always kinematic; free-area-placed objects become non-kinematic (physically
"live"/droppable) only when `Rigid == true`.

**`fillAllFreeSpawns`** only gates Step F (whether leftover unused items get shoved into arbitrary free
spawn areas at all).

---

## 5. Retry / rejection loop (Q5)

Confirmed: the **entire** placement pipeline (Steps A–F, including all `BuildLookup`/clearing) is
wrapped in a `while (attempt < 50 && !success)` loop. On each iteration:

* `rnd` is **completely re-seeded** as `new System.Random(Seed + attempt)` (not reused/continued).
* All mutable placement state (`usedItemNames`, `usedPuzzleSpawns`, `puzzleHasItem`, each spawn area's
  `currentItems`/`usedSpots`) is cleared/reset, and item transforms are reset via `RestoreOriginalStates()`.
* After a full pass, validity is `!DetectCircularDependencies() && ValidatePuzzleDependencies()`.
  If invalid, the loop retries with `attempt+1` (i.e. a **different** effective seed, `Seed+attempt`,
  not the same RNG stream continued or resumed).
* Cap is **50 attempts**. If all 50 fail, the loop simply exits with whatever the 50th (invalid)
  attempt produced — there's no explicit "give up" flag/exception visible in this function.

**For a faithful Python port**: you must actually run `DetectCircularDependencies`/`ValidatePuzzleDependencies`-
equivalent logic (or otherwise determine whether a given `Seed+attempt` combination is "valid") to know
which attempt number's RNG stream actually produced the final result. If you already know from the live
game which `Seed` was configured and it succeeded on the first attempt (the overwhelmingly common case
when the puzzle/item data is well-formed), `attempt=1` (i.e. `System.Random(Seed+1)`) is enough — but do
not assume this without verifying, since a data configuration that occasionally creates circular puzzle
dependencies or orphaned items would force `attempt=2,3,...` and completely change the RNG stream (and
thus the entire layout) relative to naively using `System.Random(Seed)` or `System.Random(Seed+1)`
blindly.

---

## 6. Hash/Dictionary-order dependence (Q6)

**Finding: no gameplay-relevant HashSet/Dictionary enumeration order dependency was found.** Every
sequence that drives an `rnd.Next(...)`/`rnd.NextDouble()` call, or that determines *which* item/area/
puzzle is at a given list index for a subsequent RNG pick, is built from a `List<T>` (via `.ToList()`)
or a fixed array (`SpawnArea[]`), never iterated directly off a `HashSet<T>` or `Dictionary<K,V>`.
Specifically:

* `allItems`, `puzzleDefs`, `requiredEscapeItemNames`, `spawnAreas` are all `List<T>`/array fields —
  deterministic index order, matching Unity inspector/serialization order.
* `validItems`, `orderedPuzzles`, all `candidatePools[p]`, `eligible` (Step B), `tier1`/`tier2` (Step D),
  `areas`/`freeSpots` (inside `PlaceItemInFreeArea`/`PlaceItemInSpecificFreeArea`) are all produced via
  LINQ `.Where(...).ToList()` (optionally `.OrderBy(...)`, which is a stable sort) over those lists —
  fully deterministic given the same source-list order and same predicate results.
* The only `Dictionary`/`HashSet` **enumerations** found are:
  * `BuildLookup`'s iteration is actually over `allItems` (a `List<GameObject>`), not a dictionary — it
    only *writes into* `itemsByName` (a `Dictionary<string,GameObject>`); it never reads it back via
    enumeration.
  * The final `Debug.Log` loop iterates `puzzleHasItem` (`Dictionary<string,bool>`) — this happens
    **after** all RNG-driven placement decisions have already been made and only affects the printed
    log line order, not the placement result. **No RNG or placement-order impact.**
  * `assignedItem.Values.Any(x=>x==item)` (Step B) and `candidatePools.Keys`/removal-loop (Step A) both
    enumerate a `Dictionary<PuzzleDef,...>` whose keys were inserted in `orderedPuzzles` order with no
    removals — .NET's `Dictionary` preserves insertion order under those conditions, but even if it
    didn't, both usages are order-independent (`Any`/existence checks, or removing the same item from
    every entry regardless of visit order) and consume no RNG.

**Conclusion for the Python port**: you do **not** need to replicate .NET's internal `Dictionary`/
`HashSet` bucket/hash ordering anywhere in this algorithm — plain Python `list`s in the same order as
the serialized Unity data, plus a faithful `System.Random` port, are sufficient. (`itemsByName` and
`usedItemNames`/`usedPuzzleSpawns`/`puzzleHasItem` can be ordinary Python `dict`/`set` — only used for
O(1) lookups/membership tests, never enumerated in an order-sensitive way during placement.)

---

## 7. Open questions / things flagged as uncertain (do not silently assume)

* **`b__4` lambda** (`SeedManager.<>c__DisplayClass20_1._GeneratePlacement_b__4`, used in Step A's
  candidate-pool filter) could not be independently decompiled — its `MethodInfo` slot resolves to a
  non-code metadata token rather than a direct function address in this IDB (likely IL2CPP
  identical-code-folding shared it with another lambda's native body). **By strong structural analogy**
  with its sibling `b__6` (`IsSafeContainerPlacement`) and the later `b__11` (`IsItemAllowedForPuzzle`,
  used in the exact same "candidate pool for puzzle p" role in Step D), I infer **`b__4` = `IsItemAllowedForPuzzle(item, puzzleDef)`**. This is a GUESS based on code-structure symmetry, not a direct decompile — verify against `IsItemAllowedForPuzzle` (`0x180252200`) behavior if in doubt.
* **`itemSeedData.<field> != 4` type filter** (lambdas `b__20_5`/`b__20_12`/`b__20_15`, reused across
  Steps A and D): the decompiler shows this as `component_ptr->_1.byval_arg.bits != 4`, which reads like
  a mistyped/misdecoded field access rather than a clean enum comparison (Hex-Rays appears to be
  misinterpreting the `ItemSeedData` component's actual field layout as if it were `Il2CppClass.byval_arg`
  metadata). The **direction** of the filter (excludes items where this field equals the literal `4`)
  is confirmed, but the **semantic meaning** (which `ItemSeedData` field, and what enum/value `4`
  represents — plausibly an "ItemType" or "PlacementCategory" enum value for e.g. "Escape" or
  "KeyItem") is **not verified** from the decompile alone. Recommend inspecting `ItemSeedData`'s actual
  managed field layout (e.g. via `il2cpp_dump.cs`/metadata if available) before hardcoding a Python
  equivalent — GUESS flagged.
* **Apparent quirk/bug in Step E** (`PlaceItemInSpecificFreeArea` mandatory-item handling): the caller
  in `GeneratePlacement` adds the mandatory item name to `usedItemNames` unconditionally after calling
  `PlaceItemInSpecificFreeArea`, even though that function can silently do nothing if the area's
  `freeSpots.Where(...).ToList()` is empty (its early `return` on `Count==0` happens before any
  placement or list-add, with no signal back to the caller). Net effect: an item can be marked "used"
  without ever actually being placed anywhere, if its mandatory spawn area has no free spot left. This
  is a faithful reproduction of the **original game's** behavior (verified from decompiled bytes), not
  a mistake in this analysis — flagging it because a "smarter" reimplementation might be tempted to
  "fix" this, which would silently diverge from the real game's output for that edge case.
* ~~`System.Random` exact algorithm not independently re-derived~~ — **RESOLVED, see Section 10**: a
  prior session already fully reverse-engineered `System.Random` bit-for-bit for this binary. It is
  the standard .NET Core Knuth subtractive generator; use that dump directly.

---

## 8. Key addresses / symbols for reference

| Symbol | Address |
|---|---|
| `SeedManager.GeneratePlacement` | `0x18024F770` |
| `SeedManager.BuildLookup` | `0x18024E920` |
| `SeedManager.PlaceItemInFreeArea` | `0x1802529E0` |
| `SeedManager.PlaceItemInSpecificFreeArea` | `0x180252FD0` |
| `SeedManager.PlaceItemAtTransform` | `0x180252780` |
| `SeedManager.Shuffle<T>` | `0x1802D7590` |
| `SeedManager.ValidatePuzzleDependencies` | `0x180253CF0` |
| `SeedManager.DetectCircularDependencies` | `0x18024EC00` |
| `SeedManager.SaveOriginalStates` | `0x180253470` |
| `SeedManager.RestoreOriginalStates` | `0x180253190` |
| `SeedManager.GetEffectivePuzzleOfItem` | `0x180251930` |
| `SeedManager.IsItemAllowedForPuzzle` | `0x180252200` |
| `SeedManager.IsSafeContainerPlacement` | `0x1802523F0` |
| `System.Random..ctor(int)` (IL2CPP thunk) | `0x1804680D0` |
| `UnityEngine.Random.RandomRangeInt` | `0x180728F30` |
| `UnityEngine.PlayerPrefs.GetInt` | `0x180727780` |
| `StringLiteral_2252` = `"RandomSeed"` | `0x180c39198` |
| `StringLiteral_6078` = `"GameSeed"` | `0x180c4e0b0` |
| Lambda `GeneratePlacement_b__20_0` (valid-item filter) | `0x180256120` |
| Lambda `GeneratePlacement_b__20_1` (puzzle has spawnPoint) | `0x1802563c0` |
| Lambda `GeneratePlacement_b__20_2` (OrderBy key = Priority) | `0x180256420` |
| Lambda `GeneratePlacement_b__20_3` (notUsed, instance) | `0x180253c60` |
| Lambda `..DisplayClass20_1_b__6` (IsSafeContainerPlacement) | `0x180256580` |
| Lambda `..DisplayClass20_2_b__7` (item identity check) | `0x1802565b0` |
| Lambda `..DisplayClass20_2_b__8` (eligible unfilled puzzle for escape item) | `0x180256610` |
| Lambda `..DisplayClass20_3_b__11` (IsItemAllowedForPuzzle, tier1 backfill) | `0x180256550` |
| Lambda `..PlaceItemInFreeArea_b__0` (area eligibility) | `0x180256820` |
| Lambda `..PlaceItemInFreeArea_b__1`/`b__2` (free-spot filter) | `0x1802568d0` / `0x180256970` |
| Lambda `..PlaceItemInSpecificFreeArea_b__0` (free-spot filter) | `0x180256a10` |

## 9. IDB annotations added this session

* `set_comments` at `0x18024F770` (GeneratePlacement entry): full RNG-call-order summary.
* `append_comments` at `0x1802D7590` (Shuffle): exact algorithm description.
* `append_comments` at `0x1802529E0` (PlaceItemInFreeArea): area/spot selection + shuffle + fallback description.
* `append_comments` at `0x180252FD0` (PlaceItemInSpecificFreeArea): free-spot pick description.
* Pre-existing "TAS bonus" comments at the `Seed`/`rnd` construction site (left in place, independently verified correct).

Raw decompilation dumps used to produce this report are appended to
`for_claude_the_logic_pro.txt` under searchable `[[IDA:...]]` tags.

---

## 10. `System.Random` algorithm — resolved via prior session's RE (no longer uncertain)

`for_claude_the_logic_pro.txt` already contains a prior session's full bit-for-bit
reverse-engineering of `System.Random..ctor`/`InternalSample`/`Sample`/`Next`/`Next(int)`/
`Next(int,int)`/`NextDouble` for this exact binary (tags `[[IDA:SystemRandom_*]]`, full report at
`C:\Users\ir0n1c\grannyseedpredictor\re_system_random.md`). It is confirmed to be the **standard
.NET Core `System.Random`** implementation: classic Knuth subtractive lagged-Fibonacci generator,
`MSEED=161803398`, `MBIG=2147483647`, seed normalization `(seed==int.MinValue) ? int.MaxValue :
Math.Abs(seed)` (the modern .NET Core-style fix, not the legacy Framework overflow-throw behavior),
55-element state array, 4 warm-up passes, `_inext=0`/`_inextp=21` initial indices. Only one `Random`
implementation exists in the binary (no Xoshiro/CompatPrng/Net5Compat variants, no AppContext-switch
dispatch) — so there is no ambiguity about which algorithm variant applies. **Use that dump directly**
as the reference for the Python `System.Random` port; this resolves the uncertainty flagged in
Section 1 and the (now struck-through) bullet in Section 7.
