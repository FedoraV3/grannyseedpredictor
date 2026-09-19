# SeedManager Predicates & Validators — Full RE Report

Target: `GameAssembly.dll` (Granny Legacy, Unity 2022.3.62f2, IL2CPP x64), imagebase `0x180000000`.
Follow-up session to `re_generateplacement.md`. All addresses are VAs. Cross-checked against
`C:\Users\ir0n1c\Desktop\Il2CppDumper-win-v6.7.46\dump.cs` (Il2CppDumper output, generated directly
from this build's `global-metadata.dat` — ground truth for managed class layout/RVAs) wherever a
static decompile was ambiguous. Raw dumps appended to
`C:\Users\ir0n1c\grannyseedpredictor\for_claude_the_logic_pro.txt` under
`[[IDA:Predicates_ResolvedGuesses]]`, `[[IDA:PuzzleDef_Fields_confirmed]]`,
`[[IDA:IsItemAllowedForPuzzle_0x180252200_full]]`, `[[IDA:IsSafeContainerPlacement_0x1802523F0_full]]`,
`[[IDA:GetEffectivePuzzleOfItem_0x180251930_summary]]`, `[[IDA:ValidatePuzzleDependencies_0x180253CF0_pseudocode]]`,
`[[IDA:DetectCircularDependencies_0x18024EC00_pseudocode]]`, `[[IDA:SpawnArea_TrueLayout_and_Q6_resolution]]`.

---

## 0. Confirmed struct layouts (ground truth: this IDB's declared structs + dump.cs agree exactly)

```csharp
class ItemSeedData : MonoBehaviour {           // object header 0x20 bytes (MonoBehaviour base)
    string itemName;             // abs 0x20
    int    category;             // abs 0x28   [Tooltip: "1=escape-only, 2=escape+puzzle, 3=puzzle-only, 3=free"]
    List<string> containedItems; // abs 0x30
}                                               // Tooltip has a typo: the "3=free" should read "4=free"

class PuzzleDef {                              // plain object header 0x10 bytes
    string puzzleName;                    // abs 0x10
    Transform spawnPoint;                 // abs 0x18
    List<string> allowedItemNames;        // abs 0x20
    List<string> excludeItemNames;        // abs 0x28
    int Priority;                         // abs 0x30
    List<string> requiredItemNames;       // abs 0x38
    List<PuzzleForbiddenCombo> forbiddenCombos; // abs 0x40
    List<string> containedItems;          // abs 0x48
}

class SpawnArea {                              // plain object header 0x10 bytes
    string areaName;                 // abs 0x10
    Transform[] freeSpots;           // abs 0x18
    int maxItems;                    // abs 0x20   (ctor default = 1)
    int currentItems;                // abs 0x24
    List<Transform> usedSpots;       // abs 0x28   (ctor default = new List<Transform>())
    List<string> allowedItemNames;   // abs 0x30   (ctor default = new List<string>())
    string mandatoryItemName;        // abs 0x38   (NO ctor default -> stays null)
}
```

`PuzzleDef.requiredItemNames` (abs `0x38`) and `PuzzleDef.forbiddenCombos` (abs `0x40`) are **not**
read by `IsItemAllowedForPuzzle`. `requiredItemNames` **is** read by `IsSafeContainerPlacement`,
`ValidatePuzzleDependencies`, and `DetectCircularDependencies`. `forbiddenCombos` has no xrefs found
in any of the seed-placement functions in this build — either dead data or consumed elsewhere outside
scope.

---

## 1. `IsItemAllowedForPuzzle(item, puzzle)` — `0x180252200`

```csharp
bool IsItemAllowedForPuzzle(GameObject item, PuzzleDef puzzle) {
    ItemSeedData isd = item?.GetComponent<ItemSeedData>();
    if (isd == null) return false;
    // puzzle == null / isd fields null falls to an IL2CPP NullReferenceException throw helper on this path
    if (puzzle.allowedItemNames != null) {
        if (puzzle.allowedItemNames.Count > 0)
            return puzzle.allowedItemNames.Any(n => n.Trim().Equals(isd.itemName.Trim(), StringComparison.OrdinalIgnoreCase));
        // Count == 0 (present but empty): fall through to the exclude-list check
        if (puzzle.excludeItemNames != null) {
            if (puzzle.excludeItemNames.Count <= 0) return true;
            return !puzzle.excludeItemNames.Any(n => n.Trim().Equals(isd.itemName.Trim(), StringComparison.OrdinalIgnoreCase));
        }
    }
    throw; // unreachable in well-formed data (see below)
}
```

Confirmed by decompile of `0x180252200` plus the folded lambda bodies at `0x1802567C0`
(`IsItemAllowedForPuzzle_b__0`/`_b__1`, both fold to the *same* native address — see §5):

```
return x.Trim().Equals(itemSeedData.itemName.Trim(), comparisonType: 5);
```
`comparisonType == 5` is `System.StringComparison.OrdinalIgnoreCase` (standard BCL enum value,
confirmed via `int_convert`; the enum type itself isn't emitted with a name in this stripped IL2CPP
binary since it's a well-known framework enum).

### Answers to Q1
- **`allowedItemNames` empty → allow all** (subject to the exclude list), **not** allow-none. This is
  the ALLOW-mode: if `allowedItemNames.Count > 0`, only listed items pass (allowlist semantics); if
  `Count == 0`, the allowlist imposes no restriction and control falls to the exclude list.
- **`excludeItemNames`**: only consulted when `allowedItemNames` is empty/absent. Empty
  `excludeItemNames` (`Count <= 0`) → allow. Non-empty → block if the item's name matches any entry
  (BLOCKLIST semantics).
- **`requiredItemNames` and `forbiddenCombos` do not participate in this predicate at all** — they are
  read by other functions (`ValidatePuzzleDependencies`/`DetectCircularDependencies` for
  `requiredItemNames`; no consumer found in this build for `forbiddenCombos`).
- **Matching field**: `ItemSeedData.itemName` (a plain string field on the component), **not** the
  `GameObject`'s own `.name`.
- **Case sensitivity**: case-**insensitive** (`StringComparison.OrdinalIgnoreCase`), and **both sides
  are `.Trim()`'d** (leading/trailing whitespace stripped) before comparison — this whitespace-trim
  detail was not previously documented and matters for a byte-exact Python port if any scene data has
  incidental whitespace in item/allow/exclude-list name strings.

---

## 2. `IsSafeContainerPlacement(containerItem, candidatePuzzle)` — `0x1802523F0`

```csharp
bool IsSafeContainerPlacement(GameObject containerItem, PuzzleDef candidatePuzzle) {
    ItemSeedData isd = containerItem?.GetComponent<ItemSeedData>();
    if (isd == null) return true;                       // nothing to guard, trivially safe
    if (isd.containedItems != null && isd.containedItems.Count != 0) {
        foreach (string containedName in isd.containedItems) {
            foreach (PuzzleDef p in this.puzzleDefs) {
                // p.requiredItemNames (abs offset 0x38 — verified via disasm pointer math p+56)
                if (p.requiredItemNames != null && p.requiredItemNames.Contains(containedName)) {
                    if (p.spawnPoint == candidatePuzzle.spawnPoint)
                        return false;   // unsafe: this puzzle needs `containedName` as a prerequisite,
                                        // and we'd be placing the container that HIDES that very item
                                        // at that same puzzle's own spawn location
                }
            }
        }
    }
    return true;
}
```

### Answer to Q2
`IsSafeContainerPlacement` guards against a **self-referential/unsolvable placement**: for every item
name the candidate container *contains* (`ItemSeedData.containedItems`), it scans every `PuzzleDef` in
`this.puzzleDefs` and checks whether that puzzle's `requiredItemNames` list demands the contained item
as a prerequisite. If such a puzzle exists **and** its `spawnPoint` equals the `candidatePuzzle`'s own
`spawnPoint` (i.e., we are about to place the container item precisely at that puzzle's location), the
placement is rejected (`false`) — because the item needed to unlock that puzzle would be sealed inside
a container sitting right at the puzzle itself, an unsolvable trap. `containedItems` (on `ItemSeedData`)
is the list of item names hidden inside the container; the comparison target on the `PuzzleDef` side is
`requiredItemNames` (**not** `PuzzleDef.containedItems`, which is a separate, unrelated field at a
different offset — confirmed by the exact `p+0x38` pointer arithmetic in the disassembly, matching
`requiredItemNames`'s declared offset).

Note: an earlier pass at this decompile mistakenly assumed the `PuzzleDef`-side field was
`containedItems`; re-verifying the raw pointer offset (`p + 56` = `p + 0x38`) against the confirmed
`PuzzleDef_Fields` struct settles it as `requiredItemNames`.

---

## 3. `GetEffectivePuzzleOfItem(itemName, visited=null)` — `0x180251930`

```csharp
PuzzleDef GetEffectivePuzzleOfItem(string itemName, HashSet<string> visited = null) {
    if (string.IsNullOrEmpty(itemName)) return null;
    visited ??= new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    if (visited.Contains(itemName)) return null;     // cycle guard against infinite recursion
    visited.Add(itemName);
    if (!itemsByName.TryGetValue(itemName, out GameObject itemGO) || itemGO == null) return null;

    foreach (PuzzleDef p in this.puzzleDefs) {
        foreach (GameObject g in this.allItems) {
            var gIsd = g?.GetComponent<ItemSeedData>();
            if (gIsd?.containedItems != null &&
                gIsd.containedItems.Any(n => n.Trim().Equals(itemName, OrdinalIgnoreCase))) {
                // g "contains" the target item by name
                if (Vector3.Distance(g.transform.position, p.spawnPoint.position) < 0.01f)
                    return p;   // g is sitting at p's spawn point -> p is the item's effective puzzle
                // else: g itself might be a further-nested container placed by a deeper puzzle -
                // recurse using g's OWN itemName
                PuzzleDef nested = GetEffectivePuzzleOfItem(g's itemName, visited);
                if (nested != null) return nested;
            }
        }
    }
    return null;
}
```

Resolves "which puzzle produces this item" purely via **spatial proximity** (`< 0.01` units, via
helper `sub_1801B8C20`, a plain distance function) between a candidate container's live transform
position and a puzzle's `spawnPoint` — there is **no explicit "owning puzzle" field** on either
`ItemSeedData` or `PuzzleDef`; the relationship is inferred at validation time from where objects have
actually been placed by the earlier steps of `GeneratePlacement`. This is used by both
`ValidatePuzzleDependencies` and `DetectCircularDependencies`.

---

## 4. `ValidatePuzzleDependencies()` — `0x180253CF0` (no RNG, confirmed)

```csharp
bool ValidatePuzzleDependencies() {
    // 1. Effective puzzle per item
    var effectivePuzzleOf = new Dictionary<string, PuzzleDef>(OrdinalIgnoreCase);
    foreach (name in itemsByName.Keys)
        if (!IsNullOrEmpty(name)) effectivePuzzleOf[name] = GetEffectivePuzzleOfItem(name);

    // 2. Which items does each puzzle "unlock" (i.e. resolve to it as their effective puzzle)?
    var puzzleUnlocksItems = new Dictionary<string, HashSet<string>>(OrdinalIgnoreCase);
    foreach (p in puzzleDefs) if (p != null) puzzleUnlocksItems.TryAdd(p.puzzleName, new HashSet<string>(OrdinalIgnoreCase));

    var freelyObtainable = new HashSet<string>(OrdinalIgnoreCase);
    foreach (name in itemsByName.Keys) {
        if (effectivePuzzleOf.TryGetValue(name, out var puzzle) && puzzle != null)
            puzzleUnlocksItems[puzzle.puzzleName].Add(name);
        else
            freelyObtainable.Add(name);   // no effective puzzle -> obtainable from the start
    }

    // 3. Universe of names that MUST eventually be reachable
    var requiredNames = new HashSet<string>(OrdinalIgnoreCase);
    foreach (p in puzzleDefs)
        if (p?.requiredItemNames != null)
            foreach (name in p.requiredItemNames) if (!IsNullOrEmpty(name)) requiredNames.Add(name);
    foreach (name in requiredEscapeItemNames) if (!IsNullOrEmpty(name)) requiredNames.Add(name);

    // 4. Fixed-point reachability propagation
    var obtainableItems = new HashSet<string>(freelyObtainable, OrdinalIgnoreCase);
    var processedPuzzles = new HashSet<string>(OrdinalIgnoreCase);
    bool changed = true;
    while (changed) {
        changed = false;
        foreach (p in puzzleDefs) {
            if (p == null || IsNullOrEmpty(p.puzzleName)) continue;
            if (processedPuzzles.Contains(p.puzzleName)) continue;
            if (p.requiredItemNames == null || p.requiredItemNames.Count == 0) continue;  // *** see quirk below ***
            bool allSatisfied = p.requiredItemNames
                .Where(n => !IsNullOrEmpty(n))
                .All(n => obtainableItems.Contains(n));
            if (allSatisfied) {
                processedPuzzles.Add(p.puzzleName);
                if (puzzleUnlocksItems.TryGetValue(p.puzzleName, out var unlocked))
                    foreach (item in unlocked) obtainableItems.Add(item);
                changed = true;
            }
        }
    }

    // 5. Final acceptance check
    foreach (name in requiredNames)
        if (!obtainableItems.Contains(name) && GetEffectivePuzzleOfItem(name) != null)
            return false;
    return true;
}
```

**Quirk (flagged, verified from disasm, not a guess):** a `PuzzleDef` with an **empty or null
`requiredItemNames`** is *never* added to `processedPuzzles` by the fixed-point loop (the loop body is
gated on `requiredItemNames.Count > 0`), so the items it "unlocks" (`puzzleUnlocksItems[p.puzzleName]`)
are only ever added to `obtainableItems` if they were already `freelyObtainable`. In other words, a
prerequisite-free puzzle's own resulting items are not automatically propagated as obtainable by this
loop. Replicate this exactly in the Python port — do not "fix" it by special-casing empty
`requiredItemNames` as trivially satisfied.

**Acceptance criterion**: returns `true` (attempt valid) unless some name in `requiredNames` (the union
of every puzzle's `requiredItemNames` plus `requiredEscapeItemNames`) is **not** in `obtainableItems`
**and** has a resolvable effective puzzle (`GetEffectivePuzzleOfItem(name) != null`) — i.e., an item is
genuinely puzzle-gated but its prerequisite chain is unsatisfiable given the current placement.

---

## 5. `DetectCircularDependencies()` — `0x18024EC00` (+ DFS helper `0x180253760`) (no RNG, confirmed)

```csharp
bool DetectCircularDependencies() {
    var graph = new Dictionary<string, List<string>>(OrdinalIgnoreCase);
    foreach (p in puzzleDefs) {
        string puzzleKey = PREFIX1 + p.puzzleName;      // string literal prefix, distinguishes puzzle nodes
        graph.TryAdd(puzzleKey, new List<string>());
        if (p.requiredItemNames != null)
            foreach (name in p.requiredItemNames)
                if (!IsNullOrEmpty(name)) {
                    string itemKey = PREFIX2 + name;    // different prefix, distinguishes item nodes
                    graph.TryAdd(itemKey, new List<string>());
                    graph[puzzleKey].Add(itemKey);      // edge: Puzzle --requires--> Item
                }
    }
    foreach (itemName in itemsByName.Keys) {
        string itemKey = PREFIX2 + itemName;
        graph.TryAdd(itemKey, new List<string>());
        var effPuzzle = GetEffectivePuzzleOfItem(itemName);
        if (effPuzzle != null) {
            string puzzleKey = PREFIX1 + effPuzzle.puzzleName;
            graph.TryAdd(puzzleKey, new List<string>());
            graph[itemKey].Add(puzzleKey);              // edge: Item --obtained from--> Puzzle
        }
    }

    var color = new Dictionary<string,int>(OrdinalIgnoreCase);  // 0/absent=white, 1=gray, 2=black
    bool hasCycle = false;
    var stack = new Stack<string>();
    void Dfs(string node) {
        if (hasCycle) return;
        color[node] = 1; stack.Push(node);
        if (graph.TryGetValue(node, out var neighbors))
            foreach (n in neighbors) {
                int c = color.TryGetValue(n, out var cc) ? cc : 0;
                if (c == 0) { Dfs(n); if (hasCycle) return; }
                else if (c == 1) { hasCycle = true; return; }   // back-edge to a gray ancestor = cycle
                // c == 2: fully processed, skip
            }
        stack.Pop(); color[node] = 2;
    }
    foreach (node in graph.Keys) if (!color.ContainsKey(node)) color[node] = 0;
    foreach (node in graph.Keys) if (!hasCycle && color[node] == 0) Dfs(node);
    return hasCycle;
}
```

Classic white/gray/black DFS cycle detection over a bipartite Puzzle↔Item dependency graph. Two
distinct string-literal key prefixes (`StringLiteral_1778` for puzzle-node keys, `StringLiteral_6568`
for item-node keys) keep the two ID spaces from colliding; the literal text itself could **not** be
read statically (both are IL2CPP lazy metadata-usage slots holding encoded tokens, not real pointers,
until first executed) — this is a genuine, disclosed gap, but it has zero bearing on the algorithm's
correctness since it's purely an internal namespacing detail.

Both `ValidatePuzzleDependencies` and `DetectCircularDependencies` were re-confirmed to contain **zero**
`Next`/`NextDouble`/`rnd` references anywhere in their disassembly — they are pure bookkeeping and do
not themselves consume RNG, but their **boolean result decides whether the `attempt` loop in
`GeneratePlacement` retries with a new `System.Random(Seed+attempt+1)`**, so they must be reproduced
exactly to know which attempt's RNG stream is the one that actually won.

---

## 6. Lambda resolution table (all resolved, no remaining guesses)

| Lambda | Address | Role | Resolution method |
|---|---|---|---|
| `b__20_0` | `0x180256120` | valid-item filter: `item!=null && item.GetComponent<ItemSeedData>()!=null` | direct decompile |
| `b__20_1` | `0x1802563c0` | puzzle has spawnPoint: `p.spawnPoint != null` | direct decompile |
| `b__20_2` | `0x180256420` | `OrderBy` key selector: `p.Priority` | direct decompile |
| `b__20_3` | `0x180253c60` | notUsed: `!usedItemNames.Contains(item.ItemSeedData.itemName)` | direct decompile |
| `DisplayClass20_1_b__6` | `0x180256580` | `IsSafeContainerPlacement(i, puzzle)` | direct decompile |
| `DisplayClass20_2_b__7` | `0x1802565b0` | identity check: `v == item` (Step B "already assigned" test) | direct decompile |
| `DisplayClass20_2_b__8` | `0x180256610` | eligible-puzzle-for-escape-item test (spawn unused, unassigned, `IsItemAllowedForPuzzle`) | direct decompile |
| `DisplayClass20_3_b__11` | `0x180256550` | `IsItemAllowedForPuzzle(i, puzzle)` (Step D tier-1) | direct decompile |
| `b__4` (DisplayClass20_1, Step A) | **same VA `0x180256550`** | `IsItemAllowedForPuzzle(i, puzzle)` | **RESOLVED** — dump.cs ground-truth VA lookup shows `b__4` and `b__11` share the identical native address (IL2CPP/MSVC identical-code-folding of two structurally-identical closures). Confirms the prior session's symmetry-based GUESS was correct. |
| `b__20_5` (typeFilter) | `0x180256440` | `item.ItemSeedData.category != 4` | **RESOLVED** — disasm shows `cmp dword ptr [rax+28h], 4`; offset `+0x28` = `ItemSeedData.category` (confirmed against declared struct + dump.cs). `4` = the "free" category (per dump.cs's own `[Tooltip]` string, with an apparent typo — literal text is `"1=escape-only, 2=escape+puzzle, 3=puzzle-only, 3=free"`, clearly intended as `4=free`). |
| `PlaceItemInFreeArea_b__0` | `0x180256820` | area eligibility: `currentItems<maxItems && freeSpots.Length!=0 && (allowedItemNames.Count==0 \|\| allowedItemNames.Contains(item.itemName))` | direct decompile |
| `PlaceItemInFreeArea_b__1` | `0x1802568d0` | free-spot filter: `s!=null && !usedSpots.Contains(s)` (shuffled-areas pass) | direct decompile |
| `PlaceItemInFreeArea_b__2` | `0x180256970` | same shape as `b__1`, used in the unshuffled fallback pass | direct decompile |
| `PlaceItemInSpecificFreeArea_b__0` | `0x180256a10` | free-spot filter: `s!=null && !usedSpots.Contains(s)` | direct decompile |
| `IsItemAllowedForPuzzle_b__0`/`_b__1` | both `0x1802567C0` | `x.Trim().Equals(itemSeedData.itemName.Trim(), OrdinalIgnoreCase)` | **RESOLVED** — folded to one address; direct decompile of that address |

---

## 7. Answers to the specific questions

**Q1 — see §1.** Empty `allowedItemNames` = allow-all (not allow-none). Matching is against
`ItemSeedData.itemName`, case-insensitive (`OrdinalIgnoreCase`), both sides `.Trim()`'d.
`requiredItemNames`/`forbiddenCombos` do not participate in `IsItemAllowedForPuzzle` at all.

**Q2 — see §2.** `IsSafeContainerPlacement` prevents placing a container item at a puzzle's spawn point
when that same puzzle's `requiredItemNames` demands one of the container's `containedItems` — avoiding
a self-locking arrangement where a puzzle's prerequisite item is sealed inside a container sitting at
that puzzle's own location.

**Q3 — RESOLVED, see §6 row `b__20_5`.** The field is definitively `ItemSeedData.category` (an `int`,
object offset `0x28`, confirmed both by disassembly `[rax+28h]` and by the declared `ItemSeedData_Fields`
struct / `dump.cs`'s explicit `// 0x28` comment). `4` corresponds to the "free" category per the
developer's own `[Tooltip]` attribute text in `dump.cs` (`1=escape-only, 2=escape+puzzle, 3=puzzle-only,
4=free` — the dump's literal tooltip string has a typo repeating "3=" for the last entry, but the
intended enumeration is unambiguous from context). This was **not** a guess — it is a hard address/byte
match.

**Q4 — RESOLVED, see §6 row `b__4`.** `b__4` and `b__11` share the exact same native code address
(`0x180256550`) per Il2CppDumper's ground-truth VA table, because IL2CPP/MSVC's identical-code-folding
linker pass merged the two structurally-identical closures. The prior session's symmetry-based
inference (`b__4 == IsItemAllowedForPuzzle`) is confirmed correct, not merely plausible.

**Q5 — see §4/§5.** Both validators contain zero RNG. `ValidatePuzzleDependencies`'s acceptance
criterion and `DetectCircularDependencies`'s cycle check are given as exact pseudocode above; a Python
port must implement both faithfully (including the flagged `requiredItemNames`-empty quirk in
`ValidatePuzzleDependencies`) to know which `attempt` (and therefore which `Seed+attempt` RNG stream)
is the one whose placement result is finally accepted.

**Q6 — see `[[IDA:SpawnArea_TrueLayout_and_Q6_resolution]]` and below.** The **compiled code's** `SpawnArea`
class has the **full 7-field layout matching `dump.cs` exactly** (`areaName, freeSpots, maxItems,
currentItems, usedSpots, allowedItemNames, mandatoryItemName`, offsets `0x10`–`0x38`). Both
`allowedItemNames` (read in `PlaceItemInFreeArea_b__0`, confirmed via decompile) and `mandatoryItemName`
(read in `GeneratePlacement`'s Step E, confirmed via disassembly `mov rcx,[rbx+38h]` →
`String.IsNullOrEmpty`) are real fields that the game **does** actively read. However,
`SpawnArea..ctor()` (`0x1802550F0`, decompiled) contains field initializers `maxItems=1`,
`usedSpots=new List<Transform>()`, `allowedItemNames=new List<string>()` — but **no** initializer for
`mandatoryItemName` (stays C# default `null`). Combined with the prior session's independent,
byte-exact UnityPy finding that the actual serialized `level1` scene data only carries bytes for 5 of
the 7 fields (i.e. `allowedItemNames`/`mandatoryItemName` are never overwritten by deserialization for
any of the 65 `SpawnArea` instances across the 10 `SeedManager` variants), the practical, data-grounded
conclusion is:
  - `allowedItemNames` ends up as a real, non-null, **empty** list for every area in the actual scene
    data → the eligibility filter's `Count==0` branch always fires → this field is a functional no-op
    for this scene (never actually restricts anything), even though the code path is fully live and
    would work correctly if populated.
  - `mandatoryItemName` stays `null` for every area → `IsNullOrEmpty(null)==true` → **Step E's entire
    per-area block, and its RNG consumption, never fires** for any of the 10 `SeedManager` variants in
    this scene. The Python port can safely treat Step E as dead code for this game's actual data (while
    still implementing it faithfully in case a future/different scene populates `mandatoryItemName`).

This resolves the apparent "contradiction" from `re_scene_data.md`: it was never really a contradiction
between the code and the data — the C# class genuinely has 7 fields (both `dump.cs` and this IDB's
struct agree, both are ground truth for the compiled binary), but the specific `level1` scene asset was
evidently authored/serialized before the two trailing fields were added to `SpawnArea` and was never
re-saved in the Unity Editor since, so Unity's deserializer simply leaves those two fields at their
`.ctor`-assigned defaults instead of crashing or reading garbage.

---

## 8. Remaining unknowns (explicitly flagged, not guessed)

1. **Exact text of the two `DetectCircularDependencies` node-key prefixes** (`StringLiteral_1778`,
   `StringLiteral_6568`) could not be read statically — both are IL2CPP lazy metadata-usage slots
   holding encoded tokens rather than real string pointers at rest, and are only resolved by the
   runtime on first execution. This has **no effect on correctness** (purely an internal namespacing
   detail to keep puzzle-name and item-name graph nodes from colliding), but if perfect fidelity to the
   literal graph-node string values is ever needed, it would require either a runtime trace/dump or
   parsing `global-metadata.dat`'s string-literal table directly (not attempted this session).
2. **`IsSafeContainerPlacement`'s inner "resolve contained-item-name to a `GameObject`" step**: the
   decompile shows a `HashSet<string>`-shaped enumerator (a fully-shared-generic decompiler artifact,
   not a literal `HashSet`) walking `isd.containedItems`, but I did not trace whether the per-name
   membership test against `PuzzleDef.requiredItemNames` operates on plain item-name strings the whole
   way through (my read is that it does — `List<string>.Contains(string)` — but the exact
   variable-renaming the decompiler chose for this loop was confusing enough that I want to flag it as
   "high confidence, not textbook-certain"). Re-disassembling the tight loop at `0x180252530`–`0x18025267c`
   instruction-by-instruction would fully settle any residual doubt; the pseudocode in §2 reflects my
   best reading of both the decompile and the `p+56`/`p+0x38`→`requiredItemNames` offset cross-check.
3. **`PuzzleDef.forbiddenCombos`** has no confirmed consumer anywhere in the seed-placement pipeline in
   this build (no xrefs found in any of the functions examined this session or the prior session). It
   may be dead/vestigial data, or consumed by unrelated gameplay code never reached from
   `GeneratePlacement`. Not investigated further (out of scope — it does not affect the RNG stream).
4. **Whether `PuzzleDef.allowedItemNames`/`excludeItemNames` can ever be a true C# `null`** (as opposed
   to an empty list) in live scene data was not independently checked this session (unlike `SpawnArea`,
   I did not decompile `PuzzleDef..ctor` to check for field initializers). If they can be null in some
   scene variant, `IsItemAllowedForPuzzle` would hit the `NullReferenceException` throw path documented
   in §1's pseudocode. Recommend checking `PuzzleDef..ctor` (VA `0x18024D570` per `dump.cs`) before
   assuming this can never happen for the 10 `SeedManager` variants in `scene_data.json`.

---

## 9. IDB annotations added this session

* `append_comments` at `0x180256440` (typeFilter lambda): resolved field/offset/meaning.
* `append_comments` at `0x180256550` (b__11 / shared with b__4): ICF-folding confirmation.
* `append_comments` at `0x1802567C0` (shared b__0/b__1): exact comparison semantics.
* `append_comments` at `0x1802550F0` (SpawnArea..ctor): field-initializer findings, Step E implication.
* `append_comments` at `0x180253760` (DetectCircularDependencies DFS helper): coloring algorithm summary.
* `append_comments` at `0x180252200` (IsItemAllowedForPuzzle): full predicate summary.
* `append_comments` at `0x1802523F0` (IsSafeContainerPlacement): full guard summary.
* `append_comments` at `0x180253CF0` (ValidatePuzzleDependencies): full algorithm + quirk summary.

Raw decompilation dumps used to produce this report are appended to
`C:\Users\ir0n1c\grannyseedpredictor\for_claude_the_logic_pro.txt` under the searchable tags listed at
the top of this report.
