"""
test_simulator.py

Acceptance test: simulate(915074960) must reproduce the real-game placement
captured in dumps/ exactly (31/31 items on the right slot).
"""

import re
from pathlib import Path

import ground_truth as gt
import simulator

TEST_SEED = 915074960


def _dump_pair_for_seed(seed: int) -> tuple[str, str]:
    """Find the specific config/placement dump pair this acceptance test
    validates against, matched by seed then by their shared capture
    timestamp (same technique as validate_all.py's discover_pairs).

    Deliberately does NOT use "the newest dump in dumps/": that directory
    accumulates captures from later, unrelated dev/test sessions (different
    seeds, different house variants), so "newest" would silently point this
    test at the wrong capture instead of the one it's actually meant to
    validate.
    """
    dumps_dir = Path(__file__).parent / "dumps"
    prefix = f"placement_{seed}_"
    # All candidates share the same seed prefix and a fixed-width
    # YYYYMMDD_HHMMSS_fff timestamp, so (unlike the general "newest dump"
    # problem elsewhere in this codebase) a lexicographic sort here really
    # is chronological. Use the most recent capture for this seed: an
    # earlier capture on record for this seed is missing freeSpot position
    # data (an incomplete/aborted dump).
    placements = sorted(p for p in dumps_dir.glob("placement_*.json") if p.name.startswith(prefix))
    if not placements:
        raise FileNotFoundError(f"No placement_{seed}_*.json found in {dumps_dir}")
    placement_path = placements[-1]

    ts_match = re.search(r"(20\d{6}_\d{6})", placement_path.name)
    assert ts_match, f"Could not extract capture timestamp from {placement_path.name}"
    ts = ts_match.group(1)
    configs = [c for c in dumps_dir.glob("config_*.json") if ts in c.name]
    if not configs:
        raise FileNotFoundError(
            f"No config_*.json matching capture timestamp {ts} (for seed {seed}) in {dumps_dir}"
        )
    return str(configs[0]), str(placement_path)


def test_simulate_matches_ground_truth():
    config_path, placement_path = _dump_pair_for_seed(TEST_SEED)
    cfg_dump, placement_dump = gt.load_dumps(config_path, placement_path)
    truth = gt.build_ground_truth(cfg_dump, placement_dump)

    cfg = simulator.load_config(config_path)
    seed = cfg["gameSeed"]
    assert seed == TEST_SEED, f"expected seed {TEST_SEED} from config, got {seed}"

    simulated = simulator.simulate(seed, cfg)

    ok, diffs = gt.compare(truth, simulated)

    if not ok:
        print(f"\n{len(diffs)} mismatch(es) out of {len(truth)} items:")
        for line in diffs:
            print(f"  {line}")

    assert ok, f"simulate({seed}) diverged from ground truth: {diffs}"
    assert len(simulated) == 31, f"expected 31 placed items, got {len(simulated)}"
    print(f"\nPASS: {len(truth)}/{len(truth)} items matched ground truth exactly.")


if __name__ == "__main__":
    test_simulate_matches_ground_truth()
