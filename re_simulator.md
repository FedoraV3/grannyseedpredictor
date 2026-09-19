# `simulator.py` — reconciliation against `re_predicates.md`

## Result

**31/31 items matched. PASS.** (Unchanged from before reconciliation.)

```
$ python test_simulator.py
PASS: 31/31 items matched ground truth exactly.
```

`simulate(915074960, load_config(<newest config dump>))` reproduces the real-game
placement in `dumps/placement_915074960_20260917_172110_502.json` exactly, on
**attempt 1** (`NetRandom(915074961)`), with zero special-casing of item names,
seeds, or the known answer. `ground_truth.compare()` returns `(True, [])`.

This document supersedes the "INFERRED — no `re_predicates.md` existed" claims
in the previous version of this file. `re_predicates.md` (from IDA
disassembly) now exists and was used as ground truth wherever it disagreed
with the earlier empirically-derived predicates. Each change below was made
and tested **individually**, per the task's method, so the effect of each one
on the 31/31 result is known precisely rather than inferred from a single
combined diff.

## Change-by-change log

### 1. `IsItemAllowedForPuzzle`: remove the `requiredItemNames` clause

**Report says (S1):** `IsItemAllowedForPuzzle` never reads
`PuzzleDef.requiredItemNames` at all — it is read only by
`ValidatePuzzleDependencies`/`DetectCircularDependencies`. The old simulator's
`return allowed and item.itemName not in puzzle.requiredItemNames` clause is
not part of the real function.

**Change:** removed the clause. `is_item_allowed_for_puzzle` is now exactly
allow-list-membership-or-not-exclude-list-membership, nothing else.

**Effect on test:** **No change — still 31/31.** This was checked explicitly,
not just observed: a script scanned every `PuzzleDef` for a case where its own
`requiredItemNames` overlaps a name that would otherwise be allowed for it
(i.e., is in its non-empty `allowedItemNames`, or isn't excluded when
`allowedItemNames` is empty). Result: **zero puzzles** in this build's actual
scene data have that overlap. So the removed clause was already a dead no-op
for this seed/scene — it never once fired, in either direction, for any of
the 16 puzzles or 31 items. This is a clean, non-lucky finding: the report and
the empirical implementation don't actually conflict *in effect* on this
data, even though the old code's derivation of the clause was based on a
misreading of the disassembly. Kept removed per the report, since it is
authoritative and the removal is confirmed harmless (not "harmless because it
never mattered" in a hand-wavy sense — verified by direct inspection of the
overlap set, which is empty).

### 2. Name matching: `.Trim()` + `StringComparison.OrdinalIgnoreCase`

**Report says (S1, S3, S4, S5):** the folded `IsItemAllowedForPuzzle` lambda
at `0x1802567C0` is `x.Trim().Equals(itemSeedData.itemName.Trim(),
StringComparison.OrdinalIgnoreCase)` (comparisonType constant `5`, confirmed
byte-for-byte). `GetEffectivePuzzleOfItem`'s contained-item-name match is
`n.Trim().Equals(itemName, OrdinalIgnoreCase)`. Every Dictionary/HashSet built
inside `ValidatePuzzleDependencies`/`DetectCircularDependencies`
(`effectivePuzzleOf`, `puzzleUnlocksItems`, `freelyObtainable`,
`requiredNames`, `obtainableItems`, `processedPuzzles`, the DFS `graph` and
`color` maps) is explicitly constructed with `StringComparer.OrdinalIgnoreCase`.

**Change:** added `name_eq(a, b)` / `name_in(name, names)` helpers
(`.strip().lower()` on both sides, i.e. Trim + case-insensitive — .NET
`OrdinalIgnoreCase` is a byte-wise case-fold with no locale sensitivity, which
`str.lower()` matches exactly for the ASCII item/puzzle names in this scene).
Applied to:
  - `is_item_allowed_for_puzzle`'s allow-list/exclude-list checks — **VERIFIED**
    directly from S1.
  - `GetEffectivePuzzleOfItem`'s contained-item-name match — **VERIFIED**
    directly from S3.
  - Every OrdinalIgnoreCase-keyed set/dict inside
    `validate_puzzle_dependencies`/`detect_circular_dependencies` — **VERIFIED**
    directly from S4/S5 (all keyed on normalized names via `_norm_name`).
  - `usedItemNames` membership, `SpawnArea.allowedItemNames` membership,
    `itemsByName` lookup, `requiredEscapeItemNames` lookup — **INFERRED
    EXTENSION**. The report does not decompile a comparer constant for these
    specific call sites (they're documented as plain `.Contains(...)` /
    dictionary lookups). Applied name_eq/name_in here anyway, for consistency
    with every other name-keyed container the report *does* confirm as
    OrdinalIgnoreCase, per the task's explicit instruction. Flagged clearly in
    the module docstring and at the `name_eq`/`name_in` definitions as the one
    place behavior goes beyond the report's literal text.

**Effect on test:** **No change — still 31/31.** This build's actual item
names, puzzle allow/exclude lists, and area allow-lists contain no whitespace
or casing inconsistencies (verified informally: every string in the config
JSON round-trips identically through `.strip().lower()` vs. exact match for
this seed's data), so making matching Trim+case-insensitive doesn't change
which items pass which filters here. The change is still correct to make —
it's a latent-bug fix that would only manifest on a scene variant with
whitespace/casing inconsistencies in its authored name lists.

### 3. `IsSafeContainerPlacement`: verify field direction, trim/case handling

**Report says (S2):** the guard iterates *every* `PuzzleDef` (not just the
candidate), checking whether that puzzle's `requiredItemNames` contains one of
the item's `containedItems`, **and** whether that puzzle's `spawnPoint`
reference-equals the *candidate* puzzle's `spawnPoint`. The comparison is a
plain `List<string>.Contains(containedName)` — the *default* comparer, i.e.
**case-sensitive, no Trim** — explicitly called out in the report as different
from `IsItemAllowedForPuzzle`'s comparer.

**What the empirical version had:** only checked the *candidate* puzzle's own
`requiredItemNames` against the item's `containedItems` (a single-puzzle
check, not the all-puzzles-with-spawnPoint-equality form), using plain `in`
(already case-sensitive, so no change needed there).

**Change:** rewrote `is_safe_container_placement(item, candidate_puzzle,
puzzle_defs)` to literally loop over all `puzzle_defs`, using instanceId
equality on `spawnPointId` as the practical proxy for Unity reference equality
on `spawnPoint` (verified: all 16 puzzles in this build have distinct
spawn-point instanceIds, so this loop only ever fires for `p is
candidate_puzzle`, an exact behavioral match to the old single-puzzle rule on
this build's data). Left name comparison as plain case-sensitive `in`
(unchanged) — confirmed correct per the report's explicit statement that this
call site has no comparer argument, unlike `IsItemAllowedForPuzzle`.

**Effect on test:** **No change — still 31/31.** Expected: given the unique
spawn-point-per-puzzle fact, the literal all-puzzles form is mathematically
equivalent to the old single-puzzle form for this build's data. The change
matters only for hygiene/future-proofing (a scene variant that reuses a spawn
point across two `PuzzleDef`s, or that has case/whitespace-inconsistent
`containedItems`/`requiredItemNames` entries, would now be handled correctly
where the old code might not have been).

Also confirmed: direction of comparison (item's `containedItems` vs.
`PuzzleDef.requiredItemNames`, not `PuzzleDef.containedItems`) already matched
between the empirical version and the report — no change needed there.

### 4. `ValidatePuzzleDependencies` / `DetectCircularDependencies`: real implementation

**Report says (S3, S4, S5):** both are pure bookkeeping (zero RNG), but their
boolean result decides which `attempt`'s RNG stream the retry loop keeps.
`GetEffectivePuzzleOfItem` resolves "which puzzle produces an item" via
spatial proximity (`< 0.01` units) between a candidate container's live
position and a puzzle's `spawnPoint` position, with recursion for
multiply-nested containers and an `OrdinalIgnoreCase` cycle guard.
`ValidatePuzzleDependencies` does fixed-point reachability propagation with an
explicitly disclosed quirk: a `PuzzleDef` with empty/null `requiredItemNames`
is *never* added to `processedPuzzles`, so the items it unlocks only reach
`obtainableItems` if they were already freely obtainable — the report
explicitly says not to "fix" this. `DetectCircularDependencies` is a
white/gray/black DFS over a bipartite Puzzle/Item graph built from
`requiredItemNames` edges and `GetEffectivePuzzleOfItem`-resolved edges.

**What the old simulator had:** `_validate_attempt(puzzle_has_item)` — a
proxy that just checked "every puzzle spot got filled", explicitly
acknowledged in the previous version of this file as not a faithful port.

**Change:**
  - Extended the data model (`Item.position`, `PuzzleDef.spawnPointPosition`,
    `Spot.position`) and `load_config` to carry the scene-authored transform
    positions needed for the spatial `< 0.01` check (`startPosition` for
    items, `spawnPoint.position` for puzzles, free-spot `position`).
  - Implemented `make_effective_puzzle_resolver` (= `GetEffectivePuzzleOfItem`,
    parameterized over a `position_of` callback bound to one attempt's
    `result` dict, since which puzzle "produced" an item depends on where
    that attempt actually placed things).
  - Implemented `validate_puzzle_dependencies` and
    `detect_circular_dependencies` as literal ports of the S4/S5 pseudocode,
    including the disclosed empty-`requiredItemNames` quirk (implemented
    exactly as specified, not "fixed").
  - Replaced `success = _validate_attempt(puzzle_has_item)` with
    `success = not detect_circular_dependencies(...) and
    validate_puzzle_dependencies(...)`.
  - Removed `_validate_attempt` entirely (dead code after the swap).

**Effect on test:** **No change — still 31/31, attempt 1.** Sanity-checked the
wiring beyond just "the test still passes" (since the real risk with a
validator swap is a bug that makes it vacuously always-`True`, which would
silently hide a broken port): a synthetic 2-item, 1-puzzle scenario was
constructed by hand (a puzzle `Door` requiring item `Key`; a container `Box`
containing `Key`, positioned at `Door`'s own spawn point) and confirmed
`validate_puzzle_dependencies` correctly returns `False` (unsatisfiable
deadlock: `Door` needs `Key`, but `Key`'s only source is sealed inside a
container sitting at `Door` itself) and `detect_circular_dependencies`
correctly returns `True` (cycle: `Door -> Key -> Door`) for that case, while
returning `True`/`False` (no deadlock/no cycle) for the trivial freely-
obtainable case. This confirms the implementation is a genuine, non-trivial
check, not an accidental tautology, and that it correctly identifies the
kind of deadlock/cycle it's designed to catch.

For this seed's actual data, `detect_circular_dependencies` returns `False`
and `validate_puzzle_dependencies` returns `True` on attempt 1 (verified
directly, not just inferred from the overall test passing), so attempt 1 is
accepted exactly as it was under the old proxy and as it was in the real
game's own single successful `GeneratePlacement` call for this seed.

## Predicates: verified vs. inferred (final state)

**VERIFIED against `re_predicates.md`:**
- `is_item_allowed_for_puzzle` — S1, including confirmed removal of the
  `requiredItemNames` clause.
- `category_ok` — S6 (`b__20_5`, `cmp dword ptr [rax+28h], 4`).
- `valid(item)` — matches `b__20_0`.
- `is_safe_container_placement` — S2, including the all-puzzles /
  spawn-point-equality form and the case-sensitive, non-Trim comparer.
- `make_effective_puzzle_resolver` (`GetEffectivePuzzleOfItem`) — S3.
- `validate_puzzle_dependencies` (`ValidatePuzzleDependencies`) — S4,
  including the disclosed quirk.
- `detect_circular_dependencies` (`DetectCircularDependencies`) — S5.

**INFERRED EXTENSION (beyond the report's literal text, by pattern
consistency, per explicit task instruction):**
- Trim+OrdinalIgnoreCase matching for `usedItemNames` membership,
  `SpawnArea.allowedItemNames` membership, `itemsByName` lookup, and
  `requiredEscapeItemNames` lookup. The report documents these call sites as
  plain `.Contains(...)`/dictionary-lookup without a decompiled comparer
  constant. This is the **only** remaining place where `re_predicates.md` and
  the implementation are not in 1:1, byte-verified correspondence — flagged
  here explicitly rather than silently assumed.

**No remaining disagreement found** between `re_predicates.md` and the
implementation after all four changes: every discrepancy listed in the task
was checked individually, and in each case the report's account was adopted
and confirmed either behaviorally neutral (no test-result change, verified by
direct inspection of *why* — the specific overlap/whitespace/duplicate-
spawn-point conditions that would make the change observable are all absent
from this build's actual scene data) or newly, correctly exercised (the
synthetic deadlock/cycle sanity checks for item 4).

## Unresolved risks / honesty notes

- **The Trim+OrdinalIgnoreCase extension (item 2's INFERRED EXTENSION above)**
  has no decompiled confirmation for the four call sites listed. It is a
  reasonable, low-risk extrapolation (every other name-keyed container in the
  same codebase that *was* decompiled uses this comparer), but it is not
  itself byte-verified. If a future `re_predicates.md` update decompiles
  those specific lambdas and finds plain ordinal case-sensitive comparison
  instead, this would need to be reverted for those four sites specifically.
- **`is_safe_container_placement`'s all-puzzles loop degenerates to a
  single-puzzle check on this build's data** because every puzzle has a
  unique spawn-point instanceId (verified: 16/16 unique). If a future scene
  variant reuses a spawn point across two `PuzzleDef`s, the literal all-
  puzzles form (now implemented) would behave differently from — and more
  correctly than — the old single-puzzle empirical rule. This is a
  behavioral difference that exists only in scene variants not present in
  this repo's dumps, so it could not be exercised or confirmed against real
  ground truth here.
- **`GetEffectivePuzzleOfItem`'s fallback position for items not yet placed
  this attempt** (`_position_of` returning `item.position`, the original
  scene `startPosition`, when the item has no entry in `result` yet) is
  **INFERRED** — the report's pseudocode assumes `g.transform.position` is
  always meaningful at the point `ValidatePuzzleDependencies`/
  `DetectCircularDependencies` run (i.e., after `GeneratePlacement`'s
  placement steps have already run), and does not describe what happens for
  an item that failed to get placed at all. This only matters for attempts
  that don't place every item, which never occurs for seed 915074960
  (attempt 1 places all 31 items and succeeds), so it remains unexercised by
  the acceptance test. Flagged rather than silently assumed correct for other
  seeds/attempts.
- **`PuzzleDef.forbiddenCombos`** remains parsed-but-unused, per the report's
  explicit statement that it has no confirmed consumer in this build's
  seed-placement pipeline.
- **The two `DetectCircularDependencies` graph-node string-literal prefixes**
  (report S8 item 1) could not be read statically by the RE session; this has
  no bearing on correctness (internal namespacing only) and the Python port
  uses `('P', name)`/`('I', name)` tuples for the same purpose, which is
  behaviorally identical regardless of what the real prefix text is.

## Debugging method used

Per the task's explicit instruction, each of the four changes above was made
**one at a time**, with `python test_simulator.py` run immediately after each,
before moving to the next change. No change broke the 31/31 result, so there
was no case requiring the "diagnose why removing the clause broke the test"
investigation the task anticipated as a possibility — the report and the
prior empirical implementation turned out to already agree *in effect* on
every discrepancy for this build's specific data, even where the prior
implementation's *derivation* was based on a misreading of the disassembly
(item 1) or an incomplete implementation (item 4, the validator proxy). Item
4's real implementation was additionally sanity-checked with hand-constructed
synthetic scenarios (see above) to confirm it is a genuine, correctly-wired
check rather than a tautology that happens to pass by construction.
