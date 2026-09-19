# What activates ONE of the 10 `SeedSystemManager_*` GameObjects at runtime

**Status: SOLVED.** The mechanism is `ObjectsManager.Start()`, driven by two PlayerPrefs
ints (`CURVERSION` and `Extras`, gated by `FlashL`), with the 10 GameObjects wired into
Inspector-assigned `GameObject` fields named `Seed_Main` / `Seed_Extra` / `Seed_17` ...
`Seed_10`.

The earlier static search failed only because the scene references them by *field*, and the
fields are named `Seed_*`, not `SeedSystemManager*` — so the string "SeedSystemManager"
genuinely never appears in `GameAssembly.dll`.

---

## 1. VERIFIED — scene bytes (`Granny Legacy_Data/level1`)

Scanned all 29,318 objects in `level1` for a `PPtr` (int32 fileID==0 + int64 pathID)
targeting any of the 10 `SeedSystemManager_*` GameObjects, their `Transform`s, or their
`SeedManager` MonoBehaviours (30 target path_ids in total).

Note: the path_ids listed in the task brief (26563, 27026, ...) are the **`SeedManager`
MonoBehaviour** path_ids, not the GameObject path_ids. Resolved mapping:

| Variant | GameObject | Transform | SeedManager | `m_IsActive` |
|---|---|---|---|---|
| SeedSystemManager_1.0 | 3954 | 12957 | 27102 | false |
| SeedSystemManager_1.1 | 6491 | 15470 | 28160 | false |
| SeedSystemManager_1.2 | 6211 | 15191 | 28036 | false |
| SeedSystemManager_1.3 | 6558 | 15536 | 28189 | false |
| SeedSystemManager_1.4 | 8179 | 17141 | 28938 | false |
| SeedSystemManager_1.5 | 3017 | 12029 | 26563 | false |
| SeedSystemManager_1.6 | 3973 | 12976 | 27118 | false |
| SeedSystemManager_1.7 | 6218 | 15198 | 28041 | false |
| SeedSystemManager_Normal | 3762 | 12767 | 27026 | false |
| SeedSystemManager_More | 7759 | 16727 | 28759 | false |

All ten sit under `GameManager/HandleObjects/`.

### Exactly 30 PPtr hits, from exactly 3 objects

**(a) `Transform` path_id 11782** — the `Transform` of `GameManager/HandleObjects`.
Holds all 10 variant Transforms in its `m_Children` array. Structural only; not activation.

**(b) `MonoBehaviour` path_id 26472, script class `ObjectsManager`,
on `GameManager/HandleObjects` (GameObject 2765).**
A contiguous run of 11 `PPtr<GameObject>` at raw byte offsets 1488..1619, in this order:

| byte offset | pathID | GameObject | field (from `dump.cs`) |
|---|---|---|---|
| 1488 | 3762 | SeedSystemManager_Normal | `Seed_Main` (0x288) |
| 1500 | 7759 | SeedSystemManager_More | `Seed_Extra` (0x290) |
| 1512 | 6218 | SeedSystemManager_1.7 | `Seed_17` (0x298) |
| 1524 | 3973 | SeedSystemManager_1.6 | `Seed_16` (0x2A0) |
| 1536 | 3017 | SeedSystemManager_1.5 | `Seed_15` (0x2A8) |
| 1548 | 8179 | SeedSystemManager_1.4 | `Seed_14` (0x2B0) |
| 1560 | 6558 | SeedSystemManager_1.3 | `Seed_13` (0x2B8) |
| 1572 | 6211 | SeedSystemManager_1.2 | `Seed_12` (0x2C0) |
| 1584 | 6491 | SeedSystemManager_1.1 | `Seed_11` (0x2C8) |
| 1596 | 3954 | SeedSystemManager_1.0 | `Seed_10` (0x2D0) |
| 1608 | 7862 | `ItemSpawnsAcrossMap (Seed System)` | `SeedStuff` (0x2D8) |

These are ten separate `GameObject` fields (no array length prefix precedes them) — the
serialized order matches the declared field order in `ObjectsManager` one-for-one.

**(c) `MonoBehaviour` path_id 28793, script class `Paused`,
on `GameManager/PauseManager` (GameObject 7838).**
The same 10 GameObjects at raw byte offsets 564..683 (the last fields of the object), in
the same order, matching `Paused`'s declared fields `SeedSysMain`, `SeedSysExtra`,
`SeedSys17` ... `SeedSys10` (0xC8..0x110). Cross-check: the `PPtr` just before them at
offset 456 resolves to path_id 26472 — i.e. `Paused.OM` correctly points at the
`ObjectsManager` above. Layout confirmed.

No other object in `level1` references any of the 30 target path_ids. (A handful of
"bare int64" coincidental byte matches inside `Mesh`/`MeshRenderer`/animation blobs were
also enumerated and are noise — no `fileID==0` prefix, wrong field context.)

## 2. VERIFIED — binary (`GameAssembly.dll`, IDA + Il2CppDumper `dump.cs`)

`ObjectsManager.Start()` (RVA `0x22E7B0`, VA `0x18022E7B0`) is the activator. It calls
`GameObject.SetActive(..., true)` on `SeedStuff` and then on exactly ONE `Seed_*` field:

```
if (PlayerPrefs.GetInt("Extras") == 0) {
    ... if (PlayerPrefs.GetInt("CURVERSION") == 8) { if (PlayerPrefs.GetInt("FlashL") == 1) { SeedStuff.SetActive(true); Seed_Main.SetActive(true); } }
    else if (CURVERSION == 7) { if (FlashL == 1) { SeedStuff.SetActive(true); Seed_17.SetActive(true); ... } }
    else if (CURVERSION == 6) -> Seed_16
    else if (CURVERSION == 5) -> Seed_15
    else if (CURVERSION == 4) -> Seed_14
    else if (CURVERSION == 3) -> Seed_13
    else if (CURVERSION == 2) -> Seed_12
    else if (CURVERSION == 1) -> Seed_11
    else if (CURVERSION == 0) -> Seed_10
} else {                       // Extras != 0
    if (FlashL == 1) { SeedStuff.SetActive(true); Seed_Extra.SetActive(true); }
}
```

Every branch is additionally gated on `PlayerPrefs.GetInt("FlashL") == 1`. If `FlashL != 1`
no `Seed_*` object and no `SeedStuff` object is activated at all — the seed system is off
and the legacy fixed/preset item layout is used instead.

String literals resolved via `Il2CppDumper/stringliteral.json`:
`StringLiteral_4166 = "CURVERSION"`, `StringLiteral_5730 = "Extras"`,
`StringLiteral_5882 = "FlashL"`, `StringLiteral_2099 = "Preset"`,
`StringLiteral_2117 = "PresetChoosin"`.

`ObjectsManager.Update()` does not touch any `Seed_*` field (decompiled, 81 lines, zero hits).

### Corroboration: `Paused.Update()`
Uses the identical selection rule to pick which variant's `SeedManager` to read for the
in-game stats panel:

```
if (PlayerPrefs.GetInt("FlashL") == 1) {
    if (Extras == 1)              go = SeedSysExtra;
    else if (CURVERSION == 8)     go = SeedSysMain;
    else if (CURVERSION == 7)     go = SeedSys17;   ... down to CURVERSION == 0 -> SeedSys10;
    Data_Preset.text = "Seed: " + go.GetComponent<SeedManager>().<field at +0x40>.ToString();
}
```
`+0x40` is `SeedManager.Seed` — this is the seed number shown to the player.

### Where the prefs come from: the retail main menu
`MainMenu.Update()` writes all three, from actual UI widgets (field names from `dump.cs`):

- `public Slider VersionControl;` → `PlayerPrefs.SetInt("CURVERSION", v)` where `v` is the
  slider value 0..8, and `MainMenu.Start()` sets the label `Ver_Text` to
  `"Game Version (1.0)"` ... `"Game Version (1.8)"` for values 0..8 respectively.
- `public Toggle Extras;` → `PlayerPrefs.SetInt("Extras", Extras.isOn)`. In
  `MainMenu.Update()` this toggle is `set_interactable(true)` only when the version slider
  reads `8.0`; at slider value `7.0` it is forced `isOn = false` and `interactable = false`.
- `public Toggle Flash;` (distinct from `public Toggle FlashLight;`) →
  `PlayerPrefs.SetInt("FlashL", 1|0)`; when 1 the menu shows `SeedButton` and hides
  `SliderPreset`, when 0 vice versa. This is the "random items / seed system" master switch.

`MainMenu.Start()` has a one-time defaults block that writes `CURVERSION = 8` and
`FlashL = 0`.

## 3. Resulting mapping

| `FlashL` | `Extras` | `CURVERSION` | Activated GameObject |
|---|---|---|---|
| 0 | any | any | none (seed system disabled; preset layout used) |
| 1 | 1 | any | `SeedSystemManager_More` |
| 1 | 0 | 8 | `SeedSystemManager_Normal` |
| 1 | 0 | 7 | `SeedSystemManager_1.7` |
| 1 | 0 | 6 | `SeedSystemManager_1.6` |
| 1 | 0 | 5 | `SeedSystemManager_1.5` |
| 1 | 0 | 4 | `SeedSystemManager_1.4` |
| 1 | 0 | 3 | `SeedSystemManager_1.3` |
| 1 | 0 | 2 | `SeedSystemManager_1.2` |
| 1 | 0 | 1 | `SeedSystemManager_1.1` |
| 1 | 0 | 0 | `SeedSystemManager_1.0` |

## 4. Can a retail player ever get a variant other than `_Normal`?

**YES — verified, and it is a first-class, documented main-menu option, not a debug path.**

Evidence:
- `MainMenu` owns a real `UnityEngine.UI.Slider VersionControl` whose value is written
  straight to `PlayerPrefs "CURVERSION"` and whose label reads
  `"Game Version (1.0)"` ... `"Game Version (1.8)"`.
- `MainMenu` owns a real `UnityEngine.UI.Toggle Extras` written to `PlayerPrefs "Extras"`,
  explicitly made interactable when the slider is at 8.
- `ObjectsManager.Start()` and `Paused.Update()` both branch on those prefs to select
  1 of the 10 variants.
- The player-visible in-game stats panel (`Paused.Data_Preset`) reads the seed from
  whichever variant that selection chose, so all 10 are live gameplay states.

So `_Normal` is only what the **default** prefs produce (`CURVERSION = 8`, `Extras = 0`),
which is why the MelonLoader dump of a default play session matched `_Normal` exactly.
Per-version seed prediction is **playable, not hypothetical** — it just requires the
predictor to know which `CURVERSION` / `Extras` the user picked in the main menu.

The three prefs also live in the Windows registry under the game's PlayerPrefs key, so a
predictor can read the player's current selection directly rather than asking.

## 5. INFERRED (clearly marked)

- The gate pref is literally named `"FlashL"`; the `Flash` Toggle it comes from is separate
  from the `FlashLight` Toggle. INFERRED: the key name is a leftover/reused key, and the
  toggle is presented to the player as the random-items/seed-system switch (supported by it
  showing `SeedButton` when on and `SliderPreset` when off, i.e. seed mode vs. preset mode).
- INFERRED: "1.8" in the menu label is the same thing the scene calls `_Normal`, since
  `CURVERSION == 8` selects `Seed_Main` → `SeedSystemManager_Normal`.
- INFERRED: `_More` is the "Extras" mode layered on top of version 1.8, since the Extras
  toggle is only interactable at slider value 8. At runtime, however, `ObjectsManager.Start`
  checks `Extras != 0` *before* looking at `CURVERSION`, so a pref edited outside the menu
  would give `_More` at any version value.

## 6. UNKNOWN

- Whether `ExtrasBought` / `FlashBought` (GameObjects on `MainMenu`) gate the Extras/seed
  options behind an unlock or purchase in some builds. Not investigated; the pref-writing
  code itself is unconditional in `MainMenu.Update()`.

## 7. Reproduction

Throwaway scan script (scratchpad, not in the project):
`%TEMP%\claude\C--Users-ir0n1c-grannyseedpredictor\05ea5d06-2cb6-45c0-92ab-ec71b31cc20c\scratchpad\findrefs.py`
plus saved decompilations `ObjectsManager_Start.c`, `MainMenu.c`, `Paused_Update.c` in the
same directory. `dump.cs` used: `C:\Users\ir0n1c\Desktop\Il2CppDumper-win-v6.7.46\dump.cs`
(`ObjectsManager` at line 94188, `Paused` at line 94324).
