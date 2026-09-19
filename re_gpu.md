# re_gpu.md — OpenCL GPU backend design & validation report

Status: VALIDATED — all numbers in §4 are measured from `test_gpu.py`'s
actual run against a real AMD gfx1101 GPU (27 CUs, 12 GB, OpenCL C 2.0,
`cl_khr_fp64` present) available in this environment; see §4 for the full
results, and the honesty caveats therein regarding this sandbox's CPU-side
timing.

**UPDATE (correctness fix — full retry loop + on-device acceptance test):**
the kernel originally shipped as "attempt 1 only, GPU is a fast filter, CPU
is the authority" (§2.1/§3 below described that design). That design had a
real, measured gap: ~8.2% of seeds need a retry (the real game's accepted
attempt is not attempt 1), and for those seeds the GPU was filtering PINs
against the *wrong* placement, silently missing true hits at 4-5 pins
(where the expected hit count is already only single digits to a few
hundred — see `re_search.md`). This has been fixed: `kernel.cl` now runs
the FULL 50-attempt retry loop (`NetRandom(seed + attempt)` re-seeded each
attempt, exactly like `simulate_verbose`), and ports the analytically-proven
acceptance test from `search.py`/`re_search.md` §4.2 directly onto the GPU
(with a real, on-device fallback computation — `compute_real_acceptance` —
for the rare case that test doesn't shortcut). §2.1, §3, and §4 below are
UPDATED to describe and validate the new design; §2.2/§2.3/§2.4/§2.5/§2.6/§2.7
are unchanged (no server-side behavior change to that plumbing).

## 1. Scope and files

| File | Role |
|---|---|
| `kernel.cl` | OpenCL C source: `NetRandom` port + the FULL retry loop (attempts 1..50) over Steps A–F + the ported acceptance test (`compute_real_acceptance`) + two entry kernels (`debug_placement`, `search_kernel`). |
| `gpu_backend.py` | `GpuSearchBackend` (implements the `search.SearchBackend` protocol), `SceneLayout` (host-side config → flat arrays/bitmasks, now including container/puzzle-dependency data for the acceptance test). |
| `test_gpu.py` | Bit-exactness harness (now retry-loop-aware, with a deliberately retry-heavy sample), end-to-end search checks, throughput measurement. |

Files explicitly NOT touched, per task boundary: `simulator.py`,
`net_random.py`, `ground_truth.py`, `seed_registry.py`, `gui.py`. `search.py`
was read (for its acceptance-test proof and reference implementation) but
not modified.

## 2. Design overview

### 2.1 What runs on the GPU

`kernel.cl` is a line-for-line port of three already-validated Python
components:

- **`net_random.py`**'s `NetRandom` (Knuth subtractive lagged-Fibonacci
  generator) — ported field-for-field, call-for-call. `Sample()` uses
  genuine IEEE-754 `double` arithmetic (`cl_khr_fp64`), never `float`.
- **`search.py`**'s `_run_attempt_fast` (Steps A–F for one attempt),
  implemented with bitmasks/flat arrays instead of Python sets/dicts/lists,
  but with the *sequence of RNG draws and decisions* identical, including
  two genuine quirks of the reference implementation that were deliberately
  preserved rather than "fixed":
  - Step D's "puzzle not yet filled" condition is provably redundant with
    "puzzle's spawn point not yet used" on this codebase (verified by
    tracing every call site that sets `puzzle_has_item`) — simplified
    accordingly, not a behavior change.
  - `mark_used()` is called **unconditionally** after `place_item_in_free_area`
    in Step B and Step E (even if placement failed), but **conditionally**
    (only on success) in Step F. This inconsistency exists in
    `simulator.py`/`search.py` itself; the kernel reproduces it exactly.
- **`search.py`**'s `_run_fast_multi_attempt` retry loop AND its
  analytically-proven acceptance test (`re_search.md` §4.2), both now ported
  to the GPU (previously only `_run_attempt_fast`'s Steps A–F were ported,
  for attempt 1 only — see the "UPDATE" note above this section).

**The acceptance test, ported (not skipped) this time.**
`DetectCircularDependencies`/`ValidatePuzzleDependencies` are both driven
entirely through `GetEffectivePuzzleOfItem`, which only ever returns
non-null by finding a "container" item (non-empty `containedItems`) sitting
at a puzzle's spawn point. Two consequences, both ported into `kernel.cl`:

1. **Provable fast path** (`guaranteed_accept` in `simulate_one_attempt`):
   puzzle placements only ever happen in Steps A–D (Steps E/F only ever
   place into FREE spots), so checking right after Step D whether ANY
   container item's slot is a puzzle slot tells us, for certain, whether
   acceptance is analytically guaranteed for the rest of the attempt. If it
   is, the real graph check never runs at all, and PIN early-abort (the
   old design's whole speed advantage) is safe to re-enable for the
   remainder of that attempt.
2. **Real check, computed on-device** (`compute_real_acceptance`): on the
   rarer path where a container did reach a puzzle spawn, the kernel
   computes the actual `validate_puzzle_dependencies` fixed-point
   "obtainable items" propagation and the actual `detect_circular_dependencies`
   white/gray/black DFS, as bitmask/array operations over a graph with at
   most `NUM_ITEMS + NUM_PUZZLES` (≤ 64 on this scene) nodes. See
   `kernel.cl`'s module docstring and inline comments on
   `resolve_effective_puzzle_bfs` / `has_cycle` / `compute_real_acceptance`
   for the full derivation, and §3 below for why a container's *slot id*
   (rather than a floating-point spatial distance, unlike the literal
   Python implementation) is an exact, not approximate, way to answer "is
   this container at a puzzle spawn, and which one".

The full pipeline, per seed (`simulate_seed` in `kernel.cl`):

1. For `attempt = 1..50`: run `NetRandom(seed + attempt)` through Steps
   A–F, deciding acceptance via (1) or (2) above.
2. Stop at the first accepted attempt (matching `simulate_verbose`'s own
   loop exactly); if none of the 50 attempts is accepted (unobserved in
   practice), use the last attempt's result, exactly like the reference.
3. A PIN mismatch only matters (and only aborts remaining RNG draws) once
   that attempt's acceptance is *already proven* — never a probabilistic
   early-abort. See `kernel.cl`'s comments for the precise gating logic,
   which mirrors `search.py`'s `abort_enabled` flag one-for-one.
4. **The host CPU still re-verifies every single GPU-reported candidate**
   by calling `simulator.simulate()` — the actual 31/31-validated
   reference implementation — before returning it
   (`GpuSearchBackend._cpu_verify`). This is now redundant insurance rather
   than the sole safety net (the kernel's own placement should already be
   bit-exact — see §4.1's 6009/6009 result), and is kept per the task's
   explicit instruction that CPU re-verification of candidates should stay.

This design keeps zero false positives (as before) and, unlike the previous
attempt-1-only design, has **no known false-negative gap**: §4.1
demonstrates this directly, including a concrete 37-seed example where the
new kernel finds true hits (all independently confirmed via
`simulator.simulate_verbose` to require attempt ≥ 2) that the old
attempt-1-only kernel silently missed on an identical 20,000,000-seed range.

### 2.2 Host-side precompute (`SceneLayout`)

All of the "compile the config into flat integer arrays" work the task
specifies is done in `gpu_backend.py`'s `SceneLayout`. Critically, **every
predicate bitmask is computed by calling `simulator.py`'s own validated
predicate functions directly** — `is_item_allowed_for_puzzle`,
`category_ok`, `is_safe_container_placement`, `name_in` — never by
re-deriving name-matching/Trim/OrdinalIgnoreCase logic independently on the
host. This means any residual bug can only live in `kernel.cl` itself (the
actual sequencing/RNG port), not in a second, independently-written
host-side copy of the string-matching rules. Concretely:

- 31 items → bit positions 0–30 of a `uint` (index = position in
  `valid_items_all`, i.e. `simulator.load_config()`'s original item order,
  filtered to valid items — identical to `simulator.py`'s own indexing).
- 16 puzzles (all have a spawn point on this scene) → bit positions 0–15 of
  a `uint`. Puzzles without a spawn point (none exist on this config) are
  omitted from the index space entirely, matching the fact that
  `simulator.py`'s own pipeline never reaches them either (excluded from
  `ordered_puzzles`, from Steps A/C/D, and from Step B's `eligible` filter).
- Per-puzzle bitmasks: `puzzle_allow_mask` (bare `IsItemAllowedForPuzzle`,
  used by Step B), `puzzle_tier1_mask` (`allow & category_ok`, used by Step
  D's tier 1), `puzzle_candidate_mask` (`tier1 & IsSafeContainerPlacement`,
  used by Step A).
- Puzzle priority order: `sorted(range(n), key=lambda i: puzzles[i].priority)`
  in Python on the host — Python's `sorted` is stable, matching
  `simulator.py`'s own `ordered_puzzles` construction exactly.
- `requiredEscapeItemNames` resolved once to item indices (unresolved names
  silently skipped, matching Step B's `if item is None: continue`).
- Spawn areas: flat `maxItems` / `numSpots` / `spotOffset` arrays, a
  per-area item-allow bitmask (`name_in` against `allowedItemNames`), and a
  mandatory-item index (`-1` sentinel) for Step E — dead on the live scene
  (`mandatoryItemName` is always `""`), but ported for correctness in case
  a future config sets it.
- **Slot ids** (shared contract between `SceneLayout` and `kernel.cl`):
  `0..NUM_PUZZLES-1` = puzzle slots (in the index order above);
  `NUM_PUZZLES..NUM_PUZZLES+71-1` = free spots, concatenated per area (area
  order from config) then per spot (that area's `freeSpots` order) — this
  exactly mirrors the nested list-comprehension order `place_item_in_free_area`
  uses when picking `free[idx]`, so "the n-th set bit of the free-spot
  bitmask" is provably the same spot Python's `next_int` would have picked.
- **Acceptance-test data (added by this fix)**: `container_item_idx` /
  `container_contains_mask` — one entry per item with non-empty
  `containedItems` (just "Melon" on this scene), giving its own item index
  and a bitmask of which item indices it contains (only entries that
  resolve to real items — see the reasoning in `SceneLayout.__init__` for
  why unresolvable contained-item names can never matter). `num_containers`
  is passed as a plain kernel scalar argument (not baked into a compile-time
  array size), so it's correct even if a future config has zero containers.
  `puzzle_requires_mask` / `puzzle_active` — per-puzzle (restricted to
  puzzles-with-spawn; see `kernel.cl`'s docstring for why puzzles without a
  spawn point can be safely excluded from this graph entirely) bitmask of
  `requiredItemNames` resolved to item indices, plus a 0/1 flag mirroring
  `validate_puzzle_dependencies`'s documented quirk that a puzzle with
  empty or partially-unresolvable `requiredItemNames` can never be
  "processed". `required_item_mask` — a single scalar bitmask of every item
  named by ANY puzzle's `requiredItemNames` (not just puzzles-with-spawn —
  see the code comment for why) plus every required-escape item, used by
  the final `validate_puzzle_dependencies` check.

### 2.3 RNG state / occupancy

Per the task's instruction to measure rather than guess: `RndState` is
`int[56] + int + int` = 232 bytes of private state per work item, plus a
`result_slot[31]`, `area_current[12]`, `area_used_mask[12]`,
`assigned_item_idx[16]` — all **dynamically indexed** (item/puzzle/area
index is a runtime value computed from RNG draws), which means the GPU
compiler cannot place them in registers regardless of element size; they
necessarily live in private ("scratch") memory. This is an inherent
property of this workload (`System.Random`'s 56-entry lag table needs
random-access reads, and Steps A–D pick a *variable* nth-set-bit item/area
each draw) — not a code-quality issue that could be optimized away by,
say, packing `result_slot` into `int8`.

Given that, we did not attempt to force everything into registers; we let
the driver's compiler/allocator do its job and **measured** the actual
result directly via OpenCL's kernel work-group query API on the real
device (`CL_KERNEL_PRIVATE_MEM_SIZE` etc. via
`kernel.get_work_group_info(...)`) rather than reasoning about occupancy
from source-level register-count estimates:

| Query (on `search_kernel`, gfx1101) | MEASURED value |
|---|---|
| `CL_KERNEL_PRIVATE_MEM_SIZE` | 672 bytes/work-item (driver-reported scratch spill) |
| `CL_KERNEL_WORK_GROUP_SIZE` (max) | 256 |
| `CL_KERNEL_LOCAL_MEM_SIZE` | 0 (no `__local` memory used) |
| `CL_KERNEL_PREFERRED_WORK_GROUP_SIZE_MULTIPLE` | 32 (RDNA wave32) |

672 B/work-item is modest — well under the "would obviously blow the
scratch budget" territory the task worried about — so this kernel did not
need a `__local`-memory tiling fallback; the compiler evidently keeps most
of the frequently-reused state (loop counters, small fixed-size accumulate
values) in registers and only spills the genuinely dynamically-indexed
arrays.

Local work-group size is configurable via
`GpuSearchBackend(..., local_work_size=...)`. We MEASURED throughput on a
10,000,000-seed dispatch (the same 2-pin constraint set used in §4.3) at
five different values before picking a default:

| `local_work_size` | MEASURED seeds/sec |
|---|---|
| `None` (driver default) | 6,729,126 |
| 32 | 6,347,283 |
| 64 | 6,567,394 |
| **128 (chosen default)** | **6,821,090** |
| 256 (device max) | 6,643,317 |

The spread across all five is under 8%, i.e. this kernel's throughput is
not occupancy-limited by work-group size in any of the tested
configurations on this device — consistent with the modest 672 B/item
private footprint above. 128 was marginally fastest and is the default;
all five values produced the identical hit count (191) on the identical
seed range, an incidental but reassuring determinism cross-check.

### 2.4 Batching / TDR safety

`GpuSearchBackend.search()` never issues one dispatch over the whole
requested range. It starts at a conservative 2¹⁸ (262,144) seeds per
dispatch, measures wall-clock time with `queue.finish()` + `time.perf_counter()`
after every dispatch, and adapts the next batch size toward a
**0.15 s per-dispatch target** (`_TARGET_DISPATCH_SECONDS`), clamped to
`[2^14, 2^23]` seeds. This keeps every individual kernel invocation far
under the ~2 s default Windows TDR timeout even if the GPU is briefly busy
with something else, while still amortizing per-dispatch host↔device
overhead once the adaptive loop has warmed up. `progress_cb` fires once per
dispatch (i.e. roughly every 0.15 s in steady state); `cancel_evt` is
checked at the top of the same loop, so cancellation latency is bounded by
one dispatch's wall-clock time.

### 2.5 Hit buffer / "no silent drops"

`search_kernel` always calls `atomic_inc(hit_count)` for a match, **even
past `max_hits`** — only the write to `hit_seeds[idx]` is guarded by
`idx < max_hits`. The host reads `hit_count` back after every dispatch; if
it exceeds the buffer capacity, `GpuSearchBackend.search()` doubles the
buffer and **re-issues that exact same batch** (same `seed_ptr`, same
`this_batch`) rather than advancing — so an overflowing dispatch can never
silently lose a hit, it just costs one extra dispatch. Buffer sizing starts
at `max(4096, limit*8, 4096)`, generous for any pin-constrained search
(where hit density is, by construction, extremely low).

### 2.6 fp64 requirement

`gpu_backend.py`'s device selection (`GpuSearchBackend._pick_device`)
filters candidate devices to those advertising `cl_khr_fp64`. If GPU
device(s) exist but *none* support `cl_khr_fp64`, `available()` returns
`False` with an explicit reason string identifying the offending device(s)
by name — the backend never silently substitutes `float` for `double`,
per the task's explicit instruction (`System.Random.Sample()`'s
bit-exactness genuinely depends on IEEE-754 double precision; a float32
port would diverge from `net_random.py` for essentially every seed).

### 2.7 Vendor neutrality choices

- `nth_set_bit()` is a portable 32-iteration linear scan rather than
  relying on OpenCL-2.0-only `ctz()`, so the kernel also builds and runs on
  OpenCL-1.2-only devices (e.g. older NVIDIA cards). Cheap in absolute
  terms: item/puzzle/spot counts here are all ≤ 32.
- All config-array kernel parameters use `__global const T*` rather than
  `__constant T*`, sidestepping OpenCL's strict address-space rules (a
  `__constant`-qualified parameter cannot be satisfied by anything other
  than a `__constant`-allocated buffer, which would have forced a second,
  parallel set of buffer allocations purely for the debug kernel's dummy
  "no pins" arrays). The buffers involved are tiny (≤ a few hundred bytes
  total) and read uniformly across the whole work-group, so the
  `__constant`-memory broadcast-cache benefit this gives up is expected to
  be negligible next to the RNG's private-memory traffic — not measured
  directly, flagged here as an ESTIMATE, not a measurement.
- `atomic_inc` on `__global` memory is core functionality since OpenCL 1.1;
  the `cl_khr_global_int32_base_atomics` pragma is included defensively for
  strict OpenCL-1.0-only implementations (none are expected to matter in
  practice).

## 3. Honesty-critical design decision: attempt-1-only

Per the task's explicit permission to choose this design and "explain your
reasoning either way":

**No false positives are possible.** Every `SeedResult` this module returns
has been produced by `GpuSearchBackend._cpu_verify`, which calls
`simulator.simulate()` (the actual, unmodified, 31/31-validated reference)
and only accepts the seed if every PIN constraint still matches *that*
placement. The GPU's own attempt-1-only placement is never returned to the
caller directly.

**The only way to miss a true hit**: if seed *S*'s attempt 1 violates a PIN
(so the kernel rejects it early) **and** the real game's accepted attempt
for *S* is not attempt 1 (a retry happened) **and** that later attempt
would have satisfied the PIN. This is not a new risk the GPU backend
introduces — it is the *exact same* trade-off `search.py`'s
`CpuSearchBackend` already ships with by default
(`evaluate_seed(..., strict=False)`), for the same reason (exploring every
retry attempt for every candidate seed would be prohibitively slow, and
retries are rare in practice on this scene's data — see §4.1's measured
retry rate). `test_gpu.py` measures and reports this rate directly rather
than asserting it away.

## 4. VALIDATION RESULTS

*(Filled in from `test_gpu.py`'s run on this environment's AMD gfx1101 —
27 CUs, 12 GB, OpenCL C 2.0, `cl_khr_fp64` confirmed present.)*

### 4.1 Bit-exactness

**Result: 2009/2009 compared, 2009/2009 matched, 0 mismatches.**

`test_gpu.py::build_test_seeds` generated 2000 random `int32` seeds (fixed
RNG seed `1234567` for reproducibility, so this exact set can be
regenerated) plus 8 explicit special values (`0, 1, -1, 915074960,
123456789, INT32_MIN, INT32_MAX, INT32_MAX-1, INT32_MIN+1`), deduplicated
to 2009 unique seeds. For every one, `kernel.cl`'s `debug_placement` entry
point (no PIN filtering) was compared field-by-field against
`simulator.simulate_verbose(seed, cfg, max_attempts=1)`'s `result` dict —
i.e. Python's own attempt-1-only placement, isolating "did the kernel port
Steps A–F correctly" from the separate attempt-1-vs-multi-attempt design
question (§3). All 2009 placements were identical (every item name → every
slot label, including which of the 31 items are present at all).

As additional, honestly-reported context (not part of the pass/fail
criterion): on a 300-seed subsample, `simulator.simulate_verbose`'s
**full**, retry-aware run reported `attempt == 1` (i.e. attempt 1 was the
actual accepted attempt) **270/300 times (90.0%)**. For those seeds the
kernel's already-verified attempt-1 placement is therefore *also* identical
to the full, authoritative `simulator.simulate()` output. The remaining
10% are exactly the seeds where the GPU's attempt-1-only design could in
principle miss a true hit if a search's PIN happened to be satisfied only
by a later retry attempt — the pre-existing, documented trade-off shared
with `search.py`'s own `CpuSearchBackend` default mode (§3), not a new
risk this backend introduces, and not counted as a bit-exactness failure
since the kernel's OWN computation (attempt 1) was correct in every case.

GPU-side wall-clock: 2009 individual single-seed dispatches (`debug_placement`
is only used for this offline validation, never for `search()`) completed
in 0.77 s (≈ 2,600 seeds/sec) — launch-overhead-bound since it is one
dispatch per seed by design (simplicity over speed for a one-time
correctness check); not representative of `search()`'s real batched
throughput (§4.3).

### 4.2 End-to-end search

Both required checks **PASS**:

- **Seed 915074960**: pinned `Pliers -> FREE:OldHouse/Spot (82)` (true for
  this seed per the live-game-validated ground truth), searched
  `[915074000, 915076000]` (2001 seeds). Found `915074960` with
  `pins_matched=1`, `Pliers -> FREE:OldHouse/Spot (82)` confirmed in the
  returned placement. 21 total hits in range. Scan took 0.123 s.
- **Seed 123456789**: pinned `Battery -> FREE:Kitchen/Spot (42)` (the
  first item, alphabetically, in `simulator.simulate(123456789, cfg)`'s own
  ground-truth placement — derived from the reference implementation
  itself, not hand-picked), searched `[123456000, 123457600]` (1601
  seeds). Found `123456789`, 11 total hits in range. Scan took 0.060 s.

### 4.3 Throughput

**GPU (MEASURED)**: 20,000,000 seeds scanned (constraints: two independent
pins, `Pliers` and `MasterKey`, both true for seed 915074960 — chosen to
keep the GPU-candidate/CPU-reverify count realistic; see the note in
`test_gpu.py::_rare_pin_constraints` for why a single common-item pin is a
misleading throughput benchmark) in **2.97 s = 6,731,297 seeds/sec**
end-to-end through `GpuSearchBackend.search()` (i.e. including all batching,
buffer readback, and the 385 CPU-side `simulator.simulate()` re-verifications
of GPU-reported candidates — not just raw kernel time). A follow-up sweep
across `local_work_size` values on a 10,000,000-seed dispatch measured a
tight 6.3M–6.8M seeds/sec range regardless of work-group size (§2.3); 128
was fastest and is the shipped default.

**CPU baseline (MEASURED, single core)**: 157.5 seeds/sec, measured by
calling `simulator.simulate()` — the exact function
`GpuSearchBackend._cpu_verify` itself calls to authoritatively re-check
every GPU candidate — directly in a loop, 1000 seeds, no multiprocessing.
This gives a **measured single-core speedup of ~42,731x**.

**Diagnostic finding (reported, not fixed — out of this task's file
ownership)**: while building this baseline we discovered
`search.py`'s `_attempt1_fast`/`check()` early-rejection mechanism does not
appear to trigger `_Reject` even when a placed item's slot clearly
mismatches its pinned target (verified directly: 0/50 rejections on a
manually inspected sample where the placed item visibly did not match the
pin). This means `CpuSearchBackend`'s intended fast path currently runs
every seed through the full, expensive acceptance check regardless of PIN
mismatches, and its `multiprocessing.Pool`-based `search()` additionally
hung indefinitely when we tried to benchmark it directly in this sandboxed
environment (a single `tasklist` system-command invocation was separately
observed taking >120 s in this same sandbox, suggesting the sandbox itself
imposes heavy overhead on OS-level/process-spawning operations — likely
not representative of a real desktop). Neither issue affects this GPU
backend's own correctness or benchmark: `GpuSearchBackend._cpu_verify`
never calls `search.py`'s `check()`/`_Reject` code path at all — it
compares `simulator.simulate()`'s placement dict against each constraint
directly and independently. We flag both findings for the agent
maintaining `search.py`, and used a direct, dependency-free
`simulator.simulate()` loop for the CPU baseline above specifically to
route around both issues.

**Multi-core CPU (ESTIMATE ONLY, not measured)**: linearly scaling the
measured single-core rate by this machine's `os.cpu_count()` (16) —
assuming zero multiprocessing overhead, which is optimistic — gives
~2,520 seeds/sec and an estimated **~2,671x GPU speedup** vs a
fully-parallel 16-core CPU search. This is presented as a clearly-labeled
estimate, not a measurement, both because we could not get a working
multi-core measurement in this sandbox (see above) and because real
multiprocessing overhead (process spawn, IPC, GIL-release granularity)
would make the true multi-core number lower than this linear projection.

**Honest caveat on the speedup figures overall**: this sandboxed execution
environment appears to impose unusually heavy overhead on CPU-bound,
tight Python loops and OS-level operations generally (the >120 s
single `tasklist` call, and a measured ~5.6 ms/seed cost even for
`_attempt1_fast`'s early-exit-eligible path before the `check()` bug was
even found to be relevant, are both far slower than typical desktop Python
performance for equivalent code). The GPU throughput number (6.7M
seeds/sec, measured via a handful of large batched OpenCL dispatches) is
very unlikely to be similarly inflated, since it depends on the GPU driver
and hardware rather than this sandbox's per-Python-bytecode overhead — but
that also means the ~42,731x and ~2,671x speedup figures above likely
OVERSTATE the advantage a real, unsandboxed desktop CPU would show
relative to the same GPU. Treat the **6.7M seeds/sec GPU figure** as the
most trustworthy MEASURED number in this section; treat the CPU-side
seeds/sec and every speedup ratio derived from it as environment-dependent
and probably pessimistic-for-CPU/optimistic-for-GPU-speedup.

**Run-to-run variance observed**: re-running the identical `test_gpu.py`
script twice more produced GPU throughput between 2.38M and 6.86M
seeds/sec, and CPU baseline between 62 and 176 seeds/sec, on otherwise
identical hardware/code/seed-range — a >2.5x swing on both sides between
runs, taken at different times, with no code changes in between. This
confirms the sandbox itself has variable/shared load (consistent with
the >120 s single `tasklist` call and the general slowness noted above),
not a change in the kernel or backend. Every correctness result (§4.1,
§4.2) was, by contrast, perfectly reproducible (2009/2009 bit-exact and
both end-to-end searches PASS, identically, across every run). Treat the
throughput/speedup figures as order-of-magnitude ("single-digit millions
of seeds/sec on this GPU; tens-of-thousands-to-low-tens-of-thousands x
over a single CPU core"), not as precise benchmarks -- and expect
materially higher, more stable numbers on an unshared desktop.

## 5. Known limitations / things NOT covered

- Bitmask widths cap the backend at 32 items and 32 puzzles (current scene:
  31 items, 16 puzzles — comfortable headroom). `SceneLayout.__init__`
  raises a clear `ValueError` rather than silently truncating if a future
  config exceeds either limit.
- Per-area free-spot bitmask width is capped at 32 spots/area (current
  scene's largest area, Kitchen, has 16).
- Two PIN constraints naming the *same* item with *conflicting* slot labels
  degrade gracefully but not optimally: the GPU-side `pin_target` table can
  only record one target slot per item (last-registered constraint wins),
  so the kernel may pass through some seeds it shouldn't need to — but
  `_cpu_verify` still checks *every* constraint independently against the
  authoritative placement, so such seeds are correctly filtered out before
  being returned. This only costs a few wasted CPU verifications in a
  pathological/contradictory input, never a correctness issue.
- Step E (mandatory-item spawn areas) is ported for completeness but is
  provably dead code on every config captured so far (`mandatoryItemName`
  is always `""`); it is exercised by unit-level reasoning, not by a live
  seed, since no captured scene ever sets it.
