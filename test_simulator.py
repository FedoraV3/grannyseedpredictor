"""
test_simulator.py

Acceptance test: simulate(915074960) must reproduce the real-game placement
captured in dumps/ exactly (31/31 items on the right slot).
"""

from pathlib import Path

import ground_truth as gt
import simulator
from dump_utils import capture_stamp, pair_dumps

TEST_SEED = 915074960


def _dump_pair_for_seed(seed: int) -> tuple[str, str]:
    """Find the specific config/placement dump pair this acceptance test
    validates against: the newest placement captured for `seed`, together
    with its config (paired by dump_utils.pair_dumps).

    Deliberately does NOT use "the newest dump in dumps/": that directory
    accumulates captures from later, unrelated dev/test sessions (different
    seeds, different house variants), so "newest" would silently point this
    test at the wrong capture instead of the one it's actually meant to
    validate.
    """
    dumps_dir = Path(__file__).parent / "dumps"
    prefix = f"placement_{seed}_"
    # Use the most recent capture for this seed: an earlier capture on record
    # for this seed is missing freeSpot position data (an incomplete/aborted
    # dump).
    placements = [
        p for p in dumps_dir.glob(f"{prefix}*.json")
        if capture_stamp(p.name) is not None
    ]
    if not placements:
        raise FileNotFoundError(f"No placement_{seed}_*.json found in {dumps_dir}")
    placement_path = max(placements, key=lambda p: capture_stamp(p.name))

    # Refuse rather than fall back to an older capture if this placement can't
    # be paired with its own config dump.
    pairing = pair_dumps(dumps_dir)
    config_for = {pair.placement: pair.config for pair in pairing.pairs}
    if placement_path not in config_for:
        raise FileNotFoundError(
            f"Could not pair {placement_path.name} with a config in {dumps_dir}: "
            f"{pairing.unpaired.get(placement_path, 'unknown reason')}"
        )
    return str(config_for[placement_path]), str(placement_path)


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
