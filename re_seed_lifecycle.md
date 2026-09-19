# GameSeed / RandomSeed PlayerPrefs lifecycle — Granny: Legacy 1.8.9 (GameAssembly.dll, IL2CPP x64)

All findings below are derived directly from IDA MCP decompilation/disassembly of the loaded
`GameAssembly.dll` (imagebase `0x180000000`) and cross-checked against
`C:\Users\ir0n1c\Desktop\Il2CppDumper-win-v6.7.46\dump.cs`. Full decompile transcripts are appended to
`C:\Users\ir0n1c\grannyseedpredictor\for_claude_the_logic_pro.txt` under the `[[IDA:...]]` tags referenced
throughout this document.

## 1. Complete call map for "GameSeed" and "RandomSeed"

String literals:
- `"GameSeed"` = `StringLiteral_6078` @ `0x180c4e0b0`
- `"RandomSeed"` = `StringLiteral_2252` @ `0x180c39198`

`PlayerPrefs` icall addresses (resolved via `func_query`):
- `PlayerPrefs.GetInt(string)` → `0x1807277c0`
- `PlayerPrefs.GetInt(string,int)` → `0x180727780` (`GetInt_6449952640`, the 2-arg overload with default value)
- `PlayerPrefs.SetInt` → `0x1807279c0`
- `PlayerPrefs.TrySetInt` → `0x180727b30`
- `PlayerPrefs.Save` → `0x180727900`

**Exhaustive xrefs to the "GameSeed" string literal (only 4 references in the entire binary, 2 functions):**

| Address | Function | Action |
|---|---|---|
| `0x18024a112`/`0x18024a125` | `Menu_Seed$$.ctor` @ `0x18024a100` | Sets `this->SeedPrefKey = "GameSeed"` (field only, no Get/SetInt call here) |
| `0x18024fbab` | `SeedManager$$GeneratePlacement` @ `0x18024f770` | Interns the string literal at function entry (IL2CPP metadata init pattern) |
| `0x18024fc89` | `SeedManager$$GeneratePlacement` @ `0x18024f770` | `PlayerPrefs.GetInt("GameSeed")` — **read only** |

**Exhaustive xrefs to the "RandomSeed" string literal (8 references, 3 functions):**

| Address | Function | Action |
|---|---|---|
| `0x1802214da` | `MainMenu$$Start` @ `0x180220e70` | Interns literal (used later in first-run defaults block) |
| `0x180221c0b` | `MainMenu$$Start` | `PlayerPrefs.SetInt("RandomSeed", 1)` — one-time default (see §3) |
| `0x180249ec2`/`0x180249f56` | `Menu_Seed$$Start` @ `0x180249eb0` | `PlayerPrefs.GetInt("RandomSeed")` to initialize the UI toggle's `isOn` state |
| `0x180249fb2`/`0x180249fd2` | `Menu_Seed$$Update` @ `0x180249fa0` | Interns literal / `PlayerPrefs.SetInt("RandomSeed", toggle.isOn)` — **every frame** |
| `0x18024fbb7` | `SeedManager$$GeneratePlacement` | Interns literal |
| `0x18024fc5f` | `SeedManager$$GeneratePlacement` | `PlayerPrefs.GetInt("RandomSeed")` — read only, gates the random-fallback branch |

`Menu_Seed$$ExitTypingModeAndSave` @ `0x180249e00` also writes `"GameSeed"` (via the cached `SeedPrefKey`
field, not a direct string-literal reference, so it does not show up in the xref list above) — see §2.

No other function in `GameAssembly.dll` references either string literal. No other PlayerPrefs key
containing "Seed" exists anywhere in the binary (verified via `search_text`/`find_regex` across the whole
image and via `dump.cs`). `ItemSeedData` is an unrelated per-pickup-item component class (not a PlayerPrefs
key). The `"Seed :"` string (`StringLiteral_2677`) used in `Paused$$Update` is only a UI label for
displaying `SeedManager.Seed` in the pause menu — read-only, no PlayerPrefs write.

## 2. Full seed lifecycle, in order

1. **First launch after install / after a version bump** — `MainMenu$$Start` (`0x180220e70`) runs a
   one-time defaults block gated by `PlayerPrefs.GetInt(StringLiteral_1136) == 0`. If triggered, it sets
   `CURVERSION = 8` and, among a batch of unrelated defaults, **unconditionally sets `RandomSeed = 1`**.
   It does **not** touch `GameSeed`. (`0x180221541`-`0x180221c9e`)
2. **Menu_Seed UI screen opens** (`Menu_Seed$$Start`, `0x180249eb0`): reads `GameSeed` via
   `PlayerPrefs.GetInt` and populates the seed `TMP_InputField.text` with it; reads `RandomSeed` and sets
   the "use random seed" `Toggle.isOn` accordingly. **Read only.**
3. **While the Menu_Seed screen is active**, `Menu_Seed$$Update` (`0x180249fa0`) runs every frame and:
   - unconditionally writes `PlayerPrefs.SetInt("RandomSeed", RandomToggle.isOn)` + `PlayerPrefs.Save()`
     **every single frame**, regardless of whether the player is typing.
   - if the player is in "typing" mode (`IsTypingSeed == true`, entered via `BeginTypingSeed()`,
     `0x18024a9d0`... not decompiled — presumably wired to the input field's `OnSelect`) and presses
     Enter or Escape, it parses the input text (`int.TryParse`, default 0 on empty/invalid) and
     writes `PlayerPrefs.SetInt("GameSeed", parsedValue)` + `PlayerPrefs.Save()` immediately.
4. **`Menu_Seed$$ExitTypingModeAndSave`** (`0x180249e00`) performs the identical
   trim/parse/`SetInt("GameSeed", ...)`/`Save()` sequence. It has **no direct code caller** anywhere in
   the assembly — its only xrefs are IL2CPP metadata-table entries, meaning it is invoked through Unity's
   managed event/delegate system (almost certainly the `TMP_InputField`'s `OnEndEdit` or `OnDeselect`
   UnityEvent bound in the Inspector to this method). This means it fires whenever the seed input field
   loses focus/finishes editing — **including cases where the field's text was never changed by the
   player in the current session**, e.g. if the field lost focus for any UI-navigation reason while still
   showing whatever text `Start()` populated it with (or a stale value from an earlier scene visit if the
   `Menu_Seed` GameObject persists in the scene without a fresh `Start()` call).
5. **`SeedManager$$GeneratePlacement`** (`0x18024f770`), called when a level actually generates item
   placement, reads `RandomSeed` and (if `RandomSeed==1` AND the Inspector-serialized
   `SeedManager.RandomizeSeed` field at `this+0x44` is also true) ignores the stored seed entirely and
   draws a fresh `UnityEngine.Random.RandomRangeInt(0, 999999999)`. Otherwise it reads `GameSeed` via
   `PlayerPrefs.GetInt` and stores it at `this+0x40`. The retry loop (up to 50 attempts) then constructs
   `new System.Random(Seed + attemptNumber)` at `this+0x50` and uses that RNG for the entire placement
   algorithm. **This function never writes `GameSeed` or `RandomSeed`.**

## 3. Per-save seed persistence

No code path was found anywhere in `GameAssembly.dll` that copies a seed value into or out of a save-game
file/structure. Searching the whole binary for `"Seed"`-containing PlayerPrefs keys, and for any function
touching `SavedVariables`/`VariablesAsset` (Unity Visual Scripting) in relation to seed data, found nothing.
`SavedVariables`/`VariablesAsset` classes exist (`TypeDefIndex` 4490/4491/4497) but have zero cross-reference
overlap with `SeedManager`, `Menu_Seed`, or either seed string literal. **There is no evidence of a
per-save/per-slot seed anywhere in this binary; `GameSeed` and `RandomSeed` are both flat, global,
install-wide PlayerPrefs values, not tied to any particular save file.** (If the game has save slots at
all, they do not appear to carry their own seed — UNCONFIRMED beyond "no code references found"; would
need to inspect the save-file format/save-related classes directly to fully rule this out, which was out
of scope for the string-xref-driven search performed here.)

## 4. Does anything auto-overwrite GameSeed on new-game/continue?

**No.** The complete, exhaustive xref list for the `"GameSeed"` literal contains exactly two functions:
`Menu_Seed..ctor` (just caches the key name, doesn't call Get/SetInt) and `SeedManager.GeneratePlacement`
(read-only). There is no "new game" or "continue/load save" function anywhere in the binary that writes
`GameSeed`. The **only** two functions in the entire assembly that ever call
`PlayerPrefs.SetInt("GameSeed", ...)` are `Menu_Seed$$Update` and `Menu_Seed$$ExitTypingModeAndSave`, and
both require the Menu_Seed UI object/input field to have been interacted with (typing + Enter/Escape, or
the field losing focus/ending edit) — not an automatic consequence of starting or continuing a game.

`PlayerPrefs.Save()` is called explicitly and immediately after every `SetInt("GameSeed", ...)` and after
every `SetInt("RandomSeed", ...)` call found in this binary (see disassembly excerpts in
`for_claude_the_logic_pro.txt`). There is no batching/delay in the managed code — each write is followed
by an explicit `Save()` in the same function, in the same frame it was set. **Caveat (UNCONFIRMED from
this DLL):** Unity's actual Windows-registry-backed `PlayerPrefs` storage engine lives in `UnityPlayer.dll`
/ native engine code, not in `GameAssembly.dll`. Whether that native layer caches all preference reads for
the lifetime of the process, or writes through to the registry synchronously on every `Save()` call, is
**not determinable from GameAssembly.dll** and would require examining `UnityPlayer.dll` or empirical
registry-monitoring (e.g. Procmon) during a real run.

## 5. Answer to Question 6 — how to reliably force a specific seed

Based purely on the control flow actually present in `GameAssembly.dll`:

**(a) Write the registry key, then start a NEW GAME — RECOMMENDED, should work.**
No code path triggered by starting a new game touches `GameSeed`. The only risk is if the player (or some
automatic UI flow) visits the `Menu_Seed` screen with stale seed text and that field then loses focus
(triggering `ExitTypingModeAndSave`) or the Enter/Escape branch of `Update()` fires, in which case the
*currently displayed* text would be re-written — but that is UI-input-driven, not automatic. If the
player never opens/touches the Seed menu between writing the registry and generating placement, the
registry value should be read unmodified by `GeneratePlacement`. **Important precondition:** the
`RandomSeed` PlayerPrefs value must be `0` (i.e., "use random seed" toggle OFF) — if it is `1` AND the
current level's `SeedManager.RandomizeSeed` Inspector field happens to be true, `GeneratePlacement` will
ignore `GameSeed` entirely and roll a fresh random seed regardless of what's in the registry. Also note
`MainMenu.Start` forces `RandomSeed = 1` once per version bump (`CURVERSION` mismatch) — so after a game
update, the toggle may silently reset to "on" and must be turned off again (via the Seed menu UI, or by
also patching the `RandomSeed` registry value alongside `GameSeed`) before a manual seed will take effect.

**(b) Write the registry key, then CONTINUE a save — should also work, same reasoning.**
No "continue/load save" logic was found anywhere that reads a seed out of save data and re-injects it into
`PlayerPrefs["GameSeed"]`; no such save-to-pref copy path exists in this binary at all (§3). The same
`RandomSeed` toggle precondition from (a) applies identically here — it is a global state, not tied to the
save being continued.

**(c) A runtime (Harmony) patch is NOT required by the evidence gathered** — no automatic overwrite path
exists in the code. If your tool still empirically observes the seed reverting after writing the registry
and launching fresh, the most likely explanations *outside* the scope of this IL2CPP-only binary are: (i)
the Menu_Seed UI screen being visited/its input field losing focus during that session before
`GeneratePlacement` ran, re-committing whatever stale text was cached in the field; (ii) the `RandomSeed`
toggle being `1` with `SeedManager.RandomizeSeed` true, causing the random fallback branch to run instead
(though this would produce a *genuinely random* value each run, not consistently return the same old
value, so it doesn't cleanly explain a repeat of exactly `915074960`); or (iii) native
`UnityPlayer.dll`/Windows-registry `PlayerPrefs` caching/timing behavior not visible in `GameAssembly.dll`
(see caveat in §4). To settle which of these applies, the next concrete steps would be: confirm whether
`Menu_Seed`'s GameObject is marked `DontDestroyOnLoad` / persists across scene loads (requires scene/prefab
data, not present in this DLL), check the live value of the `RandomSeed` and `CURVERSION` registry entries
at the moment of the discrepancy, and/or monitor registry writes with Process Monitor across a full launch
to see exactly which process write (if any) changes `GameSeed` back to the old value.

## 6. SeedSystemManager / CURVERSION variant selection — does not exist as described

`dump.cs` and the full IDA function list contain **no `SeedSystemManager` class or function of any kind**.
There is exactly **one** `SeedManager` class (`TypeDefIndex 2503`, `MonoBehaviour`) in the entire assembly
— not 10 variants. `CURVERSION` (`StringLiteral_4166` @ `0x180c43130`) is used only as a simple
one-time-migration flag in `MainMenu$$Start` (§3 above: sets `CURVERSION = 8` and resets several defaults,
including `RandomSeed = 1`, if the migration flag `StringLiteral_1136` hasn't been set for this version
yet). No `VersionControl`-driven selection between multiple seed-manager variants was found; `VersionControl`
(`0x180214fd0` `Start`) is an unrelated class that toggles various level GameObjects/props by difficulty
version and has no connection to `SeedManager` or either seed PlayerPrefs key.

## Confirmed field/struct layout

- `SeedManager` (`0x180254f00` ctor): `Seed` = `this+0x40` (int), `RandomizeSeed` = `this+0x44` (bool,
  Inspector-serialized), `activatePlacedItems` = `+0x45`, `fillAllFreeSpawns` = `+0x46`, `Rigid` = `+0x47`,
  `EscapeItemPuzzleChance` = `+0x48` (float), `rnd` (System.Random) = `+0x50`.
- `Menu_Seed` (`0x18024a100` ctor): `IsTypingSeed` = `+0x20` (bool), `SeedInput` (TMP_InputField) = `+0x28`,
  `RandomToggle` (Toggle) = `+0x30`, `SeedPrefKey` (string, = "GameSeed") = `+0x38`,
  `SavingOrNo` (GameObject) = `+0x40`.

## Renames/comments added to the IDB

- `0x18024fc48` — GeneratePlacement seed-read logic explained (read-only, RandomSeed/RandomizeSeed gating).
- `0x180249fa0` — Menu_Seed.Update annotated as sole automatic per-frame RandomSeed writer + conditional GameSeed writer.
- `0x180249e00` — Menu_Seed.ExitTypingModeAndSave annotated: UI-event-invoked GameSeed writer, no static caller.
- `0x180221541` — MainMenu.Start first-run defaults block annotated (CURVERSION=8, RandomSeed=1 defaults).

Full pseudocode/disasm transcripts backing every claim above are in
`C:\Users\ir0n1c\grannyseedpredictor\for_claude_the_logic_pro.txt` under tags:
`[[IDA:SeedManager_GeneratePlacement_GameSeedRead_0x18024f770]]`,
`[[IDA:MenuSeed_Update_GameSeedWrite_0x180249fa0]]`,
`[[IDA:MenuSeed_ExitTypingModeAndSave_GameSeedWrite_0x180249e00]]`,
`[[IDA:MainMenu_Start_FirstRunDefaults_0x180221541]]`,
`[[IDA:PlayerPrefs_SeedKeys_XrefSummary]]`.
