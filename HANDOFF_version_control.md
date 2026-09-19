# HANDOFF — Version-Control Seeds feature

Continue work on the Granny Legacy seed predictor at `C:\Users\ir0n1c\grannyseedpredictor`.
Windows, PowerShell + Git Bash, not a git repo. Backups of every file I edited are in `backups/*.pre_*`.

## The feature

Let the user pick a **house version** in the GUI and generate seeds specific to that version.
The game ships 10 `SeedManager` variants (`SeedSystemManager_1.0`…`_1.7`, `_Normal`, `_More`),
each with a different item/puzzle/spawn-area set — earlier versions have fewer items and fewer
accessible areas. **This feature is essentially COMPLETE and validated.** What remains is a final
regression re-run and documentation.

## Sizes (verified, from `scene_data.json` → `seedManagerVariants`)

| variant | items | puzzles | areas | freeSpots | totalSlots | GPU mask |
|---|---|---|---|---|---|---|
| 1.0 | 6 | 1 | 6 | 38 | 39 | 32-bit |
| 1.1 | 7 | 2 | 7 | 47 | 49 | 32-bit |
| 1.2 | 7 | 4 | 7 | 47 | 51 | 32-bit |
| 1.3 | 13 | 7 | 8 | 51 | 58 | 32-bit |
| 1.4 | 17 | 8 | 9 | 59 | 67 | 32-bit |
| 1.5 | 25 | 9 | 10 | 62 | 71 | 32-bit |
| 1.6 | 26 | 10 | 10 | 62 | 72 | 32-bit |
| 1.7 | 27 | 13 | 11 | 66 | 79 | 32-bit |
| Normal | 31 | 16 | 12 | 71 | 87 | 32-bit |
| More | 35 | 18 | 12 | 71 | 89 | **64-bit** |

Smaller variants are **easier** to solve (fewer slots ⇒ each pin costs fewer bits):
v1.0 supports ~5–6 pins reliably; Normal only ~4.

## What was built (all done, all verified)

1. **`variants.py` (NEW)** — loads any variant from `scene_data.json` and returns the exact dict
   shape `simulator.load_config()` returns, so it is a drop-in substitute.
   API: `VARIANT_DISPLAY_ORDER`, `list_variants()`, `variant_stats(name)`, `load_variant_config(name)`.
   Scene data lacks positions/instanceIds, so they are joined from the newest `dumps/config_*.json`
   by the `path` string (spots, puzzle spawn points) and by `itemName` (items). Coverage is 100%
   for 9 variants; `More` has 6 objects with no match (4 items + 2 puzzle spawn points) which get
   `position=None` and a synthetic negative instanceId, surfaced via the returned `"unmatched"` list.
   **List order from `scene_data.json` is preserved exactly — it is RNG-load-bearing.**
   Data paths are anchored to the module dir (not CWD).

2. **`simulator.py`** — fixed a latent precision bug: `EscapeItemPuzzleChance` is a C# `float32`,
   and IL2CPP promotes it to double as `0.699999988079071`, but the JSON dump stores `0.7`.
   Added `_as_float32()` and applied it in `load_config()`.

3. **`search.py`** — feasibility is now variant-aware. `estimate_expected_matches(num_pins, total_slots)`
   and `feasibility_message(num_pins, total_slots)` take a slot count; thresholds now derive from the
   estimate instead of hardcoded pin counts. Added `total_slots_for_config(config)` (returns 87 for Normal,
   matching the old hardcoded constant).

4. **`kernel.cl` + `gpu_backend.py`** — added a selectable 64-bit item-mask mode.
   `ITEM_MASK_BITS` (default 32) selects `typedef uint|ulong imask_t` plus an `IBIT(i)` macro.
   `nth_set_bit` was split into `nth_set_bit_u` (32-bit, area-spot masks) and `nth_set_bit_i`
   (item masks). Only **item-indexed** masks widened; area-spot masks and `puzzle_used_mask`
   stay `uint` (max 16 spots/area, max 18 puzzles). Host side uses `SceneLayout.imask_dtype`.
   `MAX_ITEMS` raised to 64. 64-bit is selected automatically when `num_items > 32` (only `More`).

5. **`gui.py`** — "House version" `ttk.Combobox` (default `Normal`), live per-variant stats,
   `_rebuild_item_table()` / `_rebuild_for_variant()` that rebuild the item rows and slot catalog
   and reset all pins on change, dynamic `Items (N)` label, variant-aware feasibility, notes for
   `More` (64-bit mask + unmatched objects), and in-game apply instructions (see below).
   Falls back to today's mod-dump path if `variants.py`/`scene_data.json` is unavailable.

6. **`gpu_backend.py` layout-cache fix** — `_get_layout` cached on `id(config)` alone. Variant
   switching creates and drops config dicts, so a recycled `id` could silently return e.g. a
   31-item layout for a 6-item config. The cache entry now also retains a strong reference to the
   config and verifies identity on hit.

## Reverse-engineering result — how the player selects a version (VERIFIED)

`ObjectsManager.Start()` (RVA `0x22E7B0`, on `GameManager/HandleObjects`) picks the variant.
Its Inspector fields are named `Seed_Main` / `Seed_Extra` / `Seed_17`…`Seed_10`, which is why an
earlier search for the string "SeedSystemManager" found nothing in the binary.

```
if (PlayerPrefs.GetInt("FlashL") != 1)   -> no seed system at all; old fixed preset layout
else if (PlayerPrefs.GetInt("Extras") != 0) -> _More
else switch (PlayerPrefs.GetInt("CURVERSION")):
        0->1.0  1->1.1  2->1.2  3->1.3  4->1.4  5->1.5  6->1.6  7->1.7  8->Normal
```

In the MainMenu these are real controls: a **"Game Version" slider labelled (1.0)…(1.8)** writing
`CURVERSION`, an **Extras toggle** (interactable only at slider 8) writing `Extras`, and a
**Flash toggle** (not the flashlight) writing `FlashL`. So per-version prediction is **playable,
not hypothetical**. `Normal` is the default (`CURVERSION=8, Extras=0`), which is why the existing
mod dump matched it.

Reports written: `re_version_control.md`, `re_variant_activation.md`.

> **This corrects `v1.0_CONCLUSION.md` §4**, which claims no `VersionControl` selector exists and
> that `CURVERSION` is a dead migration flag. Both claims are wrong: `VersionControl` and
> `VersionControlItemChange` exist, and `CURVERSION` is read 10× in `VersionControl.Start`.

## Validation already performed (all PASSED — do not redo unless you change something)

- `validate_all.py` after the float32 fix: **155/155 placements** (5 samples × 31). The one
  `SKIPPED` sample is pre-existing — that earliest dump predates the mod recording spot positions.
- `test_gpu.py` after the float32 fix: **6009/6009 bit-exact** (3655 attempt-1 + 345 retry-required),
  seeds 915074960 and 123456789 found, **6.09M seeds/sec**.
- `variants.py` self-test: `load_variant_config("Normal")` is **identical** to the newest mod dump.
- Behavioral equivalence: scene-derived Normal vs dump config → **503/503 identical layouts**
  (incl. the three in-game-confirmed seeds).
- 64-bit mode forced onto the 31-item config: **4507/4507** bit-identical to both the CPU simulator
  and the 32-bit GPU path.
- **Cross-variant GPU-vs-CPU acceptance test: all 10 variants PASS**, including `More` at 35 items /
  64-bit with 40 retry-required seeds. Harness:
  `<scratchpad>\validate_variants_gpu.py` (scratchpad =
  `C:\Users\ir0n1c\AppData\Local\Temp\claude\C--Users-ir0n1c-grannyseedpredictor\05ea5d06-2cb6-45c0-92ab-ec71b31cc20c\scratchpad`)

## WHAT REMAINS

Items 1-3 below were COMPLETED after this handoff was first written. Re-verified end state:
`test_gpu.py` 6009/6009 (345/345 retry) at ~5.9M seeds/sec; `validate_all.py` 155/155 (1 pre-existing
SKIP); `variants.py` self-test PASS; cross-variant GPU-vs-CPU **all 10 variants PASS**; `import gui` OK;
headless GUI drives all 10 variants correctly (rows/catalog/slots update, pins reset, round-trips).
Docs updated in `v1.0_CONCLUSION.md` (sections 2.5.1, 4, 5, 8, 9, 11) and `README.md`.

~~1. Final regression re-run~~ — DONE, all green.
~~2. Manual GUI check~~ — DONE headlessly; a human eyeball of the live window is still nice-to-have.
~~3. Documentation~~ — DONE. Three unmeasured claims were caught and corrected during review:
   the float32 divergence rate (doc said 0.0043%, true derived value ~0.0000163% / ~700 seeds of 2^32),
   a README line implying the in-game *version-selection* steps were empirically confirmed (they are
   decompilation-derived and NOT yet confirmed in-game), and a "never retries" claim now qualified as
   0/6,600 sampled seeds with the causal explanation marked INFERRED.

### STILL OPEN

**A. In-game confirmation of a non-Normal seed — the one real gap.**
Nothing from v1.0-v1.7/More has been played end-to-end. Ready-made test case:
**house version 1.0, seed `7908`** -> Hammer=PUZZLE:Safe, Pliers=Kitchen/Spot (35),
MasterKey=Bathroom/Spot (47), PDKey=BasementArea/Spot (3), SafeKey=Bedrooms/Spot (58),
Code=Bedroom GrannyGrandpa/Spot (68). First four were pinned; **SafeKey and Code are unpinned
predictions** and are the stronger test. Apply: Flash ON, Game Version (1.0), Extras OFF, then
Seed menu -> 7908 -> Enter -> new run.

**B. Negative seeds may be unusable (pre-existing, not introduced by this feature).**
The GUI's "Full 32-bit range" is `(-2147483648, 2147483647)`, so the first hits returned are
typically negative (a 4-pin v1.0 search returned `-2147349665`). Every in-game-confirmed seed
(915074960, 123456789, 10408711) is positive, and it is unverified whether the in-game Seed menu
accepts a minus sign. Either confirm negatives work in-game, or default the GUI to a non-negative
range. The user was asked about this and has not yet answered.

**C. Optional — auto-detect the player's current version.**
`seed_registry.py` already reads the game's PlayerPrefs registry key
(`HKCU\Software\Omega Mega Gigal Intel\Granny: Legacy\`). It could read `CURVERSION`, `Extras` and
`FlashL` to preselect the matching variant in the GUI, preventing seeds generated for the wrong
version. **Reading is safe; writing is known NOT to reach the game** (`v1.0_CONCLUSION.md` section 3)
— do not attempt to set the version by writing the registry.

## Working agreements from this session
- Subagents available: `builder` (implementation), `the-ida-analyst` (IDA MCP / binary RE),
  `scout` (recon, boilerplate, reading large outputs), `command-executer` (runs a command and reports
  only what you ask for — use it for long/noisy output).
- Prefer `grep` over reading long files. Put throwaway scripts in the scratchpad, never the project root.
- Don't "fix" faithfully-reproduced game quirks (`v1.0_CONCLUSION.md` §2.6).
- The project has a hard-won rule: **never claim a result you did not measure.** §8 of
  `v1.0_CONCLUSION.md` is a list of confident wrong claims that cost real time.
