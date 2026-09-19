# Granny Legacy Seed Predictor

A tool for **Granny: Legacy** that predicts which random seeds produce a
desired item placement, and lets you search millions to billions of seeds
for one that matches. It works by exactly reproducing the game's own item
randomizer in Python (and, for speed, on the GPU), not by guessing or
approximating.

## What it does

You tell the tool which items you want pinned to specific spots (or which
you'd merely prefer), and it searches the 32-bit signed seed space
(`-2147483648 .. 2147483647`) for seeds whose resulting item placement
satisfies those constraints. Every reported seed is guaranteed correct: it
is either produced directly by, or independently re-verified against,
`simulator.py` -- a bit-exact re-implementation of the game's own
`SeedManager.GeneratePlacement` (including its internal retry loop, escape
item rules, puzzle-dependency validation, and circular-dependency check).

## Validation evidence

This is not a heuristic or an approximation -- it is a from-scratch,
reverse-engineered reimplementation, checked against the real game at every
level:

- **Bit-exact `.NET System.Random`** (`net_random.py`): reproduces the
  exact PRNG sequence Unity's Mono/.NET runtime produces for a given seed.
- **Reference simulator** (`simulator.py`): reproduces the real game's item
  placement **31/31 items correct** on two independently-obtained real
  seeds --
  - `915074960` (the seed originally used to build and tune the simulator)
  - `123456789` (an independent seed, chosen after the simulator was
    finished, never used during development -- 28 of the 31 items land in
    *different* slots between the two seeds, so this is not a vacuous
    match).
- **GPU backend** (`gpu_backend.py` + `kernel.cl`): an OpenCL port of the
  full placement algorithm (including the retry loop and the puzzle
  dependency/circular-dependency acceptance check), validated at
  **6009/6009 seeds bit-exact** against `simulator.py` -- including all 345
  sampled seeds that require a retry (the hardest case to get right).
  Every seed the GPU backend reports is additionally re-verified on the CPU
  with `simulator.simulate()` before being returned, so there is no
  possible false positive.
- **Measured throughput**: ~6.05 million seeds/sec on an AMD RX 7700 XT
  (gfx1101, 27 CUs) -- a full sweep of the entire 2^32 seed space in
  roughly **12 minutes**.

See `CLAUDE_FINDINGS.md` for the full reverse-engineering log, and
`re_*.md` for the detailed per-subsystem writeups.

## Installation

### Automatic (easiest, Windows)

**Double-click `install.bat`, press ENTER when it asks, and wait.**

That is the whole procedure. The installer does everything else by itself:

1. Checks whether this computer already has Python. If it does not, it
   downloads Python 3.12.10 from the official site (python.org) and
   installs it — no questions, no options to choose. If Windows pops up a
   permission window, click **Yes**.
2. Installs the parts the program needs (`numpy`, and `pyopencl` for
   graphics-card speed — if that one fails it just says so and carries on,
   the program still works).
3. Opens the program.

It shows `STEP 1 of 3` … `STEP 3 of 3` as it goes. The screen can look
frozen for a minute or two while Python installs — that is normal, just
don't close the window.

If anything goes wrong, it stops and prints a short message saying exactly
what to do. The one you are most likely to see is **"ALMOST THERE — ONE
MORE STEP"**: it means Python got installed but this window can't see it
yet, so close the window and double-click `install.bat` once more.

**After the first time you don't need the installer again — just
double-click `run.bat` to open the program.**

### Manual

1. **Install Python 3.9 or newer** (3.11+ recommended) from
   <https://www.python.org/downloads/>. In the installer, tick
   **"Add python.exe to PATH"** and keep the **"tcl/tk and IDLE"**
   component — `gui.py` needs `tkinter`.

2. **Install the libraries:**

   ```
   python -m pip install -r requirements.txt
   ```

   Or individually:

   ```
   python -m pip install numpy
   python -m pip install pyopencl   # optional, GPU acceleration
   ```

   `pyopencl` is optional. Without it the tool runs on the multiprocessing
   CPU backend — everything works, it's just much slower. GPU search also
   needs up-to-date graphics drivers providing an OpenCL runtime with
   `cl_khr_fp64` support.

3. **(Optional) use a virtual environment** if you'd rather not install
   into your system Python:

   ```
   python -m venv .venv
   .venv\Scripts\activate
   python -m pip install -r requirements.txt
   ```

4. **Check the install:**

   ```
   python -c "import tkinter, numpy; print('ok')"
   ```

5. **Start the tool** — `run.bat`, or:

   ```
   python gui.py
   ```

### Troubleshooting

| Symptom | Fix |
|---|---|
| `install.bat` can't find Python after installing it | Close the window, open a new one, run `install.bat` again (PATH refresh). |
| Nothing appears when you run `run.bat` | Run `run_debug.bat` instead — it keeps a console open and shows the error. |
| `ModuleNotFoundError: No module named 'tkinter'` | Re-run the Python installer and enable the "tcl/tk and IDLE" component. |
| `pyopencl` fails to install or no GPU is offered | Not fatal — the CPU backend is used. For GPU, update your graphics drivers (they supply the OpenCL runtime). |
| GUI starts but reports no scene dump | A `dumps/config_*.json` must exist — see "Regenerating scene dumps" below. |

## How to run

```
run.bat
```

or directly:

```
python gui.py
```

A `dumps/config_*.json` scene dump must already
exist in `dumps/` (see "Regenerating dumps" below) -- the GUI loads the
newest one automatically.

On startup, the GUI tries to initialize an OpenCL GPU backend
(`gpu_backend.GpuSearchBackend`). If a working `cl_khr_fp64`-capable device
is found, it is used by default and the default search range is the full
32-bit seed space (a full sweep takes on the order of 10-15 minutes on a
modern discrete GPU). If no GPU is available, the tool falls back to a
multiprocessing CPU backend (`search.CpuSearchBackend`) and defaults to a
smaller "quick scan" range, since a full 2^32 sweep on CPU alone is
impractical. You can switch backends and the search range manually at any
time from the GUI.

The GUI includes a **House version** dropdown (default `Normal`) that lets
you choose which version to generate seeds for. The feasibility estimates,
pin ceiling, and item table all update based on your selection. Make sure
the in-game Game Version matches the version you selected in the tool.

## Using the tool

1. For each item you care about, set its state to **Pin** (the seed is
   rejected unless the item lands there) or **Prefer** (scored, but never
   rejects a seed), and choose a target slot.
2. Click **Generate**. Progress, live throughput, and an estimated time
   remaining are shown while the search runs; **Cancel** stops it.
3. Select a result to see its full 31-item placement and the seed itself,
   shown large in the **"Apply this seed"** panel.

## How to apply a seed

**Use the game's own Seed menu -- this is the only apply path confirmed to
work reliably:**

1. **In the tool:** note the house version you selected.
2. Click **"Copy seed to clipboard"** in the GUI (or just note the number).
3. **In-game (Main Menu first):**
   - Toggle **Flash ON** (this enables the seed system).
   - Set the **Game Version slider** to match the version you used in the tool:
     - Versions 1.0–1.7 → set to (1.0)–(1.7)
     - `Normal` version → set to (1.8)
     - `More` version → set to (1.8), then enable **Extras toggle**
4. Go to **Seed menu**.
5. Type the seed, then **press Enter**.
6. Start a **new** run.

⚠️ **The in-game version *must* match the version in the tool.** A correct
seed on the wrong Game Version will produce a different, wrong layout.

**What is and is not confirmed in-game here.** Steps 4-6 (the Seed menu
path) are empirically confirmed -- seed `123456789` was applied this way and
reproduced 31/31 placements. Step 3 (Flash / Game Version / Extras) is
*derived from decompilation* of `ObjectsManager.Start()`, which selects the
variant from the `FlashL`, `CURVERSION` and `Extras` PlayerPrefs; it has
**not yet been confirmed by playing a non-`Normal` version end-to-end.**
`Normal` is the game's default (`CURVERSION=8, Extras=0`), which is why the
existing dumps and the 155/155 in-game ledger are all `Normal`.

There is also an **"Advanced / experimental"** option that writes the seed
directly to the Windows registry (`seed_registry.py`). **Do not rely on
this** -- it was empirically found to *not* reliably reach the game (the
game's own Seed menu UI can re-commit a stale value over it). It is kept
only as a labelled, confirmation-gated fallback, mainly useful for
inspecting/backing up the current registry state. If you use it, the game
must be fully closed first.

## Pin-count feasibility

Every additional **Pin** constraint shrinks the pool of matching seeds
roughly geometrically, because each pin only matches if that specific item
lands on that specific slot out of the scene's addressable slots. Over the
full `2^32` seed space, the rough expected number of matching seeds for `k`
independent pins is approximately `2^32 / N^k` for `N` on the order of the
variant's total slots.

**For the Normal variant (87 slots):**

| Pins | Expected matching seeds (rough) |
|---|---|
| 1-3 | Usually thousands to millions -- easy |
| 4 | Tens to low hundreds -- still findable, may need the full range |
| 5 | Single digits -- a match may not exist at all |
| 6+ | Essentially zero -- almost certainly impossible |

**For earlier variants**, the denominators are smaller, so **more pins are
feasible**: v1.0 (39 slots) reliably supports ~5–6 pins; v1.1–1.3 support
~4–5; v1.4–1.7 support ~3–4.

This is a simplified model (it treats every slot as equally likely and
ignores that puzzle slots have far fewer eligible candidate items than free
slots, and any correlation between items), so treat it as a rough
estimate, not a guarantee -- see `search.feasibility_message()`. If a
search comes back empty, especially after already scanning the full 2^32
range, the most effective fix is almost always to **remove one pin
constraint** and search again.

## House Versions

The game ships 10 distinct house versions, each with a different set of
items, puzzles, and accessible areas. Each version has a different pinning
ceiling due to its slot count (smaller versions allow more pins). A seed
generated for one version will **not** work on another — the version
**must match** between the tool and the in-game setting.

| Version | Items | Puzzles | Total Slots | Pin Ceiling |
|---|---|---|---|---|
| 1.0 | 6 | 1 | 39 | ~5–6 |
| 1.1 | 7 | 2 | 49 | ~4–5 |
| 1.2 | 7 | 4 | 51 | ~4–5 |
| 1.3 | 13 | 7 | 58 | ~4 |
| 1.4 | 17 | 8 | 67 | ~3–4 |
| 1.5 | 25 | 9 | 71 | ~3–4 |
| 1.6 | 26 | 10 | 72 | ~3–4 |
| 1.7 | 27 | 13 | 79 | ~3–4 |
| Normal | 31 | 16 | 87 | ~4 |
| More | 35 | 18 | 89 | ~3–4 |

To select a version in-game: open the Main Menu and use the **"Game
Version" slider** to choose from (1.0) to (1.8), corresponding to versions
1.0 through 1.7 and Normal respectively. For the `More` version, set the
slider to (1.8) and enable the **Extras toggle**. The **Flash toggle**
(separate from the flashlight) must be ON for the seed system to be active.

## Regenerating scene dumps (`SeedDumper` mod)

The simulator's config comes from a `config_<seed>_<timestamp>.json` dump
produced by the `SeedDumper` MelonLoader mod, which observes
(never alters) `SeedManager.GeneratePlacement` via a Harmony
prefix/postfix patch. To regenerate a dump (e.g. after a game update
changes the scene layout):

1. Close the game, then copy `SeedDumper.dll` (in the project root) into
   the game's `Mods\` folder (MelonLoader must already be installed).
2. Launch the game and play until item placement happens (this runs once
   per level load; you don't need to do anything special beyond loading a
   level that uses the randomizer).
3. Two JSON files are written to `dumps/`: `config_<seed>_<ts>.json` (the
   simulation input, written before placement runs) and
   `placement_<seed>_<ts>.json` (the resulting item positions, written
   after -- useful as ground truth for re-validating the simulator).
4. Remove the mod by deleting `SeedDumper.dll` from `Mods\` when done.

## Project layout

| File | Role |
|---|---|
| `simulator.py` | Reference implementation of the game's item randomizer (31/31 validated on two real seeds) |
| `net_random.py` | Bit-exact `.NET System.Random` |
| `variants.py` | Loads and handles the 10 house versions from `scene_data.json`; drop-in interface matching `simulator.load_config()` |
| `search.py` | Backend-agnostic search engine, `Constraint`/`SeedResult` types, `CpuSearchBackend` |
| `gpu_backend.py` + `kernel.cl` | OpenCL GPU search backend (`GpuSearchBackend`), CPU-reverified; supports 32-bit and 64-bit item masks |
| `install.bat` | One-click installer: finds/installs Python, installs the libraries, then launches `run.bat` |
| `requirements.txt` | Python package requirements (`numpy`; optional `pyopencl`) |
| `run.bat` / `run_debug.bat` | Launchers (windowed / console-with-errors) |
| `gui.py` | Tkinter front-end: version selector, constraint editor, backend selection, search, results, apply-seed flow |
| `seed_registry.py` | Windows registry read/write for the game's `PlayerPrefs` seed value (experimental apply path) |
| `ground_truth.py` | Ground-truth placement data used during simulator validation |
| `dumps/` | Captured `config_*.json` / `placement_*.json` dumps |
| `scene_data.json` | Scene metadata for all 10 house versions extracted from the game's binary; source for `variants.py` |
| `CLAUDE_FINDINGS.md`, `re_*.md` | Reverse-engineering notes and validation logs |
