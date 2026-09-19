"""
test_gpu.py

Validation harness for gpu_backend.py / kernel.cl. Three mandatory checks:

  1. Bit-exactness: the kernel's FULL retry-loop placement (attempts 1..50,
     `NetRandom(seed + attempt)` re-seeded each attempt, with the ported
     `DetectCircularDependencies`/`ValidatePuzzleDependencies` acceptance
     test deciding when to stop retrying -- see kernel.cl's module
     docstring) must exactly match `simulator.simulate()` -- the real,
     31/31-validated, retry-aware reference -- for >= 5000 seeds. Critically,
     this sample is NOT purely random: since only ~8-9% of seeds need a
     retry at all (see re_search.md S5.2/S5.6), a naive random sample would
     barely exercise the new retry-loop/acceptance-test code this fix adds.
     `build_retry_heavy_seeds` instead scans a large contiguous seed range
     with `simulator.simulate_verbose` and keeps every seed whose accepted
     attempt is >= 2, on top of the usual random+special baseline sample --
     guaranteeing hundreds of retry-required seeds are actually exercised.
     Results are reported broken out by attempt-1 vs retry-required seeds.

  2. End-to-end search: pin an item->slot known true for seed 915074960 and
     for seed 123456789 and confirm `GpuSearchBackend.search()` finds each
     seed in a range containing it.

  3. Throughput: measure seeds/sec for `search()` on the AMD GPU, and (if
     search.py's CpuSearchBackend can be imported) report the speedup.

Run directly: `python test_gpu.py`
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

import simulator
from gpu_backend import GpuSearchBackend, Constraint
from dump_utils import newest_dump_file

# Force line-buffered stdout even when redirected to a file/pipe, so partial
# progress survives if this script is killed mid-run (observed during
# development: a mis-tuned throughput test produced a huge number of GPU
# candidate hits, each requiring a full simulator.simulate() CPU re-verify,
# and the run had to be killed -- with block-buffering that lost every line
# printed so far).
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def _newest_config_path() -> str:
    return str(newest_dump_file(Path(__file__).parent / "dumps", "config_*.json"))


# ---------------------------------------------------------------------------
# 1. Bit-exactness
# ---------------------------------------------------------------------------
def build_baseline_seeds(n_random: int = 2000) -> list[int]:
    """Specials + random int32 seeds -- mostly attempt-1 seeds (~91%, see
    re_search.md S5.6), by construction. On its own this would barely
    exercise the retry loop/acceptance-test code this fix adds -- see
    `build_retry_heavy_seeds` below, which is combined with this sample."""
    rng = random.Random(1234567)  # fixed for reproducibility
    seeds = set()
    specials = [
        0, 1, -1, 915074960, 123456789,
        -2147483648, 2147483647, -2147483647, 2147483646,
    ]
    for s in specials:
        seeds.add(s)
    while len(seeds) < n_random + len(specials):
        seeds.add(rng.randint(-2147483648, 2147483647))
    return sorted(seeds)


def build_retry_heavy_seeds(cfg: dict, range_start: int, range_size: int) -> dict[int, tuple[int, dict]]:
    """Scans `range_size` CONSECUTIVE seeds starting at `range_start` with
    `simulator.simulate_verbose`, returning {seed: (accepted_attempt, result)}
    for every one of them. Deliberately NOT a random sample: only ~8-9% of
    seeds need any retry at all (re_search.md S5.2/S5.6), so a naive random
    sample would mostly test attempt=1 seeds and barely exercise this fix's
    new retry-loop / on-device acceptance-test code. Scanning a contiguous
    range and keeping every seed (not just the retries) gives an honest,
    unbiased mix while still guaranteeing a substantial retry-required
    subset shows up (empirically ~8-10% of any few-thousand-seed range on
    this scene's config). The full `result` placement is cached here too so
    the comparison loop below never needs to re-run `simulate_verbose` for
    seeds already scanned."""
    out = {}
    for seed in range(range_start, range_start + range_size):
        full = simulator.simulate_verbose(seed, cfg)
        out[seed] = (full["attempt"], full["result"])
    return out


def test_bit_exactness(backend: GpuSearchBackend, cfg: dict) -> bool:
    print("\n=== 1. Bit-exactness (kernel FULL retry-loop vs simulator.simulate()) ===")

    baseline_seeds = build_baseline_seeds(2000)
    print(f"Baseline sample (specials + random): {len(baseline_seeds)} seeds")

    RETRY_SCAN_RANGE = 4000
    print(f"Scanning {RETRY_SCAN_RANGE} consecutive seeds starting at 500,000,000 "
          f"with simulator.simulate_verbose to find retry-required (attempt >= 2) "
          f"seeds -- this is the slow, one-time CPU-side cost of building an honest "
          f"retry-heavy test sample, not part of what's being measured/timed below ...")
    t_scan0 = time.time()
    attempt_by_seed = build_retry_heavy_seeds(cfg, 500_000_000, RETRY_SCAN_RANGE)
    print(f"  scan done in {time.time() - t_scan0:.1f}s")

    retry_seeds = sorted(s for s, (a, _) in attempt_by_seed.items() if a > 1)
    attempt1_seeds_from_scan = sorted(s for s, (a, _) in attempt_by_seed.items() if a == 1)
    print(f"  found {len(retry_seeds)} retry-required seeds "
          f"({len(retry_seeds)/RETRY_SCAN_RANGE:.1%} of the scanned range) and "
          f"{len(attempt1_seeds_from_scan)} attempt-1 seeds in that same range")

    all_seeds = sorted(set(baseline_seeds) | set(attempt_by_seed.keys()))
    print(f"Combined test set: {len(all_seeds)} unique seeds "
          f"({len(retry_seeds)} of which are known retry-required)")
    assert len(all_seeds) >= 5000, "test set fell below the required 5000-seed minimum"

    t0 = time.time()
    gpu_placements = backend.debug_placements(cfg, all_seeds)
    gpu_dt = time.time() - t0
    print(f"GPU debug_placement (full retry loop): {len(all_seeds)} seeds in {gpu_dt:.2f}s "
          f"({len(all_seeds)/gpu_dt:,.0f} seeds/sec, one dispatch/seed -- launch-overhead bound, "
          f"NOT representative of search()'s batched throughput -- see section 3)")

    n_matched = n_mismatched = 0
    retry_matched = retry_mismatched = 0
    attempt1_matched = attempt1_mismatched = 0
    mismatch_examples = []

    for seed in all_seeds:
        # Reuse the scan's own simulate_verbose result where we already have
        # it (avoids re-running the same seed's simulate_verbose twice);
        # baseline-only seeds (not in the scanned range) need a fresh call.
        cached = attempt_by_seed.get(seed)
        if cached is not None:
            is_retry = cached[0] > 1
            py_placement = cached[1]
        else:
            is_retry = None  # unknown -- baseline seed outside the scanned range
            py_placement = simulator.simulate(seed, cfg)
        gpu_placement = gpu_placements[seed]
        matched = (py_placement == gpu_placement)

        if matched:
            n_matched += 1
        else:
            n_mismatched += 1
            if len(mismatch_examples) < 5:
                diffs = []
                keys = set(py_placement) | set(gpu_placement)
                for k in sorted(keys):
                    pv, gv = py_placement.get(k), gpu_placement.get(k)
                    if pv != gv:
                        diffs.append(f"    {k}: python={pv!r} gpu={gv!r}")
                mismatch_examples.append((seed, diffs))

        if is_retry is True:
            retry_matched += matched
            retry_mismatched += not matched
        elif is_retry is False:
            attempt1_matched += matched
            attempt1_mismatched += not matched

    print(f"\nRESULT: {n_matched}/{len(all_seeds)} matched, {n_mismatched} mismatched "
          f"(kernel's full retry-loop placement vs simulator.simulate(), the real "
          f"authoritative reference)")
    print(f"  Broken out by known classification (from the {RETRY_SCAN_RANGE}-seed scan):")
    print(f"    attempt-1 seeds:      {attempt1_matched}/{attempt1_matched + attempt1_mismatched} matched")
    print(f"    retry-required seeds: {retry_matched}/{retry_matched + retry_mismatched} matched "
          f"(THE seeds this fix was specifically about)")
    if n_mismatched:
        print("Mismatch examples (kernel bug candidates):")
        for seed, diffs in mismatch_examples:
            print(f"  seed={seed}:")
            for d in diffs:
                print(d)

    return n_mismatched == 0


# ---------------------------------------------------------------------------
# 2. End-to-end search
# ---------------------------------------------------------------------------
def test_end_to_end(backend: GpuSearchBackend, cfg: dict) -> bool:
    print("\n=== 2. End-to-end search ===")
    ok = True

    # --- seed 915074960 ---
    constraints = [Constraint(item_name="Pliers", slot_label="FREE:OldHouse/Spot (82)", kind="pin")]
    t0 = time.time()
    results = backend.search(cfg, constraints, seed_range=(915074000, 915076000), limit=50)
    dt = time.time() - t0
    found = [r for r in results if r.seed == 915074960]
    print(f"[915074960] scanned 2001 seeds in {dt:.3f}s, hits in range={len(results)}, "
          f"915074960 found={bool(found)}")
    if found:
        print(f"  pins_matched={found[0].pins_matched} Pliers -> {found[0].placement.get('Pliers')}")
    if not found:
        print("  FAIL")
        ok = False
    else:
        print("  PASS")

    # --- seed 123456789: derive a true pin from its own placement ---
    truth_placement = simulator.simulate(123456789, cfg)
    assert truth_placement, "simulate(123456789) produced no placement -- can't build a pin"
    pin_item, pin_slot = next(iter(sorted(truth_placement.items())))
    constraints2 = [Constraint(item_name=pin_item, slot_label=pin_slot, kind="pin")]
    t0 = time.time()
    results2 = backend.search(cfg, constraints2, seed_range=(123456000, 123457600), limit=50)
    dt2 = time.time() - t0
    found2 = [r for r in results2 if r.seed == 123456789]
    print(f"\n[123456789] pin {pin_item} -> {pin_slot}")
    print(f"  scanned 1601 seeds in {dt2:.3f}s, hits in range={len(results2)}, "
          f"123456789 found={bool(found2)}")
    if not found2:
        print("  FAIL")
        ok = False
    else:
        print("  PASS")

    return ok


# ---------------------------------------------------------------------------
# 3. Throughput
# ---------------------------------------------------------------------------
def _rare_pin_constraints(cfg: dict) -> list[Constraint]:
    """Two independent pins (both true for seed 915074960) instead of one --
    a single common item pin (e.g. "Pliers" alone) matches roughly 1 in ~87
    seeds, which over a 10-20M-seed throughput run yields hundreds of
    thousands of GPU candidate hits, each requiring a full
    `simulator.simulate()` CPU re-verify (§2.1) -- turning "measure GPU
    kernel throughput" into "measure CPU verification throughput" instead.
    Two independent pins drop the expected hit rate by ~87x, giving a
    throughput measurement that is actually dominated by the GPU kernel,
    matching realistic multi-pin search usage.
    """
    truth = simulator.simulate(915074960, cfg)
    return [
        Constraint(item_name="Pliers", slot_label=truth["Pliers"], kind="pin"),
        Constraint(item_name="MasterKey", slot_label=truth["MasterKey"], kind="pin"),
    ]


def test_throughput(backend: GpuSearchBackend, cfg: dict) -> None:
    print("\n=== 3. Throughput ===")
    constraints = _rare_pin_constraints(cfg)
    print("Constraints: " + ", ".join(f"{c.item_name}->{c.slot_label}" for c in constraints))

    N = 20_000_000
    lo, hi = 300_000_000, 300_000_000 + N - 1

    def progress(done, total, hits):
        print(f"  ... {done:,}/{total:,} seeds scanned, {hits} verified hits so far", flush=True)

    t0 = time.time()
    results = backend.search(cfg, constraints, seed_range=(lo, hi), limit=0, progress_cb=progress)
    dt = time.time() - t0
    rate = N / dt
    print(f"GPU search(): {N:,} seeds in {dt:.2f}s = {rate:,.0f} seeds/sec "
          f"({len(results)} verified hits)")
    print(f"Device: {backend.device_info()}")

    # --- Single-core CPU baseline, measured directly (no multiprocessing) ---
    #
    # NOTE (diagnostic finding, NOT fixed here -- out of this task's file
    # ownership boundary): while building this baseline we found that
    # search.py's `_attempt1_fast`/`check()` early-rejection path did not
    # trigger `_Reject` for any of a 50-seed manual sample despite the
    # placed item clearly mismatching its pinned slot (verified by calling
    # `_attempt1_fast` directly and inspecting its returned placement) --
    # so `CpuSearchBackend`'s "early rejection" fast path currently runs
    # every seed all the way to the expensive acceptance check regardless
    # of PIN mismatches, and its multiprocessing Pool additionally hung
    # indefinitely in this sandboxed environment. Neither issue is in a
    # file this task owns (search.py), and neither affects THIS backend's
    # correctness or benchmark, since `GpuSearchBackend._cpu_verify` never
    # calls search.py's `check()`/`_Reject` machinery -- it compares
    # `simulator.simulate()`'s placement dict against each constraint
    # directly. To get a robust, reproducible CPU baseline for the speedup
    # figure below we therefore measure raw `simulator.simulate()` --
    # exactly the function `_cpu_verify` itself calls -- single-core,
    # in-process, with no multiprocessing dependency.
    N_cpu = 1000
    lo_c = 410_500_000
    pin_pairs = [(c.item_name, c.slot_label) for c in constraints if c.kind == "pin"]
    t0 = time.time()
    cpu_hits = 0
    for seed in range(lo_c, lo_c + N_cpu):
        placement = simulator.simulate(seed, cfg)
        if all(placement.get(n) == s for n, s in pin_pairs):
            cpu_hits += 1
    dt_cpu = time.time() - t0
    cpu_rate = N_cpu / dt_cpu
    print(f"\nCPU baseline (single core, raw simulator.simulate() -- the same "
          f"function GpuSearchBackend._cpu_verify uses to authoritatively check "
          f"every GPU candidate): {N_cpu:,} seeds in {dt_cpu:.2f}s = "
          f"{cpu_rate:,.1f} seeds/sec ({cpu_hits} hits)")
    print(f"MEASURED speedup (GPU vs this 1 CPU core, this machine): {rate/cpu_rate:,.0f}x")

    n_cores = os.cpu_count() or 1
    est_multicore_rate = cpu_rate * n_cores
    print(f"\nESTIMATE ONLY (not measured -- linear core scaling assumed, no "
          f"multiprocessing overhead subtracted): a {n_cores}-core CPU search "
          f"scanning at this same per-core rate would do ~{est_multicore_rate:,.0f} "
          f"seeds/sec, i.e. an estimated ~{rate/est_multicore_rate:,.1f}x GPU "
          f"speedup vs a fully-parallel {n_cores}-core CPU search.")


if __name__ == "__main__":
    cfg_path = _newest_config_path()
    print(f"Using config: {cfg_path}")
    cfg = simulator.load_config(cfg_path)

    backend = GpuSearchBackend(cfg)
    print(f"GPU available: {backend.available()}")
    print(f"Device: {backend.device_info()}")

    if not backend.available():
        raise SystemExit(f"GPU backend not available: {backend._unavailable_reason}")

    r1 = test_bit_exactness(backend, cfg)
    r2 = test_end_to_end(backend, cfg)
    test_throughput(backend, cfg)

    print("\n=== SUMMARY ===")
    print(f"Bit-exactness: {'PASS' if r1 else 'FAIL'}")
    print(f"End-to-end search: {'PASS' if r2 else 'FAIL'}")

    if not (r1 and r2):
        raise SystemExit(1)
