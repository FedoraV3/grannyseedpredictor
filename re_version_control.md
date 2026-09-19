# VersionControl / CURVERSION / SeedSystemManager Investigation

Target: GameAssembly.dll, Granny Legacy v1.8.9, IL2CPP x64, imagebase 0x180000000.
Investigation date: 2026-09-18. All findings below are from live decompilation/disassembly via
ida-pro-mcp against the loaded IDB (`GameAssembly.dll.i64`), not from memory or assumption.

Full raw decompiled dumps and disasm slices supporting this report are appended to
`C:\Users\ir0n1c\grannyseedpredictor\for_claude_the_logic_pro.txt` under the keyword
`[[IDA:VersionControl_CURVERSION_Verified]]` (and related earlier keywords for the GameSeed/RandomSeed
side of the investigation).

---

## 1. Does `VersionControl` / `VersionControlItemChange` actually exist?

**VERIFIED — YES, both classes exist.** `list_funcs` confirms:

| Method | VA | Size |
|---|---|---|
| `VersionControl$$RemoveNavGrandpa` | 0x180214e10 | 0xe0 |
| `VersionControl$$RemoveNavGranny` | 0x180214ef0 | 0xe0 |
| `VersionControl$$Start` | 0x180214fd0 | 0x27e0 |
| `VersionControl.__c__DisplayClass73_0$$_RemoveNavGrandpa_b__0` | 0x18022c0a0 | 0x90 |
| `VersionControl.__c__DisplayClass74_0$$_RemoveNavGranny_b__0` | 0x18022c130 | 0x90 |
| `VersionControlItemChange$$Start` | 0x18022c620 | 0x1d0 |

`VersionControl_o` is 624 bytes with a 72-member `VersionControl_Fields` struct (offset 0x10). It
contains **exactly** the array fields `re_scene_data.md` claimed:

```
DisableOneSixObjects    @ +0x108  (UnityEngine_GameObject_array*)
DisableOneFiveObjects   @ +0x110  (UnityEngine_GameObject_array*)
DisableOneFourObjects   @ +0x118  (UnityEngine_GameObject_array*)
DisableOneThreeObjects  @ +0x120  (UnityEngine_GameObject_array*)
DisableOneTwoObjects    @ +0x128  (UnityEngine_GameObject_array*)
```

`VersionControlItemChange_Fields` (40 bytes) contains exactly the fields claimed:

```
VersionPlayerPrefs1  @ +0x10  (int32_t)
VersionPlayerPrefs2  @ +0x14  (int32_t)
VersionPlayerPrefs3  @ +0x18  (int32_t)
ObjectsToReposition  @ +0x20  (UnityEngine_Transform_array*)
```

`VersionControl$$Start` (0x180214fd0) reads `PlayerPrefs.GetInt("CURVERSION")` (`StringLiteral_4166`
@ 0x180c43130) **ten separate times**, branching over the literal integer values `0,1,2,3,4,5,6,7,8`
(9 distinct configurations), each toggling a different set of house objects via
`GameObject.SetActive`, reassigning AI footstep-audio fields, adjusting Grandpa/Granny spawn
positions, and calling `RemoveNavGrandpa`/`RemoveNavGranny` to strip waypoints from the AI's nav
list. A trailing check on `PlayerPrefs.GetInt("Extras")` (`StringLiteral_5730` @ 0x180c4baa0)
controls the attic-tree object. Full pseudocode dump: `[[IDA:VersionControl_CURVERSION_Verified]]`.

`VersionControlItemChange$$Start` (0x18022c620):
```c
if ( PlayerPrefs.GetInt("FlashL") == 0                              // StringLiteral_5882 @ 0x180c4cb40
  && (PlayerPrefs.GetInt("CURVERSION") == this->VersionPlayerPrefs1  // StringLiteral_4166 @ 0x180c43130
   || PlayerPrefs.GetInt("CURVERSION") == this->VersionPlayerPrefs2
   || PlayerPrefs.GetInt("CURVERSION") == this->VersionPlayerPrefs3) )
{
    foreach (Transform t in ObjectsToReposition)
        t.position = this.transform.position, t.rotation = this.transform.rotation;
}
```
This is a per-instance scene component (likely multiple copies exist, each with distinct
`VersionPlayerPrefs1/2/3` Inspector values) that snaps item transforms to its own position when the
active `CURVERSION` matches one of its 3 configured version numbers.

**Verdict on Q1: `re_scene_data.md` is CORRECT.** `VersionControl` and `VersionControlItemChange`
are real classes, actively read `CURVERSION`, and have exactly the fields it described.
`v1.0_CONCLUSION.md` section 4's characterization of `CURVERSION` as "merely an unrelated
defaults-migration flag" is **WRONG** — that description only fits the one write-site in
`MainMenu$$Start` that seeds a default value once (see section 3/8 below); it does not describe the
numerous runtime reads of `CURVERSION` found in `VersionControl.Start` and
`VersionControlItemChange.Start`.

---

## 2. What selects the active `SeedSystemManager` variant?

**This could NOT be determined from static analysis — explicitly flagged as UNDETERMINED, not guessed.**

Extensive search found:

- **No string literal "SeedSystemManager" (or any variant-suffix string like "_1.0", "_Normal",
  "_More") exists anywhere in GameAssembly.dll.** Checked via `find_regex`, `find(type=string)`, and
  full-text scan of every decompiled candidate function. IL2CPP would normally intern such a string
  if any code called `GameObject.Find("SeedSystemManager_...")` — it does not.
- **`SeedManager_Fields`** (112 bytes, the actual runtime/serialized fields of the `SeedManager`
  MonoBehaviour) contains only item-placement state: `allItems`, `puzzleDefs`,
  `requiredEscapeItemNames`, `spawnAreas`, `Seed`, `RandomizeSeed`, `activatePlacedItems`,
  `fillAllFreeSpawns`, `Rigid`, `EscapeItemPuzzleChance`, `rnd`, `itemsByName`, `usedItemNames`,
  `usedPuzzleSpawns`, `puzzleHasItem`, `originalStates`. **No field referencing `VersionControl`,
  `CURVERSION`, or any sibling `SeedManager`/GameObject array.**
- **`VersionControl_Fields`** (72 members) contains no field of type `SeedManager_o*`, no field named
  anything like `SeedSystem*`, and no array of 10 GameObjects. The full 123,205-character decompiled
  body of `VersionControl$$Start` was scanned programmatically for the substrings `"SeedManager"` and
  `"SeedSystem"` — **zero occurrences of either.**
- **`ObjectsManager$$Start`** (0x18022e7b0, 11072 bytes, 540 basic blocks) — the other large
  `Start()` method with heavy PlayerPrefs traffic (52 references) — was likewise scanned and contains
  **zero occurrences** of `"SeedManager"`, `"SeedSystem"`, `"CURVERSION"`, or `"Version"`. Its
  PlayerPrefs calls are for persisting randomized decorative item positions (vases, pictures), an
  unrelated system.
- `SeedManager$$Awake` (0x18024e910) only calls `SeedManager$$BuildLookup`; `SeedManager$$Start`
  (0x180253750) only calls `SeedManager$$GeneratePlacement` (0x18024f770, confirmed correct address
  per the task). `xrefs_to` on both `Awake` and `Start` shows **only IL2CPP method-table data-xrefs**
  (used for Unity's reflection-based lifecycle dispatch), with **no code call site anywhere in the
  binary** that explicitly invokes `SeedManager.Start()`/`Awake()`. This is expected — Unity's engine
  calls these automatically on whichever component instance is active+enabled at scene load; IL2CPP
  code never needs to call them directly.

Because no code reads a PlayerPrefs key (or any other persisted value) and then calls
`GameObject.SetActive(true)`/`GetChild`/`Find` targeting one of the 10 `SeedSystemManager_*`
objects, and no manager component holds a serialized array of those 10 objects, **the selection
mechanism is not implemented in GameAssembly.dll's IL2CPP code.** Two possibilities remain, neither
confirmable by static binary analysis alone:

  (a) The choice is baked directly into `level1`'s serialized scene data — i.e. exactly one
      `SeedSystemManager_X` GameObject has `m_IsActive: 1` hardcoded in the `.unity`/scene YAML at
      build time, changed by the developer per release (this would explain why v1.0/Normal/More each
      ship as separate inactive objects left in the hierarchy rather than as a runtime-selected set).

  (b) Some Inspector-wired UnityEvent/Animator/Timeline mechanism performs the activation without a
      corresponding static call site visible to IL2CPP xrefs (analogous to how
      `Menu_Seed$$ExitTypingModeAndSave` is only reachable via metadata event tables, not a direct
      call — see `for_claude_the_logic_pro.txt`).

I could not distinguish between these from the DLL alone; resolving this would require inspecting the
actual scene asset (`level1.unity`) or `dump.cs`/`RE_Notes.md` for a serialized reference, which is
outside GameAssembly.dll's code.

---

## 3. Every PlayerPrefs key read/written on the version-selection path

Confirmed by decompilation (VA of the interned `StringLiteral_*` object given; all read via
`UnityEngine.PlayerPrefs.GetInt` unless noted):

| Key string | VA | Type | Where used |
|---|---|---|---|
| `"CURVERSION"` | 0x180c43130 (StringLiteral_4166) | int | **Read** ×10 in `VersionControl$$Start` (0x180214fd0), branches on 0-8; **read** ×3 in `VersionControlItemChange$$Start` (0x18022c620) compared to `VersionPlayerPrefs1/2/3`; **written** once (`SetInt(...,8)`) in `MainMenu$$Start`'s first-run-defaults block (~0x180221541-0x180221c9e), gated by a separate flag key `StringLiteral_1136` |
| `"Extras"` | 0x180c4baa0 (StringLiteral_5730) | int | Read once at the tail of `VersionControl$$Start` (controls `AtticTree` SetActive) |
| `"FlashL"` | 0x180c4cb40 (StringLiteral_5882) | int | Read (gate, must be 0) in `VersionControlItemChange$$Start` |

No PlayerPrefs key literally named `"VersionPlayerPrefs1"`, `"VersionPlayerPrefs2"`,
`"VersionPlayerPrefs3"`, `"HouseVersion"`, or `"GameVersion"` exists as a string anywhere in the
binary — `VersionPlayerPrefs1/2/3` are **C# field names** on `VersionControlItemChange` instances
(each instance's Inspector-serialized int, compared against the live `CURVERSION` PlayerPrefs value),
not PlayerPrefs key strings themselves. No key referencing `"SeedSystemManager"` or any per-variant
selector exists.

(For completeness/cross-reference, the unrelated `"GameSeed"`/`"RandomSeed"` keys used by
`Menu_Seed`/`SeedManager.GeneratePlacement` are documented separately in
`for_claude_the_logic_pro.txt` under `[[IDA:PlayerPrefs_SeedKeys_XrefSummary]]` — they are the
**level-content seed**, not the house-version selector, and are a separate system from `CURVERSION`.)

---

## 4. Is there an in-game menu that sets the version?

**VERIFIED — NO.** `list_funcs` filtered on `Menu_*` returns only the `Menu_Seed` class
(`BeginTypingSeed`, `ExitTypingModeAndSave`, `Start`, `Update`, `.ctor`, VAs 0x180249d30-0x18024a100).
There is no `Menu_Version`, `Menu_House`, or any comparably named UI class. The only place
`"CURVERSION"` is ever written (`SetInt`) in the entire binary is the `MainMenu$$Start` first-run
defaults block, which writes a constant `8` **once**, gated by an unrelated migration-flag key
(`StringLiteral_1136`), not by any player choice or UI control. There is no evidence of a 0-9 (or
0-8) player-facing version picker anywhere in the code.

---

## 5. Mapping from selector value to variant GameObject name

**UNDETERMINED.** Since no code ties `CURVERSION` (or any other PlayerPrefs key) to activation of a
specific `SeedSystemManager_*` GameObject, no such mapping can be derived from GameAssembly.dll.

Note that `CURVERSION`'s observed branch range is 0-8 (9 values), which does not even numerically
match the count of `SeedSystemManager` variants (10: `_1.0` through `_1.7`, `_Normal`, `_More`). This
is circumstantial evidence that `CURVERSION` (house structural/decor version, consumed by
`VersionControl`) and the `SeedSystemManager` variant set (item/puzzle/spawn-area configuration) are
**two different, independently-versioned axes** of the game's content, not the same selector. This
is an inference from the numeric mismatch, not a proven fact — flagged as **INFERRED**, not verified.

---

## Summary of VERIFIED vs INFERRED vs UNDETERMINED

**VERIFIED (decompiled, VA-cited):**
- `VersionControl` and `VersionControlItemChange` classes exist with the exact methods/fields
  `re_scene_data.md` described (`DisableOneSixObjects`, `DisableOneFiveObjects`,
  `DisableOneFourObjects`, `DisableOneThreeObjects`, `DisableOneTwoObjects`, `VersionPlayerPrefs1/2/3`).
- `CURVERSION` (PlayerPrefs int, `StringLiteral_4166` @ 0x180c43130) is **actively read at runtime**
  by `VersionControl$$Start` (0x180214fd0) and `VersionControlItemChange$$Start` (0x18022c620), not
  merely written once as a migration flag. `v1.0_CONCLUSION.md` section 4 is incorrect on this point.
- No code in GameAssembly.dll references the string `"SeedSystemManager"`, and no manager class holds
  a serialized array of the 10 `SeedSystemManager_*` GameObjects or a `SeedManager_o*` field tied to
  `CURVERSION`.
- No `Menu_Version`-equivalent UI class exists; the only `CURVERSION` write is a one-time default in
  `MainMenu$$Start`.

**INFERRED (reasonable but not proven):**
- `CURVERSION`'s 9-value range (0-8) vs. the 10 `SeedSystemManager` variants suggests these are
  separate versioning systems (house layout vs. item/puzzle config), not the same selector.

**UNDETERMINED (explicitly not guessed):**
- The actual mechanism that activates exactly one `SeedSystemManager_*` GameObject at runtime. Static
  analysis of GameAssembly.dll found no code path performing this selection; it is either baked into
  the scene's serialized `m_IsActive` state at build time, or performed through an Inspector-wired
  event mechanism with no call site visible to IL2CPP static xrefs.
- Any mapping from a selector value to a specific `SeedSystemManager_*` variant name — no such
  mapping exists in code because no such selector code was found.
