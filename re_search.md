# re_search.md — Seed search engine design, early-rejection strategy, and measured performance

## 1. Scope

`search.py` implements the backend-agnostic seed-search engine described in
`CLAUDE_FINDINGS.md`. It is the compute core behind `gui.py`. This document
covers:

- The `SearchBackend` protocol a future GPU/OpenCL backend must implement.
- The early-rejection design, including a full correctness argument (not
  just "it's usually right").
- Measured throughput, with an honest accounting of the environment noise
  encountered while measuring it.
- Known limitations.

`simulator.py`, `net_random.py`, `ground_truth.py`, and `seed_registry.py`
are treated as validated and immutable. Nothing in `search.py` alters their
behavior; where a faster equivalent of one of their internals is needed
(see §4), it is implemented as a separate, independently-verified function
in `search.py`, never as an edit to `simulator.py`.

## 2. Data model

```python
@dataclass
class Constraint:
    item_name: str
    slot_label: str   # "FREE:Area/Spot (n)" or "PUZZLE:Name" -- exactly
                       # simulate()'s own output format
    kind: str          # "pin" | "prefer"

@dataclass
class SeedResult:
    seed: int
    pins_matched: int
    prefs_matched: int
    placement: dict    # itemName -> slot_label, the full 31-item placement
```

`kind="pin"` seeds are only ever returned when `pins_matched == total_pins`
(every pin holds). `kind="prefer"` only affects ranking (`prefs_matched`
descending, after all-pins-satisfied).

## 3. `SearchBackend` protocol (the GPU backend's contract)

```python
class SearchBackend(Protocol):
    def search(self, config: dict, constraints: list[Constraint],
               seed_range: tuple[int, int] = DEFAULT_FULL_RANGE,
               limit: int = 200,
               progress_cb: Optional[Callable[[int, int, int], None]] = None,
               cancel_evt: Any = None) -> list[SeedResult]: ...
```

- `config`: whatever `simulator.load_config(path)` returns.
- `seed_range`: **inclusive** `(start, end)` over signed int32 seed values.
  `DEFAULT_FULL_RANGE = (-2147483648, 2147483647)`,
  `DEFAULT_QUICK_RANGE = (0, 100_000_000)`.
- `limit`: stop once this many hits are found (0/falsy = unbounded — the
  caller is responsible for not doing that over the full 2^32 range without
  a plan).
- `progress_cb(seeds_done, total, hits_found)`: called periodically (not
  necessarily per-seed) from whatever context `search()` runs in. Must only
  ever be given plain ints, since the GUI marshals this across a thread
  boundary.
- `cancel_evt`: anything exposing `.is_set() -> bool`. Checked between
  `progress_cb` calls; must stop scanning promptly once set.
- Return value: `list[SeedResult]`, already ranked (all-pins-satisfied is a
  given for every entry — see §4.6 — ties broken by `prefs_matched` desc),
  truncated to `limit`.
- **Every returned `SeedResult.placement` must be exactly what
  `simulator.simulate(seed, config)` would independently produce.** A GPU
  implementation is free to port the per-attempt logic (§4) to OpenCL C for
  the scan itself, but should still verify candidate hits against the
  reference Python implementation (or an equally rigorous on-device
  equivalent — see §4.6's proof for what "equally rigorous" requires) before
  reporting them. "Probably right" is not an acceptable bar here; a wrong
  seed silently written to the registry is a worse failure mode than a slow
  search.

`CpuSearchBackend(num_workers=None, chunk_size=100_000)` is the only backend
implemented today. `num_workers` defaults to `multiprocessing.cpu_count()`.

## 4. Early rejection: design and correctness proof

### 4.1 The step order (from `simulate_verbose`)

GeneratePlacement retries in a loop: `attempt = 1, 2, ..., 50`, each with its
own `NetRandom(seed + attempt)` stream, stopping at the first attempt that
passes `!DetectCircularDependencies() && ValidatePuzzleDependencies()`
("accepted"). Within one attempt:

- **Step A** — greedy puzzle←item assignment (one `next_int` draw per
  puzzle with a non-empty candidate pool, in priority order).
- **Step B** — required escape items (roll `next_double`, then either free
  or puzzle placement).
- **Step C** — commit Step A's assignments (no further RNG).
- **Step D** — backfill any puzzle spawn still empty.
- **Step E** — mandatory-item spawn areas (dead code on this scene's config
  — no area has a `mandatoryItemName`).
- **Step F** — shuffle all still-unplaced items, then place each into a
  free spot.

**Puzzle placements only ever happen in Steps A–D.** Steps E and F only ever
call `PlaceItemInFreeArea`/`PlaceItemInSpecificFreeArea` — never a puzzle
assignment. This single fact is the entire basis of the early-rejection
design below.

### 4.2 What actually gates acceptance

`DetectCircularDependencies`/`ValidatePuzzleDependencies` both consume
`GetEffectivePuzzleOfItem(name)` (`resolve_effective_puzzle` in
`simulator.py`). Reading its body (re_predicates.md S3, `simulator.py:317`):
it only ever returns non-`None` by finding some *container* item (one with
non-empty `containedItems`) whose **current position** lies within 0.01
units of some puzzle's spawn point, and recursing through nested
containers. On the current scene config, **exactly one item has non-empty
`containedItems`: `Melon`** (it hides `SparkPlug, PlayKey, PDKey, MasterKey,
RustyKey, CarKey, Remote, WPKey, RedCog, OrangeCog, CabinetKey`). Everything
else always resolves to "freely obtainable", regardless of where it's
placed.

**Consequence:** if, at any point, no container item is sitting on a puzzle
spawn, then `resolve_effective_puzzle(anything)` is `None` for every
possible query, so:

- `ValidatePuzzleDependencies`'s only failure branch
  (`resolve_effective_puzzle(n) is not None`) can never fire → it returns
  `True` unconditionally.
- `DetectCircularDependencies`'s graph has only static Puzzle→Item edges
  (from `requiredItemNames`) and zero Item→Puzzle edges → it's a DAG by
  construction → no cycle → returns `False` unconditionally.
- So **acceptance is analytically proven `True`**, with zero need to run
  either function.

Combined with §4.1 (puzzle assignments are frozen after Step D), this gives
a genuinely safe checkpoint: **right after Step D, check whether any
container item's `result[...]` label starts with `"PUZZLE:"`.** If none do,
acceptance is proven for the rest of this attempt no matter what Steps E/F
do afterward (an item that's still unplaced at this checkpoint can only ever
become a FREE placement later, never a puzzle one — see the one residual
edge case in §4.5). This check costs `O(#containers)` — one item on this
config — computed from the config, not hardcoded to "Melon" by name.

### 4.3 What this buys

Once acceptance is proven for an attempt:

1. **Skip the real acceptance check entirely** at the end of the attempt
   (`detect_circular_dependencies`/`validate_puzzle_dependencies` — the
   dominant cost of a naive per-seed simulation; see §5.1's profile).
2. **Abort the instant any PIN-constrained item is written with the wrong
   label**, anywhere in Steps E/F — since the attempt's fate is already
   sealed, a mismatch found now is definitive, not provisional.

If a container *is* on a puzzle spawn after Step D (measured: this happens
for roughly half of random seeds on this scene — see §5.2), none of the
above is safe, so the code falls through to running the attempt to
completion and computing the real acceptance check (§4.4 makes that path
itself faster too, without changing its result).

### 4.4 A second, independent, provable speedup: faster `resolve_effective_puzzle`

Profiling (cProfile, see §5.1) showed that even after §4.3, calls into the
real acceptance check (needed for the ~50% of attempts where a container
did land on a puzzle) were dominated by `GetEffectivePuzzleOfItem`'s own
inner loop: `for g in valid_items_all: if not g.containedItems: continue;
...` — i.e. it immediately discards every one of the 31 items except the 1
that's actually a container, on every single call, repeatedly.

`search.py`'s `_fast_effective_puzzle_resolver` is a drop-in replacement for
`simulator.make_effective_puzzle_resolver`'s returned closure that:

- Iterates `ctx.containers_list` (precomputed once per config: items with
  non-empty `containedItems`) instead of all 31 items. This skips exactly
  the iterations that would `continue` immediately anyway — the return
  value is identical for every input.
- Iterates `ctx.puzzles_with_spawn` instead of all `puzzle_defs` — the
  original's own first line already filters `spawnPointPosition is None`,
  so this is precomputing the same filter once instead of redoing it inside
  every call.
- Replaces `any(name_eq(n, item_name) for n in g.containedItems)` with an
  `O(1)` lookup into `ctx.containers_contained_norm` — a precomputed,
  per-container, normalized (`.strip().lower()`'d) `frozenset` of contained
  names. `name_eq` is nothing but normalized-string equality; precomputing
  the normalized form once per container instead of re-normalizing the same
  static list on every single query call is the same test, just without
  the redundant work.

`detect_circular_dependencies`/`validate_puzzle_dependencies` themselves are
`simulator.py`'s own, completely unmodified functions — only the callback
they invoke is swapped for a faster, behaviorally-identical one. This was
verified (not just argued) — see §5.3.

### 4.5 The retry loop, fully fast

Both of the above apply per-attempt. `_run_fast_multi_attempt` replays
`simulate_verbose`'s own `while attempt < max_attempts and not success:`
loop attempt-for-attempt, calling `_run_attempt_fast(seed + attempt, ...)`
for each one and stopping at the first accepted attempt (or falling through
to return the last attempt's partial result after 50 tries, matching
`simulate_verbose`'s own behavior in that — in practice unobserved — case).
**There is no fallback to the slower `simulator.simulate()` anywhere in the
per-seed hot path.** An earlier draft of this design did fall back to
`simulate()` whenever attempt 1 wasn't accepted; replacing that with a fully
fast retry loop turned out to matter a lot (§5.2's before/after).

### 4.6 Correctness argument (why there is no "fast but slightly wrong" mode)

Putting it together, for any seed:

- Every attempt is either **proven accepted** (§4.2) or has its acceptance
  **computed for real** (§4.4, itself exactly equivalent to the unmodified
  `simulator.py` functions).
- `_Reject` (the early-abort signal) is only ever raised once an attempt's
  acceptance is *already proven true* — so raising it means "this attempt
  is definitely the one the game keeps, and it definitely violates a pin."
  That is a sound, unconditional rejection, not a probabilistic one.
- When an attempt is not accepted, the loop moves on to the next attempt
  using the identical logic — exactly mirroring what the real game (and
  `simulate_verbose`) does.

This was not just argued but **checked**: an exhaustive per-seed comparison
against `simulator.simulate()`'s actual output (not just hit/miss, but full
31-item placement equality) found **0 mismatches** across every sample
tested during development (several thousand seeds, deliberately drawn from
ranges with both a normal ~92% attempt-1-acceptance rate and, separately,
compared with zero pins at all to exercise the retry loop on its own). See
`search.py`'s own `if __name__ == "__main__":` block, which re-runs a
5,000-seed instance of this check every time it's executed.

### 4.7 The one residual theoretical edge case

`Step F`'s `PlaceItemInFreeArea` can, in principle, fail to find room for an
item (returns `False`, leaving it unplaced for the rest of the attempt). If
that ever happened to a *container* item, the real acceptance check's
`_position_of` fallback uses that item's original scene `startPosition` —
which could, in a pathological scene layout, happen to coincide with a
puzzle's spawn point. §4.2's proof treats "still unplaced after Step D" as
safe (because it can only become FREE or remain unplaced, never a puzzle),
which is correct *unless* this specific pathological coincidence occurs.

This is treated as negligible, not ignored: it requires the scene's 71 free
spots to be exhausted (this config places at most 31 items into 87 total
slots — no capacity pressure exists) *and* a container's authored resting
position to exactly coincide with an unrelated puzzle's spawn point by
scene-design accident. It is documented in code (`_run_attempt_fast`'s gate
comment) rather than silently assumed away.

## 5. Measured performance

**Environment caveat:** these numbers were collected on a desktop under
significant, variable background CPU load from other applications
(including the very agent session that wrote this code) — not an idle
benchmark machine. Absolute seeds/sec should be read as "this order of
magnitude, on this machine, right now," not a guaranteed number. The
*relative* speedup figures and the correctness results are the load-bearing
claims here.

### 5.1 Where the time actually goes (cProfile, naive `simulate()`)

Profiling 300 calls to `simulator.simulate()` showed **~75% of total time**
inside `GetEffectivePuzzleOfItem` (`resolve()` in `simulator.py`) and its
`detect_circular_dependencies`/`validate_puzzle_dependencies` callers —
*not* the trace-logging in `simulate_verbose` as originally assumed. This
directly motivated §4.4's optimization; the trace-logging removal (§4's
"no logging" change) is a smaller, still-real contributor.

### 5.2 Iteration history (single core, 1 pin on `Pliers`)

| Version | seeds/sec | speedup vs naive |
|---|---|---|
| naive `simulator.simulate()` | ~130–170/sec | 1.0x (baseline) |
| fast path, naive pin-mismatch-aborts-immediately (**had a real, measured correctness bug — see below**) | ~1,150/sec | ~8.8x (but wrong) |
| fast path, provably-safe abort gate (§4.2/4.3), before §4.4/§4.5 | ~230–320/sec | ~1.1–1.8x |
| + faster `resolve_effective_puzzle` (§4.4) | ~500/sec | ~2.9x |
| + fully-fast retry loop, no `simulate()` fallback (§4.5) | **~810–850/sec** | **~5–6x** |

The first row's "8.8x" is included deliberately, as a cautionary result: an
earlier implementation aborted an attempt the instant *any* pin mismatched,
without first proving acceptance. Measuring it against ground truth over
20,000 seeds found **18 seeds (0.09%) where it silently produced the wrong
answer** — cases where attempt 1 mismatched but was itself rejected by the
real acceptance check, and a *later* attempt (with a completely independent
RNG stream) actually satisfied the pin. A broader sample found **8.2% of
seeds need at least one retry** on this scene's config — nowhere near
"vanishingly rare" — which is exactly the failure mode the task's warning
about `attempt` was pointing at. This is why the shipped design (§4.2–4.5)
replaces that shortcut with an analytically-proven one instead of a
probabilistically-safe one, even though the first version measured a bigger
speedup number.

### 5.3 Exact-equivalence verification

`search.py`'s own `__main__` block runs a 5,000-seed per-seed comparison
against `simulator.simulate()` (not just hit-count — full placement
equality) every time it's executed, plus the end-to-end check below.
Measured result on this run: **0 mismatches**. Development also included
several additional ad-hoc runs (a few thousand seeds each, with 0 and 1
pins, deliberately drawn from ranges exercising the retry path) — all 0
mismatches.

### 5.4 End-to-end verification (required by task spec)

Pin `Pliers -> FREE:OldHouse/Spot (82)` (known satisfied by seed
`915074960`, confirmed against `dumps/placement_915074960_*.json` via
`ground_truth.py` and `test_simulator.py`), searched over
`915074000..915076000`:

```
scanned 2001 seeds in ~4-15s (varies with machine load) across 16 workers
total hits in range: 21
seed 915074960 found: True
  pins_matched=1 prefs_matched=0
  Pliers -> FREE:OldHouse/Spot (82)
PASS
```

**Result: FOUND.** Confirmed on multiple runs.

### 5.5 Multiprocessing throughput

500,000-seed scan, 16 workers (`multiprocessing.cpu_count()` on this
machine), 1 pin: **~6,000 seeds/sec total (~375/sec/core)** — noticeably
below the single-core fast-path number (~810–850/sec) times 16. This gap is
attributed to (a) the same background CPU contention noted above eating
into worker throughput, (b) `cpu_count()` reporting logical (hyperthreaded)
cores rather than physical ones, and (c) `multiprocessing.Pool` IPC/pickling
overhead for chunk results. This number should be re-measured on a quieter
machine before being treated as a real capacity planning figure; the
correctness results (§5.3, §5.4) do not depend on it.

### 5.6 Acceptance-proof engagement rate

On a 2,000-seed sample: **attempt-1 acceptance rate ≈ 91%** (matches the
~8-9% retry rate noted in §5.2). Separately, the §4.2 analytic proof (no
container on a puzzle spawn after Step D) fires for **roughly half** of
attempts on this scene's config — the other half fall through to the real
(but still `resolve_effective_puzzle`-accelerated) acceptance check. Both
numbers are scene-config-dependent, not universal constants; they'd need
re-measuring if the game's scene layout changes materially (e.g. if more
items gain `containedItems`, or fewer puzzles overlap with what `Melon`
hides).

## 6. Known limitations / untested areas

- **Absolute throughput numbers are noisy** (see §5's environment caveat).
  Re-measure on a quiet machine for real capacity planning.
- **The full `2**32`-seed range** was never scanned end-to-end in testing
  (would take a very long time even at the fast-path's throughput on 5+
  pins, per the feasibility table in the task spec). Only sub-ranges up to
  500,000 seeds were exercised.
- **§4.7's residual edge case** (container item fails to find free-spot
  room) was reasoned about, not reproduced or empirically triggered — the
  current scene config has no capacity pressure that would ever exercise
  it.
- **If the scene config changes to have more than one container item, or
  nested containers** (an item with `containedItems` that is itself
  contained by another), `_fast_effective_puzzle_resolver`'s recursion
  handles this correctly in principle (it isn't hardcoded to "exactly one
  container"), but this exact scenario was not specifically exercised in
  testing, since the current scene only has one.
- **GPU backend**: not implemented (out of scope for this task); §3
  documents its contract.
